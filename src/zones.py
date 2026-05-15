from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema


ZoneRole = Literal["support", "resistance", "active"]
ZoneAlgorithm = Literal["pure_close", "structure_v1"]
PivotKind = Literal["high", "low"]
SwingTerm = Literal["internal", "external"]
STRUCTURE_ZONE_WIDTH = 500.0
STRUCTURE_MACRO_GAP = 300.0
STRUCTURE_MACRO_MAX_SOURCE_SPAN = 2000.0
STRUCTURE_IMPORTANT_ZONE_SPACING = 1600.0


@dataclass
class Candidate:
    price: float
    index: int
    origin: str


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
class StructureCandidate:
    price: float
    index: int
    origin: str
    zone_width: float
    structure_role: str
    term: SwingTerm
    broken_index: int | None = None
    leg_ids: tuple[int, ...] = ()


def detect_support_resistance_zones(
    df: pd.DataFrame,
    algorithm: ZoneAlgorithm = "pure_close",
    swing_order: int = 5,
    lookahead: int = 6,
    min_reversal_pct: float = 0.008,
    zone_tolerance_pct: float = 0.0045,
    min_touches: int = 2,
    max_zone_width_pct: float = 0.018,
    current_price: float | None = None,
    buffer_pct: float = 0.0015,
    internal_swing_order: int = 2,
    external_swing_order: int = 5,
    atr_period: int = 14,
    break_atr_mult: float = 0.2,
) -> dict[str, list[dict[str, Any]]]:
    if algorithm == "pure_close":
        return detect_support_resistance_zones_pure_close(
            df,
            swing_order=swing_order,
            lookahead=lookahead,
            min_reversal_pct=min_reversal_pct,
            zone_tolerance_pct=zone_tolerance_pct,
            min_touches=min_touches,
            max_zone_width_pct=max_zone_width_pct,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
    if algorithm == "structure_v1":
        return detect_support_resistance_zones_structure_v1(
            df,
            internal_swing_order=internal_swing_order,
            external_swing_order=external_swing_order,
            atr_period=atr_period,
            zone_tolerance_pct=zone_tolerance_pct,
            min_touches=min_touches,
            max_zone_width_pct=max_zone_width_pct,
            current_price=current_price,
            buffer_pct=buffer_pct,
            break_atr_mult=break_atr_mult,
        )
    raise ValueError(f"Unsupported zone algorithm: {algorithm}")


def detect_support_resistance_zones_pure_close(
    df: pd.DataFrame,
    swing_order: int = 5,
    lookahead: int = 6,
    min_reversal_pct: float = 0.008,
    zone_tolerance_pct: float = 0.0045,
    min_touches: int = 2,
    max_zone_width_pct: float = 0.018,
    current_price: float | None = None,
    buffer_pct: float = 0.0015,
) -> dict[str, list[dict[str, Any]]]:
    empty = {"support": [], "resistance": [], "active": [], "all": []}
    if df is None or "close" not in df.columns:
        return empty

    closes = pd.to_numeric(df["close"], errors="coerce").dropna().to_numpy(dtype=float)
    if len(closes) < (swing_order * 2 + lookahead + 1):
        return empty

    if current_price is None:
        current_price = float(closes[-1])

    support_candidates = _validated_candidates(
        closes=closes,
        swing_order=swing_order,
        lookahead=lookahead,
        min_reversal_pct=min_reversal_pct,
        comparator=np.less,
        origin="support_pivot",
    )
    resistance_candidates = _validated_candidates(
        closes=closes,
        swing_order=swing_order,
        lookahead=lookahead,
        min_reversal_pct=min_reversal_pct,
        comparator=np.greater,
        origin="resistance_pivot",
    )

    zones = []
    zones.extend(
        _build_zones(
            support_candidates,
            zone_tolerance_pct=zone_tolerance_pct,
            min_touches=min_touches,
            max_zone_width_pct=max_zone_width_pct,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
    )
    zones.extend(
        _build_zones(
            resistance_candidates,
            zone_tolerance_pct=zone_tolerance_pct,
            min_touches=min_touches,
            max_zone_width_pct=max_zone_width_pct,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
    )
    zones = _make_zones_distinct(
        zones=zones,
        max_zone_width_pct=max_zone_width_pct,
        zone_tolerance_pct=zone_tolerance_pct,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )

    support = sorted([zone for zone in zones if zone["role"] == "support"], key=lambda z: z["high"], reverse=True)
    resistance = sorted([zone for zone in zones if zone["role"] == "resistance"], key=lambda z: z["low"])
    active = sorted([zone for zone in zones if zone["role"] == "active"], key=lambda z: abs(z["mid"] - current_price))
    all_zones = support + active + resistance
    return {"support": support, "resistance": resistance, "active": active, "all": all_zones}


def detect_support_resistance_zones_structure_v1(
    df: pd.DataFrame,
    internal_swing_order: int = 2,
    external_swing_order: int = 5,
    atr_period: int = 14,
    zone_tolerance_pct: float = 0.0045,
    min_touches: int = 2,
    max_zone_width_pct: float = 0.018,
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
    external_pivots = _find_structure_pivots(ohlc, external_swing_order, atr, "external")
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

    support = sorted([zone for zone in zones if zone["role"] == "support"], key=lambda z: z["high"], reverse=True)
    resistance = sorted([zone for zone in zones if zone["role"] == "resistance"], key=lambda z: z["low"])
    active = sorted([zone for zone in zones if zone["role"] == "active"], key=lambda z: abs(z["mid"] - current_price))
    all_zones = support + active + resistance
    return {"support": support, "resistance": resistance, "active": active, "all": all_zones}


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
    external_pivots: list[StructurePivot],
    events: list[StructureEvent],
    internal_legs: list[StructureLeg],
    external_legs: list[StructureLeg],
    zone_width: float,
) -> list[StructureCandidate]:
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
    return sorted(candidates, key=lambda candidate: (candidate.price, candidate.index, candidate.origin))


def _candidate_from_pivot(
    pivot: StructurePivot,
    origin: str,
    zone_width: float,
    broken_index: int | None = None,
    leg_ids: tuple[int, ...] = (),
) -> StructureCandidate:
    price = float(pivot.body_price)
    return StructureCandidate(
        price=price,
        index=int(pivot.index),
        origin=origin,
        zone_width=float(zone_width),
        structure_role=pivot.structure_role or ("SH" if pivot.kind == "high" else "SL"),
        term=pivot.term,
        broken_index=broken_index,
        leg_ids=leg_ids,
    )


def _leg_ids_for_index(index: int, legs: list[StructureLeg]) -> tuple[int, ...]:
    return tuple(leg.id for leg in legs if leg.start_index <= index <= leg.end_index)


def _build_structure_zones(
    candidates: list[StructureCandidate],
    zone_width: float,
    min_touches: int,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    clusters: list[list[StructureCandidate]] = []
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
    candidate: StructureCandidate,
    cluster: list[StructureCandidate],
    zone_width: float,
) -> bool:
    prices = [item.price for item in cluster] + [candidate.price]
    return max(prices) - min(prices) <= zone_width


def _zone_from_structure_cluster(
    cluster: list[StructureCandidate],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> dict[str, Any]:
    cluster = sorted(cluster, key=lambda item: (item.price, item.index, item.origin))
    prices = [float(item.price) for item in cluster]
    indexes = [int(item.index) for item in cluster]
    zone_width = float(zone_width)
    role = _classify_price_cluster_role(prices, current_price, buffer_pct)
    low, high = _fixed_structure_zone_bounds(prices, role, zone_width)
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
        "last_touch_index": max(indexes),
        "broken_index": max(broken_indexes) if broken_indexes else None,
        "zone_width": zone_width,
        "leg_ids": leg_ids,
    }


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


def _structure_zones_can_share_macro_group(group: list[dict[str, Any]], zone: dict[str, Any]) -> bool:
    gap = float(zone["low"]) - float(group[-1]["high"])
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
    role = _classify_price_cluster_role(source_closes, current_price, buffer_pct)
    low, high = _fixed_structure_zone_bounds(source_closes, role, zone_width)
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
        "last_touch_index": max(source_indexes),
        "broken_index": max(broken_indexes) if broken_indexes else None,
        "zone_width": float(zone_width),
        "leg_ids": leg_ids,
    }


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


def _support_base_anchor(prices: list[float]) -> float:
    sorted_prices = sorted(float(price) for price in prices)
    if len(sorted_prices) <= 3:
        return max(sorted_prices)
    index = int(np.floor((len(sorted_prices) - 1) * 0.10))
    return sorted_prices[index]


def _resistance_base_anchor(prices: list[float]) -> float:
    sorted_prices = sorted(float(price) for price in prices)
    if len(sorted_prices) <= 3:
        return min(sorted_prices)
    index = int(np.ceil((len(sorted_prices) - 1) * 0.90))
    return sorted_prices[index]


def _structure_origin_from_origins(origins: set[str], role: ZoneRole) -> str:
    if len(origins) == 1:
        return next(iter(origins))
    if role == "resistance" and "flipped_support" in origins:
        return "flipped_support"
    if role == "support" and "flipped_resistance" in origins:
        return "flipped_resistance"
    if all(origin.startswith("structure_") for origin in origins):
        return "mixed_structure"
    return "mixed_structure"


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


def _structure_cluster_origin(cluster: list[StructureCandidate], role: ZoneRole) -> str:
    origins = {item.origin for item in cluster}
    return _structure_origin_from_origins(origins, role)


def _structure_cluster_role(cluster: list[StructureCandidate]) -> str:
    roles = [item.structure_role for item in cluster if item.structure_role]
    if not roles:
        return "unknown"
    if len(set(roles)) == 1:
        return roles[0]
    return "mixed"


def _make_structure_zones_distinct(
    zones: list[dict[str, Any]],
    zone_tolerance_pct: float,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    distinct: list[dict[str, Any]] = []
    for role in ("support", "active", "resistance"):
        role_zones = sorted([zone for zone in zones if zone["role"] == role], key=lambda item: item["low"])
        for zone in role_zones:
            zone = dict(zone)
            zone["role"] = _classify_role(
                low=float(zone["low"]),
                high=float(zone["high"]),
                current_price=current_price,
                buffer_pct=buffer_pct,
            )
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


def _validated_candidates(
    closes: np.ndarray,
    swing_order: int,
    lookahead: int,
    min_reversal_pct: float,
    comparator: Any,
    origin: str,
) -> list[Candidate]:
    pivot_indexes = argrelextrema(closes, comparator, order=swing_order)[0]
    candidates: list[Candidate] = []
    for idx in pivot_indexes:
        future = closes[idx + 1 : idx + 1 + lookahead]
        if len(future) == 0:
            continue
        pivot_close = float(closes[idx])
        if origin == "support_pivot":
            reversal = (float(np.max(future)) - pivot_close) / pivot_close
            if reversal >= min_reversal_pct:
                candidates.append(Candidate(price=pivot_close, index=int(idx), origin=origin))
        else:
            reversal = (pivot_close - float(np.min(future))) / pivot_close
            if reversal >= min_reversal_pct:
                candidates.append(Candidate(price=pivot_close, index=int(idx), origin=origin))
    return candidates


def _build_zones(
    candidates: list[Candidate],
    zone_tolerance_pct: float,
    min_touches: int,
    max_zone_width_pct: float,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    clusters: list[list[Candidate]] = []
    for candidate in sorted(candidates, key=lambda item: item.price):
        placed = False
        for cluster in clusters:
            median = float(np.median([item.price for item in cluster]))
            if abs(candidate.price - median) / median <= zone_tolerance_pct and _within_max_width(
                cluster, candidate, max_zone_width_pct
            ):
                cluster.append(candidate)
                placed = True
                break
        if not placed:
            clusters.append([candidate])

    zones: list[dict[str, Any]] = []
    for cluster in clusters:
        if len(cluster) < min_touches:
            continue
        prices = [float(item.price) for item in cluster]
        indexes = [int(item.index) for item in cluster]
        low = min(prices)
        high = max(prices)
        mid = (low + high) / 2.0
        width = high - low
        width_pct = width / mid * 100.0 if mid else 0.0
        if mid and (width / mid) > max_zone_width_pct:
            continue
        role = _classify_role(low=low, high=high, current_price=current_price, buffer_pct=buffer_pct)
        zones.append(
            {
                "origin": cluster[0].origin,
                "role": role,
                "low": low,
                "high": high,
                "mid": mid,
                "width": width,
                "width_pct": width_pct,
                "touches": len(cluster),
                "source_closes": prices,
                "source_indexes": indexes,
            }
        )
    return _merge_nearby_zones(zones, max_zone_width_pct, current_price, buffer_pct)


def _make_zones_distinct(
    zones: list[dict[str, Any]],
    max_zone_width_pct: float,
    zone_tolerance_pct: float,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    distinct: list[dict[str, Any]] = []
    for role in ("support", "active", "resistance"):
        role_zones = sorted([zone for zone in zones if zone["role"] == role], key=lambda item: item["low"])
        distinct.extend(_merge_nearby_zones(role_zones, max_zone_width_pct, current_price, buffer_pct))

    compacted: list[dict[str, Any]] = []
    for role in ("support", "active", "resistance"):
        role_zones = sorted([zone for zone in distinct if zone["role"] == role], key=lambda item: item["low"])
        for zone in role_zones:
            if not compacted or compacted[-1]["role"] != role:
                compacted.append(dict(zone))
                continue
            previous = compacted[-1]
            if _zones_overlap(previous, zone) or _zone_gap_pct(previous, zone) <= zone_tolerance_pct:
                compacted[-1] = _prefer_zone(previous, zone)
            else:
                compacted.append(dict(zone))
    return compacted


def _merge_nearby_zones(
    zones: list[dict[str, Any]],
    max_zone_width_pct: float,
    current_price: float,
    buffer_pct: float,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for zone in sorted(zones, key=lambda item: item["low"]):
        if not merged:
            merged.append(dict(zone))
            continue

        if _prices_within_max_width([merged[-1]["low"], merged[-1]["high"], zone["low"], zone["high"]], max_zone_width_pct):
            merged[-1] = _combine_zones(merged[-1], zone, current_price, buffer_pct)
        else:
            merged.append(dict(zone))
    return merged


def _combine_zones(
    first: dict[str, Any],
    second: dict[str, Any],
    current_price: float,
    buffer_pct: float,
) -> dict[str, Any]:
    closes_and_indexes = list(zip(first["source_closes"], first["source_indexes"])) + list(
        zip(second["source_closes"], second["source_indexes"])
    )
    closes_and_indexes.sort(key=lambda item: item[0])
    source_closes = [float(item[0]) for item in closes_and_indexes]
    source_indexes = [int(item[1]) for item in closes_and_indexes]
    low = min(source_closes)
    high = max(source_closes)
    mid = (low + high) / 2.0
    width = high - low
    width_pct = width / mid * 100.0 if mid else 0.0
    return {
        "origin": first["origin"] if first["origin"] == second["origin"] else "mixed_pivot",
        "role": _classify_role(low=low, high=high, current_price=current_price, buffer_pct=buffer_pct),
        "low": low,
        "high": high,
        "mid": mid,
        "width": width,
        "width_pct": width_pct,
        "touches": len(source_closes),
        "source_closes": source_closes,
        "source_indexes": source_indexes,
    }


def _zones_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return max(first["low"], second["low"]) <= min(first["high"], second["high"])


def _zone_gap_pct(first: dict[str, Any], second: dict[str, Any]) -> float:
    gap = max(0.0, float(second["low"]) - float(first["high"]))
    mid = (float(first["mid"]) + float(second["mid"])) / 2.0
    if mid == 0:
        return float("inf")
    return gap / mid


def _prefer_zone(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_score = (int(first["touches"]), -float(first["width_pct"]))
    second_score = (int(second["touches"]), -float(second["width_pct"]))
    return dict(first if first_score >= second_score else second)


def _within_max_width(cluster: list[Candidate], candidate: Candidate, max_zone_width_pct: float) -> bool:
    return _prices_within_max_width([item.price for item in cluster] + [candidate.price], max_zone_width_pct)


def _prices_within_max_width(prices: list[float], max_zone_width_pct: float) -> bool:
    low = min(prices)
    high = max(prices)
    mid = (low + high) / 2.0
    if mid == 0:
        return False
    return (high - low) / mid <= max_zone_width_pct


def _classify_role(low: float, high: float, current_price: float, buffer_pct: float) -> ZoneRole:
    if high < current_price * (1 - buffer_pct):
        return "support"
    if low > current_price * (1 + buffer_pct):
        return "resistance"
    return "active"
