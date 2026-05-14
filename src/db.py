from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .utils import json_default, utc_seconds


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
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  origin TEXT NOT NULL,
  role TEXT NOT NULL,
  low REAL NOT NULL,
  high REAL NOT NULL,
  mid REAL NOT NULL,
  width REAL NOT NULL,
  width_pct REAL NOT NULL,
  touches INTEGER NOT NULL,
  source_closes_json TEXT NOT NULL,
  source_indexes_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  price REAL NOT NULL,
  decision TEXT NOT NULL,
  signal_score REAL NOT NULL,
  nearest_support_low REAL,
  nearest_support_high REAL,
  nearest_resistance_low REAL,
  nearest_resistance_high REAL,
  distance_to_support_pct REAL,
  distance_to_resistance_pct REAL,
  reason TEXT NOT NULL,
  metadata_json TEXT
);

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
        conn.executescript(SCHEMA)
        conn.commit()


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
    sql = """
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
      fetched_at=excluded.fetched_at;
    """
    with connect(database_path) as conn:
        conn.executemany(sql, rows)
        conn.commit()
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
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(int(limit))
        query = f"""
        SELECT * FROM (
          SELECT * FROM candles {where} ORDER BY open_time DESC{limit_sql}
        ) ORDER BY open_time ASC
        """
    else:
        query = f"SELECT * FROM candles {where} ORDER BY open_time ASC"
    with connect(database_path) as conn:
        return pd.read_sql_query(query, conn, params=params)


def insert_zones(
    database_path: str | Path,
    zones: list[dict[str, Any]],
    symbol: str,
    timeframe: str,
) -> int:
    if not zones:
        return 0
    init_db(database_path)
    created_at = utc_seconds()
    rows = [
        (
            created_at,
            symbol,
            timeframe,
            zone["origin"],
            zone["role"],
            float(zone["low"]),
            float(zone["high"]),
            float(zone["mid"]),
            float(zone["width"]),
            float(zone["width_pct"]),
            int(zone["touches"]),
            json.dumps(zone["source_closes"], default=json_default),
            json.dumps(zone["source_indexes"], default=json_default),
        )
        for zone in zones
    ]
    sql = """
    INSERT INTO zones (
      created_at, symbol, timeframe, origin, role, low, high, mid, width,
      width_pct, touches, source_closes_json, source_indexes_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with connect(database_path) as conn:
        conn.executemany(sql, rows)
        conn.commit()
    return len(rows)


def insert_signal(database_path: str | Path, signal: dict[str, Any], symbol: str, timeframe: str) -> int:
    init_db(database_path)
    sql = """
    INSERT INTO signals (
      created_at, symbol, timeframe, price, decision, signal_score,
      nearest_support_low, nearest_support_high, nearest_resistance_low,
      nearest_resistance_high, distance_to_support_pct, distance_to_resistance_pct,
      reason, metadata_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    metadata = signal.get("metadata") or {}
    row = (
        utc_seconds(),
        symbol,
        timeframe,
        float(signal["price"]),
        signal["decision"],
        float(signal["signal_score"]),
        _nullable_float(signal.get("nearest_support_low")),
        _nullable_float(signal.get("nearest_support_high")),
        _nullable_float(signal.get("nearest_resistance_low")),
        _nullable_float(signal.get("nearest_resistance_high")),
        _nullable_float(signal.get("distance_to_support_pct")),
        _nullable_float(signal.get("distance_to_resistance_pct")),
        signal["reason"],
        json.dumps(metadata, default=json_default),
    )
    with connect(database_path) as conn:
        cursor = conn.execute(sql, row)
        conn.commit()
        return int(cursor.lastrowid)


def candle_count(database_path: str | Path) -> int:
    init_db(database_path)
    with connect(database_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM candles").fetchone()
        return int(row["count"])


def _nullable_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
