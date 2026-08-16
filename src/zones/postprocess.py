from __future__ import annotations

from typing import Any

import numpy as np

from .candidates import _first_reclaim_index, _high_is_confirmed_reclaimed
from .state import _classify_price_state
from .types import STRUCTURE_ADJACENT_ZONE_MIN_GAP, STRUCTURE_IMPORTANT_ZONE_SPACING, STRUCTURE_STAIR_STEP_MAX_INSERTIONS, STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP, StructurePivot, SupportCandidate


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
) -> list[dict[str, Any]]:
    filled_zones = [dict(zone) for zone in zones]
    pivot_sets = [raw_external_pivots]
    if internal_pivots is not None:
        pivot_sets.append(internal_pivots)

    insertion_count = 0
    for staircase_pivots in pivot_sets:
        while insertion_count < STRUCTURE_STAIR_STEP_MAX_INSERTIONS:
            gap_fill = _best_support_staircase_gap_fill(
                zones=filled_zones,
                staircase_pivots=staircase_pivots,
                closes=closes,
                break_atr_mult=break_atr_mult,
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


# Fill wide gaps left between adjacent persistent floors after their final spacing pass.
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
) -> list[dict[str, Any]]:
    """Recover one evidence-backed stair step inside each wide persistent gap.

    Persistent floors are overlaid after the normal staircase pass and can
    displace a nearby swing band. This pass runs on the resulting ladder and
    uses midpoint spacing so two fixed-width floors more than the configured
    maximum apart receive a reclaimed-high zone between them. These historical
    shelves remain useful when they are above the current price, so candidates
    are not limited by current price here.
    """
    boundary_zones = sorted((dict(zone) for zone in zones), key=lambda zone: float(zone["low"]))
    pivot_sets = [raw_external_pivots]
    if internal_pivots is not None:
        pivot_sets.append(internal_pivots)

    gap_fills: list[dict[str, Any]] = []
    for lower_zone, upper_zone in zip(boundary_zones, boundary_zones[1:]):
        if not _is_persistent_wick_floor(lower_zone) or not _is_persistent_wick_floor(upper_zone):
            continue
        midpoint_gap = float(upper_zone["mid"]) - float(lower_zone["mid"])
        if midpoint_gap <= STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP:
            continue

        candidate_zones: list[dict[str, Any]] = []
        for staircase_pivots in pivot_sets:
            candidate_zones.extend(
                _stair_step_candidate_zones(
                    staircase_pivots=staircase_pivots,
                    closes=closes,
                    break_atr_mult=break_atr_mult,
                    zone_width=zone_width,
                    min_touches=min_touches,
                    lower_zone=lower_zone,
                    upper_zone=upper_zone,
                    current_price=current_price,
                    buffer_pct=buffer_pct,
                    include_above_price=True,
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


# Choose the best reclaimed-high zone across all regular support gaps.
def _best_support_staircase_gap_fill(
    zones: list[dict[str, Any]],
    staircase_pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
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
            staircase_pivots=staircase_pivots,
            closes=closes,
            break_atr_mult=break_atr_mult,
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


# Build all confirmed reclaimed-high zones that fit strictly inside one gap.
def _stair_step_candidate_zones(
    staircase_pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    min_touches: int,
    lower_zone: dict[str, Any],
    upper_zone: dict[str, Any],
    current_price: float,
    buffer_pct: float,
    include_above_price: bool = False,
) -> list[dict[str, Any]]:
    from .build import _build_support_zones

    candidates = _stair_step_support_candidates(
        pivots=staircase_pivots,
        closes=closes,
        break_atr_mult=break_atr_mult,
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
) -> list[SupportCandidate]:
    lower_high = float(lower_zone["high"])
    upper_low = float(upper_zone["low"])
    support_ceiling = None if include_above_price else float(current_price) * (1.0 - float(buffer_pct))
    candidates: list[SupportCandidate] = []
    for pivot in pivots:
        if pivot.kind != "high":
            continue
        price = float(pivot.body_price)
        if price - float(zone_width) <= lower_high or price >= upper_low:
            continue
        if support_ceiling is not None and price >= support_ceiling:
            continue
        if not _high_is_confirmed_reclaimed(pivot, closes, break_atr_mult):
            continue
        candidates.append(
            SupportCandidate(
                price=price,
                index=int(pivot.index),
                origin="stair_step_flipped_resistance",
                structure_role=pivot.structure_role or "H",
                broken_index=_first_reclaim_index(pivot, closes, break_atr_mult),
            )
        )
    return candidates


# True when a zone is a pinned long-wick support floor.
def _is_persistent_wick_floor(zone: dict[str, Any]) -> bool:
    return str(zone.get("origin")) == "persistent_wick_floor"


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


def _stair_step_gap_rank(
    zone: dict[str, Any],
    lower_zone: dict[str, Any],
    upper_zone: dict[str, Any],
) -> tuple[float, float, int, float]:
    lower_gap = float(zone["low"]) - float(lower_zone["high"])
    upper_gap = float(upper_zone["low"]) - float(zone["high"])
    midpoint = (float(lower_zone["high"]) + float(upper_zone["low"])) / 2.0
    return (
        max(lower_gap, upper_gap),
        -float(zone.get("score", 0.0)),
        -int(zone["touches"]),
        abs(float(zone["mid"]) - midpoint),
    )


def _coerce_price_state(zone: dict[str, Any], current_price: float, buffer_pct: float) -> str:
    value = zone.get("price_state")
    if value in ("support", "active", "resistance"):
        return str(value)
    return _classify_price_state(float(zone["low"]), float(zone["high"]), current_price, buffer_pct)
