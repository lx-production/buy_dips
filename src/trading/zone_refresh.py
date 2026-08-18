from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..config import ZoneConfig
from ..db import connect, init_db
from ..utils import json_default, utc_seconds
from ..zones import ZoneDetectorEvidence, aggregate_ohlc_to_daily, detect_support_resistance_zones, materialize_support_zones
from ..zones.timeframes import ohlc_open_times
from .constants import DETECTOR_VERSION, EXCHANGE, FOUR_HOURS_MS, SYMBOL, ZONE_TIMEFRAME
from .state_store import (
    StateStoreError,
    get_zone_rebuild_watermark,
    set_zone_rebuild_watermark,
    validate_zone_rebuild_watermark,
    validate_zone_snapshot,
    zone_rebuild_watermark_key,
)
from .zone_identity import ZoneFingerprintCache, fingerprint_zone


Detector = Callable[..., dict[str, list[dict[str, Any]]]]


@dataclass(frozen=True)
class ZoneRefreshResult:
    zones: list[dict[str, Any]]
    zone_set_as_of: int
    rebuilt: bool


class ZoneRefreshError(RuntimeError):
    pass


def build_fingerprinted_support_zones(
    four_hour_df: pd.DataFrame,
    *,
    zone_config: ZoneConfig,
    zone_set_as_of: int,
    exchange: str = EXCHANGE,
    symbol: str = SYMBOL,
    detector_version: str = DETECTOR_VERSION,
    detector: Detector = detect_support_resistance_zones,
    fingerprint_cache: ZoneFingerprintCache | None = None,
) -> list[dict[str, Any]]:
    """Run the detector and attach source times plus lineage/revision hashes.

    Shared by live `refresh_zones` persistence and injected-detector backtest rebuilds so
    source-time resolution and fingerprinting stay one implementation.
    """
    eligible = _eligible_closed_four_hour_frame(
        four_hour_df,
        zone_config=zone_config,
        zone_set_as_of=zone_set_as_of,
    )
    result = detector(
        eligible,
        min_touches=zone_config.min_touches,
        current_price=float(eligible.iloc[-1]["close"]),
        buffer_pct=zone_config.role_buffer_pct,
        external_swing_order=zone_config.external_swing_order,
        atr_period=zone_config.atr_period,
        break_atr_mult=zone_config.break_atr_mult,
        external_min_swing_atr_mult=zone_config.external_min_swing_atr_mult,
        external_min_swing_pct=zone_config.external_min_swing_pct,
    )
    support = result.get("support")
    if not isinstance(support, list):
        raise ZoneRefreshError("Detector did not return a support zone list")
    return _fingerprint_support_zones(
        support,
        eligible,
        zone_set_as_of=int(zone_set_as_of),
        exchange=exchange,
        symbol=symbol,
        detector_version=detector_version,
        cache=fingerprint_cache,
    )


def build_fingerprinted_support_zones_from_evidence(
    evidence: ZoneDetectorEvidence | None,
    four_hour_df: pd.DataFrame,
    *,
    zone_config: ZoneConfig,
    zone_set_as_of: int,
    exchange: str = EXCHANGE,
    symbol: str = SYMBOL,
    detector_version: str = DETECTOR_VERSION,
    fingerprint_cache: ZoneFingerprintCache | None = None,
) -> list[dict[str, Any]]:
    """Materialize support from detector evidence and attach the same zf1 fingerprints.

    `evidence is None` is the empty-zone outcome (no prominent pivots). Too-short history
    still fail-closes via the shared 4h frame checks, matching the stateless rebuild path.
    """
    eligible = _eligible_closed_four_hour_frame(
        four_hour_df,
        zone_config=zone_config,
        zone_set_as_of=zone_set_as_of,
    )
    if evidence is None:
        support: list[dict[str, Any]] = []
    else:
        materialized = materialize_support_zones(
            evidence,
            min_touches=zone_config.min_touches,
            buffer_pct=zone_config.role_buffer_pct,
            break_atr_mult=zone_config.break_atr_mult,
        )
        support = materialized.get("support")
        if not isinstance(support, list):
            raise ZoneRefreshError("Detector did not return a support zone list")
    return _fingerprint_support_zones(
        support,
        eligible,
        zone_set_as_of=int(zone_set_as_of),
        exchange=exchange,
        symbol=symbol,
        detector_version=detector_version,
        four_hour_open_times=None if evidence is None else evidence.four_hour_open_times,
        daily_open_times=None if evidence is None else evidence.daily_open_times,
        cache=fingerprint_cache,
    )


def _eligible_closed_four_hour_frame(
    four_hour_df: pd.DataFrame,
    *,
    zone_config: ZoneConfig,
    zone_set_as_of: int,
) -> pd.DataFrame:
    """Slice closed 4h history through the target watermark and fail closed if it is unusable."""
    if four_hour_df is None or four_hour_df.empty or "open_time" not in four_hour_df.columns:
        raise ZoneRefreshError("No completed closed 4h candles are available")
    closed = four_hour_df.copy()
    if "is_closed" in closed.columns:
        closed = closed[closed["is_closed"].astype(int) == 1].copy()
    closed = closed.sort_values("open_time").reset_index(drop=True)
    if closed.empty:
        raise ZoneRefreshError("No completed closed 4h candles are available")
    target = int(zone_set_as_of)
    if target < 0 or target % FOUR_HOURS_MS != 0:
        raise ZoneRefreshError("zone_set_as_of is not Binance UTC 4h aligned")
    eligible = closed[closed["open_time"].astype("int64") <= target].reset_index(drop=True)
    if eligible.empty or int(eligible.iloc[-1]["open_time"]) != target:
        raise ZoneRefreshError("4h history does not include the target zone_set_as_of candle")
    if len(eligible) < max(1, int(zone_config.external_swing_order) * 2 + 1):
        raise ZoneRefreshError("Insufficient closed 4h history for support_structure_v1")
    return eligible


def _fingerprint_support_zones(
    support: list[dict[str, Any]],
    eligible: pd.DataFrame,
    *,
    zone_set_as_of: int,
    exchange: str,
    symbol: str,
    detector_version: str,
    four_hour_open_times: np.ndarray | None = None,
    daily_open_times: np.ndarray | None = None,
    cache: ZoneFingerprintCache | None = None,
) -> list[dict[str, Any]]:
    """Resolve source times on the eligible 4h/daily frames and attach lineage/revision hashes."""
    if four_hour_open_times is None or len(four_hour_open_times) == 0:
        four_hour_open_times = ohlc_open_times(eligible)
    if daily_open_times is None:
        daily_df = aggregate_ohlc_to_daily(eligible, min_bars_per_day=6)
        daily_open_times = ohlc_open_times(daily_df)
    else:
        daily_df = None
    target = int(zone_set_as_of)
    return [
        {
            **fingerprint_zone(
                zone,
                four_hour_df=eligible,
                daily_df=daily_df,
                four_hour_open_times=four_hour_open_times,
                daily_open_times=daily_open_times,
                cache=cache,
                exchange=exchange,
                symbol=symbol,
                detector_version=detector_version,
            ),
            "zone_set_as_of": target,
        }
        for zone in support
    ]


def refresh_zones(
    database_path: str | Path,
    four_hour_df: pd.DataFrame,
    *,
    zone_config: ZoneConfig,
    exchange: str = EXCHANGE,
    symbol: str = SYMBOL,
    detector_version: str = DETECTOR_VERSION,
    detector: Detector = detect_support_resistance_zones,
    now_s: int | None = None,
) -> ZoneRefreshResult:
    """Validate/load or atomically rebuild the latest fingerprinted zone snapshot."""
    if four_hour_df is None or four_hour_df.empty or "open_time" not in four_hour_df.columns:
        raise ZoneRefreshError("No completed closed 4h candles are available")
    closed = four_hour_df.copy()
    if "is_closed" in closed.columns:
        closed = closed[closed["is_closed"].astype(int) == 1].copy()
    closed = closed.sort_values("open_time").reset_index(drop=True)
    if closed.empty:
        raise ZoneRefreshError("No completed closed 4h candles are available")
    target = int(closed.iloc[-1]["open_time"])
    if target < 0 or target % FOUR_HOURS_MS != 0:
        raise ZoneRefreshError("Latest closed 4h candle is not Binance UTC aligned")

    init_db(database_path)
    key = zone_rebuild_watermark_key(exchange, symbol, ZONE_TIMEFRAME, detector_version)
    with connect(database_path) as conn:
        try:
            watermark = validate_zone_rebuild_watermark(
                conn,
                key=key,
                latest_completed_open_time=target,
                exchange=exchange,
                symbol=symbol,
                timeframe=ZONE_TIMEFRAME,
                detector_version=detector_version,
            )
        except StateStoreError as exc:
            raise ZoneRefreshError(str(exc)) from exc
        if watermark == target:
            return ZoneRefreshResult(_load_snapshot(conn, exchange, symbol, detector_version, target), target, False)
        if watermark is not None and watermark > target:
            raise ZoneRefreshError("Zone watermark is ahead of completed 4h data")

    # Shared pure rebuild path keeps live persistence identical to offline backtest fingerprints.
    fingerprinted = build_fingerprinted_support_zones(
        closed,
        zone_config=zone_config,
        zone_set_as_of=target,
        exchange=exchange,
        symbol=symbol,
        detector_version=detector_version,
        detector=detector,
    )

    written_at = utc_seconds() if now_s is None else int(now_s)
    with connect(database_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = get_zone_rebuild_watermark(conn, key)
            if current == target:
                validate_zone_snapshot(
                    conn,
                    exchange=exchange,
                    symbol=symbol,
                    timeframe=ZONE_TIMEFRAME,
                    detector_version=detector_version,
                    zone_set_as_of=target,
                )
                zones = _load_snapshot(conn, exchange, symbol, detector_version, target)
                conn.commit()
                return ZoneRefreshResult(zones, target, False)
            if current is not None and current > target:
                raise ZoneRefreshError("Concurrent runner advanced the zone watermark beyond target")

            conn.execute(
                "DELETE FROM zones WHERE exchange=? AND symbol=? AND timeframe=? AND detector_version=? AND zone_set_as_of=?",
                (exchange, symbol, ZONE_TIMEFRAME, detector_version, target),
            )
            conn.execute(
                "DELETE FROM zone_sets WHERE exchange=? AND symbol=? AND timeframe=? AND detector_version=? AND zone_set_as_of=?",
                (exchange, symbol, ZONE_TIMEFRAME, detector_version, target),
            )
            _insert_zones(conn, fingerprinted, exchange, symbol, detector_version, target, written_at)
            conn.execute(
                """
                INSERT INTO zone_sets(exchange, symbol, timeframe, detector_version, zone_set_as_of, zone_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (exchange, symbol, ZONE_TIMEFRAME, detector_version, target, len(fingerprinted), written_at),
            )
            set_zone_rebuild_watermark(conn, key, target, written_at)
            validate_zone_snapshot(
                conn,
                exchange=exchange,
                symbol=symbol,
                timeframe=ZONE_TIMEFRAME,
                detector_version=detector_version,
                zone_set_as_of=target,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ZoneRefreshResult(fingerprinted, target, True)


def _insert_zones(
    conn: sqlite3.Connection,
    zones: list[dict[str, Any]],
    exchange: str,
    symbol: str,
    detector_version: str,
    zone_set_as_of: int,
    created_at: int,
) -> None:
    sql = """
    INSERT INTO zones(
      created_at, exchange, symbol, timeframe, detector_version, zone_set_as_of,
      fingerprint_version, fingerprint, source_timeframe, source_open_times_json,
      zone_source_time, origin, role, bounds_style, low, high, mid, width, width_pct,
      touches, source_closes_json, source_indexes_json, metadata_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for zone in zones:
        known = {
            "origin", "role", "bounds_style", "low", "high", "mid", "width", "width_pct",
            "touches", "source_closes", "source_indexes", "source_timeframe", "source_open_times",
            "zone_source_time", "fingerprint_version", "fingerprint", "zone_set_as_of",
        }
        metadata = {key: value for key, value in zone.items() if key not in known}
        conn.execute(
            sql,
            (
                created_at, exchange, symbol, ZONE_TIMEFRAME, detector_version, zone_set_as_of,
                zone["fingerprint_version"], zone["fingerprint"], zone["source_timeframe"],
                json.dumps(zone["source_open_times"], separators=(",", ":")), zone["zone_source_time"],
                zone["origin"], zone.get("role", "support"), zone.get("bounds_style", "body"),
                float(zone["low"]), float(zone["high"]), float(zone["mid"]), float(zone["width"]),
                float(zone["width_pct"]), int(zone["touches"]),
                json.dumps(zone.get("source_closes", []), default=json_default, separators=(",", ":")),
                json.dumps(zone["source_indexes"], default=json_default, separators=(",", ":")),
                json.dumps(metadata, default=json_default, separators=(",", ":")),
            ),
        )


def _load_snapshot(
    conn: sqlite3.Connection,
    exchange: str,
    symbol: str,
    detector_version: str,
    zone_set_as_of: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM zones
        WHERE exchange=? AND symbol=? AND timeframe=? AND detector_version=? AND zone_set_as_of=?
        ORDER BY low ASC, fingerprint ASC
        """,
        (exchange, symbol, ZONE_TIMEFRAME, detector_version, int(zone_set_as_of)),
    ).fetchall()
    zones: list[dict[str, Any]] = []
    for row in rows:
        zone = dict(row)
        zone["source_open_times"] = json.loads(zone.pop("source_open_times_json"))
        zone["source_closes"] = json.loads(zone.pop("source_closes_json"))
        zone["source_indexes"] = json.loads(zone.pop("source_indexes_json"))
        metadata_raw = zone.pop("metadata_json", None)
        if metadata_raw:
            zone.update(json.loads(metadata_raw))
        if zone.get("fingerprint_version") != "zf1" or not str(zone.get("fingerprint", "")).startswith("zf1:"):
            raise ZoneRefreshError("Persisted zone snapshot contains an invalid fingerprint")
        source_times = zone.get("source_open_times")
        if not isinstance(source_times, list) or not source_times or int(zone["zone_source_time"]) != max(
            int(value) for value in source_times
        ):
            raise ZoneRefreshError("Persisted zone snapshot contains invalid source times")
        zones.append(zone)
    return zones
