from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema


ZoneRole = Literal["support", "resistance", "active"]


@dataclass
class Candidate:
    price: float
    index: int
    origin: str


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
    closes_and_indexes = list(zip(first["source_closes"], first["source_indexes"], strict=True)) + list(
        zip(second["source_closes"], second["source_indexes"], strict=True)
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
