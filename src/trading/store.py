from __future__ import annotations

import json
import sqlite3

from typing import Any

from ..utils import utc_seconds
from .constants import ONE_HOUR_MS


def has_setup_buy(
    conn: sqlite3.Connection,
    selected_zone_fingerprint: str,
    dip_origin_open_time: int,
    trigger_open_time: int,
) -> bool:
    """Return True when a prior BUY already used this zone lineage + dip origin.

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


def has_recent_zone_buy(
    conn: sqlite3.Connection,
    selected_zone_fingerprint: str,
    trigger_open_time: int,
    *,
    cooldown_hours: int = 24,
) -> bool:
    """Return True when the same zone lineage already has a BUY inside the cooldown window.

    The window is (`trigger - cooldown`, `trigger`): the original BUY at exactly
    `cooldown_hours` earlier does not block. The current trigger candle is
    excluded so an idempotent rerun of the same hour can still persist BUY.
    A BUY at a different lineage does not count, so a deeper zone may still
    BUY within 24h of a shallower zone.
    """
    if isinstance(cooldown_hours, bool) or not isinstance(cooldown_hours, int) or cooldown_hours <= 0:
        raise ValueError("cooldown_hours must be a positive number of hours")
    window_start = int(trigger_open_time) - cooldown_hours * ONE_HOUR_MS
    row = conn.execute(
        """
        SELECT 1 FROM decisions
        WHERE decision='BUY'
          AND selected_zone_fingerprint=?
          AND candle_open_time > ?
          AND candle_open_time < ?
        LIMIT 1
        """,
        (selected_zone_fingerprint, window_start, int(trigger_open_time)),
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


def create_trade_execution(
    conn: sqlite3.Connection,
    decision_id: int,
    mode: str,
    *,
    status: str = "started",
    created_at: int | None = None,
) -> tuple[int, bool]:
    """Create one idempotent execution row for a BUY decision and report whether it was new."""
    if mode not in {"dry_run", "live"}:
        raise ValueError("Trade execution mode must be dry_run or live")
    timestamp = utc_seconds() if created_at is None else int(created_at)
    cursor = conn.execute(
        """
        INSERT INTO trade_executions(decision_id, created_at, updated_at, mode, status)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(decision_id) DO NOTHING
        """,
        (int(decision_id), timestamp, timestamp, mode, status),
    )
    row = conn.execute(
        "SELECT id FROM trade_executions WHERE decision_id=?",
        (int(decision_id),),
    ).fetchone()
    return int(row["id"] if isinstance(row, sqlite3.Row) else row[0]), cursor.rowcount == 1


def get_trade_execution(
    conn: sqlite3.Connection,
    execution_id: int,
) -> dict[str, Any] | None:
    """Load one redacted execution row for idempotency or reconciliation."""
    row = conn.execute(
        "SELECT * FROM trade_executions WHERE id=?",
        (int(execution_id),),
    ).fetchone()
    return None if row is None else dict(row)


def has_other_in_flight_execution(
    conn: sqlite3.Connection,
    execution_id: int,
) -> bool:
    """Block a second execution while any earlier non-terminal lifecycle is unresolved."""
    row = conn.execute(
        """
        SELECT 1 FROM trade_executions
        WHERE id != ?
          AND status IN ('started', 'risk_checked', 'quoted', 'allowance_ready', 'signed', 'broadcast', 'pending')
        LIMIT 1
        """,
        (int(execution_id),),
    ).fetchone()
    return row is not None


def get_live_exposure(
    conn: sqlite3.Connection,
    *,
    now_s: int,
    exclude_execution_id: int | None = None,
) -> dict[str, int]:
    """Count conservative live attempts for UTC-day and cumulative canary limits.

    An execution counts from `signed` onward, including reverted attempts,
    because a locally signed intent may have reached the network and should not
    free budget automatically.
    """
    day_start = int(now_s) - int(now_s) % 86_400
    excluded = -1 if exclude_execution_id is None else int(exclude_execution_id)
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS utc_day_trade_count,
          COALESCE(SUM(CASE WHEN created_at >= ? THEN amount_in_raw ELSE 0 END), 0) AS utc_day_spend_raw,
          COALESCE(SUM(amount_in_raw), 0) AS cumulative_spend_raw
        FROM trade_executions
        WHERE id != ?
          AND mode = 'live'
          AND status IN ('signed', 'broadcast', 'pending', 'confirmed', 'reverted')
        """,
        (day_start, day_start, excluded),
    ).fetchone()
    return {
        "utc_day_trade_count": int(row["utc_day_trade_count"]),
        "utc_day_spend_raw": int(row["utc_day_spend_raw"]),
        "cumulative_spend_raw": int(row["cumulative_spend_raw"]),
    }


def update_trade_execution(
    conn: sqlite3.Connection,
    execution_id: int,
    *,
    updated_at: int | None = None,
    **fields: Any,
) -> None:
    """Update only allowlisted redacted execution fields, never calldata or verification tokens."""
    allowed = {
        "status",
        "reason",
        "quote_router",
        "amount_in_raw",
        "quoted_amount_out",
        "minimum_amount_out",
        "quote_deadline",
        "verification_version",
        "approval_transaction_hashes_json",
        "gas_estimate",
        "nonce",
        "transaction_hash",
        "block_number",
        "gas_used",
        "actual_prana_output_raw",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unsupported trade execution fields: {sorted(unknown)}")
    if not fields:
        return
    values = dict(fields)
    if "approval_transaction_hashes_json" in values and not isinstance(
        values["approval_transaction_hashes_json"], str
    ):
        values["approval_transaction_hashes_json"] = _json(
            values["approval_transaction_hashes_json"]
        )
    values["updated_at"] = utc_seconds() if updated_at is None else int(updated_at)
    assignments = ",".join(f"{column}=?" for column in values)
    conn.execute(
        f"UPDATE trade_executions SET {assignments} WHERE id=?",
        (*values.values(), int(execution_id)),
    )


def _json(value: Any) -> str | None:
    """Serialize compact deterministic JSON for SQLite payload columns."""
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
