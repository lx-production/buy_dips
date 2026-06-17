from __future__ import annotations

from typing import Any

import numpy as np

from .candidates import _first_reclaim_index, _high_is_confirmed_reclaimed
from .types import (
    STRUCTURE_STAIR_STEP_MAX_INSERTIONS,
    STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP,
    StructurePivot,
    SupportCandidate,
)


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
    from .build import _build_support_zones

    boundary_zones = sorted(zones, key=lambda zone: float(zone["low"]))
    best_zone: dict[str, Any] | None = None
    best_rank: tuple[float, float, int, float] | None = None

    for lower_zone, upper_zone in zip(boundary_zones, boundary_zones[1:]):
        if _coerce_price_state(lower_zone, current_price, buffer_pct) != "support":
            continue
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

