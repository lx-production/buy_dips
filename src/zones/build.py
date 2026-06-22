from __future__ import annotations

from typing import Any

import numpy as np

from .postprocess import _classify_price_state
from .types import (
    STRUCTURE_ADJACENT_STRONGER_TOUCH_MARGIN,
    STRUCTURE_ADJACENT_ZONE_MIN_GAP,
    STRUCTURE_BODY_FLOOR_BRIDGE_MAX_GAP,
    STRUCTURE_IMPORTANT_ZONE_SPACING,
    STRUCTURE_MACRO_GAP,
    STRUCTURE_MACRO_MAX_SOURCE_SPAN,
    SupportCandidate,
)


def _build_support_zones(
    candidates: list[SupportCandidate],
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    clusters: list[list[SupportCandidate]] = []
    for candidate in sorted(candidates, key=lambda item: item.price):
        placed = False
        for cluster in clusters:
            if _candidate_matches_cluster(candidate, cluster, zone_width):
                cluster.append(candidate)
                placed = True
                break
        if not placed:
            clusters.append([candidate])

    zones = [
        _zone_from_support_cluster(cluster, zone_width, current_price, buffer_pct)
        for cluster in clusters
        # A cluster must have at least min_touches unique source touches, counted as distinct (index, origin) pairs
        if len({(item.index, item.origin) for item in cluster}) >= int(min_touches)
    ]
    return _consolidate_support_zones(zones, zone_width, current_price, buffer_pct)

# Can this candidate join an existing cluster?
# True if the candidate matches the cluster based on bounds style and zone width
def _candidate_matches_cluster(
    candidate: SupportCandidate,
    cluster: list[SupportCandidate],
    zone_width: float,
) -> bool:
    if candidate.bounds_style != cluster[0].bounds_style:
        return False
    prices = [item.price for item in cluster] + [candidate.price]
    return max(prices) - min(prices) <= float(zone_width)

# Takes one cluster → full zone dict
def _zone_from_support_cluster(
    cluster: list[SupportCandidate],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> dict[str, Any]:
    cluster = sorted(cluster, key=lambda item: (item.price, item.index, item.origin))
    prices = [float(item.price) for item in cluster]
    indexes = [int(item.index) for item in cluster]
    if _cluster_uses_support_floor_bounds(cluster):
        low, high = _fixed_support_floor_zone_bounds(prices, zone_width)
    else:
        low, high = _fixed_support_zone_bounds(prices, zone_width)
    mid = (low + high) / 2.0
    width = high - low
    broken_indexes = [item.broken_index for item in cluster if item.broken_index is not None]
    flipped_count = sum(1 for item in cluster if item.origin.startswith("flipped_"))
    return {
        "origin": _support_origin(cluster),
        "role": "support",
        "bounds_style": cluster[0].bounds_style,
        "low": float(low),
        "high": float(high),
        "mid": float(mid),
        "width": float(width),
        "width_pct": float(width / mid * 100.0) if mid else 0.0,
        "touches": len(cluster),
        "source_closes": prices,
        "source_indexes": indexes,
        "score": float(len(cluster) * 2 + flipped_count),
        "structure_role": _support_cluster_role(cluster),
        "structure_bias": "support",
        "price_state": _classify_price_state(low, high, current_price, buffer_pct),
        "last_touch_index": max(indexes),
        "broken_index": max(broken_indexes) if broken_indexes else None,
        "zone_width": float(zone_width),
    }

# Walk sorted zones, low to high, build macro groups, call _combine_support_macro_group, then hand off to suppression
def _consolidate_support_zones(
    zones: list[dict[str, Any]],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    macro_zones: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []
    for zone in sorted(zones, key=lambda item: item["low"]): # Zones are walked low → high
        if not group:
            group = [zone]
            continue
        if _zone_can_join_macro_group(group, zone):
            group.append(zone)
        else:
            macro_zones.append(_combine_support_macro_group(group, zone_width, current_price, buffer_pct))
            group = [zone]
    if group:
        macro_zones.append(_combine_support_macro_group(group, zone_width, current_price, buffer_pct))
    macro_zones = _bridge_body_floor_support_gaps(macro_zones, zone_width, current_price, buffer_pct)
    return _suppress_nearby_support_zones(macro_zones)

# Gap ≤ 300 USD, source span ≤ 2000 USD, plus bounds-style check
def _zone_can_join_macro_group(group: list[dict[str, Any]], zone: dict[str, Any]) -> bool:
    if not _bounds_styles_can_share_macro_group(group, zone):
        return False
    # check against the trailing edge of the group: group[-1]["high"]
    gap = float(zone["low"]) - float(group[-1]["high"])
    if gap > STRUCTURE_MACRO_GAP:
        return False
    # Gather every touch price from the group plus the candidate zone
    source_prices = [float(price) for item in group for price in item["source_closes"]] + [
        float(price) for price in zone["source_closes"]
    ]
    return max(source_prices) - min(source_prices) <= STRUCTURE_MACRO_MAX_SOURCE_SPAN

# Can a body zone and a floor zone merge?
# One-way only: a body group can absorb one trailing floor shelf. A floor-first group cannot absorb a body zone the same way
def _bounds_styles_can_share_macro_group(group: list[dict[str, Any]], zone: dict[str, Any]) -> bool:
    zone_style = zone.get("bounds_style", "body") # if "bounds_style" is missing, it defaults to "body"
    group_styles = [item.get("bounds_style", "body") for item in group] # if "bounds_style" is missing, it defaults to "body"
    if len(set(group_styles)) == 1 and zone_style == group_styles[-1]:
        return True
    # the first zone defines the group type for the special mixed rule: a body-first group may absorb one trailing support_floor shelf
    return group_styles[0] == "body" and "support_floor" not in group_styles and zone_style == "support_floor"

# Merge a group of zones into one wider zone (or pass through if only one)
def _combine_support_macro_group(
    group: list[dict[str, Any]],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> dict[str, Any]:
    if len(group) == 1:
        zone = dict(group[0])
        zone["role"] = "support"
        return zone

    source_closes = [float(price) for zone in group for price in zone["source_closes"]]
    source_indexes = [int(index) for zone in group for index in zone["source_indexes"]]
    
    # Determine the bounds style for the new zone based on the first zone in the group
    bounds_style = group[0].get("bounds_style", "body")

    if bounds_style == "support_floor": # anchor at the low side
        low, high = _fixed_support_floor_zone_bounds(source_closes, zone_width)
    
    else: # anchor at the high side
        low, high = _fixed_support_zone_bounds(source_closes, zone_width)
    
    mid = (low + high) / 2.0
    origins = {str(zone["origin"]) for zone in group}
    structure_roles = {str(zone.get("structure_role", "unknown")) for zone in group}
    broken_indexes = [zone.get("broken_index") for zone in group if zone.get("broken_index") is not None]
    return {
        "origin": _support_origin_from_origins(origins),
        "role": "support",
        "bounds_style": bounds_style,
        "low": float(low),
        "high": float(high),
        "mid": float(mid),
        "width": float(zone_width),
        "width_pct": float(zone_width / mid * 100.0) if mid else 0.0,
        "touches": len(source_closes),
        "source_closes": source_closes,
        "source_indexes": source_indexes,
        "score": float(sum(float(zone.get("score", 0.0)) for zone in group)),
        "structure_role": next(iter(structure_roles)) if len(structure_roles) == 1 else "mixed",
        "structure_bias": "support",
        "price_state": _classify_price_state(low, high, current_price, buffer_pct),
        "last_touch_index": max(source_indexes),
        "broken_index": max(broken_indexes) if broken_indexes else None,
        "zone_width": float(zone_width),
    }


def _bridge_body_floor_support_gaps(
    zones: list[dict[str, Any]],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    bridges: list[dict[str, Any]] = []
    consumed_indexes: set[int] = set()
    indexed_zones = list(enumerate(zones))
    sorted_zones = sorted(indexed_zones, key=lambda item: float(item[1]["low"]))
    for (lower_index, lower_zone), (upper_index, upper_zone) in zip(sorted_zones, sorted_zones[1:]):
        bridge = _body_floor_support_bridge(lower_zone, upper_zone, zone_width, current_price, buffer_pct)
        if bridge is not None:
            bridges.append(bridge)
            consumed_indexes.update({lower_index, upper_index})
            consumed_indexes.update(_body_floor_companion_indexes(indexed_zones, lower_zone))
    return [dict(zone) for index, zone in indexed_zones if index not in consumed_indexes] + bridges


def _body_floor_support_bridge(
    lower_zone: dict[str, Any],
    upper_zone: dict[str, Any],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> dict[str, Any] | None:
    if lower_zone.get("bounds_style", "body") != "body" or upper_zone.get("bounds_style", "body") != "support_floor":
        return None
    if lower_zone.get("origin") != "structure_swing_low" or upper_zone.get("origin") != "structure_support_floor":
        return None

    low = float(lower_zone["high"])
    confirmation_gap = float(upper_zone["low"]) - low
    if confirmation_gap <= 0.0 or confirmation_gap > STRUCTURE_BODY_FLOOR_BRIDGE_MAX_GAP:
        return None
    high = low + float(zone_width)

    source_closes = [float(price) for price in lower_zone["source_closes"]] + [
        float(price) for price in upper_zone["source_closes"]
    ]
    source_indexes = [int(index) for index in lower_zone["source_indexes"]] + [
        int(index) for index in upper_zone["source_indexes"]
    ]
    mid = (low + high) / 2.0
    broken_indexes = [
        zone.get("broken_index")
        for zone in (lower_zone, upper_zone)
        if zone.get("broken_index") is not None
    ]
    origins = {str(lower_zone["origin"]), str(upper_zone["origin"])}
    return {
        "origin": _support_origin_from_origins(origins),
        "role": "support",
        "bounds_style": "body",
        "low": low,
        "high": high,
        "mid": mid,
        "width": float(zone_width),
        "width_pct": float(zone_width / mid * 100.0) if mid else 0.0,
        "touches": int(lower_zone["touches"]) + int(upper_zone["touches"]),
        "source_closes": source_closes,
        "source_indexes": source_indexes,
        "score": float(lower_zone.get("score", 0.0)) + float(upper_zone.get("score", 0.0)),
        "structure_role": "mixed",
        "structure_bias": "support",
        "price_state": _classify_price_state(low, high, current_price, buffer_pct),
        "last_touch_index": max(source_indexes),
        "broken_index": max(broken_indexes) if broken_indexes else None,
        "zone_width": float(zone_width),
    }


def _body_floor_companion_indexes(
    indexed_zones: list[tuple[int, dict[str, Any]]],
    body_zone: dict[str, Any],
) -> set[int]:
    body_source_indexes = {int(index) for index in body_zone["source_indexes"]}
    companion_indexes: set[int] = set()
    for index, zone in indexed_zones:
        if zone.get("bounds_style", "body") != "support_floor":
            continue
        zone_source_indexes = {int(source_index) for source_index in zone["source_indexes"]}
        if not body_source_indexes.intersection(zone_source_indexes):
            continue
        gap = float(body_zone["low"]) - float(zone["high"])
        if 0.0 <= gap <= STRUCTURE_BODY_FLOOR_BRIDGE_MAX_GAP:
            companion_indexes.add(index)
    return companion_indexes


# Run _collapse_adjacent_close_support_zones, then keep zones whose midpoints are ≥ 1000 USD apart (stronger wins)
def _suppress_nearby_support_zones(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collapsed = _collapse_adjacent_close_support_zones(zones)
    kept: list[dict[str, Any]] = []
    for zone in sorted(collapsed, key=_support_zone_rank, reverse=True):
        if all(
            abs(float(zone["mid"]) - float(previous["mid"])) >= STRUCTURE_IMPORTANT_ZONE_SPACING
            for previous in kept
        ):
            kept.append(dict(zone))
    return kept

# If two zones are < 650 USD apart (same style), keep the upper one unless the lower has 3+ more touches
def _collapse_adjacent_close_support_zones(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for zone in sorted(zones, key=lambda item: float(item["low"])):
        zone = dict(zone)
        if not kept:
            kept.append(zone)
            continue
        previous = kept[-1]
        gap = float(zone["low"]) - float(previous["high"])
        same_bounds_style = zone.get("bounds_style", "body") == previous.get("bounds_style", "body")
        if same_bounds_style and gap < STRUCTURE_ADJACENT_ZONE_MIN_GAP:
            touch_margin = int(previous["touches"]) - int(zone["touches"])
            if touch_margin >= STRUCTURE_ADJACENT_STRONGER_TOUCH_MARGIN:
                continue
            kept[-1] = zone
        else:
            kept.append(zone)
    return kept

# used for body-style zones, anchor down
def _fixed_support_zone_bounds(prices: list[float], zone_width: float) -> tuple[float, float]:
    high = _support_upper_anchor(prices)
    return high - float(zone_width), high

# used for support-floor zones, anchor up
def _fixed_support_floor_zone_bounds(prices: list[float], zone_width: float) -> tuple[float, float]:
    low = _support_floor_anchor(prices)
    return low, low + float(zone_width)

# Top anchor for bounds_style="body" zones (not support_floor / body_floor candidates).
def _support_upper_anchor(prices: list[float]) -> float:
    sorted_prices = sorted(float(price) for price in prices)
    if len(sorted_prices) <= 10:
        return max(sorted_prices)
    # chooses an index near the bottom 10% of the sorted prices
    index = int(np.floor((len(sorted_prices) - 1) * 0.10))
    return sorted_prices[index]


def _support_floor_anchor(prices: list[float]) -> float:
    sorted_prices = sorted(float(price) for price in prices)
    if len(sorted_prices) <= 10:
        return min(sorted_prices)
    index = int(np.floor((len(sorted_prices) - 1) * 0.10))
    return sorted_prices[index]


def _support_origin(cluster: list[SupportCandidate]) -> str:
    return _support_origin_from_origins({item.origin for item in cluster})

# Picks one label for a zone from the origins of the candidates in the cluster
def _support_origin_from_origins(origins: set[str]) -> str:
    if origins and all(origin in ("structure_swing_low_wick", "structure_swing_low_body_floor") for origin in origins):
        return "structure_support_floor"
    if len(origins) == 1:
        return next(iter(origins))
    if "flipped_resistance" in origins:
        return "flipped_resistance"
    if all(origin.startswith("structure_") for origin in origins):
        return "mixed_structure"
    return "mixed_structure"

# Combines structure roles (H, L, HH…) into one value or "mixed"
def _support_cluster_role(cluster: list[SupportCandidate]) -> str:
    roles = [item.structure_role for item in cluster if item.structure_role]
    if not roles:
        return "unknown"
    if len(set(roles)) == 1:
        return roles[0]
    return "mixed"

# True if all candidates in the cluster use support_floor bounds
def _cluster_uses_support_floor_bounds(cluster: list[SupportCandidate]) -> bool:
    return all(item.bounds_style == "support_floor" for item in cluster)

# Ranking tuple: (score, touches, -width_pct) — used to pick winners
def _support_zone_rank(zone: dict[str, Any]) -> tuple[float, int, float]:
    return (float(zone.get("score", 0.0)), int(zone["touches"]), -float(zone["width_pct"]))
