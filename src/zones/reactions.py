from __future__ import annotations

import numpy as np

from .build import _cluster_support_candidates, _has_minimum_unique_touches
from .candidates import _first_reclaim_index
from .factory import Zone, _latest_defined, _make_support_zone
from .types import (
    STRUCTURE_LOCAL_REACTION_LOOKBACK_BARS,
    StructurePivot,
    SupportCandidate,
)


# Build variable-width support zones from recent internal pivots (swing lows and reclaimed highs).
def _build_local_reaction_zones(
    internal_pivots: list[StructurePivot], # SwingTerm = "internal" (bars_each_side=1)
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
) -> list[Zone]:
    recent_start = max(0, len(closes) - STRUCTURE_LOCAL_REACTION_LOOKBACK_BARS)
    local_min_touches = max(2, int(min_touches))
    low_pivots_by_index: dict[int, StructurePivot] = {}
    candidates: list[SupportCandidate] = []

    for pivot in internal_pivots:
        if pivot.index < recent_start:
            continue
        if pivot.kind == "low":
            origin = "local_swing_low"
            broken_index = None
            low_pivots_by_index[int(pivot.index)] = pivot
        else:
            broken_index = _first_reclaim_index(pivot, closes, break_atr_mult)
            if broken_index is None:
                continue
            origin = "local_flipped_resistance"

        candidates.append(
            SupportCandidate(
                price=float(pivot.body_price),
                index=int(pivot.index),
                origin=origin,
                structure_role=pivot.structure_role or ("H" if pivot.kind == "high" else "L"),
                bounds_style="local_reaction",
                broken_index=broken_index,
            )
        )

    zones: list[Zone] = []
    for cluster in _cluster_support_candidates(candidates, zone_width):
        if not _has_minimum_unique_touches(cluster, local_min_touches):
            continue
        zone = _zone_from_local_reaction_cluster(
            cluster=cluster,
            low_pivots_by_index=low_pivots_by_index,
            zone_width=zone_width,
            min_touches=local_min_touches,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        if zone is not None:
            zones.append(zone)

    zones.extend(
        _build_retested_flip_zones(
            candidates=candidates,
            existing_zones=zones,
            zone_width=zone_width,
            min_touches=local_min_touches,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
    )
    return _select_local_reaction_zones(zones, zone_width)


# Build one local-reaction zone from a cluster when lows and reclaimed highs define valid bounds.
def _zone_from_local_reaction_cluster(
    cluster: list[SupportCandidate],
    low_pivots_by_index: dict[int, StructurePivot],
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
) -> Zone | None:
    lows = [candidate for candidate in cluster if candidate.origin == "local_swing_low"]
    reclaimed_highs = [candidate for candidate in cluster if candidate.origin == "local_flipped_resistance"]
    # At least min_touches (default 2) distinct low pivots (by index) are required.
    # And there must be at least one reclaimed high pivot.
    if len({candidate.index for candidate in lows}) < int(min_touches) or not reclaimed_highs:
        return None
    
    # Among all the swing lows in the cluster, pick the low pivot with the highest price (body price); 
    # if two share that price, pick the one with the smaller index (earlier in time).
    upper_anchor = max(lows, key=lambda candidate: (candidate.price, -candidate.index))
    
    # Find all the reclaimed highs that came before the upper_anchor and are below its price.
    # Resistance was there first, price later tested that area from above and held with repeated lows
    prior_reclaimed_highs = [
        candidate
        for candidate in reclaimed_highs
        if candidate.index < upper_anchor.index and candidate.price < upper_anchor.price
    ]

    if prior_reclaimed_highs:
        low = max(candidate.price for candidate in prior_reclaimed_highs) # body price
    else:
        low = float(low_pivots_by_index[upper_anchor.index].wick_price)
    
    high = float(upper_anchor.price)

    width = high - low
    if width <= 0.0 or width > float(zone_width):
        return None

    cluster = sorted(cluster, key=lambda candidate: (candidate.price, candidate.index, candidate.origin))
    source_closes = [float(candidate.price) for candidate in cluster]
    source_indexes = [int(candidate.index) for candidate in cluster]
    flipped_count = len(reclaimed_highs)
    structure_roles = {candidate.structure_role for candidate in cluster if candidate.structure_role}

    return _make_support_zone(
        origin="local_reaction_support",
        bounds_style="local_reaction",
        low=low,
        high=high,
        width=width,
        touches=len(cluster),
        source_closes=source_closes,
        source_indexes=source_indexes,
        score=len(cluster) * 2 + flipped_count,
        structure_role=next(iter(structure_roles)) if len(structure_roles) == 1 else "mixed",
        broken_index=_latest_defined(candidate.broken_index for candidate in reclaimed_highs),
        zone_width=zone_width,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )


# Add a local body-to-body zone when a reclaimed high is later retested from above,
# even if greedy price clustering split the high and the held lows into neighbors.
def _build_retested_flip_zones(
    *,
    candidates: list[SupportCandidate],
    existing_zones: list[Zone],
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
) -> list[Zone]:
    lows = [candidate for candidate in candidates if candidate.origin == "local_swing_low"]
    reclaimed_highs = [
        candidate
        for candidate in candidates
        if candidate.origin == "local_flipped_resistance" and candidate.broken_index is not None
    ]

    zones: list[Zone] = []
    for high_candidate in sorted(reclaimed_highs, key=lambda candidate: candidate.index):
        first_low = _first_retest_low(high_candidate, lows, zone_width)
        if first_low is None:
            continue
        retest_lows = [
            low
            for low in lows
            if int(low.index) > int(high_candidate.broken_index)
            and float(high_candidate.price) < float(low.price) <= float(first_low.price)
        ]
        if len({candidate.index for candidate in retest_lows}) < int(min_touches):
            continue

        low = float(high_candidate.price)
        high = float(first_low.price)
        width = high - low
        if width <= 0.0 or width > float(zone_width):
            continue

        source_candidates = sorted([high_candidate] + retest_lows, key=lambda candidate: candidate.index)
        zone = _make_support_zone(
            origin="local_retested_flip_support",
            bounds_style="local_reaction",
            low=low,
            high=high,
            width=width,
            touches=len(source_candidates),
            source_closes=[float(candidate.price) for candidate in source_candidates],
            source_indexes=[int(candidate.index) for candidate in source_candidates],
            score=len(source_candidates) * 2 + 1,
            structure_role=_candidate_role(source_candidates),
            broken_index=int(high_candidate.broken_index),
            zone_width=zone_width,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        if not _overlaps_any_zone(zone, existing_zones + zones):
            zones.append(zone)
    return zones


def _first_retest_low(
    high_candidate: SupportCandidate,
    lows: list[SupportCandidate],
    zone_width: float,
) -> SupportCandidate | None:
    if high_candidate.broken_index is None:
        return None
    upper_limit = float(high_candidate.price) + float(zone_width)
    for low in sorted(lows, key=lambda candidate: candidate.index):
        if int(low.index) <= int(high_candidate.broken_index):
            continue
        if float(high_candidate.price) < float(low.price) <= upper_limit:
            return low
    return None


def _select_local_reaction_zones(zones: list[Zone], zone_width: float) -> list[Zone]:
    selected: list[Zone] = []
    min_width = float(zone_width) * 0.2
    adjacent_gap = float(zone_width) * 1.3
    midpoint_spacing = float(zone_width) * 2.0
    for zone in sorted(zones, key=lambda item: float(item["low"])):
        if float(zone["width"]) < min_width:
            continue
        if selected and _local_zones_share_ladder_slot(selected[-1], zone, adjacent_gap, midpoint_spacing):
            if _local_zone_rank(zone) < _local_zone_rank(selected[-1]):
                selected[-1] = dict(zone)
            continue
        selected.append(dict(zone))
    return selected


def _local_zones_share_ladder_slot(
    first: Zone,
    second: Zone,
    adjacent_gap: float,
    midpoint_spacing: float,
) -> bool:
    gap = float(second["low"]) - float(first["high"])
    midpoint_gap = float(second["mid"]) - float(first["mid"])
    return gap < adjacent_gap or midpoint_gap < midpoint_spacing


def _local_zone_rank(zone: Zone) -> tuple[int, float, float]:
    origin = str(zone.get("origin", ""))
    priority = 0 if origin == "local_retested_flip_support" else 1
    return (priority, float(zone["low"]), -float(zone.get("score", 0.0)))


def _candidate_role(candidates: list[SupportCandidate]) -> str:
    roles = {candidate.structure_role for candidate in candidates if candidate.structure_role}
    if not roles:
        return "unknown"
    if len(roles) == 1:
        return next(iter(roles))
    return "mixed"


def _overlaps_any_zone(zone: Zone, zones: list[Zone]) -> bool:
    return any(max(float(zone["low"]), float(other["low"])) <= min(float(zone["high"]), float(other["high"])) for other in zones)
