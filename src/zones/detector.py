from __future__ import annotations

from typing import Any

import pandas as pd

from .build import _build_support_zones
from .candidates import _support_candidates
from .ohlc import _average_true_range, _coerce_ohlc
from .pivots import _filter_prominent_structure_pivots, _find_structure_pivots, _label_structure_pivots
from .postprocess import _fill_support_staircase_gaps, _make_support_zones_distinct, _zone_distance_sort_key
from .types import STRUCTURE_ZONE_WIDTH


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
