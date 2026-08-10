"""Backfill Binance BTCUSDT 1h candles for the Jun 2026 offline backtest window.

Backtest evaluation can start at 2026-06-01 UTC. We fetch from 2026-05-30 so the
first Jun 1 hours already have a full 48h dip-origin lookback in the candles table.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from src.candles import backfill_range  # noqa: E402
from src.config import load_config  # noqa: E402
from src.utils import ms_to_iso, resolve_path, utc_ms  # noqa: E402

# 48h before the intended backtest start so the first Jun 1 triggers have lookback.
FETCH_START_ISO = "2026-05-30T00:00:00+00:00"
BACKTEST_START_ISO = "2026-06-01T00:00:00+00:00"
TIMEFRAME = "1h"


def _iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def main() -> int:
    config = load_config(None)
    database_path = resolve_path(config.database_path)
    start_time = _iso_to_ms(FETCH_START_ISO)
    # Exclusive end: up through the latest available candle at run time.
    end_time = utc_ms() + 1

    print(
        f"Backfilling {config.exchange} {config.symbol} {TIMEFRAME} "
        f"from {FETCH_START_ISO} through now"
    )
    print(f"Intended backtest signal start: {BACKTEST_START_ISO}")
    print(f"Database path: {database_path}")

    try:
        result = backfill_range(
            database_path=database_path,
            exchange=config.exchange,
            symbol=config.symbol,
            timeframe=TIMEFRAME,
            start_time=start_time,
            end_time=end_time,
        )
    except Exception as exc:
        print(f"Backfill failed: {exc}")
        return 2

    print(f"Candles inserted or updated: {result['upserted']}")
    print(f"Rows fetched this run: {result['fetched']}")
    print(f"First candle timestamp: {result['first_candle_timestamp']} ({result['first_candle_iso']})")
    print(f"Last candle timestamp: {result['last_candle_timestamp']} ({result['last_candle_iso']})")
    print(f"Fetch window start ms: {start_time} ({ms_to_iso(start_time)})")
    print(f"Fetch window end ms (exclusive): {end_time} ({ms_to_iso(end_time - 1)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
