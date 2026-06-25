from __future__ import annotations

import numpy as np
import pandas as pd

from .types import StructurePivot, SwingTerm


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

    for idx in range(bars_each_side, len(ohlc) - bars_each_side):
        high_window = highs[idx - bars_each_side : idx + bars_each_side + 1] # highs of candles on either side of the current candle
        low_window = lows[idx - bars_each_side : idx + bars_each_side + 1] # lows of candles on either side of the current candle
        body_high = max(float(opens[idx]), float(closes[idx]))
        body_low = min(float(opens[idx]), float(closes[idx]))

        # check if the current high is the highest high in the window and if it is unique
        if highs[idx] == float(np.max(high_window)) and np.count_nonzero(high_window == highs[idx]) == 1:
            pivots.append(
                StructurePivot(
                    index=idx,
                    kind="high",
                    wick_price=float(highs[idx]),
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
                    wick_price=float(lows[idx]),
                    body_price=body_low,
                    atr=float(atr[idx]),
                    term=term,
                )
            )
    return sorted(pivots, key=lambda pivot: (pivot.index, 0 if pivot.kind == "low" else 1))


def _filter_prominent_structure_pivots(
    pivots: list[StructurePivot],
    min_swing_atr_mult: float, # default 4x
    min_swing_pct: float, # default 2.5%
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

        # Same kind as the last kept pivot -> merge, keep the more extreme one
        if pivot.kind == previous.kind:
            if _is_more_extreme_structure_pivot(pivot, previous):
                prominent[-1] = pivot
            continue

        min_move = max(
            _structure_pivot_min_move(previous, atr_mult=atr_mult, pct=pct),
            _structure_pivot_min_move(pivot, atr_mult=atr_mult, pct=pct),
        )

        # Opposite kind (high after low, or low after high) → only keep if the move is big enough
        if abs(float(pivot.wick_price) - float(previous.wick_price)) >= min_move:
            prominent.append(pivot)
    
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


def _label_structure_pivots(pivots: list[StructurePivot]) -> None:
    previous_high: float | None = None
    previous_low: float | None = None
    for pivot in pivots:
        if pivot.kind == "high":
            if previous_high is None:
                pivot.structure_role = "H"
            else:
                pivot.structure_role = "HH" if pivot.wick_price > previous_high else "LH"
            previous_high = pivot.wick_price
        else:
            if previous_low is None:
                pivot.structure_role = "L"
            else:
                pivot.structure_role = "HL" if pivot.wick_price > previous_low else "LL"
            previous_low = pivot.wick_price
