from __future__ import annotations

from .factory import (
    Zone,
    _fixed_support_bounds,
    _latest_defined,
    _make_support_zone,
    _support_origin_from_origins,
    _zone_from_support_cluster,
)

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
) -> list[Zone]:
    clusters = _cluster_support_candidates(candidates, zone_width)
    zones = [
        _zone_from_support_cluster(cluster, zone_width, current_price, buffer_pct)
        for cluster in clusters
        if _has_minimum_unique_touches(cluster, min_touches)
    ]
    zones = _merge_support_macro_groups(zones, zone_width, current_price, buffer_pct)
    zones = _bridge_body_floor_support_gaps(zones, zone_width, current_price, buffer_pct)
    return _suppress_nearby_support_zones(zones)


def _cluster_support_candidates(
    candidates: list[SupportCandidate],
    zone_width: float,
) -> list[list[SupportCandidate]]:
    clusters: list[list[SupportCandidate]] = []
    for candidate in sorted(candidates, key=lambda item: item.price):
        for cluster in clusters:
            if _candidate_matches_cluster(candidate, cluster, zone_width):
                cluster.append(candidate)
                break
        else:
            clusters.append([candidate])
    return clusters


def _candidate_matches_cluster(
    candidate: SupportCandidate,
    cluster: list[SupportCandidate],
    zone_width: float,
) -> bool:
    if candidate.bounds_style != cluster[0].bounds_style:
        return False
    prices = [item.price for item in cluster] + [candidate.price]
    return max(prices) - min(prices) <= float(zone_width)


def _has_minimum_unique_touches(cluster: list[SupportCandidate], min_touches: int) -> bool:
    unique_touches = {(item.index, item.origin) for item in cluster}
    return len(unique_touches) >= int(min_touches)


def _merge_support_macro_groups(
    zones: list[Zone],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> list[Zone]:
    macro_zones: list[Zone] = []
    group: list[Zone] = []
    for zone in sorted(zones, key=lambda item: item["low"]):
        if group and not _zone_can_join_macro_group(group, zone):
            macro_zones.append(_combine_support_macro_group(group, zone_width, current_price, buffer_pct))
            group = []
        group.append(zone)
    if group:
        macro_zones.append(_combine_support_macro_group(group, zone_width, current_price, buffer_pct))
    return macro_zones


def _zone_can_join_macro_group(group: list[Zone], zone: Zone) -> bool:
    if not _bounds_styles_can_share_macro_group(group, zone):
        return False
    gap = float(zone["low"]) - float(group[-1]["high"])
    if gap > STRUCTURE_MACRO_GAP:
        return False
    source_prices = [float(price) for item in group for price in item["source_closes"]]
    source_prices.extend(float(price) for price in zone["source_closes"])
    return max(source_prices) - min(source_prices) <= STRUCTURE_MACRO_MAX_SOURCE_SPAN


def _bounds_styles_can_share_macro_group(group: list[Zone], zone: Zone) -> bool:
    zone_style = zone.get("bounds_style", "body")
    group_styles = [item.get("bounds_style", "body") for item in group]
    if len(set(group_styles)) == 1 and zone_style == group_styles[-1]:
        return True
    return group_styles[0] == "body" and "support_floor" not in group_styles and zone_style == "support_floor"


def _combine_support_macro_group(
    group: list[Zone],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> Zone:
    if len(group) == 1:
        zone = dict(group[0])
        zone["role"] = "support"
        return zone

    source_closes = [float(price) for zone in group for price in zone["source_closes"]]
    source_indexes = [int(index) for zone in group for index in zone["source_indexes"]]
    bounds_style = group[0].get("bounds_style", "body")
    low, high = _fixed_support_bounds(source_closes, zone_width, bounds_style)
    origins = {str(zone["origin"]) for zone in group}
    structure_roles = {str(zone.get("structure_role", "unknown")) for zone in group}

    return _make_support_zone(
        origin=_support_origin_from_origins(origins),
        bounds_style=bounds_style,
        low=low,
        high=high,
        width=zone_width,
        touches=len(source_closes),
        source_closes=source_closes,
        source_indexes=source_indexes,
        score=sum(float(zone.get("score", 0.0)) for zone in group),
        structure_role=next(iter(structure_roles)) if len(structure_roles) == 1 else "mixed",
        broken_index=_latest_defined(zone.get("broken_index") for zone in group),
        zone_width=zone_width,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )


def _bridge_body_floor_support_gaps(
    zones: list[Zone],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> list[Zone]:
    bridges: list[Zone] = []
    consumed_indexes: set[int] = set()
    indexed_zones = list(enumerate(zones))
    sorted_zones = sorted(indexed_zones, key=lambda item: float(item[1]["low"]))
    for (lower_index, lower_zone), (upper_index, upper_zone) in zip(sorted_zones, sorted_zones[1:]):
        bridge = _body_floor_support_bridge(lower_zone, upper_zone, zone_width, current_price, buffer_pct)
        if bridge is None:
            continue
        bridges.append(bridge)
        consumed_indexes.update({lower_index, upper_index})
        consumed_indexes.update(_body_floor_companion_indexes(indexed_zones, lower_zone))
    return [dict(zone) for index, zone in indexed_zones if index not in consumed_indexes] + bridges


def _body_floor_support_bridge(
    lower_zone: Zone,
    upper_zone: Zone,
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> Zone | None:
    if lower_zone.get("bounds_style", "body") != "body":
        return None
    if upper_zone.get("bounds_style", "body") != "support_floor":
        return None
    if lower_zone.get("origin") != "structure_swing_low":
        return None
    if upper_zone.get("origin") != "structure_support_floor":
        return None

    low = float(lower_zone["high"])
    confirmation_gap = float(upper_zone["low"]) - low
    if confirmation_gap <= 0.0 or confirmation_gap > STRUCTURE_BODY_FLOOR_BRIDGE_MAX_GAP:
        return None

    high = low + float(zone_width)
    source_closes = [float(price) for price in lower_zone["source_closes"]]
    source_closes.extend(float(price) for price in upper_zone["source_closes"])
    source_indexes = [int(index) for index in lower_zone["source_indexes"]]
    source_indexes.extend(int(index) for index in upper_zone["source_indexes"])
    origins = {str(lower_zone["origin"]), str(upper_zone["origin"])}

    return _make_support_zone(
        origin=_support_origin_from_origins(origins),
        bounds_style="body",
        low=low,
        high=high,
        width=zone_width,
        touches=int(lower_zone["touches"]) + int(upper_zone["touches"]),
        source_closes=source_closes,
        source_indexes=source_indexes,
        score=float(lower_zone.get("score", 0.0)) + float(upper_zone.get("score", 0.0)),
        structure_role="mixed",
        broken_index=_latest_defined(zone.get("broken_index") for zone in (lower_zone, upper_zone)),
        zone_width=zone_width,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )


def _body_floor_companion_indexes(
    indexed_zones: list[tuple[int, Zone]],
    body_zone: Zone,
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


def _suppress_nearby_support_zones(zones: list[Zone]) -> list[Zone]:
    collapsed = _collapse_adjacent_close_support_zones(zones)
    kept: list[Zone] = []
    for zone in sorted(collapsed, key=_support_zone_rank, reverse=True):
        if all(
            abs(float(zone["mid"]) - float(previous["mid"])) >= STRUCTURE_IMPORTANT_ZONE_SPACING
            for previous in kept
        ):
            kept.append(dict(zone))
    return kept


def _collapse_adjacent_close_support_zones(zones: list[Zone]) -> list[Zone]:
    kept: list[Zone] = []
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


def _support_zone_rank(zone: Zone) -> tuple[float, int, float]:
    return (float(zone.get("score", 0.0)), int(zone["touches"]), -float(zone["width_pct"]))
