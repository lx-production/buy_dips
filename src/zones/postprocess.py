from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from typing import Any

import numpy as np

from .state import _classify_price_state
from .factory import _zone_from_support_cluster
from .candidates import _reclaim_index_for_pivot
from .build import _cluster_support_candidates, _has_minimum_unique_touches
from .types import STRUCTURE_ADJACENT_ZONE_MIN_GAP, STRUCTURE_IMPORTANT_ZONE_SPACING, STRUCTURE_STAIR_STEP_MAX_INSERTIONS, STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP, StructurePivot, SupportCandidate, SwingTerm


# Insert reclaimed-high stairs into wide support gaps, using a price-sorted index per pivot set.
def _fill_support_staircase_gaps(
    zones: list[dict[str, Any]],
    raw_external_pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
    internal_pivots: list[StructurePivot] | None = None,
    first_reclaim_indexes: dict[tuple[SwingTerm, int], int] | None = None,
) -> list[dict[str, Any]]:
    filled_zones = [dict(zone) for zone in zones]
    pivot_sets = [raw_external_pivots]
    if internal_pivots is not None:
        pivot_sets.append(internal_pivots)
    priced_sets = [
        _index_reclaimed_high_candidates(pivots, closes, break_atr_mult, first_reclaim_indexes)
        for pivots in pivot_sets
    ]

    insertion_count = 0
    for priced in priced_sets:
        while insertion_count < STRUCTURE_STAIR_STEP_MAX_INSERTIONS:
            gap_fill = _best_support_staircase_gap_fill(
                zones=filled_zones,
                priced=priced,
                zone_width=zone_width,
                min_touches=min_touches,
                current_price=current_price,
                buffer_pct=buffer_pct,
            )
            if gap_fill is None:
                break
            filled_zones.append(gap_fill)
            filled_zones = _make_support_zones_distinct(filled_zones, current_price=current_price, buffer_pct=buffer_pct)
            insertion_count += 1
    return filled_zones


# Fill wide gaps on the final spaced ladder using per-gap reclaimed-high clusters.
def _fill_persistent_wick_floor_gaps(
    zones: list[dict[str, Any]],
    raw_external_pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
    internal_pivots: list[StructurePivot] | None = None,
    first_reclaim_indexes: dict[tuple[SwingTerm, int], int] | None = None,
) -> list[dict[str, Any]]:
    """Recover one evidence-backed stair inside each wide adjacent gap.

    This pass runs after persistent/daily overlays and the first spacing
    resolver, so the boundaries can be any surviving origins — not only two
    persistent floors. A gap is fillable when its edge distance can hold one
    `zone_width` band plus `$650` clearance on both sides. Candidates are
    clustered inside that gap only; the full structural factory is skipped
    because its `$2000` macro-merge can swallow a middle cluster into a band
    that then shares a slot with the lower neighbor. Historical shelves above
    the current price stay eligible.
    """
    boundary_zones = sorted((dict(zone) for zone in zones), key=lambda zone: float(zone["low"]))
    pivot_sets = [raw_external_pivots]
    if internal_pivots is not None:
        pivot_sets.append(internal_pivots)
    priced_sets = [
        _index_reclaimed_high_candidates(pivots, closes, break_atr_mult, first_reclaim_indexes)
        for pivots in pivot_sets
    ]
    min_fillable_gap = _min_fillable_support_gap(zone_width)

    gap_fills: list[dict[str, Any]] = []
    for lower_zone, upper_zone in zip(boundary_zones, boundary_zones[1:]):
        gap = float(upper_zone["low"]) - float(lower_zone["high"])
        if gap < min_fillable_gap:
            continue

        candidate_zones: list[dict[str, Any]] = []
        for priced in priced_sets:
            candidate_zones.extend(
                _cluster_reclaimed_high_gap_zones(
                    priced=priced,
                    zone_width=zone_width,
                    min_touches=min_touches,
                    lower_zone=lower_zone,
                    upper_zone=upper_zone,
                    current_price=current_price,
                    buffer_pct=buffer_pct,
                )
            )
        candidate_zones = [
            zone
            for zone in candidate_zones
            if not _support_zones_share_ladder_slot(zone, lower_zone)
            and not _support_zones_share_ladder_slot(zone, upper_zone)
        ]
        if candidate_zones:
            selected = min(candidate_zones, key=lambda zone: _stair_step_gap_rank(zone, lower_zone, upper_zone))
            gap_fills.append(selected)

    return _make_support_zones_distinct(
        boundary_zones + gap_fills,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )


# Smallest edge gap that can fit one zone_width band with $650 clear of both neighbors.
# 500 + 2 * 650 = 1800, so the inserted stair cannot share a ladder slot with either side.
def _min_fillable_support_gap(zone_width: float) -> float:
    return float(zone_width) + 2.0 * STRUCTURE_ADJACENT_ZONE_MIN_GAP


# Choose the best reclaimed-high zone across all regular support gaps.
def _best_support_staircase_gap_fill(
    zones: list[dict[str, Any]],
    priced: _PricedReclaimedHighs,
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
) -> dict[str, Any] | None:
    boundary_zones = sorted(zones, key=lambda zone: float(zone["low"]))
    best_zone: dict[str, Any] | None = None
    best_rank: tuple[float, float, int, float] | None = None

    for lower_zone, upper_zone in zip(boundary_zones, boundary_zones[1:]):
        if _coerce_price_state(lower_zone, current_price, buffer_pct) != "support":
            continue
        gap = float(upper_zone["low"]) - float(lower_zone["high"])
        if gap <= STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP:
            continue
        candidate_zones = _stair_step_candidate_zones(
            priced=priced,
            zone_width=zone_width,
            min_touches=min_touches,
            lower_zone=lower_zone,
            upper_zone=upper_zone,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        if not candidate_zones:
            continue
        selected = min(candidate_zones, key=lambda zone: _stair_step_gap_rank(zone, lower_zone, upper_zone))
        rank = _stair_step_gap_rank(selected, lower_zone, upper_zone)
        if best_rank is None or rank < best_rank:
            best_zone = selected
            best_rank = rank
    return best_zone


# Cluster reclaimed highs inside one gap without macro-merge or nearby collapse.
def _cluster_reclaimed_high_gap_zones(
    priced: _PricedReclaimedHighs,
    zone_width: float,
    min_touches: int,
    lower_zone: dict[str, Any],
    upper_zone: dict[str, Any],
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    candidates = _stair_step_support_candidates_in_gap(
        priced=priced,
        zone_width=zone_width,
        lower_zone=lower_zone,
        upper_zone=upper_zone,
        current_price=current_price,
        buffer_pct=buffer_pct,
        include_above_price=True,
    )
    # Cluster by $500 only. Skip macro-merge / nearby collapse so a middle
    # shelf cannot be absorbed into a denser cluster sitting on the lower zone.
    candidate_zones = [
        _zone_from_support_cluster(cluster, zone_width, current_price, buffer_pct)
        for cluster in _cluster_support_candidates(candidates, zone_width)
        if _has_minimum_unique_touches(cluster, min_touches)
    ]
    return [
        zone
        for zone in candidate_zones
        if float(zone["low"]) > float(lower_zone["high"]) and float(zone["high"]) < float(upper_zone["low"])
    ]


# Build all confirmed reclaimed-high zones that fit strictly inside one gap.
def _stair_step_candidate_zones(
    priced: _PricedReclaimedHighs,
    zone_width: float,
    min_touches: int,
    lower_zone: dict[str, Any],
    upper_zone: dict[str, Any],
    current_price: float,
    buffer_pct: float,
    include_above_price: bool = False,
) -> list[dict[str, Any]]:
    from .build import _build_support_zones

    candidates = _stair_step_support_candidates_in_gap(
        priced=priced,
        zone_width=zone_width,
        lower_zone=lower_zone,
        upper_zone=upper_zone,
        current_price=current_price,
        buffer_pct=buffer_pct,
        include_above_price=include_above_price,
    )
    candidate_zones = _build_support_zones(
        candidates,
        zone_width=zone_width,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )
    return [
        zone
        for zone in candidate_zones
        if float(zone["low"]) > float(lower_zone["high"]) and float(zone["high"]) < float(upper_zone["low"])
    ]


# Reclaimed-high stair candidates sorted by body price so each gap can bisect instead of scanning.
@dataclass(frozen=True)
class _PricedReclaimedHighs:
    candidates: list[SupportCandidate]
    prices: list[float]


# Build every confirmed reclaimed high once; gap filters happen later by price.
def _index_reclaimed_high_candidates(
    pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    first_reclaim_indexes: dict[tuple[SwingTerm, int], int] | None = None,
) -> _PricedReclaimedHighs:
    candidates: list[SupportCandidate] = []
    for pivot in pivots:
        if pivot.kind != "high":
            continue
        reclaim_index = _reclaim_index_for_pivot(pivot, closes, break_atr_mult, first_reclaim_indexes)
        if reclaim_index is None:
            continue
        candidates.append(
            SupportCandidate(
                price=float(pivot.body_price),
                index=int(pivot.index),
                origin="stair_step_flipped_resistance",
                structure_role=pivot.structure_role or "H",
                broken_index=reclaim_index,
            )
        )
    candidates.sort(key=lambda item: item.price)
    return _PricedReclaimedHighs(candidates=candidates, prices=[float(item.price) for item in candidates])


# Collect confirmed reclaimed highs whose body price can anchor a zone inside one gap.
def _stair_step_support_candidates_in_gap(
    priced: _PricedReclaimedHighs,
    zone_width: float,
    lower_zone: dict[str, Any],
    upper_zone: dict[str, Any],
    current_price: float,
    buffer_pct: float,
    include_above_price: bool = False,
) -> list[SupportCandidate]:
    lower_high = float(lower_zone["high"])
    upper_low = float(upper_zone["low"])
    # Same exclusive bounds as the old scan: price - zone_width > lower_high and price < upper_low.
    min_price = lower_high + float(zone_width)
    max_price = upper_low
    if not include_above_price:
        max_price = min(max_price, float(current_price) * (1.0 - float(buffer_pct)))
    start = bisect_right(priced.prices, min_price)
    end = bisect_left(priced.prices, max_price)
    return list(priced.candidates[start:end])


# Collect confirmed reclaimed highs that can anchor a zone inside one gap.
def _stair_step_support_candidates(
    pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    lower_zone: dict[str, Any],
    upper_zone: dict[str, Any],
    current_price: float,
    buffer_pct: float,
    include_above_price: bool = False,
    first_reclaim_indexes: dict[tuple[SwingTerm, int], int] | None = None,
) -> list[SupportCandidate]:
    return _stair_step_support_candidates_in_gap(
        priced=_index_reclaimed_high_candidates(pivots, closes, break_atr_mult, first_reclaim_indexes),
        zone_width=zone_width,
        lower_zone=lower_zone,
        upper_zone=upper_zone,
        current_price=current_price,
        buffer_pct=buffer_pct,
        include_above_price=include_above_price,
    )


def _make_support_zones_distinct(
    zones: list[dict[str, Any]],
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    distinct: list[dict[str, Any]] = []
    for zone in sorted(zones, key=lambda item: item["low"]):
        zone = dict(zone)
        zone["role"] = "support"
        zone["structure_bias"] = "support"
        zone["price_state"] = _classify_price_state(float(zone["low"]), float(zone["high"]), current_price, buffer_pct)
        if not distinct:
            distinct.append(zone)
            continue
        previous = distinct[-1]
        if _zones_overlap(previous, zone):
            distinct[-1] = _prefer_support_zone(previous, zone)
        else:
            distinct.append(zone)
    return distinct


# Keep one zone per nearby ladder slot after overlays finish.
def _enforce_support_zone_spacing(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve nearby-slot conflicts with one priority order.

    A $650 edge gap or $1000 midpoint counts as the same ladder step. Persistent
    floors win first (older floor wins among themselves). Daily, structural, and
    local bands then compete by score, touch count, and narrower width.
    """
    kept: list[dict[str, Any]] = []
    for zone in sorted(zones, key=_spaced_support_zone_rank, reverse=True):
        zone = dict(zone)
        if any(_support_zones_share_ladder_slot(zone, previous) for previous in kept):
            continue
        kept.append(zone)
    return sorted(kept, key=lambda item: float(item["low"]))


# True when two zones are close enough to count as the same support step.
def _support_zones_share_ladder_slot(first: dict[str, Any], second: dict[str, Any]) -> bool:
    lower, upper = (first, second) if float(first["low"]) <= float(second["low"]) else (second, first)
    gap = float(upper["low"]) - float(lower["high"])
    midpoint_gap = abs(float(first["mid"]) - float(second["mid"]))
    return gap < STRUCTURE_ADJACENT_ZONE_MIN_GAP or midpoint_gap < STRUCTURE_IMPORTANT_ZONE_SPACING


# Persistent floors first, then score, touches, and narrower width.
def _spaced_support_zone_rank(zone: dict[str, Any]) -> tuple[int, int, float, int, float]:
    persistent = 1 if str(zone.get("origin")) == "persistent_wick_floor" else 0
    persistent_age = -_persistent_source_index(zone) if persistent else 0
    return (
        persistent,
        persistent_age,
        float(zone.get("score", 0.0)),
        int(zone["touches"]),
        -float(zone["width_pct"]),
    )


def _prefer_support_zone(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    # Pinned wick floors outrank swing/daily bands so distinct() cannot drop them.
    first_persistent = str(first.get("origin")) == "persistent_wick_floor"
    second_persistent = str(second.get("origin")) == "persistent_wick_floor"
    if first_persistent != second_persistent:
        return dict(first if first_persistent else second)
    if first_persistent and second_persistent:
        return dict(first if _persistent_source_index(first) <= _persistent_source_index(second) else second)
    first_score = (float(first.get("score", 0.0)), int(first["touches"]), -float(first["width_pct"]))
    second_score = (float(second.get("score", 0.0)), int(second["touches"]), -float(second["width_pct"]))
    return dict(first if first_score >= second_score else second)


# Oldest forming candle wins when two persistent floors overlap.
def _persistent_source_index(zone: dict[str, Any]) -> int:
    indexes = [int(index) for index in zone.get("source_indexes") or []]
    return min(indexes) if indexes else 0


def _zones_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return max(float(first["low"]), float(second["low"])) <= min(float(first["high"]), float(second["high"]))


# Prefer the zone that splits the gap most evenly, then higher score/touches.
def _stair_step_gap_rank(
    zone: dict[str, Any],
    lower_zone: dict[str, Any],
    upper_zone: dict[str, Any],
) -> tuple[float, float, int, float]:
    lower_gap = float(zone["low"]) - float(lower_zone["high"])
    upper_gap = float(upper_zone["low"]) - float(zone["high"])
    midpoint = (float(lower_zone["high"]) + float(upper_zone["low"])) / 2.0
    return (
        abs(lower_gap - upper_gap),
        -float(zone.get("score", 0.0)),
        -int(zone["touches"]),
        abs(float(zone["mid"]) - midpoint),
    )


def _coerce_price_state(zone: dict[str, Any], current_price: float, buffer_pct: float) -> str:
    value = zone.get("price_state")
    if value in ("support", "active", "resistance"):
        return str(value)
    return _classify_price_state(float(zone["low"]), float(zone["high"]), current_price, buffer_pct)
