from __future__ import annotations

import numpy as np
import pandas as pd


def _coerce_ohlc(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None:
        return None
    required = ["open", "high", "low", "close"]
    if any(column not in df.columns for column in required):
        return None
    # Select only OHLC columns, ensure numeric dtype, drop rows with any NaN (bad values), and reset the index.
    ohlc = df[required].apply(pd.to_numeric, errors="coerce").dropna().reset_index(drop=True)
    if ohlc.empty:
        return None
    return ohlc

# period is defaulted to 14
def _average_true_range(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    if len(closes) == 0:
        return np.array([], dtype=float)
    true_ranges = np.empty(len(closes), dtype=float)
    true_ranges[0] = highs[0] - lows[0]
    for idx in range(1, len(closes)):
        # the usual standard Wilder True Range formula, it isn't BTC-4H-only.
        true_ranges[idx] = max(
            highs[idx] - lows[idx],
            abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        )
    return pd.Series(true_ranges).rolling(window=max(1, int(period)), min_periods=1).mean().to_numpy(dtype=float)
