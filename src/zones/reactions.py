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


def _build_local_reaction_zones(
    internal_pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
) -> list[Zone]:
    recent_start = max(0, len(closes) - STRUCTURE_LOCAL_REACTION_LOOKBACK_BARS)
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
        if not _has_minimum_unique_touches(cluster, min_touches):
            continue
        zone = _zone_from_local_reaction_cluster(
            cluster=cluster,
            low_pivots_by_index=low_pivots_by_index,
            zone_width=zone_width,
            min_touches=min_touches,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        if zone is not None:
            zones.append(zone)
    return zones


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
    if len({candidate.index for candidate in lows}) < int(min_touches) or not reclaimed_highs:
        return None

    upper_anchor = max(lows, key=lambda candidate: (candidate.price, -candidate.index))
    prior_reclaimed_highs = [
        candidate
        for candidate in reclaimed_highs
        if candidate.index < upper_anchor.index and candidate.price < upper_anchor.price
    ]
    if prior_reclaimed_highs:
        low = max(candidate.price for candidate in prior_reclaimed_highs)
    else:
        low = float(low_pivots_by_index[upper_anchor.index].price)
    high = float(upper_anchor.price)

    width = high - low
    support_ceiling = float(current_price) * (1.0 - float(buffer_pct))
    if width <= 0.0 or width > float(zone_width) or high > support_ceiling:
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
