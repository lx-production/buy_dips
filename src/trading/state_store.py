from __future__ import annotations

import re
import sqlite3

from .constants import DETECTOR_VERSION, EXCHANGE, FOUR_HOURS_MS, SYMBOL, ZONE_TIMEFRAME


class StateStoreError(RuntimeError):
    pass


def zone_rebuild_watermark_key(
    exchange: str = EXCHANGE,
    symbol: str = SYMBOL,
    timeframe: str = ZONE_TIMEFRAME,
    detector_version: str = DETECTOR_VERSION,
) -> str:
    values = (exchange, symbol, timeframe, detector_version)
    if any(not value or ":" in value for value in values):
        raise ValueError("Watermark scope values must be non-empty and cannot contain ':'")
    return "zone_rebuild_watermark:" + ":".join(values)


def zone_track_state_key(
    exchange: str = EXCHANGE,
    symbol: str = SYMBOL,
    timeframe: str = ZONE_TIMEFRAME,
    detector_version: str = DETECTOR_VERSION,
) -> str:
    """bot_state key for the sticky ZoneTrackState JSON that lives between live 4h rebuilds."""
    values = (exchange, symbol, timeframe, detector_version)
    if any(not value or ":" in value for value in values):
        raise ValueError("Track-state scope values must be non-empty and cannot contain ':'")
    return "zone_track_state:" + ":".join(values)


def get_zone_rebuild_watermark(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    raw = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    if not isinstance(raw, str) or re.fullmatch(r"0|[1-9][0-9]*", raw) is None:
        raise StateStoreError(f"Malformed zone rebuild watermark for {key}")
    return int(raw)


def set_zone_rebuild_watermark(
    conn: sqlite3.Connection,
    key: str,
    open_time_ms: int,
    updated_at_s: int,
) -> None:
    if isinstance(open_time_ms, bool) or int(open_time_ms) < 0:
        raise StateStoreError("Zone rebuild watermark must be a non-negative integer")
    if int(open_time_ms) % FOUR_HOURS_MS != 0:
        raise StateStoreError("Zone rebuild watermark is not aligned to a Binance UTC 4h bucket")
    conn.execute(
        """
        INSERT INTO bot_state(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, str(int(open_time_ms)), int(updated_at_s)),
    )


def get_zone_track_state_json(conn: sqlite3.Connection, key: str) -> str | None:
    """Load the sticky-track JSON blob, or None when this detector scope has no prior tracks."""
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    raw = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    if not isinstance(raw, str) or not raw:
        raise StateStoreError(f"Malformed zone track state for {key}")
    return raw


def set_zone_track_state_json(
    conn: sqlite3.Connection,
    key: str,
    payload_json: str,
    updated_at_s: int,
) -> None:
    """Persist sticky-track JSON in the same transaction as the live zone snapshot."""
    if not payload_json:
        raise StateStoreError("Zone track state JSON must be non-empty")
    conn.execute(
        """
        INSERT INTO bot_state(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, payload_json, int(updated_at_s)),
    )


def validate_zone_rebuild_watermark(
    conn: sqlite3.Connection,
    *,
    key: str,
    latest_completed_open_time: int,
    exchange: str = EXCHANGE,
    symbol: str = SYMBOL,
    timeframe: str = ZONE_TIMEFRAME,
    detector_version: str = DETECTOR_VERSION,
) -> int | None:
    watermark = get_zone_rebuild_watermark(conn, key)
    if watermark is None:
        return None
    if watermark % FOUR_HOURS_MS != 0:
        raise StateStoreError("Zone rebuild watermark is not Binance UTC 4h aligned")
    if watermark > int(latest_completed_open_time):
        raise StateStoreError("Zone rebuild watermark is newer than the latest completed 4h candle")
    validate_zone_snapshot(
        conn,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        detector_version=detector_version,
        zone_set_as_of=watermark,
    )
    return watermark


def validate_zone_snapshot(
    conn: sqlite3.Connection,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    detector_version: str,
    zone_set_as_of: int,
) -> int:
    manifest = conn.execute(
        """
        SELECT zone_count FROM zone_sets
        WHERE exchange=? AND symbol=? AND timeframe=? AND detector_version=? AND zone_set_as_of=?
        """,
        (exchange, symbol, timeframe, detector_version, int(zone_set_as_of)),
    ).fetchone()
    if manifest is None:
        raise StateStoreError("Zone rebuild watermark has no matching zone_sets manifest")
    expected = int(manifest["zone_count"] if isinstance(manifest, sqlite3.Row) else manifest[0])
    row = conn.execute(
        """
        SELECT COUNT(*) AS count FROM zones
        WHERE exchange=? AND symbol=? AND timeframe=? AND detector_version=? AND zone_set_as_of=?
        """,
        (exchange, symbol, timeframe, detector_version, int(zone_set_as_of)),
    ).fetchone()
    actual = int(row["count"] if isinstance(row, sqlite3.Row) else row[0])
    if expected != actual:
        raise StateStoreError(f"Zone snapshot is incomplete: manifest={expected}, rows={actual}")
    return expected
