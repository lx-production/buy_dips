from __future__ import annotations

import sys
import json
import subprocess

from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone

from typing import Any

import numpy as np
import pandas as pd

from src.db import connect, init_db, upsert_candles
from src.zones.daily import DAILY_ZONE_MIN_BARS_PER_DAY
from src.zones.timeframes import aggregate_ohlc_to_daily
from src.zones.reactions import _build_local_reaction_zones
from src.trading.constants import FOUR_HOURS_MS, ONE_HOUR_MS
from src.zones.ohlc import _average_true_range, _coerce_ohlc
from src.zones.types import STRUCTURE_LOCAL_REACTION_LOOKBACK_BARS
from src.zones.rejections import _build_split_rejection_zone_pairs
from src.zones.detector import detect_support_resistance_zones_structure_v1
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
    return snapshots


# Rebuild the oracle twice so later incremental comparisons have a locked stateless baseline.
def lock_stateless_prefix_oracle(df: pd.DataFrame, **detector_kwargs: Any) -> list[PrefixOracleSnapshot]:
    first = build_stateless_prefix_oracle(df, **detector_kwargs)
    second = build_stateless_prefix_oracle(df, **detector_kwargs)
    assert first == second
    assert len(first) == len(df)
    return first


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

    oracle = lock_stateless_prefix_oracle(df, **RELAXED_DETECTOR)

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
    oracle = lock_stateless_prefix_oracle(df, **RELAXED_DETECTOR)

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
    oracle = lock_stateless_prefix_oracle(df, **RELAXED_DETECTOR)
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
    oracle = lock_stateless_prefix_oracle(df, **kwargs)
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
    oracle = lock_stateless_prefix_oracle(df, **kwargs)
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
    oracle = lock_stateless_prefix_oracle(df, **kwargs)

    before_sixth = df.iloc[: 3 * 6 + 5].reset_index(drop=True)
    after_sixth = pd.concat(
        [before_sixth, pd.DataFrame(_four_hour_day(3, 58364.97, 62000.0, 59000.0, 61000.0)[5:])],
        ignore_index=True,
    )
    complete_after_sixth = aggregate_ohlc_to_daily(after_sixth, min_bars_per_day=DAILY_ZONE_MIN_BARS_PER_DAY)
    complete_before_sixth = aggregate_ohlc_to_daily(before_sixth, min_bars_per_day=DAILY_ZONE_MIN_BARS_PER_DAY)
    after_oracle = lock_stateless_prefix_oracle(after_sixth, **kwargs)

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
    oracle = lock_stateless_prefix_oracle(df, **kwargs)
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
    oracle = lock_stateless_prefix_oracle(df, **{**RELAXED_DETECTOR, "external_swing_order": 1})
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
    oracle = lock_stateless_prefix_oracle(df, **kwargs)
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
    oracle = lock_stateless_prefix_oracle(df, **kwargs)

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
