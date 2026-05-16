from __future__ import annotations

import pandas as pd

from src.config import AppConfig
from src.signals import generate_buy_the_dips_signal
from src.zones import (
    StructureCandidate,
    StructurePivot,
    _average_true_range,
    _consolidate_structure_zones,
    _detect_structure_events,
    _find_structure_pivots,
    _fixed_structure_zone_bounds,
    _label_structure_pivots,
    _make_structure_zones_distinct,
    _zone_from_structure_cluster,
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
    assert result["all"] == []


def test_structure_v1_uses_fixed_500_dollar_zone_width() -> None:
    closes = [110, 105, 100, 106, 112, 108, 101, 107, 113]
    low_volatility = _ohlc_from_closes(closes, wick=0.5)
    high_volatility = _ohlc_from_closes(closes, wick=5.0)

    low_result = detect_support_resistance_zones_structure_v1(
        low_volatility,
        internal_swing_order=1,
        external_swing_order=1,
        atr_period=3,
        min_touches=1,
        current_price=1000,
    )
    high_result = detect_support_resistance_zones_structure_v1(
        high_volatility,
        internal_swing_order=1,
        external_swing_order=1,
        atr_period=3,
        min_touches=1,
        current_price=1000,
    )

    assert low_result["support"][0]["width"] == 500.0
    assert high_result["support"][0]["width"] == 500.0
    assert low_result["support"][0]["zone_width"] == 500.0
    assert high_result["support"][0]["zone_width"] == 500.0


def test_structure_v1_does_not_create_zones_from_internal_only_swings() -> None:
    df = _ohlc_from_closes([130, 125, 120, 110, 100, 108, 95, 109, 100.2, 111, 122, 130, 128], wick=0.5)

    result = detect_support_resistance_zones_structure_v1(
        df,
        internal_swing_order=1,
        external_swing_order=3,
        min_touches=2,
        zone_tolerance_pct=0.01,
        current_price=130,
    )

    assert not any(99 <= zone["mid"] <= 101 for zone in result["support"])


def test_structure_v1_clusters_external_pivots_with_fixed_500_dollar_width() -> None:
    df = _ohlc_from_closes(
        [67000, 66500, 66000, 66800, 67500, 66900, 66400, 66380, 67000, 67600],
        wick=25,
    )

    result = detect_support_resistance_zones_structure_v1(
        df,
        internal_swing_order=1,
        external_swing_order=2,
        atr_period=3,
        min_touches=2,
        current_price=68000,
    )

    zone = result["support"][0]
    assert zone["source_closes"] == [66000.0, 66380.0]
    assert zone["low"] == 65880.0
    assert zone["high"] == 66380.0


def test_structure_v1_anchors_support_to_low_base_of_macro_group() -> None:
    prices = [
        65776.47,
        65788.36,
        65788.36,
        65971.2,
        66010.93,
        66040.66,
        66220.28,
        66398.0,
        66398.0,
        66519.73,
        66519.73,
        66685.69,
        66820.64,
        67014.91,
        67014.91,
        67026.6,
        67383.66,
        67383.66,
        67392.05,
        67392.05,
        67502.16,
        67502.16,
    ]

    low, high = _fixed_structure_zone_bounds(prices, role="support", zone_width=500.0)

    assert low == 65288.36
    assert high == 65788.36


def test_structure_v1_consolidates_nearby_fixed_zones() -> None:
    zones = [
        _structure_zone(low=78000.0, high=78500.0, source_closes=[78250.0], score=5.0),
        _structure_zone(low=78600.0, high=79100.0, source_closes=[78850.0], score=20.0),
        _structure_zone(low=79300.0, high=79800.0, source_closes=[79550.0], score=4.0),
        _structure_zone(low=74000.0, high=74500.0, source_closes=[74250.0], score=8.0),
    ]

    result = _consolidate_structure_zones(zones, zone_width=500.0, current_price=81000.0, buffer_pct=0.0015)

    support_zones = sorted(result, key=lambda zone: zone["mid"], reverse=True)
    assert len(support_zones) == 2
    assert support_zones[0]["low"] == 79050.0
    assert support_zones[0]["high"] == 79550.0
    assert support_zones[0]["touches"] == 3


def test_active_support_biased_cluster_remains_support() -> None:
    cluster = [
        _structure_candidate(price=78793.67, index=1, origin="structure_swing_low"),
        _structure_candidate(price=78850.06, index=2, origin="flipped_resistance"),
        _structure_candidate(price=78850.06, index=2, origin="structure_swing_high"),
        _structure_candidate(price=79105.49, index=3, origin="flipped_resistance"),
        _structure_candidate(price=79105.49, index=3, origin="structure_swing_high"),
        _structure_candidate(price=79272.91, index=4, origin="flipped_resistance"),
        _structure_candidate(price=79272.91, index=4, origin="structure_swing_high"),
    ]

    zone = _zone_from_structure_cluster(cluster, zone_width=500.0, current_price=79113.21, buffer_pct=0.0015)
    distinct = _make_structure_zones_distinct([zone], zone_tolerance_pct=0.0045, current_price=79113.21, buffer_pct=0.0015)

    assert distinct[0]["role"] == "support"
    assert distinct[0]["price_state"] == "active"
    assert distinct[0]["structure_bias"] == "support"


def test_structure_v1_wick_pierce_does_not_confirm_break() -> None:
    df = pd.DataFrame(
        {
            "open": [66000, 65050, 65600, 65300, 65400],
            "high": [66100, 65100, 65900, 65500, 65600],
            "low": [65900, 64900, 65500, 64000, 65300],
            "close": [66000, 65000, 65800, 65200, 65400],
        }
    )

    result = detect_support_resistance_zones_structure_v1(
        df,
        internal_swing_order=1,
        external_swing_order=1,
        min_touches=1,
        break_atr_mult=0.0,
        current_price=67000,
    )

    assert result["support"]
    assert all(zone["origin"] != "flipped_support" for zone in result["all"])
    assert all(zone["broken_index"] is None for zone in result["all"])


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


def test_structure_events_classify_bos_then_choch() -> None:
    pivots = [
        StructurePivot(index=1, kind="low", price=100.0, body_price=101.0, atr=1.0, term="external"),
        StructurePivot(index=3, kind="high", price=110.0, body_price=109.0, atr=1.0, term="external"),
        StructurePivot(index=5, kind="low", price=105.0, body_price=106.0, atr=1.0, term="external"),
        StructurePivot(index=8, kind="high", price=116.0, body_price=115.0, atr=1.0, term="external"),
    ]
    _label_structure_pivots(pivots)
    closes = pd.Series([101, 102, 106, 108, 109, 107, 111, 114, 113, 112, 104, 103], dtype=float).to_numpy()
    atr = pd.Series([1.0] * len(closes), dtype=float).to_numpy()

    events = _detect_structure_events(pivots, closes=closes, atr=atr, break_atr_mult=0.0)

    assert events[0].direction == "bullish"
    assert events[0].event == "BOS"
    bearish_events = [event for event in events if event.direction == "bearish"]
    assert bearish_events[0].event == "CHOCH"


def test_structure_v1_flips_support_to_resistance_after_confirmed_close_break() -> None:
    df = pd.DataFrame(
        {
            "open": [66000, 65050, 65600, 65300, 64800, 64700],
            "high": [66100, 65100, 65900, 65400, 65000, 64800],
            "low": [65900, 64900, 65500, 65200, 64600, 64500],
            "close": [66000, 65000, 65800, 65300, 64700, 64600],
        }
    )

    result = detect_support_resistance_zones_structure_v1(
        df,
        internal_swing_order=1,
        external_swing_order=1,
        min_touches=1,
        break_atr_mult=0.0,
        current_price=64000,
    )

    flipped = [zone for zone in result["resistance"] if zone["origin"] == "flipped_support"]
    assert flipped
    assert flipped[0]["broken_index"] == 4


def test_structure_v1_output_is_signal_compatible() -> None:
    df = _ohlc_from_closes([110, 105, 100, 106, 112, 108, 101, 107, 113, 109], wick=0.5)
    result = detect_support_resistance_zones_structure_v1(
        df,
        internal_swing_order=1,
        external_swing_order=1,
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


def _structure_zone(low: float, high: float, source_closes: list[float], score: float) -> dict:
    mid = (low + high) / 2.0
    return {
        "origin": "flipped_resistance",
        "role": "support",
        "low": low,
        "high": high,
        "mid": mid,
        "width": high - low,
        "width_pct": (high - low) / mid * 100.0,
        "touches": len(source_closes),
        "source_closes": source_closes,
        "source_indexes": list(range(len(source_closes))),
        "score": score,
        "structure_role": "mixed",
        "last_touch_index": len(source_closes) - 1,
        "broken_index": None,
        "zone_width": high - low,
        "leg_ids": [],
    }


def _structure_candidate(price: float, index: int, origin: str) -> StructureCandidate:
    return StructureCandidate(
        price=price,
        index=index,
        origin=origin,
        zone_width=500.0,
        structure_role="mixed",
        term="external",
    )
