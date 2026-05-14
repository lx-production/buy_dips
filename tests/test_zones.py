from __future__ import annotations

import pandas as pd

from src.zones import _make_zones_distinct, detect_support_resistance_zones_pure_close


def test_support_zone_uses_exact_close_bounds() -> None:
    df = pd.DataFrame({"close": [110, 105, 100, 106, 112, 108, 101, 107, 113, 109, 100.5, 108, 114]})

    result = detect_support_resistance_zones_pure_close(
        df,
        swing_order=1,
        lookahead=2,
        min_reversal_pct=0.005,
        zone_tolerance_pct=0.012,
        min_touches=3,
        max_zone_width_pct=0.018,
        current_price=110,
    )

    zone = result["support"][0]
    assert zone["origin"] == "support_pivot"
    assert zone["low"] == 100
    assert zone["high"] == 101
    assert zone["source_closes"] == [100.0, 100.5, 101.0]
    assert zone["touches"] == 3


def test_resistance_zone_uses_exact_close_bounds() -> None:
    df = pd.DataFrame({"close": [100, 110, 120, 114, 105, 112, 121, 115, 106, 113, 120.5, 116, 108]})

    result = detect_support_resistance_zones_pure_close(
        df,
        swing_order=1,
        lookahead=2,
        min_reversal_pct=0.005,
        zone_tolerance_pct=0.01,
        min_touches=3,
        max_zone_width_pct=0.018,
        current_price=110,
    )

    zone = result["resistance"][0]
    assert zone["origin"] == "resistance_pivot"
    assert zone["low"] == 120
    assert zone["high"] == 121
    assert zone["source_closes"] == [120.0, 120.5, 121.0]
    assert zone["touches"] == 3


def test_wick_data_does_not_affect_zones() -> None:
    close = [110, 105, 100, 106, 112, 108, 101, 107, 113, 109, 100.5, 108, 114]
    base = pd.DataFrame({"close": close, "high": [200] * len(close), "low": [1] * len(close)})
    changed_wicks = pd.DataFrame({"close": close, "high": [999] * len(close), "low": [0.01] * len(close)})

    kwargs = {
        "swing_order": 1,
        "lookahead": 2,
        "min_reversal_pct": 0.005,
        "zone_tolerance_pct": 0.012,
        "min_touches": 3,
        "max_zone_width_pct": 0.018,
        "current_price": 110,
    }
    assert detect_support_resistance_zones_pure_close(base, **kwargs) == detect_support_resistance_zones_pure_close(
        changed_wicks, **kwargs
    )


def test_empty_or_insufficient_data_returns_empty_zones() -> None:
    assert detect_support_resistance_zones_pure_close(pd.DataFrame()) == {
        "support": [],
        "resistance": [],
        "active": [],
        "all": [],
    }
    result = detect_support_resistance_zones_pure_close(pd.DataFrame({"close": [1, 2, 1]}))
    assert result["all"] == []


def test_cluster_median_logic_prevents_oversized_chain_merge() -> None:
    df = pd.DataFrame({"close": [110, 105, 100, 106, 111, 107, 101.2, 108, 112, 109, 102.4, 110, 115]})

    result = detect_support_resistance_zones_pure_close(
        df,
        swing_order=1,
        lookahead=2,
        min_reversal_pct=0.005,
        zone_tolerance_pct=0.02,
        min_touches=2,
        max_zone_width_pct=0.018,
        current_price=120,
    )

    support_origin_zones = [zone for zone in result["support"] if zone["origin"] == "support_pivot"]
    assert len(support_origin_zones) == 1
    zone = support_origin_zones[0]
    assert zone["low"] == 100
    assert zone["high"] == 101.2
    assert 102.4 not in zone["source_closes"]


def test_distinct_pass_merges_nearby_same_role_zones() -> None:
    zones = [
        _zone(low=100.0, high=101.0, touches=2, origin="support_pivot"),
        _zone(low=101.2, high=101.8, touches=2, origin="resistance_pivot"),
        _zone(low=106.0, high=106.4, touches=2, origin="support_pivot"),
    ]

    result = _make_zones_distinct(
        zones=zones,
        max_zone_width_pct=0.025,
        zone_tolerance_pct=0.0045,
        current_price=110,
        buffer_pct=0.0015,
    )

    assert len(result) == 2
    assert result[0]["origin"] == "mixed_pivot"
    assert result[0]["low"] == 100.0
    assert result[0]["high"] == 101.8
    assert result[0]["touches"] == 4


def _zone(low: float, high: float, touches: int, origin: str) -> dict:
    source_closes = [low, high] if touches == 2 else [low, high, *([low] * (touches - 2))]
    mid = (low + high) / 2.0
    return {
        "origin": origin,
        "role": "support",
        "low": low,
        "high": high,
        "mid": mid,
        "width": high - low,
        "width_pct": (high - low) / mid * 100.0,
        "touches": touches,
        "source_closes": source_closes,
        "source_indexes": list(range(touches)),
    }
