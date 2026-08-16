from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .utils import utc_seconds


SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
  exchange TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  open_time INTEGER NOT NULL,
  close_time INTEGER NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL,
  is_closed INTEGER NOT NULL DEFAULT 1,
  fetched_at INTEGER NOT NULL,
  PRIMARY KEY(exchange, symbol, timeframe, open_time)
);

CREATE TABLE IF NOT EXISTS zones (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL,
  exchange TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  zone_set_as_of INTEGER NOT NULL,
  fingerprint_version TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  source_timeframe TEXT NOT NULL,
  source_open_times_json TEXT NOT NULL,
  zone_source_time INTEGER NOT NULL,
  origin TEXT NOT NULL,
  role TEXT NOT NULL,
  bounds_style TEXT NOT NULL,
  low REAL NOT NULL,
  high REAL NOT NULL,
  mid REAL NOT NULL,
  width REAL NOT NULL,
  width_pct REAL NOT NULL,
  touches INTEGER NOT NULL,
  source_closes_json TEXT NOT NULL,
  source_indexes_json TEXT NOT NULL,
  metadata_json TEXT,
  UNIQUE(exchange, symbol, timeframe, detector_version, zone_set_as_of, fingerprint)
);

CREATE TABLE IF NOT EXISTS zone_sets (
  exchange TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  zone_set_as_of INTEGER NOT NULL,
  zone_count INTEGER NOT NULL CHECK(zone_count >= 0),
  created_at INTEGER NOT NULL,
  PRIMARY KEY(exchange, symbol, timeframe, detector_version, zone_set_as_of)
);

CREATE TABLE IF NOT EXISTS backtest_zone_cache (
  exchange TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  zone_set_as_of INTEGER NOT NULL,
  detector_version TEXT NOT NULL,
  detector_signature TEXT NOT NULL,
  zone_config_hash TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  zone_count INTEGER NOT NULL CHECK(zone_count >= 0),
  zones_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(exchange, symbol, timeframe, zone_set_as_of)
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL,
  exchange TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  candle_open_time INTEGER NOT NULL,
  candle_close_time INTEGER NOT NULL,
  reference_close REAL NOT NULL,
  zone_set_as_of INTEGER NOT NULL,
  fingerprint_version TEXT NOT NULL,
  selected_zone_fingerprint TEXT,
  selected_zone_low REAL,
  selected_zone_high REAL,
  selected_zone_mid REAL,
  selected_zone_source_time INTEGER,
  selected_source_open_times_json TEXT,
  entry_region TEXT,
  higher_zone_fingerprint TEXT,
  higher_zone_low REAL,
  higher_zone_high REAL,
  next_lower_zone_fingerprint TEXT,
  next_lower_zone_low REAL,
  next_lower_zone_high REAL,
  internal_range_low REAL,
  internal_range_high REAL,
  internal_range_midpoint REAL,
  below_zone_band_low REAL,
  below_zone_band_high REAL,
  below_zone_pct REAL,
  lookback_start_time INTEGER,
  lookback_end_time INTEGER,
  dip_origin_open_time INTEGER,
  dip_origin_close REAL,
  recent_buy_in_24h INTEGER NOT NULL DEFAULT 0 CHECK(recent_buy_in_24h IN (0, 1)),
  gate_results_json TEXT NOT NULL,
  zones_rebuilt INTEGER NOT NULL CHECK(zones_rebuilt IN (0, 1)),
  decision TEXT NOT NULL CHECK(decision IN ('BUY', 'HOLD')),
  reason_code TEXT NOT NULL CHECK(reason_code IN (
    'CLOSE_NOT_BELOW_OPEN',
    'CLOSE_OUTSIDE_ENTRY_REGION',
    'NO_HIGHER_ZONE',
    'NO_RECENT_CLOSE_ABOVE_INTERNAL_MID',
    'NO_LOWER_ZONE',
    'BELOW_ZONE_OUT_OF_BAND',
    'ZONE_APPROACHED_FROM_BELOW',
    'SETUP_ALREADY_BOUGHT',
    'RECENT_BUY_IN_24H',
    'BUY_GATES_PASSED'
  )),
  mode TEXT NOT NULL CHECK(mode IN ('observe', 'dry_run', 'live')),
  strategy_version TEXT NOT NULL,
  config_version TEXT NOT NULL,
  sanitized_error TEXT,
  CHECK(decision != 'BUY' OR selected_zone_fingerprint IS NOT NULL),
  UNIQUE(exchange, symbol, timeframe, mode, strategy_version, candle_open_time)
);

CREATE INDEX IF NOT EXISTS decisions_buy_zone_time
ON decisions(decision, selected_zone_fingerprint, candle_open_time);

CREATE INDEX IF NOT EXISTS decisions_buy_setup
ON decisions(decision, selected_zone_fingerprint, dip_origin_open_time);

CREATE TABLE IF NOT EXISTS bot_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Keep Unix timestamps canonical and expose fixed UTC+7 strings for operator queries.
CREATE VIEW IF NOT EXISTS candles_readable AS
SELECT
  candles.*,
  datetime(open_time / 1000.0, 'unixepoch', '+7 hours') || ' +07:00' AS open_time_utc7,
  datetime(close_time / 1000.0, 'unixepoch', '+7 hours') || ' +07:00' AS close_time_utc7,
  datetime(fetched_at, 'unixepoch', '+7 hours') || ' +07:00' AS fetched_at_utc7
FROM candles;

CREATE VIEW IF NOT EXISTS zones_readable AS
SELECT
  zones.*,
  datetime(created_at, 'unixepoch', '+7 hours') || ' +07:00' AS created_at_utc7,
  datetime(zone_set_as_of / 1000.0, 'unixepoch', '+7 hours') || ' +07:00' AS zone_set_as_of_utc7,
  (
    SELECT json_group_array(value_utc7)
    FROM (
      SELECT datetime(json_each.value / 1000.0, 'unixepoch', '+7 hours') || ' +07:00' AS value_utc7
      FROM json_each(zones.source_open_times_json)
      ORDER BY CAST(json_each.key AS INTEGER)
    )
  ) AS source_open_times_json_utc7,
  datetime(zone_source_time / 1000.0, 'unixepoch', '+7 hours') || ' +07:00' AS zone_source_time_utc7
FROM zones;

CREATE VIEW IF NOT EXISTS zone_sets_readable AS
SELECT
  zone_sets.*,
  datetime(zone_set_as_of / 1000.0, 'unixepoch', '+7 hours') || ' +07:00' AS zone_set_as_of_utc7,
  datetime(created_at, 'unixepoch', '+7 hours') || ' +07:00' AS created_at_utc7
FROM zone_sets;

CREATE VIEW IF NOT EXISTS decisions_readable AS
SELECT
  decisions.*,
  datetime(created_at, 'unixepoch', '+7 hours') || ' +07:00' AS created_at_utc7,
  datetime(candle_open_time / 1000.0, 'unixepoch', '+7 hours') || ' +07:00' AS candle_open_time_utc7,
  datetime(candle_close_time / 1000.0, 'unixepoch', '+7 hours') || ' +07:00' AS candle_close_time_utc7,
  datetime(zone_set_as_of / 1000.0, 'unixepoch', '+7 hours') || ' +07:00' AS zone_set_as_of_utc7,
  datetime(selected_zone_source_time / 1000.0, 'unixepoch', '+7 hours') || ' +07:00'
    AS selected_zone_source_time_utc7,
  CASE
    WHEN selected_source_open_times_json IS NOT NULL
    THEN (
      SELECT json_group_array(value_utc7)
      FROM (
        SELECT datetime(json_each.value / 1000.0, 'unixepoch', '+7 hours') || ' +07:00' AS value_utc7
        FROM json_each(decisions.selected_source_open_times_json)
        ORDER BY CAST(json_each.key AS INTEGER)
      )
    )
  END AS selected_source_open_times_json_utc7,
  datetime(lookback_start_time / 1000.0, 'unixepoch', '+7 hours') || ' +07:00'
    AS lookback_start_time_utc7,
  datetime(lookback_end_time / 1000.0, 'unixepoch', '+7 hours') || ' +07:00'
    AS lookback_end_time_utc7,
  datetime(dip_origin_open_time / 1000.0, 'unixepoch', '+7 hours') || ' +07:00'
    AS dip_origin_open_time_utc7
FROM decisions;

CREATE VIEW IF NOT EXISTS bot_state_readable AS
SELECT
  bot_state.*,
  CASE
    WHEN key LIKE 'zone_rebuild_watermark:%'
    THEN datetime(CAST(value AS INTEGER) / 1000.0, 'unixepoch', '+7 hours') || ' +07:00'
  END AS value_utc7,
  datetime(updated_at, 'unixepoch', '+7 hours') || ' +07:00' AS updated_at_utc7
FROM bot_state;
"""


def connect(database_path: str | Path) -> sqlite3.Connection:
    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(database_path: str | Path) -> None:
    with connect(database_path) as conn:
        _drop_disposable_phase1_tables(conn)
        conn.executescript(SCHEMA)
        _upgrade_decisions_reason_codes(conn)
        conn.commit()


def _upgrade_decisions_reason_codes(conn: sqlite3.Connection) -> None:
    """Rebuild `decisions` when its CHECK constraint is missing a current reason code.

    SQLite cannot ALTER a CHECK list in place. Existing databases keep the old
    table after `CREATE TABLE IF NOT EXISTS`, so copy rows into a new table
    that accepts the current reason set, then drop the old one.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions'"
    ).fetchone()
    if row is None:
        return
    sql = row["sql"] if isinstance(row, sqlite3.Row) else row[0]
    required_codes = (
        "CLOSE_NOT_BELOW_OPEN",
        "ZONE_APPROACHED_FROM_BELOW",
        "SETUP_ALREADY_BOUGHT",
    )
    if sql is None or all(code in sql for code in required_codes):
        return
    conn.execute("DROP VIEW IF EXISTS decisions_readable")
    conn.execute("DROP INDEX IF EXISTS decisions_buy_zone_time")
    conn.execute("DROP INDEX IF EXISTS decisions_buy_setup")
    conn.execute("ALTER TABLE decisions RENAME TO decisions_pre_reason_upgrade")
    conn.executescript(SCHEMA)
    old_columns = [item["name"] for item in conn.execute("PRAGMA table_info(decisions_pre_reason_upgrade)")]
    new_columns = [item["name"] for item in conn.execute("PRAGMA table_info(decisions)")]
    shared = [column for column in old_columns if column in set(new_columns)]
    column_sql = ",".join(shared)
    conn.execute(
        f"INSERT INTO decisions ({column_sql}) SELECT {column_sql} FROM decisions_pre_reason_upgrade"
    )
    conn.execute("DROP TABLE decisions_pre_reason_upgrade")


def _drop_disposable_phase1_tables(conn: sqlite3.Connection) -> None:
    """Remove the retired paper schema; candle history and bot_state survive."""
    conn.execute("DROP TABLE IF EXISTS signals")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(zones)")}
    if columns and "fingerprint" not in columns:
        conn.execute("DROP TABLE zones")


def upsert_candles(
    database_path: str | Path,
    candles: Iterable[dict[str, Any]],
    exchange: str,
    symbol: str,
    timeframe: str,
) -> int:
    records = list(candles)
    if not records:
        return 0
    init_db(database_path)
    with connect(database_path) as conn:
        count = upsert_candles_conn(conn, records, exchange, symbol, timeframe)
        conn.commit()
        return count


def upsert_candles_conn(
    conn: sqlite3.Connection,
    candles: Iterable[dict[str, Any]],
    exchange: str,
    symbol: str,
    timeframe: str,
) -> int:
    records = list(candles)
    if not records:
        return 0
    fetched_at = utc_seconds()
    rows = [
        (
            exchange,
            symbol,
            timeframe,
            int(candle["open_time"]),
            int(candle["close_time"]),
            float(candle["open"]),
            float(candle["high"]),
            float(candle["low"]),
            float(candle["close"]),
            float(candle["volume"]) if candle.get("volume") is not None else None,
            int(candle.get("is_closed", 1)),
            fetched_at,
        )
        for candle in records
    ]
    conn.executemany(
        """
        INSERT INTO candles (
          exchange, symbol, timeframe, open_time, close_time, open, high, low, close,
          volume, is_closed, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(exchange, symbol, timeframe, open_time) DO UPDATE SET
          close_time=excluded.close_time,
          open=excluded.open,
          high=excluded.high,
          low=excluded.low,
          close=excluded.close,
          volume=excluded.volume,
          is_closed=excluded.is_closed,
          fetched_at=excluded.fetched_at
        """,
        rows,
    )
    return len(rows)


def load_candles_df(
    database_path: str | Path,
    exchange: str,
    symbol: str,
    timeframe: str,
    only_closed: bool = True,
    limit: int | None = None,
) -> pd.DataFrame:
    init_db(database_path)
    where = "WHERE exchange = ? AND symbol = ? AND timeframe = ?"
    params: list[Any] = [exchange, symbol, timeframe]
    if only_closed:
        where += " AND is_closed = 1"
    if limit is not None:
        params.append(int(limit))
        query = f"""
        SELECT * FROM (
          SELECT * FROM candles {where} ORDER BY open_time DESC LIMIT ?
        ) ORDER BY open_time ASC
        """
    else:
        query = f"SELECT * FROM candles {where} ORDER BY open_time ASC"
    with connect(database_path) as conn:
        return pd.read_sql_query(query, conn, params=params)


def candle_count(database_path: str | Path) -> int:
    init_db(database_path)
    with connect(database_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM candles").fetchone()
        return int(row["count"])
