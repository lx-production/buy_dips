from __future__ import annotations

import numpy as np

from .types import STRUCTURE_SUPPORT_FLOOR_RETEST_WIDTH_MULT, StructurePivot, SupportCandidate

# builds candidates from three sources, then sorts them by price
def _support_candidates(
    raw_external_pivots: list[StructurePivot], # all pivots, including noisy ones
    external_pivots: list[StructurePivot], # big, important swings
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
) -> list[SupportCandidate]:
    candidates: list[SupportCandidate] = []
    for pivot in external_pivots:
        if pivot.kind == "low":
            candidates.append(_candidate_from_pivot(pivot, origin="structure_swing_low"))
        elif _high_is_confirmed_reclaimed(pivot, closes, break_atr_mult):
            candidates.append(
                _candidate_from_pivot(
                    pivot,
                    origin="flipped_resistance",
                    broken_index=_first_reclaim_index(pivot, closes, break_atr_mult),
                )
            )

    candidates.extend(_support_floor_candidates(raw_external_pivots, external_pivots, zone_width))
    return sorted(candidates, key=lambda item: (item.price, item.index, item.origin))

# body-style candidates. Default bounds_style is "body"
# shared by both paths — lows and reclaimed highs
def _candidate_from_pivot(
    pivot: StructurePivot,
    origin: str,
    broken_index: int | None = None,
) -> SupportCandidate:
    return SupportCandidate(
        price=float(pivot.body_price), # zones are drawn from bodies by default
        index=int(pivot.index),
        origin=origin,
        # sets metadata on the candidate
        structure_role=pivot.structure_role or ("H" if pivot.kind == "high" else "L"),
        broken_index=broken_index,
    )

# Handles a special case: a prominent low where the wick is far below the body
# wick-floor support candidates
def _support_floor_candidates(
    raw_external_pivots: list[StructurePivot],
    external_pivots: list[StructurePivot],
    zone_width: float,
) -> list[SupportCandidate]:
    retest_tolerance = float(zone_width) * STRUCTURE_SUPPORT_FLOOR_RETEST_WIDTH_MULT
    prominent_lows = [
        pivot
        for pivot in external_pivots
        if pivot.kind == "low" and float(pivot.body_price) - float(pivot.price) >= float(zone_width)
    ]
    raw_lows = [pivot for pivot in raw_external_pivots if pivot.kind == "low"]

    candidates: list[SupportCandidate] = []
    for prominent_low in prominent_lows:
        floor_price = float(prominent_low.price) # wick low
        candidates.append(_support_floor_candidate(prominent_low, floor_price, "structure_swing_low_wick"))
        # For that floor_price, scan all low pivots and ask: “Did any other low’s body come back near this wick floor?”
        for raw_low in raw_lows:
            if raw_low.index == prominent_low.index:
                continue # skip the same candle — already handled as the wick
            body_floor = float(raw_low.body_price)
            # other raw lows whose body is within retest tolerance of that wick floor
            if abs(body_floor - floor_price) <= retest_tolerance:
                candidates.append(_support_floor_candidate(raw_low, body_floor, "structure_swing_low_body_floor"))
    return candidates


def _support_floor_candidate(pivot: StructurePivot, price: float, origin: str) -> SupportCandidate:
    return SupportCandidate(
        price=float(price),
        index=int(pivot.index),
        origin=origin,
        structure_role=pivot.structure_role or "L",
        bounds_style="support_floor",
    )

# did _first_reclaim_index find anything (not None)?
def _high_is_confirmed_reclaimed(
    pivot: StructurePivot,
    closes: np.ndarray,
    break_atr_mult: float,
) -> bool:
    return _first_reclaim_index(pivot, closes, break_atr_mult) is not None

# find the first close above the pivot high plus an ATR-based threshold
def _first_reclaim_index(
    pivot: StructurePivot,
    closes: np.ndarray,
    break_atr_mult: float,
) -> int | None:
    threshold = max(0.0, float(pivot.atr) * float(break_atr_mult))
    future_closes = closes[pivot.index + 1 :] # all closes after the pivot bar
    # relative indexes of future closes that are above the threshold
    offsets = np.flatnonzero(future_closes > float(pivot.price) + threshold) # pivot.price is the wick price
    if not len(offsets):
        return None
    return int(pivot.index + 1 + offsets[0]) # convert it back to an absolute index into the original closes array
