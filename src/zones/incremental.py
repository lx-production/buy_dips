from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from collections.abc import Mapping

from typing import Any

import numpy as np
import pandas as pd

from .timeframes import ONE_DAY_MS
from .candidates import _first_reclaim_index
from .ohlc import _append_average_true_range
from .daily import DAILY_ZONE_MIN_BARS_PER_DAY
from .types import STRUCTURE_LOCAL_REACTION_LOOKBACK_BARS, StructurePivot, SwingTerm, ZoneDetectorEvidence
from .pivots import _append_prominent_structure_pivot, _copy_structure_pivot, _label_structure_pivots, _structure_pivots_at_center


FOUR_HOURS_MS = 14_400_000
# Detector internals always use one bar on each side, not ZoneConfig.internal_swing_order.
INTERNAL_BARS_EACH_SIDE = 1


class IncrementalZoneDetectorError(ValueError):
    """Fail-closed error for invalid 4h input or a snapshot past/before the watermark."""


class IncrementalZoneDetectorState:
    """Ingest closed 4h candles one at a time and emit the same evidence as a full-prefix extract.

    Confirmed pivots, reclaim indexes, prominent swings, and completed UTC daily bars are
    updated as soon as the new bar supplies enough right-side context. Unconfirmed right-edge
    centers stay in the OHLC arrays until that later confirmation. Snapshot copies are labeled
    so callers cannot mutate this state.
    """

    # Capture detector knobs and start with empty 4h / daily evidence.
    def __init__(self, zone_config: Any) -> None:
        self._external_bars = max(1, int(zone_config.external_swing_order))
        self._atr_period = int(zone_config.atr_period)
        self._break_atr_mult = float(zone_config.break_atr_mult)
        self._min_swing_atr_mult = float(zone_config.external_min_swing_atr_mult)
        self._min_swing_pct = float(zone_config.external_min_swing_pct)

        self._open_times: list[int] = []
        self._opens: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._true_ranges: list[float] = []
        self._atr_values: list[float] = []

        self._raw_external_pivots: list[StructurePivot] = []
        self._internal_pivots: list[StructurePivot] = []
        self._prominent_external: list[StructurePivot] = []
        self._pending_highs: list[StructurePivot] = []
        self._first_reclaim_indexes: dict[tuple[SwingTerm, int], int] = {}
        self._recent_internal_pivots: deque[StructurePivot] = deque()

        self._daily_open_times: list[int] = []
        self._daily_opens: list[float] = []
        self._daily_highs: list[float] = []
        self._daily_lows: list[float] = []
        self._daily_closes: list[float] = []
        self._daily_true_ranges: list[float] = []
        self._daily_atr_values: list[float] = []
        self._daily_raw_pivots: list[StructurePivot] = []
        self._daily_prominent: list[StructurePivot] = []
        self._daily_bucket_day_open: int | None = None
        self._daily_bucket_opens: list[float] = []
        self._daily_bucket_highs: list[float] = []
        self._daily_bucket_lows: list[float] = []
        self._daily_bucket_closes: list[float] = []
        self._daily_bucket_complete: bool = False

    # Ingest one closed 4h candle, then confirm any pivot whose right side just filled.
    def advance(self, four_hour_candle: Any) -> None:
        candle = _closed_four_hour_candle(four_hour_candle)
        self._validate_next_open_time(candle.open_time)
        previous_close = self._closes[-1] if self._closes else None

        self._open_times.append(candle.open_time)
        self._opens.append(candle.open)
        self._highs.append(candle.high)
        self._lows.append(candle.low)
        self._closes.append(candle.close)
        _append_average_true_range(
            self._true_ranges,
            self._atr_values,
            high=candle.high,
            low=candle.low,
            previous_close=previous_close,
            period=self._atr_period,
        )

        # Internal center is the previous bar; external center sits `external_swing_order` back.
        self._confirm_new_4h_center(term="internal", bars_each_side=INTERNAL_BARS_EACH_SIDE)
        self._confirm_new_4h_center(term="external", bars_each_side=self._external_bars)
        self._resolve_pending_reclaims(candle.close)
        self._expire_local_internal_pivots()
        self._advance_daily_bucket(candle)

    # Freeze evidence at the current watermark. `zone_set_as_of` must be the last ingested open time.
    def snapshot_evidence(self, zone_set_as_of: int) -> ZoneDetectorEvidence | None:
        if not self._open_times:
            raise IncrementalZoneDetectorError("cannot snapshot evidence before ingesting a 4h candle")
        watermark = int(zone_set_as_of)
        if watermark != self._open_times[-1]:
            raise IncrementalZoneDetectorError("zone_set_as_of does not match the latest ingested 4h open_time")
        if watermark % FOUR_HOURS_MS != 0:
            raise IncrementalZoneDetectorError("zone_set_as_of is not aligned to a Binance UTC 4h bucket")
        if len(self._closes) < (self._external_bars * 2 + 1):
            return None

        prominent = [_copy_structure_pivot(pivot) for pivot in self._prominent_external]
        if not prominent:
            return None

        raw_external = [_copy_structure_pivot(pivot) for pivot in self._raw_external_pivots]
        internal = [_copy_structure_pivot(pivot) for pivot in self._internal_pivots]
        daily = [_copy_structure_pivot(pivot) for pivot in self._daily_prominent]
        _label_structure_pivots(raw_external)
        _label_structure_pivots(prominent)
        _label_structure_pivots(internal)
        _label_structure_pivots(daily)
        closes = np.asarray(self._closes, dtype=float)
        ohlc = pd.DataFrame(
            {
                "open": np.asarray(self._opens, dtype=float),
                "high": np.asarray(self._highs, dtype=float),
                "low": np.asarray(self._lows, dtype=float),
                "close": closes,
            }
        )
        return ZoneDetectorEvidence(
            ohlc=ohlc,
            closes=closes,
            current_price=float(closes[-1]),
            raw_external_pivots=raw_external,
            external_pivots=prominent,
            internal_pivots=internal,
            daily_pivots=daily,
            first_reclaim_indexes=dict(self._first_reclaim_indexes),
        )

    # Reject duplicates, backward time, and missing 4h buckets so state never skips or looks ahead.
    def _validate_next_open_time(self, open_time: int) -> None:
        if not self._open_times:
            return
        previous = self._open_times[-1]
        if open_time == previous:
            raise IncrementalZoneDetectorError("duplicate 4h open_time")
        if open_time < previous:
            raise IncrementalZoneDetectorError("out-of-order 4h open_time")
        if open_time != previous + FOUR_HOURS_MS:
            raise IncrementalZoneDetectorError("gap in 4h open_time sequence")

    # Confirm the single center that just received enough right-side bars, if any.
    def _confirm_new_4h_center(self, *, term: SwingTerm, bars_each_side: int) -> None:
        pivots = _pivots_at_new_center(
            opens=self._opens,
            highs=self._highs,
            lows=self._lows,
            closes=self._closes,
            atr_values=self._atr_values,
            bars_each_side=bars_each_side,
            term=term,
        )
        for pivot in pivots:
            self._record_confirmed_4h_pivot(pivot)

    # Store a newly confirmed 4h pivot, fold it into prominence, and freeze reclaim if it already happened.
    def _record_confirmed_4h_pivot(self, pivot: StructurePivot) -> None:
        if pivot.term == "external":
            self._raw_external_pivots.append(pivot)
            _append_prominent_structure_pivot(
                self._prominent_external,
                pivot,
                min_swing_atr_mult=self._min_swing_atr_mult,
                min_swing_pct=self._min_swing_pct,
            )
        else:
            self._internal_pivots.append(pivot)
            self._recent_internal_pivots.append(pivot)
        if pivot.kind != "high":
            return
        # Right-side bars already exist, so reclaim may have printed before confirmation.
        reclaim_index = _first_reclaim_index(
            pivot,
            np.asarray(self._closes, dtype=float),
            self._break_atr_mult,
        )
        if reclaim_index is None:
            self._pending_highs.append(pivot)
            return
        self._first_reclaim_indexes[(pivot.term, int(pivot.index))] = reclaim_index

    # A new close can reclaim older pending highs; the first hit is frozen and never moved.
    def _resolve_pending_reclaims(self, close: float) -> None:
        still_pending: list[StructurePivot] = []
        reclaim_index = len(self._closes) - 1
        for pivot in self._pending_highs:
            threshold = max(0.0, float(pivot.atr) * self._break_atr_mult)
            if close > float(pivot.wick_price) + threshold:
                self._first_reclaim_indexes[(pivot.term, int(pivot.index))] = reclaim_index
                continue
            still_pending.append(pivot)
        self._pending_highs = still_pending

    # Drop internal pivots that fell out of the 150-bar local-reaction window.
    def _expire_local_internal_pivots(self) -> None:
        recent_start = max(0, len(self._closes) - STRUCTURE_LOCAL_REACTION_LOOKBACK_BARS)
        while self._recent_internal_pivots and self._recent_internal_pivots[0].index < recent_start:
            self._recent_internal_pivots.popleft()

    # Accumulate the current UTC day; finalize daily OHLC only after six closed 4h bars.
    def _advance_daily_bucket(self, candle: _ClosedFourHourCandle) -> None:
        day_open = (candle.open_time // ONE_DAY_MS) * ONE_DAY_MS
        if self._daily_bucket_day_open is None:
            self._start_daily_bucket(day_open)
        elif day_open != self._daily_bucket_day_open:
            # Incomplete days never become daily bars. Continuous 4h input only does this at range start.
            self._start_daily_bucket(day_open)
        elif self._daily_bucket_complete:
            raise IncrementalZoneDetectorError("extra 4h candle in an already completed UTC day")

        self._daily_bucket_opens.append(candle.open)
        self._daily_bucket_highs.append(candle.high)
        self._daily_bucket_lows.append(candle.low)
        self._daily_bucket_closes.append(candle.close)
        if len(self._daily_bucket_opens) < DAILY_ZONE_MIN_BARS_PER_DAY:
            return
        self._finalize_daily_bucket()

    # Reset the in-progress UTC daily bucket to this day's 00:00 open.
    def _start_daily_bucket(self, day_open: int) -> None:
        self._daily_bucket_day_open = day_open
        self._daily_bucket_opens = []
        self._daily_bucket_highs = []
        self._daily_bucket_lows = []
        self._daily_bucket_closes = []
        self._daily_bucket_complete = False

    # Close the UTC daily candle and confirm the daily pivot center that just gained enough right days.
    def _finalize_daily_bucket(self) -> None:
        day_open = self._daily_bucket_day_open
        if day_open is None:
            raise IncrementalZoneDetectorError("cannot finalize a UTC daily bucket that was never started")
        previous_close = self._daily_closes[-1] if self._daily_closes else None
        self._daily_open_times.append(day_open)
        self._daily_opens.append(self._daily_bucket_opens[0])
        self._daily_highs.append(max(self._daily_bucket_highs))
        self._daily_lows.append(min(self._daily_bucket_lows))
        self._daily_closes.append(self._daily_bucket_closes[-1])
        _append_average_true_range(
            self._daily_true_ranges,
            self._daily_atr_values,
            high=self._daily_highs[-1],
            low=self._daily_lows[-1],
            previous_close=previous_close,
            period=self._atr_period,
        )
        self._daily_bucket_complete = True
        for pivot in _pivots_at_new_center(
            opens=self._daily_opens,
            highs=self._daily_highs,
            lows=self._daily_lows,
            closes=self._daily_closes,
            atr_values=self._daily_atr_values,
            bars_each_side=self._external_bars,
            term="external",
        ):
            self._daily_raw_pivots.append(pivot)
            _append_prominent_structure_pivot(
                self._daily_prominent,
                pivot,
                min_swing_atr_mult=self._min_swing_atr_mult,
                min_swing_pct=self._min_swing_pct,
            )


# Closed 4h OHLC already checked for alignment and is_closed=1.
@dataclass(frozen=True)
class _ClosedFourHourCandle:
    open_time: int
    open: float
    high: float
    low: float
    close: float


# Read one mapping/Series field or fail closed if the candle cannot supply it.
def _candle_field(candle: Any, key: str) -> Any:
    if isinstance(candle, Mapping) and key in candle:
        return candle[key]
    index = getattr(candle, "index", None)
    if index is not None and key in index:
        return candle[key]
    if hasattr(candle, key):
        return getattr(candle, key)
    raise IncrementalZoneDetectorError(f"4h candle is missing {key}")


# Parse a numeric candle field and reject NaN so ATR/pivots never see bad values.
def _candle_float(candle: Any, key: str) -> float:
    try:
        number = float(_candle_field(candle, key))
    except (TypeError, ValueError) as exc:
        raise IncrementalZoneDetectorError(f"4h candle {key} is not numeric") from exc
    if number != number:
        raise IncrementalZoneDetectorError(f"4h candle {key} is not numeric")
    return number


# Parse an integer candle field (open_time / is_closed) from numpy or Python scalars.
def _candle_int(candle: Any, key: str) -> int:
    value = _candle_field(candle, key)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise IncrementalZoneDetectorError(f"4h candle {key} is not an integer") from exc


# Validate alignment, closed status, and finite OHLC before the candle can enter state.
def _closed_four_hour_candle(candle: Any) -> _ClosedFourHourCandle:
    open_time = _candle_int(candle, "open_time")
    if open_time < 0 or open_time % FOUR_HOURS_MS != 0:
        raise IncrementalZoneDetectorError("4h open_time is not aligned to a Binance UTC 4h bucket")
    if _candle_int(candle, "is_closed") != 1:
        raise IncrementalZoneDetectorError("4h candle is not closed")
    return _ClosedFourHourCandle(
        open_time=open_time,
        open=_candle_float(candle, "open"),
        high=_candle_float(candle, "high"),
        low=_candle_float(candle, "low"),
        close=_candle_float(candle, "close"),
    )


# Return the 0-2 pivots at the center that just became confirmable, or [] if the series is still short.
def _pivots_at_new_center(
    *,
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    atr_values: list[float],
    bars_each_side: int,
    term: SwingTerm,
) -> list[StructurePivot]:
    bars_each_side = max(1, int(bars_each_side))
    if len(closes) < (bars_each_side * 2 + 1):
        return []
    center = len(closes) - 1 - bars_each_side
    return _structure_pivots_at_center(
        index=center,
        opens=np.asarray(opens, dtype=float),
        highs=np.asarray(highs, dtype=float),
        lows=np.asarray(lows, dtype=float),
        closes=np.asarray(closes, dtype=float),
        atr=np.asarray(atr_values, dtype=float),
        bars_each_side=bars_each_side,
        term=term,
    )
