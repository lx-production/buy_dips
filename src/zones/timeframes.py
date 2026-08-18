from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


ONE_DAY_MS = 86_400_000


# Integer open_time column for direct source-index lookup, or empty when the frame has none.
def ohlc_open_times(df: Any) -> np.ndarray:
    if df is None or getattr(df, "empty", True) or "open_time" not in getattr(df, "columns", []):
        return np.array([], dtype=np.int64)
    return np.asarray(df["open_time"].to_numpy(), dtype=np.int64)


def aggregate_ohlc_to_daily(df: Any, min_bars_per_day: int = 1) -> Any:
    if df is None or df.empty or "open_time" not in df.columns:
        return df

    required = {"open", "high", "low", "close"}
    if not required.issubset(df.columns):
        return pd.DataFrame()

    daily = df.sort_values("open_time").copy()
    daily["day_open_time"] = (daily["open_time"].astype("int64") // ONE_DAY_MS) * ONE_DAY_MS

    aggregations: dict[str, str] = {
        "open_time": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    optional_aggregations = {
        "exchange": "first",
        "symbol": "first",
        "close_time": "max",
        "volume": "sum",
        "is_closed": "min",
        "fetched_at": "max",
    }
    for column, aggregation in optional_aggregations.items():
        if column in daily.columns:
            aggregations[column] = aggregation

    grouped = daily.groupby("day_open_time", as_index=False, sort=True).agg(aggregations)
    grouped["bar_count"] = daily.groupby("day_open_time", sort=True).size().to_numpy(dtype=int)
    grouped = grouped[grouped["bar_count"] >= max(1, int(min_bars_per_day))].copy()
    if "is_closed" in grouped.columns:
        grouped = grouped[grouped["is_closed"].astype(int) == 1].copy()

    grouped["timeframe"] = "1d"
    grouped["open_time"] = grouped["day_open_time"].astype("int64")
    return grouped.drop(columns=["day_open_time", "bar_count"]).reset_index(drop=True)
