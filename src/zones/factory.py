from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .state import _classify_price_state
from .types import BoundsStyle, SupportCandidate


Zone = dict[str, Any]


# Build one support zone dict from a cluster of candidates (bounds, score, metadata).
def _zone_from_support_cluster(
    cluster: list[SupportCandidate],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> Zone:
    cluster = sorted(cluster, key=lambda item: (item.price, item.index, item.origin))
    source_closes = [float(item.price) for item in cluster]
    source_indexes = [int(item.index) for item in cluster]
    bounds_style = cluster[0].bounds_style
    low, high = _fixed_support_bounds(source_closes, zone_width, bounds_style)
    flipped_count = sum(1 for item in cluster if item.origin.startswith("flipped_"))

    return _make_support_zone(
        origin=_support_origin_from_origins({item.origin for item in cluster}),
        bounds_style=bounds_style,
        low=low,
        high=high,
        width=high - low,
        touches=len(cluster),
        source_closes=source_closes,
        source_indexes=source_indexes,
        score=len(cluster) * 2 + flipped_count,
        structure_role=_support_cluster_role(cluster),
        broken_index=_latest_defined(item.broken_index for item in cluster),
        zone_width=zone_width,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )


# Assemble the standard zone dictionary with derived fields (mid, width_pct, price_state).
def _make_support_zone(
    *,
    origin: str,
    bounds_style: BoundsStyle,
    low: float,
    high: float,
    width: float,
    touches: int,
    source_closes: list[float],
    source_indexes: list[int],
    score: float,
    structure_role: str,
    broken_index: int | None,
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> Zone:
    low = float(low)
    high = float(high)
    width = float(width)
    mid = (low + high) / 2.0
    return {
        "origin": origin,
        "role": "support",
        "bounds_style": bounds_style,
        "low": low,
        "high": high,
        "mid": float(mid),
        "width": width,
        "width_pct": float(width / mid * 100.0) if mid else 0.0,
        "touches": int(touches),
        "source_closes": source_closes,
        "source_indexes": source_indexes,
        "score": float(score),
        "structure_role": structure_role,
        "structure_bias": "support",
        "price_state": _classify_price_state(low, high, current_price, buffer_pct),
        "last_touch_index": max(source_indexes),
        "broken_index": broken_index,
        "zone_width": float(zone_width),
    }


# Pick body-style or support-floor bounds based on bounds_style.
def _fixed_support_bounds(
    prices: list[float],
    zone_width: float,
    bounds_style: BoundsStyle,
) -> tuple[float, float]:
    if bounds_style == "support_floor":
        return _fixed_support_floor_zone_bounds(prices, zone_width)
    return _fixed_support_zone_bounds(prices, zone_width)


# Body zone: anchor high from prices, extend downward by zone_width.
def _fixed_support_zone_bounds(prices: list[float], zone_width: float) -> tuple[float, float]:
    high = _support_upper_anchor(prices)
    return high - float(zone_width), high


# Support-floor zone: anchor low from prices, extend upward by zone_width.
def _fixed_support_floor_zone_bounds(prices: list[float], zone_width: float) -> tuple[float, float]:
    low = _support_floor_anchor(prices)
    return low, low + float(zone_width)


# Upper anchor for body zones: max price, or 10th percentile when cluster is large.
def _support_upper_anchor(prices: list[float]) -> float:
    sorted_prices = sorted(float(price) for price in prices)
    if len(sorted_prices) <= 10:
        return max(sorted_prices)
    return _lower_decile(sorted_prices)


# Floor anchor for support-floor zones: min price, or 10th percentile when cluster is large.
def _support_floor_anchor(prices: list[float]) -> float:
    sorted_prices = sorted(float(price) for price in prices)
    if len(sorted_prices) <= 10:
        return min(sorted_prices)
    return _lower_decile(sorted_prices)


# Return the price at the 10th percentile index of a sorted price list.
def _lower_decile(sorted_prices: list[float]) -> float:
    index = int((len(sorted_prices) - 1) * 0.10)
    return sorted_prices[index]


# Collapse multiple candidate origins into one zone origin label.
def _support_origin_from_origins(origins: set[str]) -> str:
    floor_origins = {"structure_swing_low_wick", "structure_swing_low_body_floor"}
    if origins and origins.issubset(floor_origins):
        return "structure_support_floor"
    if len(origins) == 1:
        return next(iter(origins))
    if "flipped_resistance" in origins:
        return "flipped_resistance"
    return "mixed_structure"


# Derive structure_role from the cluster: single role, mixed, or unknown.
def _support_cluster_role(cluster: list[SupportCandidate]) -> str:
    roles = {item.structure_role for item in cluster if item.structure_role}
    if not roles:
        return "unknown"
    if len(roles) == 1:
        return next(iter(roles))
    return "mixed"


# Return the latest non-None index from an iterable, or None if all are missing.
def _latest_defined(values: Iterable[int | None]) -> int | None:
    defined = [int(value) for value in values if value is not None]
    return max(defined) if defined else None
