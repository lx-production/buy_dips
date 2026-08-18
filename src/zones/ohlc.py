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

# Wilder true range for one bar. The first bar has no previous close, so it is just high-low.
def _true_range(high: float, low: float, previous_close: float | None) -> float:
    if previous_close is None:
        return float(high) - float(low)
    return max(
        float(high) - float(low),
        abs(float(high) - float(previous_close)),
        abs(float(low) - float(previous_close)),
    )


# Rolling ATR mean of the last `period` true ranges, same as min_periods=1 pandas rolling.
def _rolling_atr(true_ranges: list[float], period: int) -> float:
    window = max(1, int(period))
    return float(np.mean(true_ranges[-window:]))


# Append one bar's true range and ATR so incremental state matches full-prefix `_average_true_range`.
def _append_average_true_range(
    true_ranges: list[float],
    atr_values: list[float],
    *,
    high: float,
    low: float,
    previous_close: float | None,
    period: int,
) -> None:
    true_ranges.append(_true_range(high, low, previous_close))
    atr_values.append(_rolling_atr(true_ranges, period))


# Full-prefix ATR via the same rolling true-range mean incremental state appends one bar at a time.
def _average_true_range(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    if len(closes) == 0:
        return np.array([], dtype=float)
    true_ranges: list[float] = []
    atr_values: list[float] = []
    previous_close: float | None = None
    for idx in range(len(closes)):
        _append_average_true_range(
            true_ranges,
            atr_values,
            high=float(highs[idx]),
            low=float(lows[idx]),
            previous_close=previous_close,
            period=period,
        )
        previous_close = float(closes[idx])
    return np.asarray(atr_values, dtype=float)
