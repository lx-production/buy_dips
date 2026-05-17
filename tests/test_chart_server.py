from __future__ import annotations

import pandas as pd

from src.chart_server import _chart_pivots, _visible_support_zones


def test_visible_support_zones_keep_below_price_and_nearest_two_above() -> None:
    zones = [
        _zone(80.0, 85.0, touches=4),
        _zone(70.0, 75.0, touches=3),
        _zone(102.0, 107.0, touches=2),
        _zone(110.0, 115.0, touches=8),
        _zone(130.0, 135.0, touches=10),
    ]

    visible = _visible_support_zones(zones, current_price=100.0)

    assert [(zone["low"], zone["high"]) for zone in visible] == [
        (80.0, 85.0),
        (70.0, 75.0),
        (102.0, 107.0),
        (110.0, 115.0),
    ]


def test_visible_support_zones_keep_price_touching_zone() -> None:
    zones = [_zone(98.0, 103.0), _zone(104.0, 109.0), _zone(120.0, 125.0)]

    visible = _visible_support_zones(zones, current_price=100.0, above_count=1)

    assert [(zone["low"], zone["high"]) for zone in visible] == [(98.0, 103.0), (104.0, 109.0)]


def test_chart_pivots_hide_internal_pivots_by_default() -> None:
    df = pd.DataFrame(
        {
            "open_time": list(range(9)),
            "open": [100, 110, 105, 112, 108, 120, 100, 115, 105],
            "high": [101, 111, 106, 113, 109, 121, 101, 116, 106],
            "low": [99, 109, 104, 111, 107, 119, 99, 114, 104],
            "close": [100, 110, 105, 112, 108, 120, 100, 115, 105],
        }
    )

    pivots = _chart_pivots(
        df,
        visible_start_index=0,
        internal_swing_order=1,
        external_swing_order=1,
        atr_period=3,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        show_internal_pivots=False,
    )

    assert pivots
    assert {pivot["term"] for pivot in pivots} == {"external"}


def test_chart_pivots_can_include_internal_debug_labels() -> None:
    df = pd.DataFrame(
        {
            "open_time": list(range(9)),
            "open": [100, 110, 105, 112, 108, 120, 100, 115, 105],
            "high": [101, 111, 106, 113, 109, 121, 101, 116, 106],
            "low": [99, 109, 104, 111, 107, 119, 99, 114, 104],
            "close": [100, 110, 105, 112, 108, 120, 100, 115, 105],
        }
    )

    pivots = _chart_pivots(
        df,
        visible_start_index=0,
        internal_swing_order=1,
        external_swing_order=1,
        atr_period=3,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        show_internal_pivots=True,
    )

    assert {pivot["term"] for pivot in pivots} == {"external", "internal"}


def _zone(low: float, high: float, touches: int = 1) -> dict:
    return {"low": low, "high": high, "touches": touches}
