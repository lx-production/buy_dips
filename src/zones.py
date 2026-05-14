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
    return zones


def _within_max_width(cluster: list[Candidate], candidate: Candidate, max_zone_width_pct: float) -> bool:
    prices = [item.price for item in cluster] + [candidate.price]
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
