from __future__ import annotations

from typing import Any

import pandas as pd

from .constants import FOUR_HOURS_MS, ONE_HOUR_MS


class FourHourAggregationError(RuntimeError):
    pass


class OverdueIncompleteFourHourError(FourHourAggregationError):
    pass


def latest_overdue_bucket_open_time(now_ms: int) -> int:
    bucket = (int(now_ms) // FOUR_HOURS_MS) * FOUR_HOURS_MS - FOUR_HOURS_MS
    if bucket < 0:
        raise FourHourAggregationError("No completed 4h bucket exists before now_ms")
    return bucket


def aggregate_four_hour_bucket(
    hourly_df: pd.DataFrame,
    bucket_open_time: int,
    *,
    now_ms: int,
) -> dict[str, Any] | None:
    """Aggregate one Binance UTC 4h bucket, failing once an incomplete bucket is due."""
    bucket_open_time = int(bucket_open_time)
    if bucket_open_time < 0 or bucket_open_time % FOUR_HOURS_MS != 0:
        raise FourHourAggregationError("4h bucket open_time is not Binance UTC aligned")
    bucket_end = bucket_open_time + FOUR_HOURS_MS
    if int(now_ms) < bucket_end:
        return None

    required = {"open_time", "open", "high", "low", "close", "is_closed"}
    if hourly_df is None or not required.issubset(hourly_df.columns):
        raise OverdueIncompleteFourHourError(f"Overdue 4h bucket {bucket_open_time} has no complete 1h input")

    expected = [bucket_open_time + offset * ONE_HOUR_MS for offset in range(4)]
    rows = hourly_df[
        hourly_df["open_time"].astype("int64").isin(expected)
        & (hourly_df["is_closed"].astype(int) == 1)
    ].copy()
    rows = rows.sort_values("open_time").drop_duplicates("open_time", keep="last")
    actual = [int(value) for value in rows["open_time"].tolist()]
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        raise OverdueIncompleteFourHourError(
            f"Overdue 4h bucket {bucket_open_time} is incomplete; missing 1h open_times: {missing}"
        )

    for column in ("open", "high", "low", "close"):
        if pd.to_numeric(rows[column], errors="coerce").isna().any():
            raise OverdueIncompleteFourHourError(
                f"Overdue 4h bucket {bucket_open_time} contains invalid {column} values"
            )

    volume = None
    if "volume" in rows.columns and not rows["volume"].isna().all():
        numeric_volume = pd.to_numeric(rows["volume"], errors="coerce")
        if numeric_volume.isna().any():
            raise OverdueIncompleteFourHourError(
                f"Overdue 4h bucket {bucket_open_time} contains invalid volume values"
            )
        volume = float(numeric_volume.sum())
    return {
        "open_time": bucket_open_time,
        "close_time": bucket_end - 1,
        "open": float(rows.iloc[0]["open"]),
        "high": float(rows["high"].max()),
        "low": float(rows["low"].min()),
        "close": float(rows.iloc[-1]["close"]),
        "volume": volume,
        "is_closed": 1,
    }


def aggregate_overdue_buckets(
    hourly_df: pd.DataFrame,
    *,
    now_ms: int,
    after_open_time: int | None = None,
) -> list[dict[str, Any]]:
    latest = latest_overdue_bucket_open_time(now_ms)
    first = latest if after_open_time is None else int(after_open_time) + FOUR_HOURS_MS
    if first > latest:
        return []
    bars = []
    for bucket_open_time in range(first, latest + 1, FOUR_HOURS_MS):
        bar = aggregate_four_hour_bucket(hourly_df, bucket_open_time, now_ms=now_ms)
        if bar is not None:
            bars.append(bar)
    return bars
