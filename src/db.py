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
    'CLOSE_OUTSIDE_ENTRY_REGION',
    'CLOSE_NOT_BELOW_ZONE_MID',
    'NO_HIGHER_ZONE',
    'NO_RECENT_CLOSE_ABOVE_INTERNAL_MID',
    'NO_LOWER_ZONE',
    'BELOW_ZONE_OUT_OF_BAND',
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

CREATE TABLE IF NOT EXISTS bot_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
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
        conn.commit()


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
