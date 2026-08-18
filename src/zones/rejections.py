from __future__ import annotations

from bisect import bisect_right

import pandas as pd

from .factory import Zone, _make_support_zone
from .types import (
    STRUCTURE_SPLIT_REJECTION_MAX_RETEST_BARS,
    STRUCTURE_SPLIT_REJECTION_MIN_RETEST_WIDTH_MULT,
    STRUCTURE_SPLIT_REJECTION_MIN_WICK_WIDTH_MULT,
    StructurePivot,
)


# Split a deep rejection into its wick-retest floor and body-rejection shelf.
def _build_split_rejection_zone_pairs(
    ohlc: pd.DataFrame,
    external_pivots: list[StructurePivot],
    internal_pivots: list[StructurePivot],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> list[tuple[Zone, Zone]]:
    pairs: list[tuple[Zone, Zone]] = []
    min_wick_span = float(zone_width) * STRUCTURE_SPLIT_REJECTION_MIN_WICK_WIDTH_MULT
    min_retest_span = float(zone_width) * STRUCTURE_SPLIT_REJECTION_MIN_RETEST_WIDTH_MULT
    # Retest is only valid in the next 4 bars, so index internal lows once and bisect.
    internal_lows = sorted(
        (pivot for pivot in internal_pivots if pivot.kind == "low"),
        key=lambda item: int(item.index),
    )
    internal_low_indexes = [int(pivot.index) for pivot in internal_lows]

    for pivot in external_pivots:
        if pivot.kind != "low" or not 0 <= int(pivot.index) < len(ohlc):
            continue

        candle = ohlc.iloc[int(pivot.index)]
        body_high = max(float(candle["open"]), float(candle["close"]))
        if body_high - float(pivot.wick_price) < min_wick_span:
            continue

        retest = _first_split_rejection_retest(
            pivot=pivot,
            internal_lows=internal_lows,
            internal_low_indexes=internal_low_indexes,
            body_high=body_high,
            min_retest_span=min_retest_span,
            zone_width=zone_width,
        )
        if retest is None:
            continue

        roles = {str(value) for value in (pivot.structure_role, retest.structure_role) if value}
        structure_role = next(iter(roles)) if len(roles) == 1 else "mixed"
        lower_zone = _make_support_zone(
            origin="wick_retest_support",
            bounds_style="local_reaction",
            low=float(pivot.wick_price),
            high=float(retest.wick_price),
            width=float(retest.wick_price) - float(pivot.wick_price),
            touches=2,
            source_closes=[float(pivot.wick_price), float(retest.wick_price)],
            source_indexes=[int(pivot.index), int(retest.index)],
            score=4.0,
            structure_role=structure_role,
            broken_index=None,
            zone_width=zone_width,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        upper_zone = _make_support_zone(
            origin="body_rejection_support",
            bounds_style="support_floor",
            low=body_high,
            high=body_high + float(zone_width),
            width=float(zone_width),
            touches=2,
            source_closes=[body_high, float(retest.body_price)],
            source_indexes=[int(pivot.index), int(retest.index)],
            score=4.0,
            structure_role=structure_role,
            broken_index=None,
            zone_width=zone_width,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        pairs.append((lower_zone, upper_zone))

    return pairs


# Find the first quick higher-low whose wick retests the rejection floor while its body holds above.
def _first_split_rejection_retest(
    *,
    pivot: StructurePivot,
    internal_lows: list[StructurePivot],
    internal_low_indexes: list[int],
    body_high: float,
    min_retest_span: float,
    zone_width: float,
) -> StructurePivot | None:
    last_retest_index = int(pivot.index) + STRUCTURE_SPLIT_REJECTION_MAX_RETEST_BARS
    start = bisect_right(internal_low_indexes, int(pivot.index))
    end = bisect_right(internal_low_indexes, last_retest_index)
    for candidate in internal_lows[start:end]:
        wick_gap = float(candidate.wick_price) - float(pivot.wick_price)
        if min_retest_span <= wick_gap <= float(zone_width) and float(candidate.body_price) >= body_high:
            return candidate
    return None


# Replace an ambiguous mixed band between the two rejection shelves with the evidence-backed pair.
def _overlay_split_rejection_zones(
    zones: list[Zone],
    rejection_pairs: list[tuple[Zone, Zone]],
) -> list[Zone]:
    selected = [dict(zone) for zone in zones]
    for lower_zone, upper_zone in rejection_pairs:
        envelope_low = float(lower_zone["low"])
        envelope_high = float(upper_zone["high"])
        replaceable = [
            index
            for index, zone in enumerate(selected)
            if str(zone.get("origin")) == "mixed_structure"
            and float(zone["low"]) >= envelope_low
            and float(zone["high"]) <= envelope_high
        ]
        if not replaceable:
            continue
        selected = [zone for index, zone in enumerate(selected) if index not in replaceable]
        selected.extend((dict(lower_zone), dict(upper_zone)))
    return sorted(selected, key=lambda zone: float(zone["low"]))
