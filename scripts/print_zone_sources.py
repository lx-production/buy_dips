from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from src.config import load_config  # noqa: E402
from src.db import load_candles_df  # noqa: E402
from src.utils import resolve_path  # noqa: E402
from src.zones import detect_support_resistance_zones  # noqa: E402


UTC_PLUS_7 = timezone(timedelta(hours=7))


def main() -> int:
    config = load_config(None)
    df = load_candles_df(
        resolve_path(config.database_path),
        config.exchange,
        config.symbol,
        config.timeframe,
        only_closed=True,
    )
    if df.empty:
        print("No closed candles found.")
        return 0

    zone_config = config.zones
    zones = detect_support_resistance_zones(
        df,
        zone_tolerance_pct=zone_config.zone_tolerance_pct,
        min_touches=zone_config.min_touches,
        current_price=float(df.iloc[-1]["close"]),
        buffer_pct=zone_config.role_buffer_pct,
        internal_swing_order=zone_config.internal_swing_order,
        external_swing_order=zone_config.external_swing_order,
        atr_period=zone_config.atr_period,
        break_atr_mult=zone_config.break_atr_mult,
        external_min_swing_atr_mult=zone_config.external_min_swing_atr_mult,
        external_min_swing_pct=zone_config.external_min_swing_pct,
    )

    for zone in zones["support"]:
        print(
            f"support {zone['low']:.2f}-{zone['high']:.2f} "
            f"touches={zone['touches']} origin={zone['origin']}"
        )
        for index in sorted((int(value) for value in zone["source_indexes"]), key=lambda idx: int(df.iloc[idx]["open_time"])):
            open_time = int(df.iloc[index]["open_time"])
            print(f"  i={index:4d} open_time_utc+7={_ms_to_utc7(open_time)} raw_ms={open_time}")
    return 0


def _ms_to_utc7(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(UTC_PLUS_7).strftime("%Y-%m-%d %H:%M:%S %Z")


if __name__ == "__main__":
    raise SystemExit(main())
