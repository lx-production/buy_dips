from __future__ import annotations

from typing import Any

import pandas as pd

from .build import _build_support_zones
from .ohlc import _average_true_range, _coerce_ohlc
from .reactions import _build_local_reaction_zones
from .types import STRUCTURE_ZONE_WIDTH, ZoneDetectorEvidence
from .candidates import _first_reclaim_indexes_for_pivots, _support_candidates
from .rejections import _build_split_rejection_zone_pairs, _overlay_split_rejection_zones
from .persistent import _build_persistent_wick_floor_zones, _overlay_persistent_wick_floors
from .pivots import _filter_prominent_structure_pivots, _find_structure_pivots, _label_structure_pivots
from .daily import _daily_body_support_zones_from_pivots, _extract_daily_structure_pivots, _overlay_daily_support_zones
from .postprocess import _enforce_support_zone_spacing, _fill_persistent_wick_floor_gaps, _fill_support_staircase_gaps, _make_support_zones_distinct


# Public detector entry: run the current support_structure_v1 pipeline.
def detect_support_resistance_zones(
    df: pd.DataFrame,
    min_touches: int = 2,
    current_price: float | None = None,
    buffer_pct: float = 0.0015,
    external_swing_order: int = 5, # 5 candles each side, or 20 hours on each side, or 40 + 4 hours total
    atr_period: int = 14,
    break_atr_mult: float = 0.2,
    external_min_swing_atr_mult: float = 4.0, # might be too strict, could be reduced if fine-tuning
    external_min_swing_pct: float = 2.5,
) -> dict[str, list[dict[str, Any]]]:
    return detect_support_resistance_zones_structure_v1(
        df,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
        external_swing_order=external_swing_order,
        atr_period=atr_period,
        break_atr_mult=break_atr_mult,
        external_min_swing_atr_mult=external_min_swing_atr_mult,
        external_min_swing_pct=external_min_swing_pct,
    )


# Extract features from the full frame, then materialize the support ladder from that evidence.
def detect_support_resistance_zones_structure_v1(
    df: pd.DataFrame,
    external_swing_order: int = 5,
    atr_period: int = 14,
    external_min_swing_atr_mult: float = 4.0,
    external_min_swing_pct: float = 2.5,
    min_touches: int = 2,
    current_price: float | None = None,
    buffer_pct: float = 0.0015,
    break_atr_mult: float = 0.2,
) -> dict[str, list[dict[str, Any]]]:
    empty = {"support": [], "resistance": [], "active": [], "all": []}
    evidence = extract_zone_detector_evidence(
        df,
        current_price=current_price,
        external_swing_order=external_swing_order,
        atr_period=atr_period,
        break_atr_mult=break_atr_mult,
        external_min_swing_atr_mult=external_min_swing_atr_mult,
        external_min_swing_pct=external_min_swing_pct,
    )
    if evidence is None:
        return empty
    return materialize_support_zones(
        evidence,
        min_touches=min_touches,
        buffer_pct=buffer_pct,
        break_atr_mult=break_atr_mult,
    )


# Pull pivots, daily swings, and first reclaim indexes from a closed OHLC frame. No zone dicts yet.
def extract_zone_detector_evidence(
    df: pd.DataFrame,
    *,
    current_price: float | None = None,
    external_swing_order: int = 5,
    atr_period: int = 14,
    break_atr_mult: float = 0.2,
    external_min_swing_atr_mult: float = 4.0,
    external_min_swing_pct: float = 2.5,
) -> ZoneDetectorEvidence | None:
    ohlc = _coerce_ohlc(df)
    if ohlc is None:
        return None

    bars_each_side = max(1, int(external_swing_order))
    if len(ohlc) < (bars_each_side * 2 + 1):
        return None

    highs = ohlc["high"].to_numpy(dtype=float)
    lows = ohlc["low"].to_numpy(dtype=float)
    closes = ohlc["close"].to_numpy(dtype=float)
    atr = _average_true_range(highs=highs, lows=lows, closes=closes, period=atr_period)
    if current_price is None:
        current_price = float(closes[-1])

    raw_external_pivots = _find_structure_pivots(ohlc, bars_each_side, atr, "external")
    internal_pivots = _find_structure_pivots(ohlc, 1, atr, "internal")
    external_pivots = _filter_prominent_structure_pivots(
        raw_external_pivots,
        min_swing_atr_mult=external_min_swing_atr_mult,
        min_swing_pct=external_min_swing_pct,
    )
    if not external_pivots:
        return None

    _label_structure_pivots(raw_external_pivots)
    _label_structure_pivots(external_pivots)
    _label_structure_pivots(internal_pivots)
    daily_pivots = _extract_daily_structure_pivots(
        df,
        external_swing_order=external_swing_order,
        atr_period=atr_period,
        external_min_swing_atr_mult=external_min_swing_atr_mult,
        external_min_swing_pct=external_min_swing_pct,
    )
    first_reclaim_indexes = _first_reclaim_indexes_for_pivots(
        [*raw_external_pivots, *internal_pivots],
        closes,
        break_atr_mult,
    )
    return ZoneDetectorEvidence(
        ohlc=ohlc,
        closes=closes,
        current_price=float(current_price),
        raw_external_pivots=raw_external_pivots,
        external_pivots=external_pivots,
        internal_pivots=internal_pivots,
        daily_pivots=daily_pivots,
        first_reclaim_indexes=first_reclaim_indexes,
    )


# Build the support ladder from extracted evidence, then resolve nearby conflicts last.
def materialize_support_zones(
    evidence: ZoneDetectorEvidence,
    *,
    min_touches: int = 2,
    buffer_pct: float = 0.0015,
    break_atr_mult: float = 0.2,
) -> dict[str, list[dict[str, Any]]]:
    current_price = float(evidence.current_price)
    candidates = _support_candidates(
        raw_external_pivots=evidence.raw_external_pivots,
        external_pivots=evidence.external_pivots,
        closes=evidence.closes,
        break_atr_mult=break_atr_mult,
        zone_width=STRUCTURE_ZONE_WIDTH,
    )
    zones = _build_support_zones(
        candidates,
        zone_width=STRUCTURE_ZONE_WIDTH,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )
    local_reaction_zones = _build_local_reaction_zones(
        internal_pivots=evidence.internal_pivots,
        closes=evidence.closes,
        break_atr_mult=break_atr_mult,
        zone_width=STRUCTURE_ZONE_WIDTH,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )
    # Keep structural and local families side by side. Each builder already
    # dedupes internally; an early cross-family suppress can drop a structural
    # shelf that later survives once a nearby local loses to a persistent floor.
    zones = [*zones, *local_reaction_zones]
    zones = _make_support_zones_distinct(zones, current_price=current_price, buffer_pct=buffer_pct)
    zones = _fill_support_staircase_gaps(
        zones=zones,
        raw_external_pivots=evidence.raw_external_pivots,
        closes=evidence.closes,
        break_atr_mult=break_atr_mult,
        zone_width=STRUCTURE_ZONE_WIDTH,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
        internal_pivots=evidence.internal_pivots,
    )
    rejection_pairs = _build_split_rejection_zone_pairs(
        ohlc=evidence.ohlc,
        external_pivots=evidence.external_pivots,
        internal_pivots=evidence.internal_pivots,
        zone_width=STRUCTURE_ZONE_WIDTH,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )
    zones = _overlay_split_rejection_zones(zones, rejection_pairs)
    zones = _make_support_zones_distinct(zones, current_price=current_price, buffer_pct=buffer_pct)
    daily_zones = _daily_body_support_zones_from_pivots(
        evidence.daily_pivots,
        zone_width=STRUCTURE_ZONE_WIDTH,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )
    zones = _overlay_daily_support_zones(zones, daily_zones)
    zones = _make_support_zones_distinct(zones, current_price=current_price, buffer_pct=buffer_pct)
    # Pin long-wick floors last so merge/daily cannot absorb or shift their bounds.
    persistent_floors = _build_persistent_wick_floor_zones(
        evidence.raw_external_pivots,
        zone_width=STRUCTURE_ZONE_WIDTH,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )
    zones = _overlay_persistent_wick_floors(zones, persistent_floors)
    # Overlaps only here; nearby-slot conflicts wait for the unified resolver.
    zones = _make_support_zones_distinct(zones, current_price=current_price, buffer_pct=buffer_pct)
    # Persistent first, then daily/structural/local by score, touches, width.
    zones = _enforce_support_zone_spacing(zones)
    # Recover reclaimed-high stairs in wide gaps on the final spaced ladder.
    zones = _fill_persistent_wick_floor_gaps(
        zones=zones,
        raw_external_pivots=evidence.raw_external_pivots,
        closes=evidence.closes,
        break_atr_mult=break_atr_mult,
        zone_width=STRUCTURE_ZONE_WIDTH,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
        internal_pivots=evidence.internal_pivots,
    )
    # Same resolver after gap-fill so inserted stairs cannot sit on top of a neighbor.
    zones = _enforce_support_zone_spacing(zones)

    support = sorted(zones, key=lambda zone: float(zone["low"]))
    return {"support": support, "resistance": [], "active": [], "all": support}
