"""Backfill BTCUSDT 4H candles from 2024-02-26 up to the existing earliest candle."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from src.binance_client import BinanceSpotClient  # noqa: E402
from src.candles import INTERVAL_MS  # noqa: E402
from src.config import load_config  # noqa: E402
from src.db import load_candles_df, upsert_candles  # noqa: E402
from src.paper_trading import PAPER_MODE_WARNING  # noqa: E402
from src.utils import ms_to_iso, resolve_path  # noqa: E402

# Extend history back to this date (UTC, start of day).
START_ISO = "2024-02-26T00:00:00+00:00"
# Stop before the first candle from the standard 12-month backfill.
END_EXCLUSIVE_ISO = "2025-05-14T12:00:00+00:00"


def _iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def backfill_range(
    database_path: str | Path,
    exchange: str,
    symbol: str,
    timeframe: str,
    start_time: int,
    end_time: int,
    client: BinanceSpotClient | None = None,
) -> dict[str, Any]:
    client = client or BinanceSpotClient()
    interval_ms = INTERVAL_MS.get(timeframe)
    if interval_ms is None:
        raise ValueError(f"Unsupported timeframe for Phase 1: {timeframe}")

    total_upserted = 0
    all_rows: list[dict[str, Any]] = []
    cursor = start_time
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

    closed_df = load_candles_df(database_path, exchange, symbol, timeframe, only_closed=False)
    first_open_time = int(closed_df["open_time"].min()) if not closed_df.empty else None
    last_open_time = int(closed_df["open_time"].max()) if not closed_df.empty else None
    return {
        "upserted": total_upserted,
        "fetched": len(all_rows),
        "first_candle_timestamp": first_open_time,
        "last_candle_timestamp": last_open_time,
        "first_candle_iso": ms_to_iso(first_open_time),
        "last_candle_iso": ms_to_iso(last_open_time),
        "database_path": str(database_path),
    }


def main() -> int:
    config = load_config(None)
    database_path = resolve_path(config.database_path)
    start_time = _iso_to_ms(START_ISO)
    end_time = _iso_to_ms(END_EXCLUSIVE_ISO)

    print(PAPER_MODE_WARNING)
    print(f"Backfilling {config.symbol} {config.timeframe} from {START_ISO} to before {END_EXCLUSIVE_ISO}")

    try:
        result = backfill_range(
            database_path=database_path,
            exchange=config.exchange,
            symbol=config.symbol,
            timeframe=config.timeframe,
            start_time=start_time,
            end_time=end_time,
        )
    except Exception as exc:
        print(f"Backfill failed: {exc}")
        return 2

    print(f"Candles inserted or updated: {result['upserted']}")
    print(f"First candle timestamp: {result['first_candle_timestamp']} ({result['first_candle_iso']})")
    print(f"Last candle timestamp: {result['last_candle_timestamp']} ({result['last_candle_iso']})")
    print(f"Database path: {result['database_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
