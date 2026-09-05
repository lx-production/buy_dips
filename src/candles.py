from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .binance_client import BinanceSpotClient
from .db import load_candles_df, upsert_candles
from .utils import ms_to_iso, utc_ms


INTERVAL_MS = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}


def backfill_range(
    database_path: str | Path,
    exchange: str,
    symbol: str,
    timeframe: str,
    start_time: int,
    end_time: int,
    client: BinanceSpotClient | None = None,
) -> dict[str, Any]:
    """Fetch and upsert closed Binance klines for [start_time, end_time).

    Times are Unix milliseconds. end_time is exclusive (open_time must be < end_time).
    """
    client = client or BinanceSpotClient()
    interval_ms = INTERVAL_MS.get(timeframe)
    if interval_ms is None:
        supported = ", ".join(sorted(INTERVAL_MS))
        raise ValueError(f"Unsupported timeframe: {timeframe}. Supported: {supported}")
    if end_time <= start_time:
        raise ValueError("end_time must be greater than start_time")

    total_upserted = 0
    all_rows: list[dict[str, Any]] = []
    cursor = start_time
    # Walk forward in 1000-kline pages until we reach the exclusive end.
    while cursor < end_time:
        batch = client.fetch_klines(
            symbol=symbol,
            interval=timeframe,
            limit=1000,
            start_time=cursor,
            end_time=end_time - 1,
        )
        if not batch:
            break
        total_upserted += upsert_candles(database_path, batch, exchange, symbol, timeframe)
        all_rows.extend(batch)
        last_open_time = int(batch[-1]["open_time"])
        next_cursor = last_open_time + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1000:
            break

    stored_df = load_candles_df(database_path, exchange, symbol, timeframe, only_closed=False)
    first_open_time = int(stored_df["open_time"].min()) if not stored_df.empty else None
    last_open_time = int(stored_df["open_time"].max()) if not stored_df.empty else None
    return {
        "upserted": total_upserted,
        "fetched": len(all_rows),
        "first_candle_timestamp": first_open_time,
        "last_candle_timestamp": last_open_time,
        "first_candle_iso": ms_to_iso(first_open_time),
        "last_candle_iso": ms_to_iso(last_open_time),
        "database_path": str(database_path),
    }


def backfill_days(
    database_path: str | Path,
    exchange: str = "binance",
    symbol: str = "BTCUSDT",
    timeframe: str = "4h",
    days: int = 365,
    client: BinanceSpotClient | None = None,
) -> dict[str, Any]:
    """Fetch and upsert closed klines for the last `days` days up to now."""
    if days <= 0:
        raise ValueError("days must be a positive integer")
    end_time = utc_ms()
    start_time = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    # Inclusive "now": treat current ms as exclusive upper bound by adding 1.
    return backfill_range(
        database_path=database_path,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        start_time=start_time,
        end_time=end_time + 1,
        client=client,
    )


def backfill_12_months(
    database_path: str | Path,
    exchange: str = "binance",
    symbol: str = "BTCUSDT",
    timeframe: str = "4h",
    client: BinanceSpotClient | None = None,
) -> dict[str, Any]:
    """Fetch and upsert roughly the last 12 months of closed klines."""
    return backfill_days(
        database_path=database_path,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        days=365,
        client=client,
    )


def fetch_latest_candles(
    database_path: str | Path,
    exchange: str = "binance",
    symbol: str = "BTCUSDT",
    timeframe: str = "4h",
    limit: int = 1000,
    client: BinanceSpotClient | None = None,
) -> int:
    client = client or BinanceSpotClient()
    rows = client.fetch_klines(symbol=symbol, interval=timeframe, limit=limit)
    return upsert_candles(database_path, rows, exchange, symbol, timeframe)
