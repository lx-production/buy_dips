"""Print the latest n candle rows from the SQLite DB (defaults from config.yaml)."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from src.config import load_config  # noqa: E402
from src.db import load_candles_df  # noqa: E402
from src.utils import resolve_path  # noqa: E402

# Display offset for candle boundaries (DB stores UTC ms from the exchange).
UTC_PLUS_7 = timezone(timedelta(hours=7))


def _ms_to_iso_in_tz(ms: int, tz: timezone) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=tz).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + " (UTC+7)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="How many most recent candles to print (default: 20)",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="Path to config YAML (default: CONFIG_PATH env or config.yaml)",
    )
    parser.add_argument(
        "--include-incomplete",
        action="store_true",
        help="Include rows with is_closed=0 (default: closed candles only)",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")

    config = load_config(args.config)
    database_path = resolve_path(config.database_path)
    df = load_candles_df(
        database_path,
        config.exchange,
        config.symbol,
        config.timeframe,
        only_closed=not args.include_incomplete,
        limit=args.limit,
    )

    print(
        f"exchange={config.exchange} symbol={config.symbol} timeframe={config.timeframe} "
        f"database={database_path}"
    )
    print(f"rows_printed={len(df)} (latest {args.limit} by open_time, oldest first in list)")
    if df.empty:
        print("No matching candles in the database.")
        return 0

    print("Candle open/close times below: wall clock in (UTC+7). Stored values in DB are UTC.")
    for _, row in df.iterrows():
        ot = int(row["open_time"])
        ct = int(row["close_time"])
        vol = row["volume"]
        vol_s = f"{float(vol):.6f}" if not pd.isna(vol) else "n/a"
        print(
            f"open={_ms_to_iso_in_tz(ot, UTC_PLUS_7)} close={_ms_to_iso_in_tz(ct, UTC_PLUS_7)} "
            f"O={row['open']:.2f} H={row['high']:.2f} L={row['low']:.2f} C={row['close']:.2f} "
            f"V={vol_s} is_closed={int(row['is_closed'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
