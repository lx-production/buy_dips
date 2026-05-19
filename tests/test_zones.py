from __future__ import annotations

import pandas as pd

from src.config import AppConfig
from src.signals import generate_buy_the_dips_signal
from src.zones import (
    StructurePivot,
    _average_true_range,
    _filter_prominent_structure_pivots,
    _find_structure_pivots,
    _fixed_structure_zone_bounds,
    _label_structure_pivots,
    detect_support_resistance_zones_structure_v1,
)


def test_empty_or_insufficient_data_returns_empty_zones() -> None:
    assert detect_support_resistance_zones_structure_v1(pd.DataFrame()) == {
        "support": [],
        "resistance": [],
        "active": [],
        "all": [],
    }
    result = detect_support_resistance_zones_structure_v1(_ohlc_from_closes([1, 2, 1], wick=0.1), external_swing_order=2)
    assert result == {"support": [], "resistance": [], "active": [], "all": []}


def test_support_v1_uses_fixed_500_dollar_zone_width() -> None:
    closes = [67000, 66500, 66000, 66800, 67500, 66900, 66400, 66380, 67000, 67600]
    low_volatility = _ohlc_from_closes(closes, wick=25)
    high_volatility = _ohlc_from_closes(closes, wick=250)

    low_result = detect_support_resistance_zones_structure_v1(
        low_volatility,
        external_swing_order=2,
        atr_period=3,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        min_touches=2,
        current_price=68000,
    )
    high_result = detect_support_resistance_zones_structure_v1(
        high_volatility,
        external_swing_order=2,
        atr_period=3,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        min_touches=2,
        current_price=68000,
    )

    assert low_result["support"][0]["width"] == 500.0
    assert high_result["support"][0]["width"] == 500.0
    assert low_result["support"][0]["zone_width"] == 500.0
    assert high_result["support"][0]["zone_width"] == 500.0


def test_support_v1_clusters_external_swing_lows_only() -> None:
    df = _ohlc_from_closes(
        [67000, 66500, 66000, 66800, 67500, 66900, 66400, 66380, 67000, 67600],
        wick=25,
    )

    result = detect_support_resistance_zones_structure_v1(
        df,
        external_swing_order=2,
        atr_period=3,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        min_touches=2,
        current_price=68000,
    )

    assert result["resistance"] == []
    assert result["active"] == []
    assert result["all"] == result["support"]
    zone = result["support"][0]
    assert zone["origin"] == "structure_swing_low"
    assert zone["role"] == "support"
    assert zone["source_closes"] == [66000.0, 66380.0]
    assert zone["low"] == 65880.0
    assert zone["high"] == 66380.0


def test_support_v1_does_not_create_zones_from_swing_highs() -> None:
    df = _ohlc_from_closes([100, 120, 110, 121, 112], wick=0.5)

    result = detect_support_resistance_zones_structure_v1(
        df,
        external_swing_order=1,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        min_touches=2,
        current_price=130,
    )

    assert result == {"support": [], "resistance": [], "active": [], "all": []}


def test_support_v1_excludes_failed_overhead_zones() -> None:
    df = _ohlc_from_closes(
        [67000, 66500, 66000, 66800, 67500, 66900, 66400, 66380, 67000, 67600],
        wick=25,
    )

    result = detect_support_resistance_zones_structure_v1(
        df,
        external_swing_order=2,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        min_touches=2,
        current_price=65000,
    )

    assert result == {"support": [], "resistance": [], "active": [], "all": []}


def test_price_inside_support_zone_counts_as_support() -> None:
    df = _ohlc_from_closes(
        [67000, 66500, 66000, 66800, 67500, 66900, 66400, 66380, 67000, 67600],
        wick=25,
    )

    result = detect_support_resistance_zones_structure_v1(
        df,
        external_swing_order=2,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        min_touches=2,
        current_price=66000,
    )

    assert len(result["support"]) == 1
    zone = result["support"][0]
    assert zone["low"] <= 66000 <= zone["high"]

    signal = generate_buy_the_dips_signal(_signal_df([67500, 67000, 66500, 66000]), result, AppConfig())
    assert signal["distance_to_support_pct"] == 0.0
    assert signal["nearest_resistance_low"] is None
    assert signal["distance_to_resistance_pct"] is None


def test_fixed_support_bounds_anchor_to_highest_touch_body() -> None:
    low, high = _fixed_structure_zone_bounds([66000.0, 66380.0], zone_width=500.0)

    assert low == 65880.0
    assert high == 66380.0


def test_internal_and_external_structure_pivots_have_different_granularity() -> None:
    df = _ohlc_from_closes([100, 110, 105, 112, 108, 120, 90, 115, 80, 118, 70, 125, 100], wick=0.5)
    atr = _average_true_range(
        highs=df["high"].to_numpy(dtype=float),
        lows=df["low"].to_numpy(dtype=float),
        closes=df["close"].to_numpy(dtype=float),
        period=3,
    )

    internal = _find_structure_pivots(df, swing_order=1, atr=atr, term="internal")
    external = _find_structure_pivots(df, swing_order=3, atr=atr, term="external")

    assert len(internal) > len(external)
    assert external


def test_prominent_structure_pivots_ignore_small_reversals_and_keep_extremes() -> None:
    pivots = [
        StructurePivot(index=1, kind="high", price=110.0, body_price=109.0, atr=1.0, term="external"),
        StructurePivot(index=2, kind="high", price=112.0, body_price=111.0, atr=1.0, term="external"),
        StructurePivot(index=3, kind="low", price=111.0, body_price=111.5, atr=1.0, term="external"),
        StructurePivot(index=4, kind="high", price=115.0, body_price=114.0, atr=1.0, term="external"),
        StructurePivot(index=5, kind="low", price=100.0, body_price=101.0, atr=1.0, term="external"),
        StructurePivot(index=6, kind="low", price=98.0, body_price=99.0, atr=1.0, term="external"),
        StructurePivot(index=7, kind="high", price=103.0, body_price=102.0, atr=1.0, term="external"),
    ]

    result = _filter_prominent_structure_pivots(pivots, min_swing_atr_mult=4.0, min_swing_pct=0.0)

    assert [(pivot.index, pivot.kind, pivot.price) for pivot in result] == [
        (4, "high", 115.0),
        (6, "low", 98.0),
        (7, "high", 103.0),
    ]


def test_prominent_structure_pivots_can_require_percent_move() -> None:
    pivots = [
        StructurePivot(index=1, kind="high", price=100.0, body_price=99.0, atr=0.0, term="external"),
        StructurePivot(index=2, kind="low", price=94.0, body_price=95.0, atr=0.0, term="external"),
        StructurePivot(index=3, kind="low", price=89.0, body_price=90.0, atr=0.0, term="external"),
    ]

    result = _filter_prominent_structure_pivots(pivots, min_swing_atr_mult=0.0, min_swing_pct=10.0)

    assert [(pivot.index, pivot.kind, pivot.price) for pivot in result] == [(1, "high", 100.0), (3, "low", 89.0)]


def test_structure_pivot_labels_are_preserved_for_chart_debugging() -> None:
    pivots = [
        StructurePivot(index=1, kind="low", price=100.0, body_price=101.0, atr=1.0, term="external"),
        StructurePivot(index=3, kind="high", price=110.0, body_price=109.0, atr=1.0, term="external"),
        StructurePivot(index=5, kind="low", price=105.0, body_price=106.0, atr=1.0, term="external"),
        StructurePivot(index=8, kind="high", price=116.0, body_price=115.0, atr=1.0, term="external"),
    ]

    _label_structure_pivots(pivots)

    assert [pivot.structure_role for pivot in pivots] == ["L", "H", "HL", "HH"]


def test_support_v1_output_is_signal_compatible() -> None:
    df = _ohlc_from_closes([110, 105, 100, 106, 112, 108, 101, 107, 113, 109], wick=0.5)
    result = detect_support_resistance_zones_structure_v1(
        df,
        external_swing_order=1,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        min_touches=1,
        current_price=109,
    )

    for zone in result["all"]:
        for key in ("low", "high", "mid", "width", "width_pct", "touches", "origin", "role", "source_closes", "source_indexes"):
            assert key in zone

    signal = generate_buy_the_dips_signal(df, result, AppConfig())
    assert signal["decision"] in {"HOLD", "ALERT_ONLY", "PREPARE_MANUAL_REVIEW", "STRONG_BUY_SIGNAL"}


def _ohlc_from_closes(closes: list[float], wick: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [close + wick for close in closes],
            "low": [close - wick for close in closes],
            "close": closes,
        }
    )


def _signal_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [close + 1 for close in closes],
            "low": [close - 1 for close in closes],
            "close": closes,
            "close_time": [1_700_000_000_000 + i * 14_400_000 for i in range(len(closes))],
            "is_closed": [1] * len(closes),
        }
    )
