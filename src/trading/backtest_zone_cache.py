from __future__ import annotations

import json
import math
import hashlib

from pathlib import Path
from dataclasses import dataclass

from typing import Any

import pandas as pd

from ..config import ZoneConfig
from ..db import connect
from ..utils import json_default, utc_seconds
from .constants import DETECTOR_VERSION, ZONE_TIMEFRAME


@dataclass(frozen=True)
class ZoneCacheIdentity:
    detector_version: str
    detector_signature: str
    zone_config_hash: str


def build_zone_cache_identity(zone_config: ZoneConfig) -> ZoneCacheIdentity:
    """Build stable hashes for every code and config input that controls zone detection."""
    config_payload = json.dumps(zone_config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return ZoneCacheIdentity(
        detector_version=DETECTOR_VERSION,
        detector_signature=_detector_implementation_hash(),
        zone_config_hash=hashlib.sha256(config_payload.encode("utf-8")).hexdigest(),
    )


def build_four_hour_input_hashes(four_hour_history: pd.DataFrame) -> dict[int, str]:
    """Hash each prefix of the exact 4h frame so only affected snapshots become stale."""
    if four_hour_history is None or four_hour_history.empty:
        return {}
    digest = hashlib.sha256()
    prefixes: dict[int, str] = {}
    ordered = four_hour_history.sort_values("open_time").drop_duplicates("open_time", keep="last")
    for row in ordered.itertuples(index=False):
        payload = _canonical_four_hour_row(row)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest.update(encoded.encode("utf-8"))
        digest.update(b"\n")
        prefixes[int(payload["open_time"])] = digest.hexdigest()
    return prefixes


def prune_incompatible_zone_cache(
    database_path: str | Path,
    *,
    exchange: str,
    symbol: str,
    identity: ZoneCacheIdentity,
) -> None:
    """Delete cache rows from older detector code or config within the active market scope."""
    with connect(database_path) as conn:
        conn.execute(
            """
            DELETE FROM backtest_zone_cache
            WHERE exchange=? AND symbol=? AND timeframe=?
              AND (detector_version<>? OR detector_signature<>? OR zone_config_hash<>?)
            """,
            (
                exchange,
                symbol,
                ZONE_TIMEFRAME,
                identity.detector_version,
                identity.detector_signature,
                identity.zone_config_hash,
            ),
        )
        conn.commit()


def load_cached_zone_snapshot(
    database_path: str | Path,
    *,
    exchange: str,
    symbol: str,
    zone_set_as_of: int,
    input_hash: str,
    identity: ZoneCacheIdentity,
) -> list[dict[str, Any]] | None:
    """Return one validated cache hit, or None when the row is missing, stale, or corrupt."""
    with connect(database_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM backtest_zone_cache
            WHERE exchange=? AND symbol=? AND timeframe=? AND zone_set_as_of=?
            """,
            (exchange, symbol, ZONE_TIMEFRAME, int(zone_set_as_of)),
        ).fetchone()
    if row is None:
        return None
    if (
        row["detector_version"] != identity.detector_version
        or row["detector_signature"] != identity.detector_signature
        or row["zone_config_hash"] != identity.zone_config_hash
        or row["input_hash"] != input_hash
    ):
        return None
    try:
        zones = json.loads(row["zones_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not _is_valid_cached_snapshot(zones, int(row["zone_count"]), int(zone_set_as_of)):
        return None
    return zones


def store_cached_zone_snapshot(
    database_path: str | Path,
    zones: list[dict[str, Any]],
    *,
    exchange: str,
    symbol: str,
    zone_set_as_of: int,
    input_hash: str,
    identity: ZoneCacheIdentity,
) -> None:
    """Atomically replace one snapshot so interrupted replays retain all completed work."""
    zones_json = json.dumps(zones, default=json_default, sort_keys=True, separators=(",", ":"))
    with connect(database_path) as conn:
        conn.execute(
            """
            INSERT INTO backtest_zone_cache(
              exchange, symbol, timeframe, zone_set_as_of, detector_version,
              detector_signature, zone_config_hash, input_hash, zone_count, zones_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(exchange, symbol, timeframe, zone_set_as_of) DO UPDATE SET
              detector_version=excluded.detector_version,
              detector_signature=excluded.detector_signature,
              zone_config_hash=excluded.zone_config_hash,
              input_hash=excluded.input_hash,
              zone_count=excluded.zone_count,
              zones_json=excluded.zones_json,
              created_at=excluded.created_at
            """,
            (
                exchange,
                symbol,
                ZONE_TIMEFRAME,
                int(zone_set_as_of),
                identity.detector_version,
                identity.detector_signature,
                identity.zone_config_hash,
                input_hash,
                len(zones),
                zones_json,
                utc_seconds(),
            ),
        )
        conn.commit()


def _detector_implementation_hash() -> str:
    """Hash detector source dependencies so code edits invalidate persisted snapshots automatically."""
    source_root = Path(__file__).resolve().parents[1]
    paths = sorted((source_root / "zones").glob("**/*.py"))
    paths.extend(
        [
            source_root / "trading" / "constants.py",
            source_root / "trading" / "zone_identity.py",
            source_root / "trading" / "zone_refresh.py",
        ]
    )
    digest = hashlib.sha256()
    for path in sorted(paths):
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _canonical_four_hour_row(row: Any) -> dict[str, Any]:
    """Convert a pandas row into deterministic JSON containing every detector data input."""
    volume = getattr(row, "volume", None)
    return {
        "open_time": int(row.open_time),
        "close_time": int(row.close_time),
        "open": float(row.open),
        "high": float(row.high),
        "low": float(row.low),
        "close": float(row.close),
        "volume": None if volume is None or pd.isna(volume) else float(volume),
        "is_closed": int(getattr(row, "is_closed", 1)),
    }


def _is_valid_cached_snapshot(zones: Any, expected_count: int, zone_set_as_of: int) -> bool:
    """Reject malformed derived cache rows so replay can rebuild them from canonical candles."""
    if not isinstance(zones, list) or len(zones) != expected_count:
        return False
    for zone in zones:
        if not isinstance(zone, dict):
            return False
        try:
            if int(zone.get("zone_set_as_of", -1)) != zone_set_as_of:
                return False
            if not str(zone.get("fingerprint", "")).startswith("zf1:"):
                return False
            if zone.get("fingerprint_version") != "zf1":
                return False
            if zone.get("source_timeframe") not in {"4h", "1d"}:
                return False
            if not all(key in zone for key in ("low", "mid", "high", "source_open_times", "zone_source_time")):
                return False
            low = float(zone["low"])
            mid = float(zone["mid"])
            high = float(zone["high"])
            if not all(math.isfinite(value) for value in (low, mid, high)) or not low <= mid <= high:
                return False
            source_times = zone["source_open_times"]
            if not isinstance(source_times, list) or not source_times:
                return False
            canonical_source_times = [int(value) for value in source_times]
            if any(value < 0 for value in canonical_source_times):
                return False
            if int(zone["zone_source_time"]) != max(canonical_source_times):
                return False
        except (TypeError, ValueError):
            return False
    return True
