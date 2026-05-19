from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd


PivotKind = Literal["high", "low"]
SwingTerm = Literal["internal", "external"]
BoundsStyle = Literal["body", "support_floor"]

STRUCTURE_ZONE_WIDTH = 500.0
STRUCTURE_MACRO_GAP = 300.0
STRUCTURE_MACRO_MAX_SOURCE_SPAN = 2000.0
STRUCTURE_IMPORTANT_ZONE_SPACING = 1600.0
STRUCTURE_SUPPORT_FLOOR_RETEST_WIDTH_MULT = 0.2
STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP = 4000.0
STRUCTURE_STAIR_STEP_MAX_INSERTIONS = 6


@dataclass
class StructurePivot:
    index: int
    kind: PivotKind
    price: float
    body_price: float
    atr: float
    term: SwingTerm
    structure_role: str | None = None


@dataclass
class SupportCandidate:
    price: float
    index: int
    origin: str
    structure_role: str
    bounds_style: BoundsStyle = "body"
    broken_index: int | None = None


def detect_support_resistance_zones(
    df: pd.DataFrame,
    zone_tolerance_pct: float = 0.0045,
    min_touches: int = 2,
    current_price: float | None = None,
    buffer_pct: float = 0.0015,
    internal_swing_order: int = 2,
    external_swing_order: int = 5,
    atr_period: int = 14,
    break_atr_mult: float = 0.2,
    external_min_swing_atr_mult: float = 4.0,
    external_min_swing_pct: float = 2.5,
) -> dict[str, list[dict[str, Any]]]:
    return detect_support_resistance_zones_structure_v1(
        df,
        zone_tolerance_pct=zone_tolerance_pct,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
        internal_swing_order=internal_swing_order,
        external_swing_order=external_swing_order,
        atr_period=atr_period,
        break_atr_mult=break_atr_mult,
        external_min_swing_atr_mult=external_min_swing_atr_mult,
        external_min_swing_pct=external_min_swing_pct,
    )


def detect_support_resistance_zones_structure_v1(
    df: pd.DataFrame,
    internal_swing_order: int = 2,
    external_swing_order: int = 5,
    atr_period: int = 14,
    external_min_swing_atr_mult: float = 4.0,
    external_min_swing_pct: float = 2.5,
    zone_tolerance_pct: float = 0.0045,
    min_touches: int = 2,
    current_price: float | None = None,
    buffer_pct: float = 0.0015,
    break_atr_mult: float = 0.2,
) -> dict[str, list[dict[str, Any]]]:
    _ = internal_swing_order, zone_tolerance_pct
    empty = {"support": [], "resistance": [], "active": [], "all": []}
    ohlc = _coerce_ohlc(df)
    if ohlc is None:
        return empty

    order = max(1, int(external_swing_order))
    if len(ohlc) < (order * 2 + 1):
        return empty

    highs = ohlc["high"].to_numpy(dtype=float)
    lows = ohlc["low"].to_numpy(dtype=float)
    closes = ohlc["close"].to_numpy(dtype=float)
    atr = _average_true_range(highs=highs, lows=lows, closes=closes, period=atr_period)
    if current_price is None:
        current_price = float(closes[-1])

    raw_external_pivots = _find_structure_pivots(ohlc, order, atr, "external")
    external_pivots = _filter_prominent_structure_pivots(
        raw_external_pivots,
        min_swing_atr_mult=external_min_swing_atr_mult,
        min_swing_pct=external_min_swing_pct,
    )
    if not external_pivots:
        return empty

    _label_structure_pivots(raw_external_pivots)
    _label_structure_pivots(external_pivots)
    candidates = _support_candidates(
        raw_external_pivots=raw_external_pivots,
        external_pivots=external_pivots,
        closes=closes,
        break_atr_mult=break_atr_mult,
        zone_width=STRUCTURE_ZONE_WIDTH,
    )
    zones = _build_support_zones(
        candidates,
        zone_width=STRUCTURE_ZONE_WIDTH,
        min_touches=min_touches,
        current_price=float(current_price),
        buffer_pct=buffer_pct,
    )
    zones = _make_support_zones_distinct(zones, current_price=float(current_price), buffer_pct=buffer_pct)
    zones = _fill_support_staircase_gaps(
        zones=zones,
        raw_external_pivots=raw_external_pivots,
        closes=closes,
        break_atr_mult=break_atr_mult,
        zone_width=STRUCTURE_ZONE_WIDTH,
        min_touches=min_touches,
        current_price=float(current_price),
        buffer_pct=buffer_pct,
    )

    support = sorted(zones, key=lambda zone: _zone_distance_sort_key(zone, float(current_price)))
    return {"support": support, "resistance": [], "active": [], "all": support}


def _support_candidates(
    raw_external_pivots: list[StructurePivot],
    external_pivots: list[StructurePivot],
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


def _candidate_from_pivot(
    pivot: StructurePivot,
    origin: str,
    broken_index: int | None = None,
) -> SupportCandidate:
    return SupportCandidate(
        price=float(pivot.body_price),
        index=int(pivot.index),
        origin=origin,
        structure_role=pivot.structure_role or ("H" if pivot.kind == "high" else "L"),
        broken_index=broken_index,
    )


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
        floor_price = float(prominent_low.price)
        candidates.append(_support_floor_candidate(prominent_low, floor_price, "structure_swing_low_wick"))
        for raw_low in raw_lows:
            if raw_low.index == prominent_low.index:
                continue
            body_floor = float(raw_low.body_price)
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
        if len({(item.index, item.origin) for item in cluster}) >= int(min_touches)
    ]
    return _consolidate_support_zones(zones, zone_width, current_price, buffer_pct)


def _candidate_matches_cluster(
    candidate: SupportCandidate,
    cluster: list[SupportCandidate],
    zone_width: float,
) -> bool:
    if candidate.bounds_style != cluster[0].bounds_style:
        return False
    prices = [item.price for item in cluster] + [candidate.price]
    return max(prices) - min(prices) <= float(zone_width)


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


def _consolidate_support_zones(
    zones: list[dict[str, Any]],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    macro_zones: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []
    for zone in sorted(zones, key=lambda item: item["low"]):
        if not group:
            group = [zone]
            continue
        if _zones_can_share_macro_group(group, zone):
            group.append(zone)
        else:
            macro_zones.append(_combine_support_macro_group(group, zone_width, current_price, buffer_pct))
            group = [zone]
    if group:
        macro_zones.append(_combine_support_macro_group(group, zone_width, current_price, buffer_pct))
    return _suppress_nearby_support_zones(macro_zones)


def _zones_can_share_macro_group(group: list[dict[str, Any]], zone: dict[str, Any]) -> bool:
    gap = float(zone["low"]) - float(group[-1]["high"])
    if gap > STRUCTURE_MACRO_GAP:
        return False
    source_prices = [float(price) for item in group for price in item["source_closes"]] + [
        float(price) for price in zone["source_closes"]
    ]
    return max(source_prices) - min(source_prices) <= STRUCTURE_MACRO_MAX_SOURCE_SPAN


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
    low, high = _fixed_support_zone_bounds(source_closes, zone_width)
    mid = (low + high) / 2.0
    origins = {str(zone["origin"]) for zone in group}
    structure_roles = {str(zone.get("structure_role", "unknown")) for zone in group}
    broken_indexes = [zone.get("broken_index") for zone in group if zone.get("broken_index") is not None]
    return {
        "origin": _support_origin_from_origins(origins),
        "role": "support",
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


def _suppress_nearby_support_zones(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for zone in sorted(zones, key=_support_zone_rank, reverse=True):
        if all(abs(float(zone["mid"]) - float(previous["mid"])) >= STRUCTURE_IMPORTANT_ZONE_SPACING for previous in kept):
            kept.append(dict(zone))
    return kept


def _fill_support_staircase_gaps(
    zones: list[dict[str, Any]],
    raw_external_pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    filled_zones = [dict(zone) for zone in zones]
    for _ in range(STRUCTURE_STAIR_STEP_MAX_INSERTIONS):
        gap_fill = _best_support_staircase_gap_fill(
            zones=filled_zones,
            raw_external_pivots=raw_external_pivots,
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
    return filled_zones


def _best_support_staircase_gap_fill(
    zones: list[dict[str, Any]],
    raw_external_pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
) -> dict[str, Any] | None:
    support_zones = sorted(
        [zone for zone in zones if _coerce_price_state(zone, current_price, buffer_pct) == "support"],
        key=lambda zone: float(zone["low"]),
    )
    best_zone: dict[str, Any] | None = None
    best_rank: tuple[float, float, int, float] | None = None

    for lower_zone, upper_zone in zip(support_zones, support_zones[1:]):
        gap = float(upper_zone["low"]) - float(lower_zone["high"])
        if gap <= STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP:
            continue
        candidates = _stair_step_support_candidates(
            raw_external_pivots=raw_external_pivots,
            closes=closes,
            break_atr_mult=break_atr_mult,
            zone_width=zone_width,
            lower_zone=lower_zone,
            upper_zone=upper_zone,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        candidate_zones = _build_support_zones(
            candidates,
            zone_width=zone_width,
            min_touches=min_touches,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        candidate_zones = [
            zone
            for zone in candidate_zones
            if float(zone["low"]) > float(lower_zone["high"]) and float(zone["high"]) < float(upper_zone["low"])
        ]
        if not candidate_zones:
            continue
        selected = min(candidate_zones, key=lambda zone: _stair_step_gap_rank(zone, lower_zone, upper_zone))
        rank = _stair_step_gap_rank(selected, lower_zone, upper_zone)
        if best_rank is None or rank < best_rank:
            best_zone = selected
            best_rank = rank
    return best_zone


def _stair_step_support_candidates(
    raw_external_pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    lower_zone: dict[str, Any],
    upper_zone: dict[str, Any],
    current_price: float,
    buffer_pct: float,
) -> list[SupportCandidate]:
    lower_high = float(lower_zone["high"])
    upper_low = float(upper_zone["low"])
    support_ceiling = float(current_price) * (1.0 - float(buffer_pct))
    candidates: list[SupportCandidate] = []
    for pivot in raw_external_pivots:
        if pivot.kind != "high":
            continue
        price = float(pivot.body_price)
        if price - float(zone_width) <= lower_high or price >= upper_low or price >= support_ceiling:
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


def _high_is_confirmed_reclaimed(
    pivot: StructurePivot,
    closes: np.ndarray,
    break_atr_mult: float,
) -> bool:
    return _first_reclaim_index(pivot, closes, break_atr_mult) is not None


def _first_reclaim_index(
    pivot: StructurePivot,
    closes: np.ndarray,
    break_atr_mult: float,
) -> int | None:
    threshold = max(0.0, float(pivot.atr) * float(break_atr_mult))
    future_closes = closes[pivot.index + 1 :]
    offsets = np.flatnonzero(future_closes > float(pivot.price) + threshold)
    if not len(offsets):
        return None
    return int(pivot.index + 1 + offsets[0])


def _fixed_support_zone_bounds(prices: list[float], zone_width: float) -> tuple[float, float]:
    high = _support_base_anchor(prices)
    return high - float(zone_width), high


def _fixed_support_floor_zone_bounds(prices: list[float], zone_width: float) -> tuple[float, float]:
    low = _support_floor_anchor(prices)
    return low, low + float(zone_width)


def _support_base_anchor(prices: list[float]) -> float:
    sorted_prices = sorted(float(price) for price in prices)
    if len(sorted_prices) <= 10:
        return max(sorted_prices)
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


def _support_cluster_role(cluster: list[SupportCandidate]) -> str:
    roles = [item.structure_role for item in cluster if item.structure_role]
    if not roles:
        return "unknown"
    if len(set(roles)) == 1:
        return roles[0]
    return "mixed"


def _cluster_uses_support_floor_bounds(cluster: list[SupportCandidate]) -> bool:
    return all(item.bounds_style == "support_floor" for item in cluster)


def _support_zone_rank(zone: dict[str, Any]) -> tuple[float, int, float]:
    return (float(zone.get("score", 0.0)), int(zone["touches"]), -float(zone["width_pct"]))


def _prefer_support_zone(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_score = (float(first.get("score", 0.0)), int(first["touches"]), -float(first["width_pct"]))
    second_score = (float(second.get("score", 0.0)), int(second["touches"]), -float(second["width_pct"]))
    return dict(first if first_score >= second_score else second)


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


def _classify_price_state(low: float, high: float, current_price: float, buffer_pct: float) -> str:
    if high < current_price * (1 - buffer_pct):
        return "support"
    if low > current_price * (1 + buffer_pct):
        return "resistance"
    return "active"


def _zone_distance_sort_key(zone: dict[str, Any], current_price: float) -> tuple[float, float, int]:
    low = float(zone["low"])
    high = float(zone["high"])
    price = float(current_price)
    if low <= price <= high:
        distance = 0.0
    elif price < low:
        distance = low - price
    else:
        distance = price - high
    return (distance, -float(zone.get("score", 0.0)), -int(zone["touches"]))


def _coerce_ohlc(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None:
        return None
    required = ["open", "high", "low", "close"]
    if any(column not in df.columns for column in required):
        return None
    ohlc = df[required].apply(pd.to_numeric, errors="coerce").dropna().reset_index(drop=True)
    if ohlc.empty:
        return None
    return ohlc


def _average_true_range(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    if len(closes) == 0:
        return np.array([], dtype=float)
    true_ranges = np.empty(len(closes), dtype=float)
    true_ranges[0] = highs[0] - lows[0]
    for idx in range(1, len(closes)):
        true_ranges[idx] = max(
            highs[idx] - lows[idx],
            abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        )
    return pd.Series(true_ranges).rolling(window=max(1, int(period)), min_periods=1).mean().to_numpy(dtype=float)


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
