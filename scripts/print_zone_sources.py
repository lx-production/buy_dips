.venv/bin/python -c "
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.db import load_candles_df
from src.utils import resolve_path
from src.zones import detect_support_resistance_zones

UTC_PLUS_7 = timezone(timedelta(hours=7))

def ms_to_utc7(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(UTC_PLUS_7).strftime('%Y-%m-%d %H:%M:%S %Z')

c = load_config(None)
df = load_candles_df(resolve_path(c.database_path), c.exchange, c.symbol, c.timeframe, only_closed=True)
zc = c.zones
z = detect_support_resistance_zones(
    df,
    zone_tolerance_pct=zc.zone_tolerance_pct,
    min_touches=zc.min_touches,
    current_price=float(df.iloc[-1]['close']),
    buffer_pct=zc.role_buffer_pct,
    internal_swing_order=zc.internal_swing_order,
    external_swing_order=zc.external_swing_order,
    atr_period=zc.atr_period,
    break_atr_mult=zc.break_atr_mult,
)
for zone in z['support']:
    if zone['origin'] == 'flipped_resistance' and abs(zone['mid'] - 79033.29) < 1:
        idxs = [int(i) for i in zone['source_indexes']]
        idxs_sorted = sorted(idxs, key=lambda i: int(df.iloc[i]['open_time']))
        print('source_indexes (earliest -> newest):', idxs_sorted)
        for i in idxs_sorted:
            ms = int(df.iloc[i]['open_time'])
            print(f'  i={i:4d}  open_time_utc+7={ms_to_utc7(ms)}  (raw_ms={ms})')
"