"""Backfill BTCUSDT 4H candles from 2024-02-26 up to the existing earliest candle."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from src.candles import backfill_range  # noqa: E402
from src.config import load_config  # noqa: E402
from src.utils import resolve_path  # noqa: E402

# Extend history back to this date (UTC, start of day).
START_ISO = "2024-02-26T00:00:00+00:00"
# Stop before the first candle from the standard 12-month backfill.
END_EXCLUSIVE_ISO = "2025-05-14T12:00:00+00:00"


def _iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def main() -> int:
    config = load_config(None)
    database_path = resolve_path(config.database_path)
    start_time = _iso_to_ms(START_ISO)
    end_time = _iso_to_ms(END_EXCLUSIVE_ISO)

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
