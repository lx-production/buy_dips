from __future__ import annotations

from typing import Any

import pandas as pd

from .build import _build_support_zones, _suppress_nearby_support_zones
from .candidates import _support_candidates
from .daily import _build_daily_body_support_zones, _overlay_daily_support_zones
from .ohlc import _average_true_range, _coerce_ohlc
from .pivots import _filter_prominent_structure_pivots, _find_structure_pivots, _label_structure_pivots
from .postprocess import _fill_support_staircase_gaps, _make_support_zones_distinct
from .reactions import _build_local_reaction_zones
from .rejections import _build_split_rejection_zone_pairs, _overlay_split_rejection_zones
from .types import STRUCTURE_ZONE_WIDTH


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
    ohlc = _coerce_ohlc(df)
    if ohlc is None:
        return empty

    bars_each_side = max(1, int(external_swing_order))
    if len(ohlc) < (bars_each_side * 2 + 1):
        return empty

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
        return empty

    _label_structure_pivots(raw_external_pivots)
    _label_structure_pivots(external_pivots)
    _label_structure_pivots(internal_pivots)
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
    local_reaction_zones = _build_local_reaction_zones(
        internal_pivots=internal_pivots,
        closes=closes,
        break_atr_mult=break_atr_mult,
        zone_width=STRUCTURE_ZONE_WIDTH,
        min_touches=min_touches,
        current_price=float(current_price),
        buffer_pct=buffer_pct,
    )
    zones = _suppress_nearby_support_zones(zones + local_reaction_zones)
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
    rejection_pairs = _build_split_rejection_zone_pairs(
        ohlc=ohlc,
        external_pivots=external_pivots,
        internal_pivots=internal_pivots,
        zone_width=STRUCTURE_ZONE_WIDTH,
        current_price=float(current_price),
        buffer_pct=buffer_pct,
    )
    zones = _overlay_split_rejection_zones(zones, rejection_pairs)
    zones = _make_support_zones_distinct(zones, current_price=float(current_price), buffer_pct=buffer_pct)
    daily_zones = _build_daily_body_support_zones(
        df,
        zone_width=STRUCTURE_ZONE_WIDTH,
        current_price=float(current_price),
        buffer_pct=buffer_pct,
        external_swing_order=external_swing_order,
        atr_period=atr_period,
        external_min_swing_atr_mult=external_min_swing_atr_mult,
        external_min_swing_pct=external_min_swing_pct,
    )
    zones = _overlay_daily_support_zones(zones, daily_zones)
    zones = _make_support_zones_distinct(zones, current_price=float(current_price), buffer_pct=buffer_pct)

    support = sorted(zones, key=lambda zone: float(zone["low"]))
    return {"support": support, "resistance": [], "active": [], "all": support}
