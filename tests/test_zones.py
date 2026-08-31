from __future__ import annotations

import pandas as pd

import src.zones.detector as detector_module

from src.zones import (
    StructurePivot,
    SupportCandidate,
    ZoneDetectorEvidence,
    _average_true_range,
    _build_daily_body_support_zones,
    _build_local_reaction_zones,
    _build_persistent_wick_floor_zones,
    _build_split_rejection_zone_pairs,
    _enforce_support_zone_spacing,
    _filter_prominent_structure_pivots,
    _fill_support_staircase_gaps,
    _find_structure_pivots,
    _fixed_support_zone_bounds,
    _label_structure_pivots,
    _overlay_daily_support_zones,
    _overlay_persistent_wick_floors,
    _overlay_split_rejection_zones,
    _support_floor_candidates,
    aggregate_ohlc_to_daily,
    detect_support_resistance_zones_structure_v1,
    extract_zone_detector_evidence,
    materialize_support_zones,
)
from src.zones.postprocess import _fill_persistent_wick_floor_gaps
from src.zones.build import _build_support_zones, _suppress_nearby_support_zones


def test_empty_or_insufficient_data_returns_empty_zones() -> None:
    assert detect_support_resistance_zones_structure_v1(pd.DataFrame()) == {
        "support": [],
        "all": [],
    }
    result = detect_support_resistance_zones_structure_v1(_ohlc_from_closes([1, 2, 1], wick=0.1), external_swing_order=2)
    assert result == {"support": [], "all": []}
    assert extract_zone_detector_evidence(pd.DataFrame()) is None
    assert extract_zone_detector_evidence(_ohlc_from_closes([1, 2, 1], wick=0.1), external_swing_order=2) is None


# Public detector is only extract-then-materialize; both halves must stay in lockstep.
def test_extract_then_materialize_matches_public_detector() -> None:
    df = _ohlc_from_closes(
        [67000, 66500, 66000, 66800, 67500, 66900, 66400, 66380, 67000, 67600],
        wick=25,
    )
    kwargs = {
        "external_swing_order": 2,
        "atr_period": 3,
        "external_min_swing_atr_mult": 0.0,
        "external_min_swing_pct": 0.0,
        "min_touches": 2,
        "break_atr_mult": 0.0,
        "current_price": 67600.0,
        "buffer_pct": 0.0015,
    }

    expected = detect_support_resistance_zones_structure_v1(df, **kwargs)
    evidence = extract_zone_detector_evidence(
        df,
        current_price=kwargs["current_price"],
        external_swing_order=kwargs["external_swing_order"],
        atr_period=kwargs["atr_period"],
        break_atr_mult=kwargs["break_atr_mult"],
        external_min_swing_atr_mult=kwargs["external_min_swing_atr_mult"],
        external_min_swing_pct=kwargs["external_min_swing_pct"],
    )

    assert evidence is not None
    assert isinstance(evidence, ZoneDetectorEvidence)
    assert evidence.raw_external_pivots
    assert evidence.external_pivots
    assert materialize_support_zones(
        evidence,
        min_touches=kwargs["min_touches"],
        buffer_pct=kwargs["buffer_pct"],
        break_atr_mult=kwargs["break_atr_mult"],
    ) == expected


# A later close through a high is frozen on evidence as that bar's index.
def test_extract_records_first_reclaim_index_for_reclaimed_highs() -> None:
    df = pd.DataFrame(
        {
            "open": [66000, 66900, 66500, 67200, 66800],
            "high": [66100, 67000, 66600, 67400, 66900],
            "low": [65900, 66800, 66400, 67100, 66700],
            "close": [66000, 66950, 66500, 67300, 66800],
        }
    )

    evidence = extract_zone_detector_evidence(
        df,
        current_price=65000,
        external_swing_order=1,
        break_atr_mult=0.0,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
    )

    assert evidence is not None
    assert evidence.first_reclaim_indexes[("external", 1)] == 3
    high_pivots = [pivot for pivot in evidence.external_pivots if pivot.kind == "high"]
    assert any(pivot.index == 1 for pivot in high_pivots)


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

    zone = result["support"][0]
    assert zone["origin"] == "flipped_resistance"
    assert zone["role"] == "support"
    assert zone["price_state"] == "resistance"
    assert zone["low"] == 66450.0
    assert zone["high"] == 66950.0


def test_structure_v1_adds_retested_long_wick_support_floor() -> None:
    raw_external_pivots = [
        StructurePivot(index=1, kind="low", wick_price=72945.50, body_price=74935.00, atr=1910.03, term="external"),
        StructurePivot(index=2, kind="low", wick_price=74821.57, body_price=75023.43, atr=1047.59, term="external"),
        StructurePivot(index=3, kind="low", wick_price=74937.52, body_price=75563.86, atr=896.60, term="external"),
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
        _pivot(index=1, kind="high", wick_price=101.0, body_price=100.0),
        _pivot(index=2, kind="high", wick_price=103.0, body_price=102.0),
        _pivot(index=3, kind="low", wick_price=103.0, body_price=110.0),
        _pivot(index=4, kind="low", wick_price=95.0, body_price=105.0),
        _pivot(index=5, kind="low", wick_price=94.0, body_price=106.0),
        _pivot(index=10, kind="low", wick_price=200.0, body_price=206.0),
        _pivot(index=12, kind="high", wick_price=198.0, body_price=197.0),
        _pivot(index=14, kind="low", wick_price=201.0, body_price=204.0),
        _pivot(index=16, kind="low", wick_price=199.0, body_price=205.0),
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


def test_local_retested_flip_zone_survives_split_greedy_clusters() -> None:
    pivots = [
        _pivot(index=1, kind="low", wick_price=95.0, body_price=96.0),
        _pivot(index=2, kind="low", wick_price=96.0, body_price=97.0),
        _pivot(index=3, kind="high", wick_price=100.0, body_price=100.0),
        _pivot(index=5, kind="low", wick_price=102.0, body_price=103.0),
        _pivot(index=6, kind="low", wick_price=101.5, body_price=102.0),
    ]

    zones = _build_local_reaction_zones(
        internal_pivots=pivots,
        closes=pd.Series([99.0, 99.0, 99.0, 101.0, 102.0, 103.0, 101.0]).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=5.0,
        min_touches=2,
        current_price=99.0,
        buffer_pct=0.0015,
    )

    assert any(
        zone["origin"] == "local_retested_flip_support"
        and zone["low"] == 100.0
        and zone["high"] == 103.0
        and zone["price_state"] == "resistance"
        for zone in zones
    )


def test_local_reaction_zone_can_use_local_low_wick_to_body_bounds() -> None:
    pivots = [
        _pivot(index=1, kind="low", wick_price=100.0, body_price=106.0),
        _pivot(index=2, kind="high", wick_price=102.0, body_price=102.0),
        _pivot(index=3, kind="low", wick_price=103.0, body_price=104.0),
    ]

    zones = _build_local_reaction_zones(
        internal_pivots=pivots,
        closes=pd.Series([101.0, 106.0, 102.0, 104.0]).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=10.0,
        min_touches=2,
        current_price=109.0,
        buffer_pct=0.0015,
    )

    assert [(zone["low"], zone["high"], zone["origin"], zone["bounds_style"]) for zone in zones] == [
        (100.0, 106.0, "local_reaction_support", "local_reaction")
    ]


def test_structure_v1_fills_large_support_gap_with_reclaimed_high_clusters() -> None:
    zones = [
        _support_zone(low=65510.93, high=66010.93, source_closes=[65971.20, 66010.93], score=4.0),
        _support_zone(low=73301.80, high=73801.80, source_closes=[73611.10, 73801.80], score=5.0),
    ]
    raw_external_pivots = [
        _high_pivot(index=1, wick_price=67100.00, body_price=67014.91),
        _high_pivot(index=2, wick_price=67450.00, body_price=67383.66),
        _high_pivot(index=3, wick_price=67650.00, body_price=67502.16),
        _high_pivot(index=4, wick_price=67650.00, body_price=67515.00),
        _high_pivot(index=5, wick_price=67900.00, body_price=67825.91),
        _high_pivot(index=6, wick_price=68150.00, body_price=68076.01),
        _high_pivot(index=7, wick_price=68175.00, body_price=68106.44),
        _high_pivot(index=8, wick_price=70050.00, body_price=69968.87),
        _high_pivot(index=9, wick_price=70200.00, body_price=70131.48),
        _high_pivot(index=10, wick_price=70700.00, body_price=70641.82),
        _high_pivot(index=11, wick_price=70710.00, body_price=70652.73),
        _high_pivot(index=12, wick_price=70800.00, body_price=70731.45),
        _high_pivot(index=13, wick_price=70900.00, body_price=70828.43),
        _high_pivot(index=14, wick_price=70950.00, body_price=70854.66),
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
        _high_pivot(index=1, wick_price=67100.00, body_price=67014.91),
        _high_pivot(index=2, wick_price=67450.00, body_price=67383.66),
        _high_pivot(index=3, wick_price=67650.00, body_price=67502.16),
        _high_pivot(index=4, wick_price=67650.00, body_price=67515.00),
        _high_pivot(index=5, wick_price=67900.00, body_price=67825.91),
        _high_pivot(index=6, wick_price=68150.00, body_price=68076.01),
        _high_pivot(index=7, wick_price=68175.00, body_price=68106.44),
        _high_pivot(index=8, wick_price=70050.00, body_price=69968.87),
        _high_pivot(index=9, wick_price=70200.00, body_price=70131.48),
        _high_pivot(index=10, wick_price=70700.00, body_price=70641.82),
        _high_pivot(index=11, wick_price=70710.00, body_price=70652.73),
        _high_pivot(index=12, wick_price=70800.00, body_price=70731.45),
        _high_pivot(index=13, wick_price=70900.00, body_price=70828.43),
        _high_pivot(index=14, wick_price=70950.00, body_price=70854.66),
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


def test_structure_v1_uses_internal_reclaimed_high_cluster_to_fill_large_gap() -> None:
    zones = [
        _support_zone(low=46559.99, high=47059.99, source_closes=[46700.0, 46900.0, 47059.99], score=7.0),
        _support_zone(low=51894.04, high=52394.04, source_closes=[52000.0, 52100.0, 52200.0, 52394.04], score=9.0),
    ]
    raw_external_pivots = [
        _high_pivot(index=1, wick_price=50000.0, body_price=49917.28),
        _high_pivot(index=4, wick_price=51600.0, body_price=51509.99),
    ]
    internal_pivots = [
        _high_pivot(index=1, wick_price=50000.0, body_price=49917.80),
        _high_pivot(index=2, wick_price=50020.0, body_price=49917.28),
        _high_pivot(index=3, wick_price=50040.0, body_price=49988.12),
        _high_pivot(index=4, wick_price=49900.0, body_price=49699.59),
    ]

    filled = _fill_support_staircase_gaps(
        zones=zones,
        raw_external_pivots=raw_external_pivots,
        internal_pivots=internal_pivots,
        closes=pd.Series([48000.0] * 5 + [60000.0]).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=500.0,
        min_touches=2,
        current_price=64183.27,
        buffer_pct=0.0015,
    )

    stair_steps = [zone for zone in filled if zone["origin"] == "stair_step_flipped_resistance"]
    assert [(zone["low"], zone["high"], zone["touches"]) for zone in stair_steps] == [
        (49488.12, 49988.12, 4),
    ]


def test_persistent_floor_gap_recovers_reclaimed_high_zone_above_current_price() -> None:
    lower = _support_zone(low=68620.82, high=69120.82, source_closes=[68620.82], score=2.0)
    lower["origin"] = "persistent_wick_floor"
    lower["price_state"] = "resistance"
    upper = _support_zone(low=72945.50, high=73445.50, source_closes=[72945.50], score=2.0)
    upper["origin"] = "persistent_wick_floor"
    upper["price_state"] = "resistance"
    pivots = [
        _high_pivot(index=1, wick_price=70700.00, body_price=70641.82),
        _high_pivot(index=2, wick_price=70800.00, body_price=70731.45),
        _high_pivot(index=3, wick_price=70950.00, body_price=70854.66),
    ]

    filled = _fill_persistent_wick_floor_gaps(
        zones=[lower, upper],
        raw_external_pivots=pivots,
        closes=pd.Series([63000.0, 63000.0, 63000.0, 63000.0, 80000.0]).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=500.0,
        min_touches=2,
        current_price=63093.99,
        buffer_pct=0.0015,
    )

    assert [(zone["low"], zone["high"], zone["origin"], zone["touches"]) for zone in filled] == [
        (68620.82, 69120.82, "persistent_wick_floor", 1),
        (70354.66, 70854.66, "stair_step_flipped_resistance", 3),
        (72945.50, 73445.50, "persistent_wick_floor", 1),
    ]


def test_final_ladder_gap_fill_recovers_zone_between_local_flip_and_persistent_floor() -> None:
    # Jul-style pair: local 63650-63917.88 then persistent 66082.66-66582.66.
    # Edge gap is ~$2165, so the old $4000 rule skipped it, and the mixed origins
    # also failed the persistent-only gate. A dense cluster near A would be
    # collapsed onto by _build_support_zones; cluster-only keeps the middle shelf.
    lower, upper = _mixed_origin_64k_ladder_pair()
    pivots = [
        _high_pivot(index=1, wick_price=64500.0, body_price=64420.0),
        _high_pivot(index=2, wick_price=64510.0, body_price=64430.0),
        _high_pivot(index=3, wick_price=64520.0, body_price=64440.0),
        _high_pivot(index=4, wick_price=64530.0, body_price=64445.0),
        _high_pivot(index=5, wick_price=64540.0, body_price=64450.0),
        _high_pivot(index=6, wick_price=65400.0, body_price=65300.0),
        _high_pivot(index=7, wick_price=65450.0, body_price=65350.0),
    ]

    filled = _fill_persistent_wick_floor_gaps(
        zones=[lower, upper],
        raw_external_pivots=pivots,
        closes=pd.Series([62000.0] * 8 + [80000.0]).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=500.0,
        min_touches=2,
        current_price=62000.0,
        buffer_pct=0.0015,
    )

    assert [(zone["low"], zone["high"], zone["origin"], zone["touches"]) for zone in filled] == [
        (63650.0, 63917.88, "local_retested_flip_support", 2),
        (64850.0, 65350.0, "stair_step_flipped_resistance", 2),
        (66082.66, 66582.66, "persistent_wick_floor", 1),
    ]


def test_near_price_gap_fill_recovers_65300_zone_with_local_spacing_profile() -> None:
    # Aug 7-style pair: the $1712.66 edge gap is below the regular $1800
    # minimum but can hold a strong reclaimed-high stair with $450 clearance.
    lower = _support_zone(low=64085.36, high=64370.0, source_closes=[64085.36, 64370.0], score=9.0)
    lower["origin"] = "local_retested_flip_support"
    lower["bounds_style"] = "local_reaction"
    lower["price_state"] = "active"
    upper = _support_zone(low=66082.66, high=66582.66, source_closes=[66082.66], score=2.0)
    upper["origin"] = "persistent_wick_floor"
    upper["bounds_style"] = "support_floor"
    upper["price_state"] = "resistance"
    pivots = [
        _high_pivot(index=1, wick_price=65200.0, body_price=65104.02),
        _high_pivot(index=2, wick_price=65250.0, body_price=65170.0),
        _high_pivot(index=3, wick_price=65320.0, body_price=65229.03),
        _high_pivot(index=4, wick_price=65450.0, body_price=65354.02),
    ]

    filled = _fill_persistent_wick_floor_gaps(
        zones=[lower, upper],
        raw_external_pivots=pivots,
        closes=pd.Series([63000.0] * 5 + [80000.0]).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=500.0,
        min_touches=2,
        current_price=64319.84,
        buffer_pct=0.0015,
        near_price_gap_fill_edge_clearance=450.0,
        near_price_gap_fill_midpoint_spacing=850.0,
        near_price_gap_fill_min_touches=4,
    )
    spaced = _enforce_support_zone_spacing(
        filled,
        near_price_gap_fill_edge_clearance=450.0,
        near_price_gap_fill_midpoint_spacing=850.0,
        clear_near_price_gap_fill_marker=True,
    )

    assert [(zone["low"], zone["high"], zone["origin"], zone["touches"]) for zone in spaced] == [
        (64085.36, 64370.0, "local_retested_flip_support", 2),
        (64854.02, 65354.02, "stair_step_flipped_resistance", 4),
        (66082.66, 66582.66, "persistent_wick_floor", 1),
    ]
    assert all("_near_price_gap_fill" not in zone for zone in spaced)


def test_default_near_price_profile_matches_regular_spacing_and_skips_1712_gap() -> None:
    # Default clearance is now $650, so a $1712 gap cannot hold a $500 band plus two clearances.
    lower = _support_zone(low=64085.36, high=64370.0, source_closes=[64085.36, 64370.0], score=9.0)
    lower["origin"] = "local_retested_flip_support"
    lower["bounds_style"] = "local_reaction"
    lower["price_state"] = "active"
    upper = _support_zone(low=66082.66, high=66582.66, source_closes=[66082.66], score=2.0)
    upper["origin"] = "persistent_wick_floor"
    upper["bounds_style"] = "support_floor"
    upper["price_state"] = "resistance"
    pivots = [
        _high_pivot(index=1, wick_price=65200.0, body_price=65104.02),
        _high_pivot(index=2, wick_price=65250.0, body_price=65170.0),
        _high_pivot(index=3, wick_price=65320.0, body_price=65229.03),
        _high_pivot(index=4, wick_price=65450.0, body_price=65354.02),
    ]

    filled = _fill_persistent_wick_floor_gaps(
        zones=[lower, upper],
        raw_external_pivots=pivots,
        closes=pd.Series([63000.0] * 5 + [80000.0]).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=500.0,
        min_touches=2,
        current_price=64319.84,
        buffer_pct=0.0015,
    )

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in filled] == [
        (64085.36, 64370.0, "local_retested_flip_support"),
        (66082.66, 66582.66, "persistent_wick_floor"),
    ]


def test_near_price_gap_fill_requires_configured_touch_floor() -> None:
    # Three reclaimed highs meet regular min_touches=2 but not the safer local fallback floor of four.
    lower = _support_zone(low=64085.36, high=64370.0, source_closes=[64085.36, 64370.0], score=9.0)
    lower["origin"] = "local_retested_flip_support"
    lower["price_state"] = "active"
    upper = _support_zone(low=66082.66, high=66582.66, source_closes=[66082.66], score=2.0)
    upper["origin"] = "persistent_wick_floor"
    upper["price_state"] = "resistance"
    pivots = [
        _high_pivot(index=1, wick_price=65200.0, body_price=65104.02),
        _high_pivot(index=2, wick_price=65250.0, body_price=65170.0),
        _high_pivot(index=3, wick_price=65450.0, body_price=65354.02),
    ]

    filled = _fill_persistent_wick_floor_gaps(
        zones=[lower, upper],
        raw_external_pivots=pivots,
        closes=pd.Series([63000.0] * 4 + [80000.0]).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=500.0,
        min_touches=2,
        current_price=64319.84,
        buffer_pct=0.0015,
    )

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in filled] == [
        (64085.36, 64370.0, "local_retested_flip_support"),
        (66082.66, 66582.66, "persistent_wick_floor"),
    ]


def test_final_ladder_gap_fill_skips_when_reclaimed_highs_are_not_confirmed() -> None:
    lower, upper = _mixed_origin_64k_ladder_pair()
    pivots = [
        _high_pivot(index=1, wick_price=65400.0, body_price=65300.0),
        _high_pivot(index=2, wick_price=65450.0, body_price=65350.0),
    ]

    filled = _fill_persistent_wick_floor_gaps(
        zones=[lower, upper],
        raw_external_pivots=pivots,
        closes=pd.Series([62000.0] * 10).to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=500.0,
        min_touches=2,
        current_price=62000.0,
        buffer_pct=0.0015,
    )

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in filled] == [
        (63650.0, 63917.88, "local_retested_flip_support"),
        (66082.66, 66582.66, "persistent_wick_floor"),
    ]


def test_structural_factory_collapses_middle_gap_cluster_into_lower_neighbor() -> None:
    # Same decoy + middle prices as the mixed-origin fill test. Macro-merge and
    # adjacent collapse inside _build_support_zones keep only the denser 64.45k
    # cluster, which then shares a ladder slot with zone A.
    candidates = [
        SupportCandidate(price=64420.0, index=1, origin="stair_step_flipped_resistance", structure_role="H"),
        SupportCandidate(price=64430.0, index=2, origin="stair_step_flipped_resistance", structure_role="H"),
        SupportCandidate(price=64440.0, index=3, origin="stair_step_flipped_resistance", structure_role="H"),
        SupportCandidate(price=64445.0, index=4, origin="stair_step_flipped_resistance", structure_role="H"),
        SupportCandidate(price=64450.0, index=5, origin="stair_step_flipped_resistance", structure_role="H"),
        SupportCandidate(price=65300.0, index=6, origin="stair_step_flipped_resistance", structure_role="H"),
        SupportCandidate(price=65350.0, index=7, origin="stair_step_flipped_resistance", structure_role="H"),
    ]

    zones = _build_support_zones(
        candidates,
        zone_width=500.0,
        min_touches=2,
        current_price=62000.0,
        buffer_pct=0.0015,
    )

    assert [(zone["low"], zone["high"], zone["touches"]) for zone in zones] == [
        (63950.0, 64450.0, 5),
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


def test_daily_aggregation_can_require_complete_four_hour_days() -> None:
    candles = _four_hour_day(
        day_index=0,
        open_price=100.0,
        high_price=110.0,
        low_price=90.0,
        close_price=105.0,
    )
    candles.extend(
        _four_hour_day(
            day_index=1,
            open_price=105.0,
            high_price=115.0,
            low_price=95.0,
            close_price=111.0,
        )[:5]
    )

    daily = aggregate_ohlc_to_daily(pd.DataFrame(candles), min_bars_per_day=6)

    assert len(daily) == 1
    assert float(daily.iloc[0]["open"]) == 100.0
    assert float(daily.iloc[0]["high"]) == 110.0
    assert float(daily.iloc[0]["low"]) == 90.0
    assert float(daily.iloc[0]["close"]) == 105.0
    assert daily.iloc[0]["timeframe"] == "1d"


def test_daily_body_support_zone_anchors_from_daily_low_pivot_body_low() -> None:
    candles: list[dict[str, float | int]] = []
    candles.extend(_four_hour_day(0, open_price=65000.0, high_price=66000.0, low_price=63000.0, close_price=64000.0))
    candles.extend(_four_hour_day(1, open_price=64000.0, high_price=65000.0, low_price=61000.0, close_price=62000.0))
    candles.extend(_four_hour_day(2, open_price=60672.01, high_price=60841.63, low_price=56552.82, close_price=58364.97))
    candles.extend(_four_hour_day(3, open_price=58364.97, high_price=62000.0, low_price=59000.0, close_price=61000.0))
    candles.extend(_four_hour_day(4, open_price=61000.0, high_price=65000.0, low_price=62000.0, close_price=64000.0))

    zones = _build_daily_body_support_zones(
        pd.DataFrame(candles),
        zone_width=500.0,
        current_price=65000.0,
        buffer_pct=0.0015,
        external_swing_order=1,
        atr_period=3,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
    )

    assert [(zone["low"], zone["high"], zone["origin"], zone["source_timeframe"]) for zone in zones] == [
        (57864.97, 58364.97, "daily_body_support", "1d")
    ]
    assert zones[0]["source_closes"] == [58364.97]


def test_daily_body_support_replaces_overlapping_mixed_structure_bridge_zone() -> None:
    four_hour_zone = _support_zone(
        low=57500.0,
        high=58000.0,
        source_closes=[57046.34, 57500.0, 58396.95, 58402.0],
        score=20.0,
    )
    four_hour_zone["origin"] = "mixed_structure"
    daily_zone = _support_zone(low=57864.97, high=58364.97, source_closes=[58364.97], score=102.0)
    daily_zone["origin"] = "daily_body_support"
    daily_zone["source_timeframe"] = "1d"

    zones = _overlay_daily_support_zones([four_hour_zone], [daily_zone])

    assert [(zone["low"], zone["high"], zone["origin"], zone.get("source_timeframe")) for zone in zones] == [
        (57864.97, 58364.97, "daily_body_support", "1d")
    ]


def test_daily_body_support_replaces_adjacent_flipped_resistance_above() -> None:
    flipped_zone = _support_zone(
        low=62956.70,
        high=63456.70,
        source_closes=[63206.70] * 9,
        score=19.0,
    )
    flipped_zone["origin"] = "flipped_resistance"
    daily_zone = _support_zone(low=62409.87, high=62909.87, source_closes=[62909.87], score=102.0)
    daily_zone["origin"] = "daily_body_support"
    daily_zone["source_timeframe"] = "1d"

    zones = _overlay_daily_support_zones([flipped_zone], [daily_zone])

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in zones] == [
        (62409.87, 62909.87, "daily_body_support")
    ]


def test_deep_rejection_with_quick_higher_low_builds_two_support_shelves() -> None:
    ohlc = pd.DataFrame(
        {
            "open": [62000.0, 60438.0, 60300.24, 60687.05],
            "high": [62500.0, 61547.24, 62000.0, 61276.95],
            "low": [61000.0, 59130.91, 59940.01, 59500.0],
            "close": [61500.0, 60300.24, 61056.47, 61004.95],
        }
    )
    rejection_low = StructurePivot(
        index=1,
        kind="low",
        wick_price=59130.91,
        body_price=60300.24,
        atr=1763.58,
        term="external",
        structure_role="LL",
    )
    retest_low = StructurePivot(
        index=3,
        kind="low",
        wick_price=59500.0,
        body_price=60687.05,
        atr=1819.91,
        term="internal",
        structure_role="HL",
    )

    pairs = _build_split_rejection_zone_pairs(
        ohlc=ohlc,
        external_pivots=[rejection_low],
        internal_pivots=[retest_low],
        zone_width=500.0,
        current_price=63800.0,
        buffer_pct=0.0015,
    )

    assert [[(zone["low"], zone["high"], zone["origin"]) for zone in pair] for pair in pairs] == [
        [
            (59130.91, 59500.0, "wick_retest_support"),
            (60438.0, 60938.0, "body_rejection_support"),
        ]
    ]


def test_split_rejection_pair_replaces_mixed_structure_inside_it() -> None:
    mixed_zone = _support_zone(
        low=59500.0,
        high=60000.0,
        source_closes=[59306.72, 59727.28, 59557.99, 59577.01, 59600.01, 60000.0],
        score=12.0,
    )
    mixed_zone["origin"] = "mixed_structure"
    lower_zone = _support_zone(low=59130.91, high=59500.0, source_closes=[59130.91, 59500.0], score=4.0)
    lower_zone["origin"] = "wick_retest_support"
    upper_zone = _support_zone(low=60438.0, high=60938.0, source_closes=[60438.0, 60687.05], score=4.0)
    upper_zone["origin"] = "body_rejection_support"

    zones = _overlay_split_rejection_zones([mixed_zone], [(lower_zone, upper_zone)])

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in zones] == [
        (59130.91, 59500.0, "wick_retest_support"),
        (60438.0, 60938.0, "body_rejection_support"),
    ]


def test_persistent_wick_floor_pins_long_wick_low_plus_500() -> None:
    # Mar 5 2024 dump (~4% wick) pins wick→wick+$500. The later ~2% overlapping dump does not.
    zones = _build_persistent_wick_floor_zones(
        [
            StructurePivot(
                index=2,
                kind="low",
                wick_price=59005.0,
                body_price=61410.98,
                atr=1.0,
                term="external",
                structure_role="L",
            ),
            StructurePivot(index=3, kind="high", wick_price=73777.0, body_price=73000.0, atr=1.0, term="external"),
            StructurePivot(
                index=8,
                kind="low",
                wick_price=59130.91,
                body_price=60300.24,
                atr=1.0,
                term="external",
                structure_role="LL",
            ),
        ],
        zone_width=500.0,
        current_price=63000.0,
        buffer_pct=0.0015,
    )

    assert [(zone["low"], zone["high"], zone["origin"], zone["touches"]) for zone in zones] == [
        (59005.0, 59505.0, "persistent_wick_floor", 1),
    ]
    assert zones[0]["source_closes"] == [59005.0]
    assert zones[0]["source_indexes"] == [2]


def test_persistent_wick_floor_ignores_short_wicks() -> None:
    # $500 wick at 59005 is only ~0.85% of price, so it is not a persistent dump.
    zones = _build_persistent_wick_floor_zones(
        [
            StructurePivot(index=1, kind="low", wick_price=59005.0, body_price=59505.0, atr=1.0, term="external"),
        ],
        zone_width=500.0,
        current_price=63000.0,
        buffer_pct=0.0015,
    )

    assert zones == []


def test_persistent_wick_floor_uses_percent_of_wick_price() -> None:
    # 2% of 50000 is $1000: body exactly at the cutoff pins, one dollar short does not.
    pinned = _build_persistent_wick_floor_zones(
        [
            StructurePivot(index=1, kind="low", wick_price=50000.0, body_price=51000.0, atr=1.0, term="external"),
        ],
        zone_width=500.0,
        current_price=52000.0,
        buffer_pct=0.0015,
    )
    skipped = _build_persistent_wick_floor_zones(
        [
            StructurePivot(index=1, kind="low", wick_price=50000.0, body_price=50999.0, atr=1.0, term="external"),
        ],
        zone_width=500.0,
        current_price=52000.0,
        buffer_pct=0.0015,
    )

    assert [(zone["low"], zone["high"]) for zone in pinned] == [(50000.0, 50500.0)]
    assert skipped == []


def test_overlay_persistent_wick_floors_keeps_oldest_and_drops_overlapping_swing() -> None:
    mixed = _support_zone(low=59000.0, high=59500.0, source_closes=[59000.0], score=12.0)
    mixed["origin"] = "mixed_structure"
    daily = _support_zone(low=57864.97, high=58364.97, source_closes=[58364.97], score=102.0)
    daily["origin"] = "daily_body_support"
    older = _support_zone(low=59005.0, high=59505.0, source_closes=[59005.0], score=2.0)
    older["origin"] = "persistent_wick_floor"
    older["source_indexes"] = [2]
    newer = _support_zone(low=59130.91, high=59630.91, source_closes=[59130.91], score=2.0)
    newer["origin"] = "persistent_wick_floor"
    newer["source_indexes"] = [8]
    deeper = _support_zone(low=56552.82, high=57052.82, source_closes=[56552.82], score=2.0)
    deeper["origin"] = "persistent_wick_floor"
    deeper["source_indexes"] = [4]

    zones = _overlay_persistent_wick_floors([mixed, daily], [older, newer, deeper])

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in zones] == [
        (56552.82, 57052.82, "persistent_wick_floor"),
        (57864.97, 58364.97, "daily_body_support"),
        (59005.0, 59505.0, "persistent_wick_floor"),
    ]


def test_enforce_support_zone_spacing_keeps_older_persistent_in_same_slot() -> None:
    # Gap $495 and midpoint gap $995 both sit inside the $650 / $1000 window.
    older = _support_zone(low=59005.0, high=59505.0, source_closes=[59005.0], score=2.0)
    older["origin"] = "persistent_wick_floor"
    older["source_indexes"] = [2]
    newer = _support_zone(low=60000.0, high=60500.0, source_closes=[60000.0], score=2.0)
    newer["origin"] = "persistent_wick_floor"
    newer["source_indexes"] = [8]

    zones = _enforce_support_zone_spacing([newer, older])

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in zones] == [
        (59005.0, 59505.0, "persistent_wick_floor"),
    ]


def test_enforce_support_zone_spacing_persistent_outranks_nearby_swing() -> None:
    # A stronger mixed band just above 59505 must not crowd out the pinned Mar 5 floor.
    persistent = _support_zone(low=59005.0, high=59505.0, source_closes=[59005.0], score=2.0)
    persistent["origin"] = "persistent_wick_floor"
    persistent["source_indexes"] = [2]
    mixed = _support_zone(low=59800.0, high=60300.0, source_closes=[59800.0], score=12.0)
    mixed["origin"] = "mixed_structure"

    zones = _enforce_support_zone_spacing([mixed, persistent])

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in zones] == [
        (59005.0, 59505.0, "persistent_wick_floor"),
    ]


def test_early_cross_family_suppress_would_drop_structural_64k() -> None:
    # Score-only midpoint suppress is why the detector must not mix families early:
    # local 63.3k (score 15) sits $937 from structural 64k (score 13).
    _persistent, local, structural = _jul9_conflict_zones()

    kept = _suppress_nearby_support_zones([structural, local])

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in kept] == [
        (63294.72, 63405.99, "local_reaction_support"),
    ]


def test_detector_keeps_structural_after_persistent_drops_nearby_local(monkeypatch) -> None:
    # Jul 9 16:00 UTC chain: floor 62.5k outranks local 63.3k, structural 64k stays
    # because it does not share a ladder slot with the floor.
    persistent, local, structural = _jul9_conflict_zones()
    df = _ohlc_from_closes(
        [67000, 66500, 66000, 66800, 67500, 66900, 66400, 66380, 67000, 67600],
        wick=25,
    )
    monkeypatch.setattr(detector_module, "_build_support_zones", lambda *_args, **_kwargs: [dict(structural)])
    monkeypatch.setattr(detector_module, "_build_local_reaction_zones", lambda *_args, **_kwargs: [dict(local)])
    monkeypatch.setattr(
        detector_module,
        "_build_persistent_wick_floor_zones",
        lambda *_args, **_kwargs: [dict(persistent)],
    )
    monkeypatch.setattr(detector_module, "_build_split_rejection_zone_pairs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(detector_module, "_daily_body_support_zones_from_pivots", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        detector_module,
        "_fill_support_staircase_gaps",
        lambda *_args, **kwargs: list(kwargs.get("zones") or _args[0]),
    )
    monkeypatch.setattr(
        detector_module,
        "_fill_persistent_wick_floor_gaps",
        lambda *_args, **kwargs: list(kwargs.get("zones") or _args[0]),
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

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in result["support"]] == [
        (62260.0, 62760.0, "persistent_wick_floor"),
        (64038.0, 64538.0, "flipped_resistance"),
    ]


def test_enforce_support_zone_spacing_keeps_far_apart_zones() -> None:
    # Gap $1495 and midpoint gap $1995 are outside both spacing windows, so both stay.
    older = _support_zone(low=59005.0, high=59505.0, source_closes=[59005.0], score=2.0)
    older["origin"] = "persistent_wick_floor"
    older["source_indexes"] = [2]
    far = _support_zone(low=61000.0, high=61500.0, source_closes=[61000.0], score=2.0)
    far["origin"] = "persistent_wick_floor"
    far["source_indexes"] = [8]

    zones = _enforce_support_zone_spacing([far, older])

    assert [(zone["low"], zone["high"], zone["origin"]) for zone in zones] == [
        (59005.0, 59505.0, "persistent_wick_floor"),
        (61000.0, 61500.0, "persistent_wick_floor"),
    ]


def test_detector_keeps_mar5_style_wick_floor_after_later_lower_low() -> None:
    # A later deeper low must not erase the frozen 59005-59505 shelf.
    df = pd.DataFrame(
        {
            "open": [67000.0, 66773.05, 61410.98, 64000.0, 69000.0, 68500.0, 57500.0, 59000.0],
            "high": [67100.0, 66996.36, 65000.0, 70000.0, 69500.0, 68800.0, 60000.0, 62000.0],
            "low": [66500.0, 59005.0, 61000.0, 63000.0, 68000.0, 56552.82, 57000.0, 58500.0],
            "close": [67000.0, 61410.98, 64000.0, 69000.0, 68500.0, 57500.0, 59000.0, 61000.0],
        }
    )

    result = detect_support_resistance_zones_structure_v1(
        df,
        external_swing_order=1,
        atr_period=3,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        min_touches=1,
        current_price=61000.0,
    )
    persistent = [
        (zone["low"], zone["high"], zone["origin"])
        for zone in result["support"]
        if zone["origin"] == "persistent_wick_floor"
    ]

    # Later 56552 low is deeper but only ~1.7% wick, so it does not pin or erase 59005.
    assert (59005.0, 59505.0, "persistent_wick_floor") in persistent
    assert (56552.82, 57052.82, "persistent_wick_floor") not in persistent


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
        StructurePivot(index=1, kind="high", wick_price=110.0, body_price=109.0, atr=1.0, term="external"),
        StructurePivot(index=2, kind="high", wick_price=112.0, body_price=111.0, atr=1.0, term="external"),
        StructurePivot(index=3, kind="low", wick_price=111.0, body_price=111.5, atr=1.0, term="external"),
        StructurePivot(index=4, kind="high", wick_price=115.0, body_price=114.0, atr=1.0, term="external"),
        StructurePivot(index=5, kind="low", wick_price=100.0, body_price=101.0, atr=1.0, term="external"),
        StructurePivot(index=6, kind="low", wick_price=98.0, body_price=99.0, atr=1.0, term="external"),
        StructurePivot(index=7, kind="high", wick_price=103.0, body_price=102.0, atr=1.0, term="external"),
    ]

    result = _filter_prominent_structure_pivots(pivots, min_swing_atr_mult=4.0, min_swing_pct=0.0)

    assert [(pivot.index, pivot.kind, pivot.wick_price) for pivot in result] == [
        (4, "high", 115.0),
        (6, "low", 98.0),
        (7, "high", 103.0),
    ]


def test_prominent_structure_pivots_can_require_percent_move() -> None:
    pivots = [
        StructurePivot(index=1, kind="high", wick_price=100.0, body_price=99.0, atr=0.0, term="external"),
        StructurePivot(index=2, kind="low", wick_price=94.0, body_price=95.0, atr=0.0, term="external"),
        StructurePivot(index=3, kind="low", wick_price=89.0, body_price=90.0, atr=0.0, term="external"),
    ]

    result = _filter_prominent_structure_pivots(pivots, min_swing_atr_mult=0.0, min_swing_pct=10.0)

    assert [(pivot.index, pivot.kind, pivot.wick_price) for pivot in result] == [(1, "high", 100.0), (3, "low", 89.0)]


def test_structure_pivot_labels_are_preserved_for_chart_debugging() -> None:
    pivots = [
        StructurePivot(index=1, kind="low", wick_price=100.0, body_price=101.0, atr=1.0, term="external"),
        StructurePivot(index=3, kind="high", wick_price=110.0, body_price=109.0, atr=1.0, term="external"),
        StructurePivot(index=5, kind="low", wick_price=105.0, body_price=106.0, atr=1.0, term="external"),
        StructurePivot(index=8, kind="high", wick_price=116.0, body_price=115.0, atr=1.0, term="external"),
    ]

    _label_structure_pivots(pivots)

    assert [pivot.structure_role for pivot in pivots] == ["L", "H", "HL", "HH"]


def test_structure_v1_output_has_complete_zone_fields() -> None:
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



def _ohlc_from_closes(closes: list[float], wick: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [close + wick for close in closes],
            "low": [close - wick for close in closes],
            "close": closes,
        }
    )


def _four_hour_day(
    day_index: int,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
) -> list[dict[str, float | int]]:
    start_time = 1_700_006_400_000 + day_index * 86_400_000
    closes = [open_price, open_price, open_price, open_price, open_price, close_price]
    candles: list[dict[str, float | int]] = []
    for index, close in enumerate(closes):
        candles.append(
            {
                "open_time": start_time + index * 14_400_000,
                "close_time": start_time + (index + 1) * 14_400_000 - 1,
                "open": open_price if index == 0 else closes[index - 1],
                "high": high_price if index == 2 else max(open_price, close),
                "low": low_price if index == 3 else min(open_price, close),
                "close": close,
                "volume": 1.0,
                "is_closed": 1,
            }
        )
    return candles


# Local flip 63.6k and persistent 66.0k from the mixed-origin gap-fill case.
def _mixed_origin_64k_ladder_pair() -> tuple[dict, dict]:
    lower = _support_zone(low=63650.0, high=63917.88, source_closes=[63650.0, 63917.88], score=8.0)
    lower["origin"] = "local_retested_flip_support"
    lower["bounds_style"] = "local_reaction"
    lower["price_state"] = "resistance"
    upper = _support_zone(low=66082.66, high=66582.66, source_closes=[66082.66], score=2.0)
    upper["origin"] = "persistent_wick_floor"
    upper["bounds_style"] = "support_floor"
    upper["price_state"] = "resistance"
    return lower, upper


# Persistent 62.5k, local 63.3k, and structural 64k from the Jul 9 snapshot.
def _jul9_conflict_zones() -> tuple[dict, dict, dict]:
    persistent = _support_zone(low=62260.0, high=62760.0, source_closes=[62260.0], score=2.0)
    persistent["origin"] = "persistent_wick_floor"
    persistent["bounds_style"] = "support_floor"
    persistent["source_indexes"] = [1]
    local = _support_zone(low=63294.72, high=63405.99, source_closes=[63294.72, 63405.99], score=15.0)
    local["origin"] = "local_reaction_support"
    local["bounds_style"] = "local_reaction"
    structural = _support_zone(low=64038.0, high=64538.0, source_closes=[64038.0, 64538.0], score=13.0)
    structural["origin"] = "flipped_resistance"
    structural["bounds_style"] = "body"
    structural["touches"] = 6
    return persistent, local, structural


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


def _high_pivot(index: int, wick_price: float, body_price: float) -> StructurePivot:
    return StructurePivot(
        index=index,
        kind="high",
        wick_price=wick_price,
        body_price=body_price,
        atr=0.0,
        term="external",
        structure_role="H",
    )


def _pivot(index: int, kind: str, wick_price: float, body_price: float) -> StructurePivot:
    return StructurePivot(
        index=index,
        kind=kind,
        wick_price=wick_price,
        body_price=body_price,
        atr=0.0,
        term="internal",
        structure_role="H" if kind == "high" else "L",
    )
