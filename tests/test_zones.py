from __future__ import annotations

import pandas as pd

from src.config import AppConfig
from src.signals import generate_buy_the_dips_signal
from src.zones import (
    StructurePivot,
    SupportCandidate,
    _average_true_range,
    _build_local_reaction_zones,
    _filter_prominent_structure_pivots,
    _fill_support_staircase_gaps,
    _find_structure_pivots,
    _fixed_support_zone_bounds,
    _label_structure_pivots,
    _support_floor_candidates,
    detect_support_resistance_zones_structure_v1,
)
from src.zones.build import _build_support_zones


def test_empty_or_insufficient_data_returns_empty_zones() -> None:
    assert detect_support_resistance_zones_structure_v1(pd.DataFrame()) == {
        "support": [],
        "resistance": [],
        "active": [],
        "all": [],
    }
    result = detect_support_resistance_zones_structure_v1(_ohlc_from_closes([1, 2, 1], wick=0.1), external_swing_order=2)
    assert result == {"support": [], "resistance": [], "active": [], "all": []}


def test_structure_v1_clusters_external_swing_lows_with_fixed_500_dollar_width() -> None:
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
    assert zone["source_closes"] == [66000.0, 66380.0]
    assert zone["low"] == 65880.0
    assert zone["high"] == 66380.0
    assert zone["width"] == 500.0


def test_macro_merge_preserves_complete_zone_metadata() -> None:
    candidates = [
        SupportCandidate(price=1000.0, index=1, origin="structure_swing_low", structure_role="L"),
        SupportCandidate(price=1100.0, index=2, origin="structure_swing_low", structure_role="L"),
        SupportCandidate(price=1700.0, index=3, origin="structure_swing_low", structure_role="L"),
        SupportCandidate(price=1800.0, index=4, origin="structure_swing_low", structure_role="L"),
    ]

    zones = _build_support_zones(
        candidates,
        zone_width=500.0,
        min_touches=2,
        current_price=3000.0,
        buffer_pct=0.0015,
    )

    assert zones == [
        {
            "origin": "structure_swing_low",
            "role": "support",
            "bounds_style": "body",
            "low": 1300.0,
            "high": 1800.0,
            "mid": 1550.0,
            "width": 500.0,
            "width_pct": 500.0 / 1550.0 * 100.0,
            "touches": 4,
            "source_closes": [1000.0, 1100.0, 1700.0, 1800.0],
            "source_indexes": [1, 2, 3, 4],
            "score": 8.0,
            "structure_role": "L",
            "structure_bias": "support",
            "price_state": "support",
            "last_touch_index": 4,
            "broken_index": None,
            "zone_width": 500.0,
        }
    ]


def test_body_floor_bridge_preserves_complete_zone_metadata() -> None:
    candidates = [
        SupportCandidate(price=1000.0, index=1, origin="structure_swing_low", structure_role="L"),
        SupportCandidate(price=1100.0, index=2, origin="structure_swing_low", structure_role="L"),
        SupportCandidate(
            price=1500.0,
            index=3,
            origin="structure_swing_low_wick",
            structure_role="LL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=1550.0,
            index=4,
            origin="structure_swing_low_body_floor",
            structure_role="LL",
            bounds_style="support_floor",
        ),
    ]

    zones = _build_support_zones(
        candidates,
        zone_width=500.0,
        min_touches=2,
        current_price=3000.0,
        buffer_pct=0.0015,
    )

    assert zones == [
        {
            "origin": "mixed_structure",
            "role": "support",
            "bounds_style": "body",
            "low": 1100.0,
            "high": 1600.0,
            "mid": 1350.0,
            "width": 500.0,
            "width_pct": 500.0 / 1350.0 * 100.0,
            "touches": 4,
            "source_closes": [1000.0, 1100.0, 1500.0, 1550.0],
            "source_indexes": [1, 2, 3, 4],
            "score": 8.0,
            "structure_role": "mixed",
            "structure_bias": "support",
            "price_state": "support",
            "last_touch_index": 4,
            "broken_index": None,
            "zone_width": 500.0,
        }
    ]


def test_structure_v1_returns_reclaimed_highs_as_support_only() -> None:
    df = pd.DataFrame(
        {
            "open": [66000, 66900, 66500, 67200, 66800],
            "high": [66100, 67000, 66600, 67400, 66900],
            "low": [65900, 66800, 66400, 67100, 66700],
            "close": [66000, 66950, 66500, 67300, 66800],
        }
    )

    result = detect_support_resistance_zones_structure_v1(
        df,
        external_swing_order=1,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        min_touches=1,
        break_atr_mult=0.0,
        current_price=65000,
    )

    assert result["resistance"] == []
    assert result["active"] == []
    zone = result["support"][0]
    assert zone["origin"] == "flipped_resistance"
    assert zone["role"] == "support"
    assert zone["price_state"] == "resistance"
    assert zone["low"] == 66450.0
    assert zone["high"] == 66950.0


def test_structure_v1_adds_retested_long_wick_support_floor() -> None:
    raw_external_pivots = [
        StructurePivot(index=1, kind="low", price=72945.50, body_price=74935.00, atr=1910.03, term="external"),
        StructurePivot(index=2, kind="low", price=74821.57, body_price=75023.43, atr=1047.59, term="external"),
        StructurePivot(index=3, kind="low", price=74937.52, body_price=75563.86, atr=896.60, term="external"),
    ]
    prominent_pivots = [raw_external_pivots[-1]]

    candidates = _support_floor_candidates(
        raw_external_pivots=raw_external_pivots,
        external_pivots=prominent_pivots,
        zone_width=500.0,
    )

    assert [candidate.origin for candidate in candidates] == [
        "structure_swing_low_wick",
        "structure_swing_low_body_floor",
        "structure_swing_low_body_floor",
    ]


def test_local_reaction_zones_use_recent_base_and_retested_rejection_bounds() -> None:
    pivots = [
        _pivot(index=1, kind="high", price=101.0, body_price=100.0),
        _pivot(index=2, kind="high", price=103.0, body_price=102.0),
        _pivot(index=3, kind="low", price=103.0, body_price=110.0),
        _pivot(index=4, kind="low", price=95.0, body_price=105.0),
        _pivot(index=5, kind="low", price=94.0, body_price=106.0),
        _pivot(index=10, kind="low", price=200.0, body_price=206.0),
        _pivot(index=12, kind="high", price=198.0, body_price=197.0),
        _pivot(index=14, kind="low", price=201.0, body_price=204.0),
        _pivot(index=16, kind="low", price=199.0, body_price=205.0),
    ]

    zones = _build_local_reaction_zones(
        internal_pivots=pivots,
        closes=pd.Series([300.0] * 20).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=10.0,
        min_touches=2,
        current_price=250.0,
        buffer_pct=0.0015,
    )

    assert [(zone["low"], zone["high"], zone["touches"]) for zone in zones] == [
        (102.0, 110.0, 5),
        (200.0, 206.0, 4),
    ]
    assert all(zone["origin"] == "local_reaction_support" for zone in zones)
    assert all(zone["bounds_style"] == "local_reaction" for zone in zones)


def test_structure_v1_fills_large_support_gap_with_reclaimed_high_clusters() -> None:
    zones = [
        _support_zone(low=65510.93, high=66010.93, source_closes=[65971.20, 66010.93], score=4.0),
        _support_zone(low=73301.80, high=73801.80, source_closes=[73611.10, 73801.80], score=5.0),
    ]
    raw_external_pivots = [
        _high_pivot(index=1, price=67100.00, body_price=67014.91),
        _high_pivot(index=2, price=67450.00, body_price=67383.66),
        _high_pivot(index=3, price=67650.00, body_price=67502.16),
        _high_pivot(index=4, price=67650.00, body_price=67515.00),
        _high_pivot(index=5, price=67900.00, body_price=67825.91),
        _high_pivot(index=6, price=68150.00, body_price=68076.01),
        _high_pivot(index=7, price=68175.00, body_price=68106.44),
        _high_pivot(index=8, price=70050.00, body_price=69968.87),
        _high_pivot(index=9, price=70200.00, body_price=70131.48),
        _high_pivot(index=10, price=70700.00, body_price=70641.82),
        _high_pivot(index=11, price=70710.00, body_price=70652.73),
        _high_pivot(index=12, price=70800.00, body_price=70731.45),
        _high_pivot(index=13, price=70900.00, body_price=70828.43),
        _high_pivot(index=14, price=70950.00, body_price=70854.66),
    ]

    filled = _fill_support_staircase_gaps(
        zones=zones,
        raw_external_pivots=raw_external_pivots,
        closes=pd.Series([65000.0] * 15 + [80000.0]).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=500.0,
        min_touches=2,
        current_price=76924.65,
        buffer_pct=0.0015,
    )

    stair_steps = sorted(
        [zone for zone in filled if zone["origin"] == "stair_step_flipped_resistance"],
        key=lambda zone: zone["low"],
    )
    assert [(zone["low"], zone["high"], zone["touches"]) for zone in stair_steps] == [
        (67606.44, 68106.44, 7),
        (70354.66, 70854.66, 7),
    ]


def test_structure_v1_fills_staircase_gap_to_next_active_boundary() -> None:
    zones = [
        _support_zone(low=65510.93, high=66010.93, source_closes=[65971.20, 66010.93], score=4.0),
        _support_zone(low=73301.80, high=73801.80, source_closes=[73611.10, 73801.80], score=5.0),
    ]
    zones[1]["price_state"] = "active"
    raw_external_pivots = [
        _high_pivot(index=1, price=67100.00, body_price=67014.91),
        _high_pivot(index=2, price=67450.00, body_price=67383.66),
        _high_pivot(index=3, price=67650.00, body_price=67502.16),
        _high_pivot(index=4, price=67650.00, body_price=67515.00),
        _high_pivot(index=5, price=67900.00, body_price=67825.91),
        _high_pivot(index=6, price=68150.00, body_price=68076.01),
        _high_pivot(index=7, price=68175.00, body_price=68106.44),
        _high_pivot(index=8, price=70050.00, body_price=69968.87),
        _high_pivot(index=9, price=70200.00, body_price=70131.48),
        _high_pivot(index=10, price=70700.00, body_price=70641.82),
        _high_pivot(index=11, price=70710.00, body_price=70652.73),
        _high_pivot(index=12, price=70800.00, body_price=70731.45),
        _high_pivot(index=13, price=70900.00, body_price=70828.43),
        _high_pivot(index=14, price=70950.00, body_price=70854.66),
    ]

    filled = _fill_support_staircase_gaps(
        zones=zones,
        raw_external_pivots=raw_external_pivots,
        closes=pd.Series([65000.0] * 15 + [80000.0]).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=500.0,
        min_touches=2,
        current_price=73258.01,
        buffer_pct=0.0015,
    )

    stair_steps = sorted(
        [zone for zone in filled if zone["origin"] == "stair_step_flipped_resistance"],
        key=lambda zone: zone["low"],
    )
    assert [(zone["low"], zone["high"], zone["touches"]) for zone in stair_steps] == [
        (67606.44, 68106.44, 7),
        (70354.66, 70854.66, 7),
    ]


def test_nearby_reclaimed_high_zone_survives_next_major_level() -> None:
    candidates = [
        SupportCandidate(price=73611.10, index=1, origin="flipped_resistance", structure_role="H"),
        SupportCandidate(price=73801.80, index=2, origin="structure_swing_low", structure_role="L"),
        SupportCandidate(price=74609.36, index=3, origin="structure_swing_low", structure_role="L"),
        SupportCandidate(price=74884.67, index=4, origin="flipped_resistance", structure_role="H"),
        SupportCandidate(
            price=74935.00,
            index=5,
            origin="structure_swing_low_body_floor",
            structure_role="L",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=74937.52,
            index=6,
            origin="structure_swing_low_wick",
            structure_role="L",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=75023.43,
            index=7,
            origin="structure_swing_low_body_floor",
            structure_role="L",
            bounds_style="support_floor",
        ),
    ]

    zones = sorted(
        _build_support_zones(
            candidates,
            zone_width=500.0,
            min_touches=2,
            current_price=73028.00,
            buffer_pct=0.0015,
        ),
        key=lambda zone: zone["low"],
    )

    assert [(zone["low"], zone["high"]) for zone in zones] == [
        (73301.80, 73801.80),
        (74523.43, 75023.43),
    ]


def test_adjacent_close_support_zone_collapses_to_upper_band() -> None:
    lower_band = [
        SupportCandidate(price=63206.70 + offset, index=index, origin="flipped_resistance", structure_role="H")
        for index, offset in enumerate([-150.0, -80.0, 0.0, 40.0, 60.0, 90.0, 120.0, 150.0], start=1)
    ]
    upper_band = [
        SupportCandidate(price=64288.00 + offset, index=index, origin="flipped_resistance", structure_role="H")
        for index, offset in enumerate([-120.0, -60.0, 0.0, 50.0, 90.0, 130.0], start=20)
    ]

    zones = sorted(
        _build_support_zones(
            lower_band + upper_band,
            zone_width=500.0,
            min_touches=2,
            current_price=66148.00,
            buffer_pct=0.0015,
        ),
        key=lambda zone: zone["low"],
    )

    assert [(zone["low"], zone["high"], zone["touches"]) for zone in zones] == [
        (63918.00, 64418.00, 6),
    ]


def test_stronger_adjacent_lower_support_zone_survives_weak_upper_neighbors() -> None:
    from src.zones.build import _collapse_adjacent_close_support_zones

    zones = [
        {"low": 64038.00, "high": 64538.00, "mid": 64288.00, "touches": 6, "score": 13.0, "origin": "flipped_resistance", "bounds_style": "body", "width_pct": 0.778},
        {"low": 65898.00, "high": 66398.00, "mid": 66148.00, "touches": 10, "score": 21.0, "origin": "flipped_resistance", "bounds_style": "body", "width_pct": 0.756},
        {"low": 66699.98, "high": 67199.98, "mid": 66949.98, "touches": 7, "score": 16.0, "origin": "flipped_resistance", "bounds_style": "body", "width_pct": 0.746},
        {"low": 67665.35, "high": 68165.35, "mid": 67915.35, "touches": 2, "score": 5.0, "origin": "flipped_resistance", "bounds_style": "body", "width_pct": 0.736},
    ]

    collapsed = sorted(_collapse_adjacent_close_support_zones(zones), key=lambda zone: zone["low"])

    assert [(zone["low"], zone["high"], zone["touches"]) for zone in collapsed] == [
        (64038.00, 64538.00, 6),
        (65898.00, 66398.00, 10),
        (67665.35, 68165.35, 2),
    ]


def test_support_floor_shelf_is_not_swallowed_by_body_macro_group() -> None:
    candidates = [
        SupportCandidate(
            price=89202.47,
            index=1,
            origin="structure_swing_low_body_floor",
            structure_role="HL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=89256.69,
            index=2,
            origin="structure_swing_low_wick",
            structure_role="LL",
            bounds_style="support_floor",
        ),
        SupportCandidate(price=89945.43, index=3, origin="flipped_resistance", structure_role="LH"),
        SupportCandidate(price=90226.77, index=4, origin="structure_swing_low", structure_role="HL"),
        SupportCandidate(
            price=90405.02,
            index=5,
            origin="structure_swing_low_body_floor",
            structure_role="HL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=90500.00,
            index=6,
            origin="structure_swing_low_wick",
            structure_role="LL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=90504.70,
            index=7,
            origin="structure_swing_low_body_floor",
            structure_role="HL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=90512.10,
            index=8,
            origin="structure_swing_low_body_floor",
            structure_role="LL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=90791.10,
            index=9,
            origin="structure_swing_low_wick",
            structure_role="HL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=90851.23,
            index=10,
            origin="structure_swing_low_body_floor",
            structure_role="LL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=91157.44,
            index=11,
            origin="structure_swing_low_body_floor",
            structure_role="HL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=91231.00,
            index=12,
            origin="structure_swing_low_wick",
            structure_role="LL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=91530.45,
            index=13,
            origin="structure_swing_low_wick",
            structure_role="LL",
            bounds_style="support_floor",
        ),
        SupportCandidate(price=91683.90, index=14, origin="structure_swing_low", structure_role="HL"),
        SupportCandidate(price=92130.21, index=15, origin="structure_swing_low", structure_role="LL"),
    ]

    zones = sorted(
        _build_support_zones(
            candidates,
            zone_width=500.0,
            min_touches=2,
            current_price=100000.00,
            buffer_pct=0.0015,
        ),
        key=lambda zone: zone["low"],
    )

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in zones] == [
        (89202.47, 89702.47, "structure_support_floor"),
        (90351.23, 90851.23, "flipped_resistance"),
        (91630.21, 92130.21, "structure_swing_low"),
    ]


def test_body_low_and_nearby_floor_gap_bridges_to_manual_support_band() -> None:
    candidates = [
        SupportCandidate(
            price=56078.54,
            index=1031,
            origin="structure_swing_low_wick",
            structure_role="HL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=56552.82,
            index=392,
            origin="structure_swing_low_wick",
            structure_role="LL",
            bounds_style="support_floor",
        ),
        SupportCandidate(price=57046.34, index=1031, origin="structure_swing_low", structure_role="HL"),
        SupportCandidate(price=57500.00, index=392, origin="structure_swing_low", structure_role="LL"),
        SupportCandidate(
            price=58396.95,
            index=1052,
            origin="structure_swing_low_body_floor",
            structure_role="HL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=58402.00,
            index=719,
            origin="structure_swing_low_wick",
            structure_role="LL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=58898.85,
            index=1108,
            origin="structure_swing_low_body_floor",
            structure_role="LL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=58946.00,
            index=1366,
            origin="structure_swing_low_wick",
            structure_role="HL",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=59005.00,
            index=52,
            origin="structure_swing_low_wick",
            structure_role="L",
            bounds_style="support_floor",
        ),
        SupportCandidate(
            price=59130.91,
            index=4984,
            origin="structure_swing_low_wick",
            structure_role="LL",
            bounds_style="support_floor",
        ),
    ]

    zones = sorted(
        _build_support_zones(
            candidates,
            zone_width=500.0,
            min_touches=2,
            current_price=65851.99,
            buffer_pct=0.0015,
        ),
        key=lambda zone: zone["low"],
    )

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in zones] == [
        (57500.00, 58000.00, "mixed_structure"),
    ]


def test_support_bands_anchor_to_support_upper_anchor() -> None:
    low, high = _fixed_support_zone_bounds(
        [
            65776.47,
            65788.36,
            65971.2,
            66010.93,
            66040.66,
            66220.28,
            66398.0,
            66519.73,
            66685.69,
            66820.64,
            67014.91,
        ],
        zone_width=500.0,
    )

    assert low == 65288.36
    assert high == 65788.36


def test_internal_and_external_structure_pivots_have_different_granularity() -> None:
    df = _ohlc_from_closes([100, 110, 105, 112, 108, 120, 90, 115, 80, 118, 70, 125, 100], wick=0.5)
    atr = _average_true_range(
        highs=df["high"].to_numpy(dtype=float),
        lows=df["low"].to_numpy(dtype=float),
        closes=df["close"].to_numpy(dtype=float),
        period=3,
    )

    internal = _find_structure_pivots(df, bars_each_side=1, atr=atr, term="internal")
    external = _find_structure_pivots(df, bars_each_side=3, atr=atr, term="external")

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


def test_structure_v1_output_is_signal_compatible() -> None:
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
    assert signal["nearest_resistance_low"] is None
    assert signal["distance_to_resistance_pct"] is None


def _ohlc_from_closes(closes: list[float], wick: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [close + wick for close in closes],
            "low": [close - wick for close in closes],
            "close": closes,
        }
    )


def _support_zone(low: float, high: float, source_closes: list[float], score: float) -> dict:
    mid = (low + high) / 2.0
    return {
        "origin": "structure_swing_low",
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
        "structure_bias": "support",
        "price_state": "support",
        "last_touch_index": len(source_closes) - 1,
        "broken_index": None,
        "zone_width": high - low,
    }


def _high_pivot(index: int, price: float, body_price: float) -> StructurePivot:
    return StructurePivot(
        index=index,
        kind="high",
        price=price,
        body_price=body_price,
        atr=0.0,
        term="external",
        structure_role="H",
    )


def _pivot(index: int, kind: str, price: float, body_price: float) -> StructurePivot:
    return StructurePivot(
        index=index,
        kind=kind,
        price=price,
        body_price=body_price,
        atr=0.0,
        term="internal",
        structure_role="H" if kind == "high" else "L",
    )
