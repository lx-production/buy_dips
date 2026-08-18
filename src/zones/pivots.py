from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from .types import PivotKind, StructurePivot, SwingTerm


# Sort key used everywhere pivots must stay in confirmation order: index, then low before high.
def _structure_pivot_sort_key(pivot: StructurePivot) -> tuple[int, int]:
    return (pivot.index, 0 if pivot.kind == "low" else 1)


# Copy a pivot so prominent and raw lists can carry different structure roles.
def _copy_structure_pivot(pivot: StructurePivot) -> StructurePivot:
    return replace(pivot)


# Previous same-kind wick in `pivots`, or None when this is the first of that kind.
def _previous_same_kind_wick(pivots: list[StructurePivot], kind: PivotKind) -> float | None:
    for existing in reversed(pivots):
        if existing.kind == kind:
            return float(existing.wick_price)
    return None


# Assign H/HH/LH or L/HL/LL from the previous same-kind wick and return a new pivot.
def _structure_pivot_with_role(pivot: StructurePivot, previous_same_kind_wick: float | None) -> StructurePivot:
    if pivot.kind == "high":
        role = "H" if previous_same_kind_wick is None else ("HH" if pivot.wick_price > previous_same_kind_wick else "LH")
    else:
        role = "L" if previous_same_kind_wick is None else ("HL" if pivot.wick_price > previous_same_kind_wick else "LL")
    if pivot.structure_role == role:
        return pivot
    return replace(pivot, structure_role=role)


# Append a detached, labeled copy so later list labeling cannot mutate other lists.
def _append_labeled_structure_pivot(pivots: list[StructurePivot], pivot: StructurePivot) -> StructurePivot:
    labeled = _structure_pivot_with_role(_copy_structure_pivot(pivot), _previous_same_kind_wick(pivots, pivot.kind))
    pivots.append(labeled)
    return labeled


# Confirm high/low uniqueness at `index` using only the window [index-n, index+n].
def _structure_pivots_at_center(
    *,
    index: int,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    atr: np.ndarray,
    bars_each_side: int,
    term: SwingTerm,
) -> list[StructurePivot]:
    bars_each_side = max(1, int(bars_each_side))
    high_window = highs[index - bars_each_side : index + bars_each_side + 1]
    low_window = lows[index - bars_each_side : index + bars_each_side + 1]
    body_high = max(float(opens[index]), float(closes[index]))
    body_low = min(float(opens[index]), float(closes[index]))
    pivots: list[StructurePivot] = []

    # Unique highest high in the window becomes a high pivot at this center.
    if highs[index] == float(np.max(high_window)) and np.count_nonzero(high_window == highs[index]) == 1:
        pivots.append(
            StructurePivot(
                index=index,
                kind="high",
                wick_price=float(highs[index]),
                body_price=body_high,
                atr=float(atr[index]),
                term=term,
            )
        )

    # Unique lowest low in the window becomes a low pivot at this center.
    if lows[index] == float(np.min(low_window)) and np.count_nonzero(low_window == lows[index]) == 1:
        pivots.append(
            StructurePivot(
                index=index,
                kind="low",
                wick_price=float(lows[index]),
                body_price=body_low,
                atr=float(atr[index]),
                term=term,
            )
        )
    return sorted(pivots, key=_structure_pivot_sort_key)


# Scan a full OHLC frame for confirmed swing highs/lows with `bars_each_side` context.
def _find_structure_pivots(
    ohlc: pd.DataFrame,
    bars_each_side: int,
    atr: np.ndarray,
    term: SwingTerm,
) -> list[StructurePivot]:
    bars_each_side = max(1, int(bars_each_side))
    if len(ohlc) < (bars_each_side * 2 + 1):
        return []

    # convert the OHLC data to numpy arrays for faster computation
    highs = ohlc["high"].to_numpy(dtype=float)
    lows = ohlc["low"].to_numpy(dtype=float)
    opens = ohlc["open"].to_numpy(dtype=float)
    closes = ohlc["close"].to_numpy(dtype=float)

    pivots: list[StructurePivot] = []
    last_confirmable = len(ohlc) - bars_each_side
    for idx in range(bars_each_side, last_confirmable):
        pivots.extend(
            _structure_pivots_at_center(
                index=idx,
                opens=opens,
                highs=highs,
                lows=lows,
                closes=closes,
                atr=atr,
                bars_each_side=bars_each_side,
                term=term,
            )
        )
    return sorted(pivots, key=_structure_pivot_sort_key)


# Fold one chronological pivot into the prominent reducer. Same-kind swings replace the last
# kept pivot when more extreme; opposite-kind swings need a large enough move.
def _append_prominent_structure_pivot(
    prominent: list[StructurePivot],
    pivot: StructurePivot,
    min_swing_atr_mult: float,
    min_swing_pct: float,
) -> None:
    atr_mult = max(0.0, float(min_swing_atr_mult))
    pct = max(0.0, float(min_swing_pct))
    # Zero thresholds mean "keep every raw pivot", including same-kind neighbors.
    if atr_mult == 0.0 and pct == 0.0:
        _append_labeled_structure_pivot(prominent, pivot)
        return
    if not prominent:
        _append_labeled_structure_pivot(prominent, pivot)
        return

    previous = prominent[-1]
    if pivot.kind == previous.kind:
        if _is_more_extreme_structure_pivot(pivot, previous):
            previous_wick = _previous_same_kind_wick(prominent[:-1], pivot.kind)
            prominent[-1] = _structure_pivot_with_role(_copy_structure_pivot(pivot), previous_wick)
        return

    min_move = max(
        _structure_pivot_min_move(previous, atr_mult=atr_mult, pct=pct),
        _structure_pivot_min_move(pivot, atr_mult=atr_mult, pct=pct),
    )
    if abs(float(pivot.wick_price) - float(previous.wick_price)) >= min_move:
        _append_labeled_structure_pivot(prominent, pivot)


# Keep only swings that reverse far enough, replacing a same-kind last swing when more extreme.
def _filter_prominent_structure_pivots(
    pivots: list[StructurePivot],
    min_swing_atr_mult: float, # default 4x
    min_swing_pct: float, # default 2.5%
) -> list[StructurePivot]:
    atr_mult = max(0.0, float(min_swing_atr_mult))
    pct = max(0.0, float(min_swing_pct))
    if not pivots:
        return []
    if atr_mult == 0.0 and pct == 0.0:
        labeled: list[StructurePivot] = []
        for pivot in pivots:
            _append_labeled_structure_pivot(labeled, pivot)
        return labeled

    prominent: list[StructurePivot] = []
    for pivot in sorted(pivots, key=_structure_pivot_sort_key):
        _append_prominent_structure_pivot(
            prominent,
            pivot,
            min_swing_atr_mult=atr_mult,
            min_swing_pct=pct,
        )

    # Alternating highs and lows (starting with whichever pivot came first), with each reversal large enough to pass min_move.
    return prominent


def _is_more_extreme_structure_pivot(candidate: StructurePivot, current: StructurePivot) -> bool:
    if candidate.kind == "high":
        return candidate.wick_price > current.wick_price
    return candidate.wick_price < current.wick_price

# minimum move that qualifies as a real swing
def _structure_pivot_min_move(pivot: StructurePivot, atr_mult: float, pct: float) -> float:
    atr_move = abs(float(pivot.atr)) * atr_mult
    pct_move = abs(float(pivot.wick_price)) * pct / 100.0
    return max(atr_move, pct_move)


# Assign H/HH/LH or L/HL/LL in confirmation order. Replaces list items so frozen pivots stay immutable.
def _label_structure_pivots(pivots: list[StructurePivot]) -> None:
    previous_high: float | None = None
    previous_low: float | None = None
    for index, pivot in enumerate(pivots):
        if pivot.kind == "high":
            previous_wick = previous_high
            previous_high = pivot.wick_price
        else:
            previous_wick = previous_low
            previous_low = pivot.wick_price
        pivots[index] = _structure_pivot_with_role(pivot, previous_wick)
