from __future__ import annotations

import sys
import json
import subprocess

from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone

from typing import Any

import pytest
import numpy as np
import pandas as pd

from src.config import ZoneConfig
from src.db import connect, init_db, upsert_candles
from src.zones.daily import DAILY_ZONE_MIN_BARS_PER_DAY
from src.zones.timeframes import aggregate_ohlc_to_daily
from src.zones.reactions import _build_local_reaction_zones
from src.trading.constants import FOUR_HOURS_MS, ONE_HOUR_MS
from src.zones.ohlc import _average_true_range, _coerce_ohlc
from src.zones.types import STRUCTURE_LOCAL_REACTION_LOOKBACK_BARS
from src.zones.rejections import _build_split_rejection_zone_pairs
from src.zones.detector import detect_support_resistance_zones_structure_v1, extract_zone_detector_evidence, materialize_support_zones
from src.zones.incremental import IncrementalZoneDetectorError, IncrementalZoneDetectorState
from src.zones.candidates import _first_reclaim_index, _high_is_confirmed_reclaimed
from src.zones.pivots import _filter_prominent_structure_pivots, _find_structure_pivots


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_SCRIPT = PROJECT_ROOT / "scripts" / "benchmark_backtest.py"
FOUR_HOUR_MS = FOUR_HOURS_MS
# 2024-01-01 00:00:00 UTC, already aligned to Binance 4h buckets.
ALIGNED_START_MS = 1_704_067_200_000

# Relaxed detector knobs so short synthetic prefixes still emit structure.
RELAXED_DETECTOR = {
    "external_swing_order": 2,
    "atr_period": 3,
    "external_min_swing_atr_mult": 0.0,
    "external_min_swing_pct": 0.0,
    "min_touches": 1,
    "break_atr_mult": 0.0,
    "internal_swing_order": 1,
}


@dataclass(frozen=True)
class PrefixOracleSnapshot:
    # One stateless detector result for df.iloc[:prefix_len].
    prefix_len: int
    open_time: int | None
    zones: list[dict[str, Any]]


# Canonicalize one JSON-like value so snapshot equality does not depend on numpy types.
def canonicalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): canonicalize_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [canonicalize_value(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if hasattr(value, "item"):
        return canonicalize_value(value.item())
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return value


# Sort a zone list by stable keys so later incremental/stateless compares are order-independent.
def canonicalize_zone_snapshot(zones: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    canonical_zones = [canonicalize_value(dict(zone)) for zone in zones or []]
    return sorted(
        canonical_zones,
        key=lambda zone: (
            float(zone.get("low", 0.0)),
            float(zone.get("high", 0.0)),
            str(zone.get("origin", "")),
            str(zone.get("bounds_style", "")),
            tuple(zone.get("source_indexes") or ()),
            tuple(zone.get("source_closes") or ()),
        ),
    )


# Run the current stateless detector and return a canonical support snapshot.
def detect_canonical_support_snapshot(df: pd.DataFrame, **detector_kwargs: Any) -> list[dict[str, Any]]:
    result = detect_support_resistance_zones_structure_v1(df, **detector_kwargs)
    return canonicalize_zone_snapshot(result.get("support") or [])


# Same snapshot via the split extract/materialize path used by the public detector.
def detect_canonical_support_snapshot_from_evidence(df: pd.DataFrame, **detector_kwargs: Any) -> list[dict[str, Any]]:
    extract_keys = (
        "current_price",
        "external_swing_order",
        "internal_swing_order",
        "atr_period",
        "break_atr_mult",
        "external_min_swing_atr_mult",
        "external_min_swing_pct",
    )
    materialize_keys = (
        "min_touches",
        "buffer_pct",
        "break_atr_mult",
        "near_price_gap_fill_edge_clearance",
        "near_price_gap_fill_midpoint_spacing",
        "near_price_gap_fill_min_touches",
    )
    evidence = extract_zone_detector_evidence(
        df,
        **{key: detector_kwargs[key] for key in extract_keys if key in detector_kwargs},
    )
    if evidence is None:
        return []
    result = materialize_support_zones(
        evidence,
        **{key: detector_kwargs[key] for key in materialize_keys if key in detector_kwargs},
    )
    return canonicalize_zone_snapshot(result.get("support") or [])


# Walk every 4h prefix, keep snapshots in memory, and never persist oracle data.
def build_stateless_prefix_oracle(df: pd.DataFrame, **detector_kwargs: Any) -> list[PrefixOracleSnapshot]:
    snapshots: list[PrefixOracleSnapshot] = []
    for prefix_len in range(1, len(df) + 1):
        prefix = df.iloc[:prefix_len].reset_index(drop=True)
        open_time = int(prefix.iloc[-1]["open_time"]) if "open_time" in prefix.columns else None
        snapshots.append(
            PrefixOracleSnapshot(
                prefix_len=prefix_len,
                open_time=open_time,
                zones=detect_canonical_support_snapshot(prefix, **detector_kwargs),
            )
        )
    # One snapshot per closed candle so later prefix compares stay aligned with df.
    assert len(snapshots) == len(df)
    return snapshots


# Numpy scalars and unsorted zone lists must not break later deep-equality checks.
def test_canonicalize_zone_snapshot_sorts_stably_and_ignores_numpy_types() -> None:
    first = {
        "origin": "flipped_resistance",
        "low": np.float64(200.0),
        "high": np.float64(700.0),
        "bounds_style": "body",
        "source_indexes": [np.int64(4)],
        "source_closes": [np.float64(200.0)],
    }
    second = {
        "origin": "structure_swing_low",
        "low": 100.0,
        "high": 600.0,
        "bounds_style": "body",
        "source_indexes": [1, 3],
        "source_closes": [100.0, 120.0],
    }

    canonical = canonicalize_zone_snapshot([first, second])

    assert [zone["origin"] for zone in canonical] == ["structure_swing_low", "flipped_resistance"]
    assert canonical[0]["low"] == 100.0
    assert isinstance(canonical[1]["source_indexes"][0], int)
    assert canonicalize_zone_snapshot([second, first]) == canonical


# Walking every prefix twice must produce the same in-memory oracle.
def test_stateless_prefix_oracle_stays_in_memory_and_is_deterministic() -> None:
    df = _ohlc_from_rows(
        [
            (67000, 67100, 66900, 67000),
            (66900, 67000, 66800, 66900),
            (66000, 66100, 65900, 66000),
            (66100, 66200, 66000, 66100),
            (66200, 66300, 66100, 66200),
            (66300, 66400, 66200, 66300),
        ]
    )

    first = build_stateless_prefix_oracle(df, **RELAXED_DETECTOR)
    second = build_stateless_prefix_oracle(df, **RELAXED_DETECTOR)
    assert first == second
    oracle = first

    assert [snapshot.prefix_len for snapshot in oracle] == list(range(1, 7))
    assert oracle[-1].open_time == ALIGNED_START_MS + 5 * FOUR_HOUR_MS
    assert oracle[-1].zones == detect_canonical_support_snapshot(df, **RELAXED_DETECTOR)


# Unique low at index 2 needs two right-side bars when external_swing_order=2.
def test_external_pivot_is_confirmed_only_after_right_side_bars() -> None:
    df = _ohlc_from_rows(
        [
            (67000, 67150, 66900, 67050),
            (67050, 67200, 66950, 67100),
            (66000, 66100, 65800, 65950),
            (66100, 66250, 66050, 66200),
            (66200, 66400, 66150, 66350),
            (66350, 66500, 66250, 66450),
        ]
    )
    oracle = build_stateless_prefix_oracle(df, **RELAXED_DETECTOR)

    before_confirm = _raw_external_pivots(df.iloc[:4], bars_each_side=2)
    after_confirm = _raw_external_pivots(df.iloc[:5], bars_each_side=2)
    confirmed_lows = [pivot for pivot in after_confirm if pivot.kind == "low" and pivot.index == 2]

    assert before_confirm == []
    assert confirmed_lows
    assert oracle[3].zones != oracle[4].zones or oracle[4].zones
    assert any(2 in zone["source_indexes"] for zone in oracle[4].zones)


# Local reaction evidence must drop once those internals fall outside the 150-bar window.
def test_internal_local_reaction_expires_after_150_bar_lookback() -> None:
    df = _local_lookback_expiry_frame()
    oracle = build_stateless_prefix_oracle(df, **RELAXED_DETECTOR)
    local_before = _local_reaction_zones_for_prefix(df, prefix_len=20)
    local_after = _local_reaction_zones_for_prefix(df, prefix_len=len(df))

    assert local_before
    assert any(pivot_index < 10 for zone in local_before for pivot_index in zone["source_indexes"])
    assert all(
        all(int(index) >= len(df) - STRUCTURE_LOCAL_REACTION_LOOKBACK_BARS for index in zone["source_indexes"])
        for zone in local_after
    )
    assert oracle[19].zones == detect_canonical_support_snapshot(df.iloc[:20].reset_index(drop=True), **RELAXED_DETECTOR)
    assert oracle[-1].zones == detect_canonical_support_snapshot(df, **RELAXED_DETECTOR)


# Config internal_swing_order must change the internal pivot set, not stay hardcoded at 1.
def test_internal_swing_order_two_finds_fewer_internal_pivots() -> None:
    df = _local_lookback_expiry_frame()
    one = extract_zone_detector_evidence(df, **_extract_kwargs({**RELAXED_DETECTOR, "internal_swing_order": 1}))
    two = extract_zone_detector_evidence(df, **_extract_kwargs({**RELAXED_DETECTOR, "internal_swing_order": 2}))
    assert one is not None and two is not None
    assert len(two.internal_pivots) < len(one.internal_pivots)


# A confirmed high becomes flipped resistance only after a later close clears its wick.
def test_high_pivot_is_reclaimed_by_a_later_closed_candle() -> None:
    df = _ohlc_from_rows(
        [
            (65000, 65100, 64800, 64950),
            (64950, 67000, 64900, 66800),
            (66800, 66900, 66400, 66500),
            (66500, 66600, 66300, 66400),
            (66400, 66500, 66200, 66300),
            (67600, 67700, 67500, 67650),
        ]
    )
    kwargs = {**RELAXED_DETECTOR, "external_swing_order": 1}
    oracle = build_stateless_prefix_oracle(df, **kwargs)
    high_pivot = next(pivot for pivot in _raw_external_pivots(df.iloc[:3], bars_each_side=1) if pivot.kind == "high")

    assert high_pivot.index == 1
    assert not _high_is_confirmed_reclaimed(high_pivot, df.iloc[:5]["close"].to_numpy(dtype=float), 0.0)
    assert _first_reclaim_index(high_pivot, df["close"].to_numpy(dtype=float), 0.0) == 5
    assert not _has_origin(oracle[4].zones, "flipped_resistance")
    assert _has_origin(oracle[5].zones, "flipped_resistance")


def test_reclaim_that_happens_during_right_bars_is_kept_after_confirmation() -> None:
    # Order=5 hides the high until five right bars exist. A close above that high
    # can only print after those right bars, otherwise the high would not be unique.
    # Incremental state must still look back from the pivot to the watermark and
    # freeze the first reclaim index instead of moving it to later closes.
    df = _ohlc_from_rows(_delayed_high_reclaim_rows())
    kwargs = {**RELAXED_DETECTOR, "external_swing_order": 5}
    oracle = build_stateless_prefix_oracle(df, **kwargs)
    unconfirmed = df.iloc[:10].reset_index(drop=True)
    confirmed = df.iloc[:11].reset_index(drop=True)
    reclaimed = df.iloc[:12].reset_index(drop=True)
    later = df.iloc[:13].reset_index(drop=True)
    highs_before = [pivot for pivot in _raw_external_pivots(unconfirmed, bars_each_side=5) if pivot.kind == "high"]
    highs_after = [pivot for pivot in _raw_external_pivots(confirmed, bars_each_side=5) if pivot.kind == "high"]
    high = next(pivot for pivot in highs_after if pivot.index == 5)
    first_reclaim = _first_reclaim_index(high, confirmed["close"].to_numpy(dtype=float), 0.0)
    later_reclaim = _first_reclaim_index(high, reclaimed["close"].to_numpy(dtype=float), 0.0)
    frozen_reclaim = _first_reclaim_index(high, later["close"].to_numpy(dtype=float), 0.0)

    assert highs_before == []
    assert first_reclaim is None
    assert later_reclaim == 11
    assert frozen_reclaim == 11
    assert not _has_origin(oracle[9].zones, "flipped_resistance")
    assert not _has_origin(oracle[10].zones, "flipped_resistance")
    assert _has_origin(oracle[11].zones, "flipped_resistance")


# Completing the sixth 4h bar of a UTC day is what admits that day into daily zones.
def test_daily_zone_waits_until_a_utc_day_has_six_four_hour_bars() -> None:
    candles: list[dict[str, float | int]] = []
    candles.extend(_four_hour_day(0, 65000.0, 66000.0, 63000.0, 64000.0))
    candles.extend(_four_hour_day(1, 64000.0, 65000.0, 61000.0, 62000.0))
    candles.extend(_four_hour_day(2, 60672.01, 60841.63, 56552.82, 58364.97))
    candles.extend(_four_hour_day(3, 58364.97, 62000.0, 59000.0, 61000.0)[:5])
    df = pd.DataFrame(candles)
    kwargs = {**RELAXED_DETECTOR, "external_swing_order": 1}
    oracle = build_stateless_prefix_oracle(df, **kwargs)

    before_sixth = df.iloc[: 3 * 6 + 5].reset_index(drop=True)
    after_sixth = pd.concat(
        [before_sixth, pd.DataFrame(_four_hour_day(3, 58364.97, 62000.0, 59000.0, 61000.0)[5:])],
        ignore_index=True,
    )
    complete_after_sixth = aggregate_ohlc_to_daily(after_sixth, min_bars_per_day=DAILY_ZONE_MIN_BARS_PER_DAY)
    complete_before_sixth = aggregate_ohlc_to_daily(before_sixth, min_bars_per_day=DAILY_ZONE_MIN_BARS_PER_DAY)
    after_oracle = build_stateless_prefix_oracle(after_sixth, **kwargs)

    assert len(complete_before_sixth) == 3
    assert len(complete_after_sixth) == 4
    assert not _has_origin(oracle[-1].zones, "daily_body_support")
    assert _has_origin(after_oracle[-1].zones, "daily_body_support")


# Same-kind prominent lows collapse to the more extreme swing.
def test_prominent_same_kind_pivot_is_replaced_by_a_more_extreme_one() -> None:
    df = _ohlc_from_rows(_prominent_replace_rows())
    kwargs = {
        **RELAXED_DETECTOR,
        "external_min_swing_pct": 2.5,
        "external_min_swing_atr_mult": 0.0,
    }
    oracle = build_stateless_prefix_oracle(df, **kwargs)
    first_low_prefix = df.iloc[:8].reset_index(drop=True)
    replaced_prefix = df.iloc[:11].reset_index(drop=True)
    first_prominent = _prominent_pivots(first_low_prefix, bars_each_side=2, min_swing_pct=2.5)
    replaced_prominent = _prominent_pivots(replaced_prefix, bars_each_side=2, min_swing_pct=2.5)
    first_lows = [(pivot.index, pivot.wick_price) for pivot in first_prominent if pivot.kind == "low"]
    replaced_lows = [(pivot.index, pivot.wick_price) for pivot in replaced_prominent if pivot.kind == "low"]

    assert first_lows
    assert replaced_lows
    assert first_lows[-1][0] == 5
    assert replaced_lows[-1][0] == 8
    assert replaced_lows[-1][1] < first_lows[-1][1]
    assert oracle[7].zones != oracle[10].zones


# A later overlapping dump must not move or replace the oldest persistent wick floor.
def test_overlapping_persistent_wick_floors_keep_the_oldest_shelf() -> None:
    df = _ohlc_from_rows(
        [
            (63000, 63200, 62800, 63100),
            (63100, 64000, 62900, 63800),
            (61410.98, 62000.0, 59005.0, 61500.0),
            (61500, 63000, 61000, 62800),
            (62800, 63500, 62000, 63200),
            (60300.24, 61000.0, 59130.91, 60500.0),
            (60500, 62000, 60000, 61800),
            (61800, 62500, 61200, 62200),
        ]
    )
    oracle = build_stateless_prefix_oracle(df, **{**RELAXED_DETECTOR, "external_swing_order": 1})
    first_floor_prefix = 5
    second_dump_prefix = 8
    first_floors = [zone for zone in oracle[first_floor_prefix - 1].zones if zone["origin"] == "persistent_wick_floor"]
    later_floors = [zone for zone in oracle[second_dump_prefix - 1].zones if zone["origin"] == "persistent_wick_floor"]

    assert first_floors
    assert any(abs(float(zone["low"]) - 59005.0) < 1e-6 for zone in first_floors)
    assert later_floors
    assert any(abs(float(zone["low"]) - 59005.0) < 1e-6 for zone in later_floors)
    assert all(abs(float(zone["low"]) - 59130.91) > 1e-6 for zone in later_floors)


# Split-rejection shelves wait until the higher-low retest itself is a confirmed pivot.
def test_split_rejection_retest_appears_only_after_the_higher_low() -> None:
    df = _split_rejection_frame()
    kwargs = {**RELAXED_DETECTOR, "external_swing_order": 1}
    oracle = build_stateless_prefix_oracle(df, **kwargs)
    before_retest = _split_rejection_pairs(df.iloc[:5].reset_index(drop=True))
    after_retest = _split_rejection_pairs(df.iloc[:6].reset_index(drop=True))

    assert before_retest == []
    assert after_retest
    assert {zone["origin"] for pair in after_retest for zone in pair} == {
        "wick_retest_support",
        "body_rejection_support",
    }
    assert oracle[4].zones == detect_canonical_support_snapshot(df.iloc[:5].reset_index(drop=True), **kwargs)
    assert oracle[5].zones == detect_canonical_support_snapshot(df.iloc[:6].reset_index(drop=True), **kwargs)


# After daily support appears, a later prefix can change overlay/spacing/gap-fill winners.
def test_daily_overlay_and_gap_fill_winner_can_change_on_a_new_prefix() -> None:
    candles: list[dict[str, float | int]] = []
    candles.extend(_four_hour_day(0, 65000.0, 66000.0, 63000.0, 64000.0))
    candles.extend(_four_hour_day(1, 64000.0, 65000.0, 61000.0, 62000.0))
    candles.extend(_four_hour_day(2, 60672.01, 60841.63, 56552.82, 58364.97))
    candles.extend(_four_hour_day(3, 58364.97, 62000.0, 59000.0, 61000.0))
    candles.extend(_four_hour_day(4, 61000.0, 65000.0, 62000.0, 64000.0))
    candles.extend(_four_hour_day(5, 64000.0, 68000.0, 63000.0, 67000.0))
    df = pd.DataFrame(candles)
    kwargs = {**RELAXED_DETECTOR, "external_swing_order": 1}
    oracle = build_stateless_prefix_oracle(df, **kwargs)

    daily_indexes = [index for index, snapshot in enumerate(oracle) if _has_origin(snapshot.zones, "daily_body_support")]
    assert daily_indexes
    first_daily = daily_indexes[0]
    assert first_daily >= 6 * 3 - 1
    changed = [
        snapshot
        for snapshot in oracle[first_daily + 1 :]
        if snapshot.zones != oracle[first_daily].zones
    ]
    assert changed
    winners_before = {(zone["low"], zone["high"], zone["origin"]) for zone in oracle[first_daily].zones}
    winners_after = {(zone["low"], zone["high"], zone["origin"]) for zone in changed[-1].zones}
    assert winners_before != winners_after


# After the extract/materialize split, every golden prefix must still match the stateless detector.
def test_extract_then_materialize_matches_stateless_oracle_on_golden_prefixes() -> None:
    for df, kwargs in _golden_prefix_frames():
        oracle = build_stateless_prefix_oracle(df, **kwargs)
        for snapshot in oracle:
            prefix = df.iloc[: snapshot.prefix_len].reset_index(drop=True)
            assert detect_canonical_support_snapshot_from_evidence(prefix, **kwargs) == snapshot.zones


# After each advance, incremental evidence + materialize must match the stateless prefix oracle.
def test_incremental_advance_matches_stateless_oracle_on_golden_prefixes() -> None:
    for df, kwargs in _golden_prefix_frames():
        oracle = build_stateless_prefix_oracle(df, **kwargs)
        state = IncrementalZoneDetectorState(_zone_config_from_kwargs(kwargs))
        for snapshot in oracle:
            row = df.iloc[snapshot.prefix_len - 1]
            state.advance(row)
            prefix = df.iloc[: snapshot.prefix_len].reset_index(drop=True)
            evidence = state.snapshot_evidence(int(row["open_time"]))
            _assert_evidence_matches(evidence, extract_zone_detector_evidence(prefix, **_extract_kwargs(kwargs)))
            assert _canonical_zones_from_evidence(evidence, kwargs) == snapshot.zones


# Out-of-order, duplicate, gapped, unclosed, and unaligned candles must fail closed.
def test_incremental_rejects_out_of_order_duplicate_gap_and_unclosed_candles() -> None:
    df = _ohlc_from_rows(
        [
            (67000, 67100, 66900, 67000),
            (66900, 67000, 66800, 66900),
            (66800, 66900, 66700, 66800),
        ]
    )
    state = IncrementalZoneDetectorState(ZoneConfig())
    first = df.iloc[0].to_dict()
    second = df.iloc[1].to_dict()
    third = df.iloc[2].to_dict()
    state.advance(first)

    duplicate = dict(second)
    duplicate["open_time"] = int(first["open_time"])
    with pytest.raises(IncrementalZoneDetectorError, match="duplicate"):
        state.advance(duplicate)

    out_of_order = dict(second)
    out_of_order["open_time"] = int(first["open_time"]) - FOUR_HOUR_MS
    with pytest.raises(IncrementalZoneDetectorError, match="out-of-order"):
        state.advance(out_of_order)

    with pytest.raises(IncrementalZoneDetectorError, match="gap"):
        state.advance(third)

    unclosed = dict(second)
    unclosed["is_closed"] = 0
    with pytest.raises(IncrementalZoneDetectorError, match="not closed"):
        state.advance(unclosed)

    unaligned = dict(second)
    unaligned["open_time"] = int(second["open_time"]) + 1
    with pytest.raises(IncrementalZoneDetectorError, match="aligned"):
        state.advance(unaligned)

    with pytest.raises(IncrementalZoneDetectorError, match="zone_set_as_of"):
        state.snapshot_evidence(int(second["open_time"]))

    state.advance(second)
    assert state.snapshot_evidence(int(second["open_time"])) is None


# Fixture set covering the step-1 transitions, reused by extract and incremental parity tests.
def _golden_prefix_frames() -> list[tuple[pd.DataFrame, dict[str, Any]]]:
    frames: list[tuple[pd.DataFrame, dict[str, Any]]] = [
        (
            _ohlc_from_rows(
                [
                    (67000, 67100, 66900, 67000),
                    (66900, 67000, 66800, 66900),
                    (66000, 66100, 65900, 66000),
                    (66100, 66200, 66000, 66100),
                    (66200, 66300, 66100, 66200),
                    (66300, 66400, 66200, 66300),
                ]
            ),
            dict(RELAXED_DETECTOR),
        ),
        (
            _ohlc_from_rows(
                [
                    (67000, 67150, 66900, 67050),
                    (67050, 67200, 66950, 67100),
                    (66000, 66100, 65800, 65950),
                    (66100, 66250, 66050, 66200),
                    (66200, 66400, 66150, 66350),
                    (66350, 66500, 66250, 66450),
                ]
            ),
            dict(RELAXED_DETECTOR),
        ),
        (_local_lookback_expiry_frame(), dict(RELAXED_DETECTOR)),
        (_local_lookback_expiry_frame(), {**RELAXED_DETECTOR, "internal_swing_order": 2}),
        (
            _ohlc_from_rows(
                [
                    (65000, 65100, 64800, 64950),
                    (64950, 67000, 64900, 66800),
                    (66800, 66900, 66400, 66500),
                    (66500, 66600, 66300, 66400),
                    (66400, 66500, 66200, 66300),
                    (67600, 67700, 67500, 67650),
                ]
            ),
            {**RELAXED_DETECTOR, "external_swing_order": 1},
        ),
        (_ohlc_from_rows(_delayed_high_reclaim_rows()), {**RELAXED_DETECTOR, "external_swing_order": 5}),
        (_ohlc_from_rows(_prominent_replace_rows()), {**RELAXED_DETECTOR, "external_min_swing_pct": 2.5, "external_min_swing_atr_mult": 0.0}),
        (
            _ohlc_from_rows(
                [
                    (63000, 63200, 62800, 63100),
                    (63100, 64000, 62900, 63800),
                    (61410.98, 62000.0, 59005.0, 61500.0),
                    (61500, 63000, 61000, 62800),
                    (62800, 63500, 62000, 63200),
                    (60300.24, 61000.0, 59130.91, 60500.0),
                    (60500, 62000, 60000, 61800),
                    (61800, 62500, 61200, 62200),
                ]
            ),
            {**RELAXED_DETECTOR, "external_swing_order": 1},
        ),
        (_split_rejection_frame(), {**RELAXED_DETECTOR, "external_swing_order": 1}),
    ]
    daily_candles: list[dict[str, float | int]] = []
    daily_candles.extend(_four_hour_day(0, 65000.0, 66000.0, 63000.0, 64000.0))
    daily_candles.extend(_four_hour_day(1, 64000.0, 65000.0, 61000.0, 62000.0))
    daily_candles.extend(_four_hour_day(2, 60672.01, 60841.63, 56552.82, 58364.97))
    daily_candles.extend(_four_hour_day(3, 58364.97, 62000.0, 59000.0, 61000.0))
    daily_candles.extend(_four_hour_day(4, 61000.0, 65000.0, 62000.0, 64000.0))
    daily_candles.extend(_four_hour_day(5, 64000.0, 68000.0, 63000.0, 67000.0))
    frames.append((pd.DataFrame(daily_candles), {**RELAXED_DETECTOR, "external_swing_order": 1}))
    return frames


# Map detector kwargs onto ZoneConfig fields used by incremental ingest.
def _zone_config_from_kwargs(kwargs: dict[str, Any]) -> ZoneConfig:
    fields: dict[str, Any] = {}
    if "min_touches" in kwargs:
        fields["min_touches"] = kwargs["min_touches"]
    if "buffer_pct" in kwargs:
        fields["role_buffer_pct"] = kwargs["buffer_pct"]
    if "external_swing_order" in kwargs:
        fields["external_swing_order"] = kwargs["external_swing_order"]
    if "internal_swing_order" in kwargs:
        fields["internal_swing_order"] = kwargs["internal_swing_order"]
    if "atr_period" in kwargs:
        fields["atr_period"] = kwargs["atr_period"]
    if "break_atr_mult" in kwargs:
        fields["break_atr_mult"] = kwargs["break_atr_mult"]
    if "near_price_gap_fill_edge_clearance" in kwargs:
        fields["near_price_gap_fill_edge_clearance"] = kwargs["near_price_gap_fill_edge_clearance"]
    if "near_price_gap_fill_midpoint_spacing" in kwargs:
        fields["near_price_gap_fill_midpoint_spacing"] = kwargs["near_price_gap_fill_midpoint_spacing"]
    if "near_price_gap_fill_min_touches" in kwargs:
        fields["near_price_gap_fill_min_touches"] = kwargs["near_price_gap_fill_min_touches"]
    if "external_min_swing_atr_mult" in kwargs:
        fields["external_min_swing_atr_mult"] = kwargs["external_min_swing_atr_mult"]
    if "external_min_swing_pct" in kwargs:
        fields["external_min_swing_pct"] = kwargs["external_min_swing_pct"]
    return ZoneConfig(**fields)


# Extract-only knobs from a detector kwargs dict, ignoring materialize-only keys.
def _extract_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "current_price",
        "external_swing_order",
        "internal_swing_order",
        "atr_period",
        "break_atr_mult",
        "external_min_swing_atr_mult",
        "external_min_swing_pct",
    )
    return {key: kwargs[key] for key in keys if key in kwargs}


# Materialize incremental or stateless evidence with the same detector knobs as the oracle.
def _canonical_zones_from_evidence(evidence: Any, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    if evidence is None:
        return []
    materialize_keys = (
        "min_touches",
        "buffer_pct",
        "break_atr_mult",
        "near_price_gap_fill_edge_clearance",
        "near_price_gap_fill_midpoint_spacing",
        "near_price_gap_fill_min_touches",
    )
    result = materialize_support_zones(
        evidence,
        **{key: kwargs[key] for key in materialize_keys if key in kwargs},
    )
    return canonicalize_zone_snapshot(result.get("support") or [])


# Compare incremental vs stateless evidence field-by-field, including pivot roles and reclaim indexes.
def _assert_evidence_matches(incremental: Any, stateless: Any) -> None:
    if incremental is None or stateless is None:
        assert incremental is None and stateless is None
        return
    pd.testing.assert_frame_equal(incremental.ohlc.reset_index(drop=True), stateless.ohlc.reset_index(drop=True), check_dtype=False)
    np.testing.assert_allclose(incremental.closes, stateless.closes)
    assert incremental.current_price == pytest.approx(stateless.current_price)
    assert incremental.raw_external_pivots == stateless.raw_external_pivots
    assert incremental.external_pivots == stateless.external_pivots
    assert incremental.internal_pivots == stateless.internal_pivots
    assert incremental.daily_pivots == stateless.daily_pivots
    assert incremental.first_reclaim_indexes == stateless.first_reclaim_indexes
    np.testing.assert_array_equal(np.asarray(incremental.four_hour_open_times, dtype=np.int64), np.asarray(stateless.four_hour_open_times, dtype=np.int64))
    np.testing.assert_array_equal(np.asarray(incremental.daily_open_times, dtype=np.int64), np.asarray(stateless.daily_open_times, dtype=np.int64))



# The benchmark must copy SQLite, write cache only on the copy, and emit stable JSON keys.
def test_benchmark_script_copies_source_db_and_leaves_its_cache_untouched(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    output = tmp_path / "benchmark.json"
    start_ms = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    _seed_backtest_db(source, start_ms=start_ms, hourly_count=48 + 8)

    completed = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK_SCRIPT),
            "--database",
            str(source),
            "--start",
            "2026-06-01T00:00:00+00:00",
            "--end",
            "2026-06-01T08:00:00+00:00",
            "--json",
            str(output),
        ],
        cwd=str(PROJECT_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    with connect(source) as conn:
        source_cache = int(conn.execute("SELECT COUNT(*) FROM backtest_zone_cache").fetchone()[0])

    assert source_cache == 0
    assert payload["zone_snapshot_count"] >= 1
    assert payload["cold"]["zone_rebuild_count"] == payload["zone_snapshot_count"]
    assert payload["cold"]["zone_cache_hit_count"] == 0
    assert payload["warm"]["zone_cache_hit_count"] == payload["zone_snapshot_count"]
    assert payload["warm"]["zone_rebuild_count"] == 0
    assert payload["cold"]["zone_full_history_scans"] == 1
    assert payload["warm"]["zone_full_history_scans"] == 0
    assert payload["warm"]["zone_state_ingested_candles"] == 0
    assert "elapsed_seconds" in payload["cold"]
    assert completed.stdout


# True when any canonical zone in the snapshot carries this origin label.
def _has_origin(zones: list[dict[str, Any]], origin: str) -> bool:
    return any(str(zone.get("origin")) == origin for zone in zones)


# Build Binance-aligned 4h OHLC from (open, high, low, close) tuples.
def _ohlc_from_rows(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    records: list[dict[str, float | int]] = []
    for index, (open_price, high_price, low_price, close_price) in enumerate(rows):
        open_time = ALIGNED_START_MS + index * FOUR_HOUR_MS
        records.append(
            {
                "open_time": open_time,
                "close_time": open_time + FOUR_HOUR_MS - 1,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": 1.0,
                "is_closed": 1,
            }
        )
    return pd.DataFrame(records)


# Six closed 4h candles that form one complete UTC day.
def _four_hour_day(
    day_index: int,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
) -> list[dict[str, float | int]]:
    start_time = ALIGNED_START_MS + day_index * 86_400_000
    closes = [open_price, open_price, open_price, open_price, open_price, close_price]
    candles: list[dict[str, float | int]] = []
    for index, close in enumerate(closes):
        candles.append(
            {
                "open_time": start_time + index * FOUR_HOUR_MS,
                "close_time": start_time + (index + 1) * FOUR_HOUR_MS - 1,
                "open": open_price if index == 0 else closes[index - 1],
                "high": high_price if index == 2 else max(open_price, close),
                "low": low_price if index == 3 else min(open_price, close),
                "close": close,
                "volume": 1.0,
                "is_closed": 1,
            }
        )
    return candles


# Early clustered local lows, then enough higher bars for the 150-bar window to slide past them.
def _local_lookback_expiry_frame() -> pd.DataFrame:
    rows: list[tuple[float, float, float, float]] = [
        (66000, 66150, 65850, 66050),
        (66050, 66200, 65900, 66100),
        (65000, 65100, 64850, 64980),
        (65200, 65400, 65100, 65300),
        (65020, 65140, 64880, 65040),
        (65300, 65500, 65200, 65420),
        (65600, 65800, 65500, 65700),
        (65800, 66000, 65700, 65900),
        (66000, 66200, 65900, 66100),
        (66200, 66400, 66100, 66300),
    ]
    for index in range(STRUCTURE_LOCAL_REACTION_LOOKBACK_BARS + 10):
        close = 70000.0 + index * 5.0
        rows.append((close - 15.0, close + 25.0 + (index % 7), close - 25.0 - (index % 5), close))
    return _ohlc_from_rows(rows)


# Unique high, then two same-kind lows so the prominent reducer keeps the deeper one.
def _prominent_replace_rows() -> list[tuple[float, float, float, float]]:
    return [
        (68000, 68200, 67800, 68100),
        (68100, 68300, 67900, 68200),
        (68200, 71000, 68100, 70800),
        (70000, 70200, 69000, 69500),
        (69000, 69200, 68000, 68500),
        (67000, 67200, 66000, 66200),
        (66500, 67500, 66300, 67000),
        (67000, 67800, 66800, 67600),
        (65000, 65200, 63000, 63200),
        (63500, 64500, 63300, 64000),
        (64000, 65000, 63800, 64800),
    ]


# High at index 5 needs five right bars; the first close above it is the next bar.
def _delayed_high_reclaim_rows() -> list[tuple[float, float, float, float]]:
    rows: list[tuple[float, float, float, float]] = []
    for index in range(5):
        close = 65000.0 + index * 20.0
        rows.append((close, close + 80.0, close - 80.0, close))
    rows.append((66000.0, 68000.0, 65800.0, 67000.0))
    for index in range(5):
        close = 66000.0 - index * 10.0
        rows.append((close, 67000.0 + index, close - 100.0, close))
    rows.append((68100.0, 68500.0, 67900.0, 68200.0))
    rows.append((68200.0, 68600.0, 68000.0, 68300.0))
    return rows


# Rejection dump plus a later higher-low retest inside the four-bar window.
def _split_rejection_frame() -> pd.DataFrame:
    return _ohlc_from_rows(
        [
            (62000.0, 62500.0, 61000.0, 61500.0),
            (60438.0, 61547.24, 59130.91, 60300.24),
            (60300.24, 62000.0, 59940.01, 61056.47),
            (61056.47, 61600.0, 60800.0, 61200.0),
            (60687.05, 61276.95, 59500.0, 61004.95),
            (61004.95, 61800.0, 60800.0, 61600.0),
        ]
    )


# Find raw external pivots on a prefix using the same ATR window as the relaxed fixtures.
def _raw_external_pivots(df: pd.DataFrame, bars_each_side: int):
    ohlc = _coerce_ohlc(df)
    assert ohlc is not None
    atr = _average_true_range(
        highs=ohlc["high"].to_numpy(dtype=float),
        lows=ohlc["low"].to_numpy(dtype=float),
        closes=ohlc["close"].to_numpy(dtype=float),
        period=3,
    )
    return _find_structure_pivots(ohlc, bars_each_side, atr, "external")


# Reduce raw external pivots with a percent-only prominent filter.
def _prominent_pivots(df: pd.DataFrame, bars_each_side: int, min_swing_pct: float):
    raw = _raw_external_pivots(df, bars_each_side=bars_each_side)
    return _filter_prominent_structure_pivots(raw, min_swing_atr_mult=0.0, min_swing_pct=min_swing_pct)


# Build local-reaction zones from internal pivots in one prefix, ignoring later overlays.
def _local_reaction_zones_for_prefix(df: pd.DataFrame, prefix_len: int) -> list[dict[str, Any]]:
    prefix = df.iloc[:prefix_len].reset_index(drop=True)
    ohlc = _coerce_ohlc(prefix)
    assert ohlc is not None
    atr = _average_true_range(
        highs=ohlc["high"].to_numpy(dtype=float),
        lows=ohlc["low"].to_numpy(dtype=float),
        closes=ohlc["close"].to_numpy(dtype=float),
        period=3,
    )
    internal = _find_structure_pivots(ohlc, 1, atr, "internal")
    return _build_local_reaction_zones(
        internal_pivots=internal,
        closes=ohlc["close"].to_numpy(dtype=float),
        break_atr_mult=0.0,
        zone_width=500.0,
        min_touches=2,
        current_price=float(ohlc["close"].iloc[-1]),
        buffer_pct=0.0015,
    )


# Run split-rejection pair detection on one prefix of closed 4h candles.
def _split_rejection_pairs(df: pd.DataFrame) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    ohlc = _coerce_ohlc(df)
    assert ohlc is not None
    atr = _average_true_range(
        highs=ohlc["high"].to_numpy(dtype=float),
        lows=ohlc["low"].to_numpy(dtype=float),
        closes=ohlc["close"].to_numpy(dtype=float),
        period=3,
    )
    external = _find_structure_pivots(ohlc, 1, atr, "external")
    internal = _find_structure_pivots(ohlc, 1, atr, "internal")
    return _build_split_rejection_zone_pairs(
        ohlc=ohlc,
        external_pivots=external,
        internal_pivots=internal,
        zone_width=500.0,
        current_price=float(ohlc["close"].iloc[-1]),
        buffer_pct=0.0015,
    )


# Seed enough 1h + warm-up 4h history for an isolated backtest benchmark.
def _seed_backtest_db(db_path: Path, *, start_ms: int, hourly_count: int, warm_up_4h: int = 20) -> None:
    init_db(db_path)
    lookback = start_ms - 48 * ONE_HOUR_MS
    hourly: list[dict[str, object]] = []
    for index in range(hourly_count):
        open_time = lookback + index * ONE_HOUR_MS
        close = 65000.0 + (index % 7) * 10.0
        hourly.append(
            {
                "open_time": open_time,
                "close_time": open_time + ONE_HOUR_MS - 1,
                "open": close,
                "high": close + 50,
                "low": close - 50,
                "close": close,
                "volume": 1.0,
                "is_closed": 1,
            }
        )
    upsert_candles(db_path, hourly, "binance", "BTCUSDT", "1h")
    first_fully_covered = ((lookback + FOUR_HOURS_MS - 1) // FOUR_HOURS_MS) * FOUR_HOURS_MS
    last = first_fully_covered - FOUR_HOURS_MS
    first = last - (warm_up_4h - 1) * FOUR_HOURS_MS
    four_hour: list[dict[str, object]] = []
    for index, open_time in enumerate(range(first, last + 1, FOUR_HOURS_MS)):
        close = 64000.0 + index * 25.0
        four_hour.append(
            {
                "open_time": open_time,
                "close_time": open_time + FOUR_HOURS_MS - 1,
                "open": close,
                "high": close + 80,
                "low": close - 80,
                "close": close,
                "volume": 1.0,
                "is_closed": 1,
            }
        )
    upsert_candles(db_path, four_hour, "binance", "BTCUSDT", "4h")
