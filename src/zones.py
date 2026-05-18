from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal
import numpy as np
import pandas as pd


ZoneRole = Literal["support", "resistance", "active"]
StructureBias = Literal["support", "resistance", "mixed"]
PivotKind = Literal["high", "low"]
SwingTerm = Literal["internal", "external"]
StructureBoundsStyle = Literal["body", "support_floor"]
STRUCTURE_ZONE_WIDTH = 500.0
STRUCTURE_MACRO_GAP = 300.0
STRUCTURE_MACRO_MAX_SOURCE_SPAN = 2000.0
STRUCTURE_IMPORTANT_ZONE_SPACING = 1600.0
STRUCTURE_SUPPORT_FLOOR_RETEST_WIDTH_MULT = 0.2
STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP = 4500.0
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
class StructureLeg:
    id: int
    start_index: int
    end_index: int
    direction: Literal["up", "down"]
    bars: int
    price_change: float
    atr_normalized_slope: float
    log_return_per_bar: float


@dataclass
class StructureEvent:
    event: Literal["BOS", "CHOCH"]
    direction: Literal["bullish", "bearish"]
    level: float
    pivot_index: int
    broken_index: int


@dataclass
class ZoneStructureCandidate:
    price: float
    index: int
    origin: str
    zone_width: float
    structure_role: str
    term: SwingTerm
    bounds_style: StructureBoundsStyle = "body"
    broken_index: int | None = None
    leg_ids: tuple[int, ...] = ()


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
        internal_swing_order=internal_swing_order,
        external_swing_order=external_swing_order,
        atr_period=atr_period,
        zone_tolerance_pct=zone_tolerance_pct,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
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
    empty = {"support": [], "resistance": [], "active": [], "all": []}
    ohlc = _coerce_ohlc(df)
    if ohlc is None:
        return empty

    min_order = max(1, min(int(internal_swing_order), int(external_swing_order)))
    if len(ohlc) < (min_order * 2 + 1):
        return empty

    highs = ohlc["high"].to_numpy(dtype=float)
    lows = ohlc["low"].to_numpy(dtype=float)
    closes = ohlc["close"].to_numpy(dtype=float)
    atr = _average_true_range(highs=highs, lows=lows, closes=closes, period=atr_period)
    if current_price is None:
        current_price = float(closes[-1])

    internal_pivots = _find_structure_pivots(ohlc, internal_swing_order, atr, "internal")
    raw_external_pivots = _find_structure_pivots(ohlc, external_swing_order, atr, "external")
    external_pivots = _filter_prominent_structure_pivots(
        raw_external_pivots,
        min_swing_atr_mult=external_min_swing_atr_mult,
        min_swing_pct=external_min_swing_pct,
    )
    if not internal_pivots and not external_pivots:
        return empty

    _label_structure_pivots(internal_pivots)
    _label_structure_pivots(external_pivots)
    internal_legs = _build_structure_legs(internal_pivots, atr)
    external_legs = _build_structure_legs(external_pivots, atr)
    events = _detect_structure_events(
        external_pivots=external_pivots,
        closes=closes,
        atr=atr,
        break_atr_mult=break_atr_mult,
    )
    candidates = _structure_candidates(
        internal_pivots=internal_pivots,
        raw_external_pivots=raw_external_pivots,
        external_pivots=external_pivots,
        events=events,
        internal_legs=internal_legs,
        external_legs=external_legs,
        zone_width=STRUCTURE_ZONE_WIDTH,
    )
    zones = _build_structure_zones(
        candidates,
        zone_width=STRUCTURE_ZONE_WIDTH,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )
    zones = _make_structure_zones_distinct(
        zones=zones,
        zone_tolerance_pct=zone_tolerance_pct,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )
    zones = _fill_structure_support_staircase_gaps(
        zones=zones,
        raw_external_pivots=raw_external_pivots,
        closes=closes,
        break_atr_mult=break_atr_mult,
        zone_width=STRUCTURE_ZONE_WIDTH,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
        zone_tolerance_pct=zone_tolerance_pct,
    )

    support = sorted([zone for zone in zones if zone["role"] == "support"], key=lambda z: _zone_distance_sort_key(z, current_price))
    resistance = sorted([zone for zone in zones if zone["role"] == "resistance"], key=lambda z: _zone_distance_sort_key(z, current_price))
    active = sorted([zone for zone in zones if zone["role"] == "active"], key=lambda z: abs(z["mid"] - current_price))
    all_zones = support + active + resistance
    return {"support": support, "resistance": resistance, "active": active, "all": all_zones}


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


def _build_structure_legs(pivots: list[StructurePivot], atr: np.ndarray) -> list[StructureLeg]:
    legs: list[StructureLeg] = []
    for previous, current in zip(pivots, pivots[1:]):
        bars = max(1, current.index - previous.index)
        atr_window = atr[previous.index : current.index + 1]
        average_atr = float(np.mean(atr_window)) if len(atr_window) else 0.0
        price_change = current.price - previous.price
        atr_normalized_slope = price_change / average_atr / bars if average_atr > 0 else 0.0
        if previous.price > 0 and current.price > 0:
            log_return_per_bar = float(np.log(current.price / previous.price) / bars)
        else:
            log_return_per_bar = 0.0
        legs.append(
            StructureLeg(
                id=len(legs),
                start_index=previous.index,
                end_index=current.index,
                direction="up" if price_change >= 0 else "down",
                bars=bars,
                price_change=float(price_change),
                atr_normalized_slope=float(atr_normalized_slope),
                log_return_per_bar=log_return_per_bar,
            )
        )
    return legs


def _detect_structure_events(
    external_pivots: list[StructurePivot],
    closes: np.ndarray,
    atr: np.ndarray,
    break_atr_mult: float,
) -> list[StructureEvent]:
    raw_events: list[tuple[str, float, int, int]] = []
    for pivot in external_pivots:
        threshold = max(0.0, float(pivot.atr) * float(break_atr_mult))
        future_closes = closes[pivot.index + 1 :]
        if len(future_closes) == 0:
            continue
        if pivot.kind == "high":
            broken_offsets = np.flatnonzero(future_closes > pivot.price + threshold)
            if len(broken_offsets):
                raw_events.append(("bullish", pivot.price, pivot.index, int(pivot.index + 1 + broken_offsets[0])))
        else:
            broken_offsets = np.flatnonzero(future_closes < pivot.price - threshold)
            if len(broken_offsets):
                raw_events.append(("bearish", pivot.price, pivot.index, int(pivot.index + 1 + broken_offsets[0])))

    trend: Literal["up", "down"] | None = None
    events: list[StructureEvent] = []
    for direction, level, pivot_index, broken_index in sorted(raw_events, key=lambda item: (item[3], item[2])):
        if direction == "bullish":
            event_type: Literal["BOS", "CHOCH"] = "BOS" if trend in (None, "up") else "CHOCH"
            trend = "up"
        else:
            event_type = "BOS" if trend in (None, "down") else "CHOCH"
            trend = "down"
        events.append(
            StructureEvent(
                event=event_type,
                direction=direction,  # type: ignore[arg-type]
                level=float(level),
                pivot_index=int(pivot_index),
                broken_index=int(broken_index),
            )
        )
    return events


def _structure_candidates(
    internal_pivots: list[StructurePivot],
    raw_external_pivots: list[StructurePivot],
    external_pivots: list[StructurePivot],
    events: list[StructureEvent],
    internal_legs: list[StructureLeg],
    external_legs: list[StructureLeg],
    zone_width: float,
) -> list[ZoneStructureCandidate]:
    candidates = [
        _candidate_from_pivot(
            pivot,
            origin="structure_swing_high" if pivot.kind == "high" else "structure_swing_low",
            leg_ids=_leg_ids_for_index(pivot.index, external_legs),
            zone_width=zone_width,
        )
        for pivot in external_pivots
    ]

    external_by_index = {pivot.index: pivot for pivot in external_pivots}
    for event in events:
        pivot = external_by_index.get(event.pivot_index)
        if pivot is None:
            continue
        candidates.append(
            _candidate_from_pivot(
                pivot,
                origin="flipped_resistance" if event.direction == "bullish" else "flipped_support",
                broken_index=event.broken_index,
                leg_ids=_leg_ids_for_index(pivot.index, external_legs),
                zone_width=zone_width,
            )
        )
    candidates.extend(
        _support_floor_candidates(
            raw_external_pivots=raw_external_pivots,
            external_pivots=external_pivots,
            external_legs=external_legs,
            zone_width=zone_width,
        )
    )
    return sorted(candidates, key=lambda candidate: (candidate.price, candidate.index, candidate.origin))


def _candidate_from_pivot(
    pivot: StructurePivot,
    origin: str,
    zone_width: float,
    broken_index: int | None = None,
    leg_ids: tuple[int, ...] = (),
) -> ZoneStructureCandidate:
    price = float(pivot.body_price)
    return ZoneStructureCandidate(
        price=price,
        index=int(pivot.index),
        origin=origin,
        zone_width=float(zone_width),
        structure_role=pivot.structure_role or ("SH" if pivot.kind == "high" else "SL"),
        term=pivot.term,
        broken_index=broken_index,
        leg_ids=leg_ids,
    )


def _support_floor_candidates(
    raw_external_pivots: list[StructurePivot],
    external_pivots: list[StructurePivot],
    external_legs: list[StructureLeg],
    zone_width: float,
) -> list[ZoneStructureCandidate]:
    floor_candidates: list[ZoneStructureCandidate] = []
    retest_tolerance = float(zone_width) * STRUCTURE_SUPPORT_FLOOR_RETEST_WIDTH_MULT
    prominent_lows = [
        pivot
        for pivot in external_pivots
        if pivot.kind == "low" and float(pivot.body_price) - float(pivot.price) >= float(zone_width)
    ]
    raw_lows = [pivot for pivot in raw_external_pivots if pivot.kind == "low"]

    for prominent_low in prominent_lows:
        floor_price = float(prominent_low.price)
        floor_candidates.append(
            _candidate_from_support_floor(
                pivot=prominent_low,
                price=floor_price,
                origin="structure_swing_low_wick",
                zone_width=zone_width,
                leg_ids=_leg_ids_for_index(prominent_low.index, external_legs),
            )
        )
        for raw_low in raw_lows:
            if raw_low.index == prominent_low.index:
                continue
            body_floor = float(raw_low.body_price)
            if abs(body_floor - floor_price) > retest_tolerance:
                continue
            floor_candidates.append(
                _candidate_from_support_floor(
                    pivot=raw_low,
                    price=body_floor,
                    origin="structure_swing_low_body_floor",
                    zone_width=zone_width,
                    leg_ids=_leg_ids_for_index(raw_low.index, external_legs),
                )
            )

    return floor_candidates


def _stair_step_support_candidates(
    raw_external_pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    lower_zone: dict[str, Any],
    upper_zone: dict[str, Any],
    current_price: float,
    buffer_pct: float,
) -> list[ZoneStructureCandidate]:
    lower_high = float(lower_zone["high"])
    upper_low = float(upper_zone["low"])
    support_ceiling = float(current_price) * (1.0 - float(buffer_pct))
    candidates: list[ZoneStructureCandidate] = []

    for pivot in raw_external_pivots:
        if pivot.kind != "high":
            continue
        price = float(pivot.body_price)
        if price - float(zone_width) <= lower_high or price >= upper_low or price >= support_ceiling:
            continue
        if not _raw_high_is_confirmed_broken(pivot, closes, break_atr_mult):
            continue
        candidates.append(
            ZoneStructureCandidate(
                price=price,
                index=int(pivot.index),
                origin="stair_step_flipped_resistance",
                zone_width=float(zone_width),
                structure_role=pivot.structure_role or "H",
                term=pivot.term,
            )
        )

    return candidates


def _raw_high_is_confirmed_broken(
    pivot: StructurePivot,
    closes: np.ndarray,
    break_atr_mult: float,
) -> bool:
    threshold = max(0.0, float(pivot.atr) * float(break_atr_mult))
    future_closes = closes[pivot.index + 1 :]
    return bool(len(future_closes) and np.any(future_closes > float(pivot.price) + threshold))


def _candidate_from_support_floor(
    pivot: StructurePivot,
    price: float,
    origin: str,
    zone_width: float,
    leg_ids: tuple[int, ...] = (),
) -> ZoneStructureCandidate:
    return ZoneStructureCandidate(
        price=float(price),
        index=int(pivot.index),
        origin=origin,
        zone_width=float(zone_width),
        structure_role=pivot.structure_role or "SL",
        term=pivot.term,
        bounds_style="support_floor",
        leg_ids=leg_ids,
    )


def _leg_ids_for_index(index: int, legs: list[StructureLeg]) -> tuple[int, ...]:
    return tuple(leg.id for leg in legs if leg.start_index <= index <= leg.end_index)


def _build_structure_zones(
    candidates: list[ZoneStructureCandidate],
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    clusters: list[list[ZoneStructureCandidate]] = []
    for candidate in sorted(candidates, key=lambda item: item.price):
        placed = False
        for cluster in clusters:
            if _structure_candidate_matches_cluster(candidate, cluster, zone_width):
                cluster.append(candidate)
                placed = True
                break
        if not placed:
            clusters.append([candidate])

    zones: list[dict[str, Any]] = []
    for cluster in clusters:
        unique_touch_keys = {(item.index, item.origin) for item in cluster}
        if len(unique_touch_keys) < min_touches:
            continue
        zones.append(_zone_from_structure_cluster(cluster, zone_width, current_price, buffer_pct))
    return _consolidate_structure_zones(zones, zone_width, current_price, buffer_pct)


def _structure_candidate_matches_cluster(
    candidate: ZoneStructureCandidate,
    cluster: list[ZoneStructureCandidate],
    zone_width: float,
) -> bool:
    if candidate.bounds_style != cluster[0].bounds_style:
        return False

    candidate_bias = _structure_candidate_bias(candidate)
    cluster_bias = _structure_candidate_group_bias(cluster)
    if candidate_bias != "mixed" and cluster_bias != "mixed" and candidate_bias != cluster_bias:
        return False

    prices = [item.price for item in cluster] + [candidate.price]
    return max(prices) - min(prices) <= zone_width


def _zone_from_structure_cluster(
    cluster: list[ZoneStructureCandidate],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> dict[str, Any]:
    cluster = sorted(cluster, key=lambda item: (item.price, item.index, item.origin))
    prices = [float(item.price) for item in cluster]
    indexes = [int(item.index) for item in cluster]
    zone_width = float(zone_width)
    price_state = _classify_price_cluster_role(prices, current_price, buffer_pct)
    structure_bias = _structure_cluster_bias(cluster)
    role = _resolve_structure_zone_role(price_state, structure_bias)
    bounds_role = _structure_bounds_role(price_state, structure_bias)
    if _cluster_uses_support_floor_bounds(cluster):
        low, high = _fixed_support_floor_zone_bounds(prices, zone_width)
    else:
        low, high = _fixed_structure_zone_bounds(prices, bounds_role, zone_width)
    mid = (low + high) / 2.0
    width = high - low
    width_pct = width / mid * 100.0 if mid else 0.0
    broken_indexes = [item.broken_index for item in cluster if item.broken_index is not None]
    leg_ids = sorted({leg_id for item in cluster for leg_id in item.leg_ids})
    origin = _structure_cluster_origin(cluster, role)
    source_closes = [float(item.price) for item in cluster]
    source_indexes = [int(item.index) for item in cluster]
    external_count = sum(1 for item in cluster if item.term == "external")
    flipped_count = sum(1 for item in cluster if item.origin.startswith("flipped_"))
    score = len(cluster) + external_count + flipped_count
    return {
        "origin": origin,
        "role": role,
        "low": float(low),
        "high": float(high),
        "mid": float((low + high) / 2.0),
        "width": float(width),
        "width_pct": float(width_pct),
        "touches": len(cluster),
        "source_closes": source_closes,
        "source_indexes": source_indexes,
        "score": float(score),
        "structure_role": _structure_cluster_role(cluster),
        "structure_bias": structure_bias,
        "price_state": price_state,
        "last_touch_index": max(indexes),
        "broken_index": max(broken_indexes) if broken_indexes else None,
        "zone_width": zone_width,
        "leg_ids": leg_ids,
    }

# Merges nearby zones into “macro” zones
def _consolidate_structure_zones(
    zones: list[dict[str, Any]],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    macro_zones: list[dict[str, Any]] = []
    for role in ("support", "active", "resistance"):
        group: list[dict[str, Any]] = []
        for zone in sorted([item for item in zones if item["role"] == role], key=lambda item: item["low"]):
            if not group:
                group = [zone]
                continue
            if _structure_zones_can_share_macro_group(group, zone):
                group.append(zone)
            else:
                macro_zones.append(_combine_structure_macro_group(group, zone_width, current_price, buffer_pct))
                group = [zone]
        if group:
            macro_zones.append(_combine_structure_macro_group(group, zone_width, current_price, buffer_pct))
    return _suppress_nearby_structure_zones(macro_zones)

# Checks if a zone can be added to an existing macro group
def _structure_zones_can_share_macro_group(group: list[dict[str, Any]], zone: dict[str, Any]) -> bool:
    gap = float(zone["low"]) - float(group[-1]["high"]) # compares the new zone’s low to the last zone in the group’s high
    if gap > STRUCTURE_MACRO_GAP:
        return False
    source_prices = [float(price) for item in group for price in item["source_closes"]] + [
        float(price) for price in zone["source_closes"]
    ]
    return max(source_prices) - min(source_prices) <= STRUCTURE_MACRO_MAX_SOURCE_SPAN


def _combine_structure_macro_group(
    group: list[dict[str, Any]],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> dict[str, Any]:
    if len(group) == 1:
        return dict(group[0])

    source_closes = [float(price) for zone in group for price in zone["source_closes"]]
    source_indexes = [int(index) for zone in group for index in zone["source_indexes"]]
    price_state = _classify_price_cluster_role(source_closes, current_price, buffer_pct)
    structure_bias = _combine_structure_biases([_coerce_structure_bias(zone.get("structure_bias", "mixed")) for zone in group])
    role = _resolve_structure_zone_role(price_state, structure_bias)
    bounds_role = _structure_bounds_role(price_state, structure_bias)
    low, high = _fixed_structure_zone_bounds(source_closes, bounds_role, zone_width)
    mid = (low + high) / 2.0
    width_pct = zone_width / mid * 100.0 if mid else 0.0
    broken_indexes = [zone.get("broken_index") for zone in group if zone.get("broken_index") is not None]
    leg_ids = sorted({int(leg_id) for zone in group for leg_id in zone.get("leg_ids", [])})
    origins = {str(zone["origin"]) for zone in group}
    structure_roles = {str(zone.get("structure_role", "unknown")) for zone in group}
    return {
        "origin": _structure_origin_from_origins(origins, role),
        "role": role,
        "low": float(low),
        "high": float(high),
        "mid": float(mid),
        "width": float(zone_width),
        "width_pct": float(width_pct),
        "touches": len(source_closes),
        "source_closes": source_closes,
        "source_indexes": source_indexes,
        "score": float(sum(float(zone.get("score", 0.0)) for zone in group)),
        "structure_role": next(iter(structure_roles)) if len(structure_roles) == 1 else "mixed",
        "structure_bias": structure_bias,
        "price_state": price_state,
        "last_touch_index": max(source_indexes),
        "broken_index": max(broken_indexes) if broken_indexes else None,
        "zone_width": float(zone_width),
        "leg_ids": leg_ids,
    }


def _resolve_structure_zone_role(price_state: ZoneRole, structure_bias: StructureBias) -> ZoneRole:
    if structure_bias in ("support", "resistance"):
        return structure_bias
    return price_state


def _structure_bounds_role(price_state: ZoneRole, structure_bias: StructureBias) -> ZoneRole:
    if structure_bias in ("support", "resistance"):
        return structure_bias
    return price_state

# scores the cluster from pivot origins (swing lows, swing highs, flipped levels)
def _structure_cluster_bias(cluster: list[ZoneStructureCandidate]) -> StructureBias:
    support_score = 0
    resistance_score = 0
    for item in cluster:
        support_weight, resistance_weight = _structure_candidate_bias_weights(item)
        support_score += support_weight
        resistance_score += resistance_weight
    return _structure_bias_from_scores(support_score, resistance_score)


def _structure_candidate_bias(candidate: ZoneStructureCandidate) -> StructureBias:
    support_score, resistance_score = _structure_candidate_bias_weights(candidate)
    return _structure_bias_from_scores(support_score, resistance_score)


def _structure_candidate_group_bias(cluster: list[ZoneStructureCandidate]) -> StructureBias:
    support_score = 0
    resistance_score = 0
    for item in cluster:
        item_support_score, item_resistance_score = _structure_candidate_bias_weights(item)
        support_score += item_support_score
        resistance_score += item_resistance_score
    return _structure_bias_from_scores(support_score, resistance_score)


def _structure_candidate_bias_weights(candidate: ZoneStructureCandidate) -> tuple[int, int]:
    if candidate.origin in ("structure_swing_low_wick", "structure_swing_low_body_floor"):
        return (1, 0)
    if candidate.origin == "stair_step_flipped_resistance":
        return (2, 0)
    if candidate.origin == "flipped_resistance":
        return (2, 0)
    if candidate.origin == "flipped_support":
        return (0, 2)
    if candidate.origin == "structure_swing_low":
        return (1, 0)
    if candidate.origin == "structure_swing_high":
        return (0, 1)
    return (0, 0)

# counts how many zones said support vs resistance
def _combine_structure_biases(biases: list[StructureBias]) -> StructureBias:
    support_score = sum(1 for bias in biases if bias == "support")
    resistance_score = sum(1 for bias in biases if bias == "resistance")
    return _structure_bias_from_scores(support_score, resistance_score)

# makes sure the bias is valid
def _coerce_structure_bias(value: Any) -> StructureBias:
    if value in ("support", "resistance", "mixed"):
        return value
    return "mixed"

# pick the most dominant bias
def _structure_bias_from_scores(support_score: int, resistance_score: int) -> StructureBias:
    if support_score > resistance_score:
        return "support"
    if resistance_score > support_score:
        return "resistance"
    return "mixed"


def _classify_price_cluster_role(prices: list[float], current_price: float, buffer_pct: float) -> ZoneRole:
    if max(prices) < current_price * (1 - buffer_pct):
        return "support"
    if min(prices) > current_price * (1 + buffer_pct):
        return "resistance"
    return "active"


def _fixed_structure_zone_bounds(prices: list[float], role: ZoneRole, zone_width: float) -> tuple[float, float]:
    if role == "support":
        high = _support_base_anchor(prices)
        return high - zone_width, high
    if role == "resistance":
        low = _resistance_base_anchor(prices)
        return low, low + zone_width
    mid = (min(prices) + max(prices)) / 2.0
    return mid - zone_width / 2.0, mid + zone_width / 2.0


def _fixed_support_floor_zone_bounds(prices: list[float], zone_width: float) -> tuple[float, float]:
    low = _support_floor_anchor(prices)
    return low, low + float(zone_width)


# prices from swing pivots (the source_closes for the zone)
def _support_base_anchor(prices: list[float]) -> float:
    sorted_prices = sorted(float(price) for price in prices) # Sort prices low → high
    if len(sorted_prices) <= 10:
        return max(sorted_prices)
    index = int(np.floor((len(sorted_prices) - 1) * 0.10)) # More touches: use the price at the 10th percentile
    return sorted_prices[index]


def _support_floor_anchor(prices: list[float]) -> float:
    sorted_prices = sorted(float(price) for price in prices)
    if len(sorted_prices) <= 10:
        return min(sorted_prices)
    index = int(np.floor((len(sorted_prices) - 1) * 0.10))
    return sorted_prices[index]


def _resistance_base_anchor(prices: list[float]) -> float:
    sorted_prices = sorted(float(price) for price in prices)
    if len(sorted_prices) <= 3:
        return min(sorted_prices)
    index = int(np.ceil((len(sorted_prices) - 1) * 0.90))
    return sorted_prices[index]


def _structure_origin_from_origins(origins: set[str], role: ZoneRole) -> str:
    if origins and all(origin in ("structure_swing_low_wick", "structure_swing_low_body_floor") for origin in origins):
        return "structure_support_floor"
    if len(origins) == 1:
        return next(iter(origins))
    if role == "resistance" and "flipped_support" in origins:
        return "flipped_support"
    if role == "support" and "flipped_resistance" in origins:
        return "flipped_resistance"
    if all(origin.startswith("structure_") for origin in origins):
        return "mixed_structure"
    return "mixed_structure"

# Thins zones that are still too close, after macro merging
def _suppress_nearby_structure_zones(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for role in ("support", "active", "resistance"):
        role_zones = [zone for zone in zones if zone["role"] == role]
        role_kept: list[dict[str, Any]] = []
        for zone in sorted(role_zones, key=_structure_zone_rank, reverse=True):
            if all(abs(float(zone["mid"]) - float(previous["mid"])) >= STRUCTURE_IMPORTANT_ZONE_SPACING for previous in role_kept):
                role_kept.append(dict(zone))
        kept.extend(role_kept)
    return kept


def _structure_zone_rank(zone: dict[str, Any]) -> tuple[float, int, float]:
    return (float(zone.get("score", 0.0)), int(zone["touches"]), -float(zone["width_pct"]))


def _structure_cluster_origin(cluster: list[ZoneStructureCandidate], role: ZoneRole) -> str:
    origins = {item.origin for item in cluster}
    return _structure_origin_from_origins(origins, role)


def _structure_cluster_role(cluster: list[ZoneStructureCandidate]) -> str:
    roles = [item.structure_role for item in cluster if item.structure_role]
    if not roles:
        return "unknown"
    if len(set(roles)) == 1:
        return roles[0]
    return "mixed"


def _cluster_uses_support_floor_bounds(cluster: list[ZoneStructureCandidate]) -> bool:
    return all(item.bounds_style == "support_floor" for item in cluster)


def _fill_structure_support_staircase_gaps(
    zones: list[dict[str, Any]],
    raw_external_pivots: list[StructurePivot],
    closes: np.ndarray,
    break_atr_mult: float,
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
    zone_tolerance_pct: float,
) -> list[dict[str, Any]]:
    filled_zones = [dict(zone) for zone in zones]
    for _ in range(STRUCTURE_STAIR_STEP_MAX_INSERTIONS):
        gap_fill = _best_structure_support_staircase_gap_fill(
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
        filled_zones = _make_structure_zones_distinct(
            filled_zones,
            zone_tolerance_pct=zone_tolerance_pct,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
    return filled_zones


def _best_structure_support_staircase_gap_fill(
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
        [
            zone
            for zone in zones
            if zone["role"] == "support" and _coerce_zone_price_state(zone, current_price, buffer_pct) == "support"
        ],
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
        candidate_zones = _build_structure_zones(
            candidates,
            zone_width=zone_width,
            min_touches=min_touches,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        candidate_zones = [
            zone
            for zone in candidate_zones
            if zone["role"] == "support"
            and float(zone["low"]) > float(lower_zone["high"])
            and float(zone["high"]) < float(upper_zone["low"])
        ]
        if not candidate_zones:
            continue

        selected = min(candidate_zones, key=lambda zone: _stair_step_gap_rank(zone, lower_zone, upper_zone))
        rank = _stair_step_gap_rank(selected, lower_zone, upper_zone)
        if best_rank is None or rank < best_rank:
            best_zone = selected
            best_rank = rank

    return best_zone


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


def _coerce_zone_price_state(zone: dict[str, Any], current_price: float, buffer_pct: float) -> ZoneRole:
    value = zone.get("price_state")
    if value in ("support", "active", "resistance"):
        return value
    return _classify_role(
        low=float(zone["low"]),
        high=float(zone["high"]),
        current_price=current_price,
        buffer_pct=buffer_pct,
    )


def _make_structure_zones_distinct(
    zones: list[dict[str, Any]],
    zone_tolerance_pct: float,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    normalized_zones: list[dict[str, Any]] = []
    for zone in zones:
        zone = dict(zone)
        price_state = _classify_role(
            low=float(zone["low"]),
            high=float(zone["high"]),
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        structure_bias = _coerce_structure_bias(zone.get("structure_bias", "mixed"))
        zone["price_state"] = price_state
        zone["role"] = _resolve_structure_zone_role(price_state, structure_bias)
        normalized_zones.append(zone)

    distinct: list[dict[str, Any]] = []
    for role in ("support", "active", "resistance"):
        role_zones = sorted([zone for zone in normalized_zones if zone["role"] == role], key=lambda item: item["low"])
        for zone in role_zones:
            zone = dict(zone)
            if not distinct or distinct[-1]["role"] != zone["role"]:
                distinct.append(zone)
                continue
            previous = distinct[-1]
            if _zones_overlap(previous, zone):
                distinct[-1] = _prefer_structure_zone(previous, zone)
            else:
                distinct.append(zone)
    return distinct


def _prefer_structure_zone(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_score = (float(first.get("score", 0.0)), int(first["touches"]), -float(first["width_pct"]))
    second_score = (float(second.get("score", 0.0)), int(second["touches"]), -float(second["width_pct"]))
    return dict(first if first_score >= second_score else second)


def _zones_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return max(first["low"], second["low"]) <= min(first["high"], second["high"])


def _classify_role(low: float, high: float, current_price: float, buffer_pct: float) -> ZoneRole:
    if high < current_price * (1 - buffer_pct):
        return "support"
    if low > current_price * (1 + buffer_pct):
        return "resistance"
    return "active"
