from __future__ import annotations

import json
import sqlite3

from typing import Any

from ..utils import utc_seconds


def has_setup_buy(
    conn: sqlite3.Connection,
    selected_zone_fingerprint: str,
    dip_origin_open_time: int,
    trigger_open_time: int,
) -> bool:
    """Return True when a prior BUY already used this zone fingerprint + dip origin.

    The current trigger candle is excluded so an idempotent rerun of the same
    hour can still persist BUY instead of flipping to SETUP_ALREADY_BOUGHT.
    """
    row = conn.execute(
        """
        SELECT 1 FROM decisions
        WHERE decision='BUY'
          AND selected_zone_fingerprint=?
          AND dip_origin_open_time=?
          AND candle_open_time < ?
        LIMIT 1
        """,
        (selected_zone_fingerprint, int(dip_origin_open_time), int(trigger_open_time)),
    ).fetchone()
    return row is not None


def insert_decision(
    conn: sqlite3.Connection,
    decision: dict[str, Any],
    *,
    exchange: str,
    symbol: str,
    timeframe: str = "1h",
    created_at: int | None = None,
) -> int:
    if decision.get("decision") == "BUY" and not decision.get("selected_zone_fingerprint"):
        raise ValueError("A BUY decision requires selected_zone_fingerprint")
    columns = [
        "created_at", "exchange", "symbol", "timeframe", "candle_open_time", "candle_close_time",
        "reference_close", "zone_set_as_of", "fingerprint_version", "selected_zone_fingerprint",
        "selected_zone_low", "selected_zone_high", "selected_zone_mid", "selected_zone_source_time",
        "selected_source_open_times_json", "entry_region", "higher_zone_fingerprint", "higher_zone_low",
        "higher_zone_high", "next_lower_zone_fingerprint", "next_lower_zone_low", "next_lower_zone_high",
        "internal_range_low", "internal_range_high", "internal_range_midpoint", "below_zone_band_low",
        "below_zone_band_high", "below_zone_pct", "lookback_start_time", "lookback_end_time",
        "dip_origin_open_time", "dip_origin_close", "recent_buy_in_24h", "gate_results_json",
        "zones_rebuilt", "decision", "reason_code", "mode", "strategy_version", "config_version",
        "sanitized_error",
    ]
    values = {
        **decision,
        "created_at": utc_seconds() if created_at is None else int(created_at),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "selected_source_open_times_json": _json(decision.get("selected_source_open_times")),
        "gate_results_json": _json(decision.get("gate_results") or {}),
        "recent_buy_in_24h": int(bool(decision.get("recent_buy_in_24h"))),
        "zones_rebuilt": int(bool(decision.get("zones_rebuilt"))),
    }
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column not in {"created_at", "exchange", "symbol", "timeframe", "candle_open_time", "mode", "strategy_version"})
    conn.execute(
        f"""
        INSERT INTO decisions({','.join(columns)}) VALUES ({placeholders})
        ON CONFLICT(exchange, symbol, timeframe, mode, strategy_version, candle_open_time)
        DO UPDATE SET {updates}
        """,
        tuple(values.get(column) for column in columns),
    )
    row = conn.execute(
        """
        SELECT id FROM decisions
        WHERE exchange=? AND symbol=? AND timeframe=? AND mode=? AND strategy_version=? AND candle_open_time=?
        """,
        (exchange, symbol, timeframe, decision["mode"], decision["strategy_version"], decision["candle_open_time"]),
    ).fetchone()
    return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
