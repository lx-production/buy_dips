from __future__ import annotations

import numpy as np
import pandas as pd

from .types import StructurePivot, SwingTerm


def _find_structure_pivots(
    ohlc: pd.DataFrame,
    swing_order: int,
    atr: np.ndarray,
    term: SwingTerm,
) -> list[StructurePivot]:
    order = max(1, int(swing_order))
    if len(ohlc) < (order * 2 + 1):
        return []

    highs = ohlc["high"].to_numpy(dtype=float)
    lows = ohlc["low"].to_numpy(dtype=float)
    opens = ohlc["open"].to_numpy(dtype=float)
    closes = ohlc["close"].to_numpy(dtype=float)
    pivots: list[StructurePivot] = []
    for idx in range(order, len(ohlc) - order):
        high_window = highs[idx - order : idx + order + 1]
        low_window = lows[idx - order : idx + order + 1]
        body_high = max(float(opens[idx]), float(closes[idx]))
        body_low = min(float(opens[idx]), float(closes[idx]))
        if highs[idx] == float(np.max(high_window)) and np.count_nonzero(high_window == highs[idx]) == 1:
            pivots.append(
                StructurePivot(
                    index=idx,
                    kind="high",
                    price=float(highs[idx]),
                    body_price=body_high,
                    atr=float(atr[idx]),
                    term=term,
                )
            )
        if lows[idx] == float(np.min(low_window)) and np.count_nonzero(low_window == lows[idx]) == 1:
            pivots.append(
                StructurePivot(
                    index=idx,
                    kind="low",
                    price=float(lows[idx]),
                    body_price=body_low,
                    atr=float(atr[idx]),
                    term=term,
                )
            )
    return sorted(pivots, key=lambda pivot: (pivot.index, 0 if pivot.kind == "low" else 1))


def _filter_prominent_structure_pivots(
    pivots: list[StructurePivot],
    min_swing_atr_mult: float,
    min_swing_pct: float,
) -> list[StructurePivot]:
    atr_mult = max(0.0, float(min_swing_atr_mult))
    pct = max(0.0, float(min_swing_pct))
    if not pivots or (atr_mult == 0.0 and pct == 0.0):
        return list(pivots)

    prominent: list[StructurePivot] = []
    for pivot in sorted(pivots, key=lambda item: (item.index, 0 if item.kind == "low" else 1)):
        if not prominent:
            prominent.append(pivot)
            continue

        previous = prominent[-1]
        if pivot.kind == previous.kind:
            if _is_more_extreme_structure_pivot(pivot, previous):
                prominent[-1] = pivot
            continue

        min_move = max(
            _structure_pivot_min_move(previous, atr_mult=atr_mult, pct=pct),
            _structure_pivot_min_move(pivot, atr_mult=atr_mult, pct=pct),
        )
        if abs(float(pivot.price) - float(previous.price)) >= min_move:
            prominent.append(pivot)

    return prominent


def _is_more_extreme_structure_pivot(candidate: StructurePivot, current: StructurePivot) -> bool:
    if candidate.kind == "high":
        return candidate.price > current.price
    return candidate.price < current.price


def _structure_pivot_min_move(pivot: StructurePivot, atr_mult: float, pct: float) -> float:
    atr_move = abs(float(pivot.atr)) * atr_mult
    pct_move = abs(float(pivot.price)) * pct / 100.0
    return max(atr_move, pct_move)


def _label_structure_pivots(pivots: list[StructurePivot]) -> None:
    previous_high: float | None = None
    previous_low: float | None = None
    for pivot in pivots:
        if pivot.kind == "high":
            if previous_high is None:
                pivot.structure_role = "H"
            else:
                pivot.structure_role = "HH" if pivot.price > previous_high else "LH"
            previous_high = pivot.price
        else:
            if previous_low is None:
                pivot.structure_role = "L"
            else:
                pivot.structure_role = "HL" if pivot.price > previous_low else "LL"
            previous_low = pivot.price
