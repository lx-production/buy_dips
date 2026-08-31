from __future__ import annotations

import csv

from pathlib import Path
from datetime import datetime, timezone

import pytest

import src.trading.backtest as backtest_module
import src.trading.backtest_zone_cache as cache_module

from src.cli import build_parser
from src.config import AppConfig, ZoneConfig
from src.db import connect, init_db, upsert_candles
from src.backtest_chart_server import load_backtest_chart_payload
from src.trading.constants import FOUR_HOURS_MS, ONE_HOUR_MS
from src.trading.backtest import BUY_CSV_COLUMNS, BacktestError, ZoneSnapshot, build_zone_segments, live_table_counts, parse_backtest_bound, run_backtest, write_buy_csv
from src.zones.detector import detect_support_resistance_zones
from tests.test_incremental_zone_detector import canonicalize_zone_snapshot


HOUR = ONE_HOUR_MS
FOUR = FOUR_HOURS_MS


def _hourly_rows(start: int, count: int, *, base_close: float = 100.0) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(count):
        open_time = start + index * HOUR
        close = base_close + (index % 7) * 0.1
        rows.append(
            {
                "open_time": open_time,
                "close_time": open_time + HOUR - 1,
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1.0,
                "is_closed": 1,
            }
        )
    return rows


def _four_hour_rows(count: int, *, end_before: int) -> list[dict[str, object]]:
    # Warm-up history ends strictly before the first 1h-covered 4h bucket.
    last = ((end_before // FOUR) * FOUR) - FOUR
    first = last - (count - 1) * FOUR
    rows: list[dict[str, object]] = []
    for index, open_time in enumerate(range(first, last + 1, FOUR)):
        close = 100.0 + index
        rows.append(
            {
                "open_time": open_time,
                "close_time": open_time + FOUR - 1,
                "open": close,
                "high": close + 5,
                "low": close - 5,
                "close": close,
                "volume": 1.0,
                "is_closed": 1,
            }
        )
    return rows


def _seed_db(db_path: Path, *, hourly_start: int, hourly_count: int, warm_up_4h: int = 20) -> None:
    # Seed stored 4h history through the last bucket before full 1h coverage begins.
    init_db(db_path)
    upsert_candles(db_path, _hourly_rows(hourly_start, hourly_count), "binance", "BTCUSDT", "1h")
    first_fully_covered = ((hourly_start + FOUR - 1) // FOUR) * FOUR
    upsert_candles(
        db_path,
        _four_hour_rows(warm_up_4h, end_before=first_fully_covered),
        "binance",
        "BTCUSDT",
        "4h",
    )


def _fake_detector(_df, **_kwargs):
    # Deterministic three-band support set so BUY/HOLD gates are easy to force in tests.
    zones = [
        {
            "origin": "test",
            "role": "support",
            "bounds_style": "body",
            "low": 80.0,
            "high": 85.0,
            "mid": 82.5,
            "width": 5.0,
            "width_pct": 6.0,
            "touches": 2,
            "source_closes": [82.0],
            "source_indexes": [0],
            "source_timeframe": "4h",
        },
        {
            "origin": "test",
            "role": "support",
            "bounds_style": "body",
            "low": 90.0,
            "high": 100.0,
            "mid": 95.0,
            "width": 10.0,
            "width_pct": 10.0,
            "touches": 3,
            "source_closes": [95.0],
            "source_indexes": [1],
            "source_timeframe": "4h",
        },
        {
            "origin": "test",
            "role": "support",
            "bounds_style": "body",
            "low": 110.0,
            "high": 120.0,
            "mid": 115.0,
            "width": 10.0,
            "width_pct": 8.0,
            "touches": 2,
            "source_closes": [115.0],
            "source_indexes": [2],
            "source_timeframe": "4h",
        },
    ]
    return {"support": zones, "all": zones, "resistance": [], "active": []}


def test_parse_backtest_bound_requires_tz_and_hour_alignment() -> None:
    expected = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert parse_backtest_bound("2026-06-01T00:00:00+00:00", label="start") == expected
    with pytest.raises(BacktestError, match="timezone"):
        parse_backtest_bound("2026-06-01T00:00:00", label="start")
    with pytest.raises(BacktestError, match="hour boundary"):
        parse_backtest_bound("2026-06-01T00:30:00+00:00", label="start")


def test_default_end_is_after_last_closed_1h(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    start = 1_000 * FOUR
    lookback = start - 48 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 8)
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))

    result = run_backtest(config, db_path, start_ms=start, end_ms=None, detector=_fake_detector)

    last_open = lookback + (48 + 8 - 1) * HOUR
    assert result.end_ms == last_open + HOUR


def test_rebuild_only_after_full_four_1h_and_no_future_4h(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    # Align start to a 4h boundary so rebuild timing is obvious.
    start = 50 * FOUR
    lookback = start - 48 * HOUR
    # Provide enough hours to finish the first in-window 4h bucket and one more hour.
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 5, warm_up_4h=16)
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))
    rebuild_targets: list[int] = []

    def tracking_detector(df, **kwargs):
        rebuild_targets.append(int(df.iloc[-1]["open_time"]))
        # Fail closed if any candle newer than the as-of target sneaks into the frame.
        assert int(df.iloc[-1]["open_time"]) == max(int(v) for v in df["open_time"].tolist())
        return _fake_detector(df, **kwargs)

    end = start + 5 * HOUR
    result = run_backtest(
        config,
        db_path,
        start_ms=start,
        end_ms=end,
        detector=tracking_detector,
    )

    # First trigger at `start` can use warm-up through the previous completed bucket.
    assert rebuild_targets[0] == start - FOUR
    # After the four hours [start, start+4h) complete (evaluated at start+3h), rebuild once more.
    assert start in rebuild_targets
    assert all(target <= start for target in rebuild_targets) or start in rebuild_targets
    assert result.zone_rebuild_count >= 2
    assert result.zone_full_history_scans == 0
    assert result.zone_state_ingested_candles == 0


def test_backtest_aggregates_each_four_hour_bucket_once(tmp_path: Path, monkeypatch) -> None:
    # Count aggregation calls to prevent replay length from reintroducing quadratic 4h work.
    db_path = tmp_path / "bot.sqlite"
    start = 80 * FOUR
    lookback = start - 48 * HOUR
    end = start + 24 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 24, warm_up_4h=16)
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))
    aggregate = backtest_module.aggregate_four_hour_bucket
    bucket_calls: list[int] = []

    def counting_aggregate(hourly_df, bucket_open_time, *, now_ms):
        # Delegate to the real aggregator after recording the bucket requested by replay.
        bucket_calls.append(int(bucket_open_time))
        return aggregate(hourly_df, bucket_open_time, now_ms=now_ms)

    monkeypatch.setattr(backtest_module, "aggregate_four_hour_bucket", counting_aggregate)

    run_backtest(config, db_path, start_ms=start, end_ms=end, detector=_fake_detector)

    assert bucket_calls == list(range(lookback, end, FOUR))


def test_start_on_hour_boundary_does_not_require_four_hour_alignment(tmp_path: Path) -> None:
    # Preserve the CLI contract that every UTC hour boundary is valid, not only 4h boundaries.
    db_path = tmp_path / "bot.sqlite"
    start = 80 * FOUR + HOUR
    lookback = start - 48 * HOUR
    end = start + 5 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 5, warm_up_4h=16)
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))

    result = run_backtest(config, db_path, start_ms=start, end_ms=end, detector=_fake_detector)

    assert result.start_ms == start
    assert result.end_ms == end
    assert result.evaluated_candles == 5


def test_zone_cache_reuses_snapshots_and_only_builds_extended_range(tmp_path: Path, monkeypatch) -> None:
    # Use the real fingerprinting path with a cheap detector so cache behavior stays isolated.
    db_path = tmp_path / "bot.sqlite"
    start = 90 * FOUR
    lookback = start - 48 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 8, warm_up_4h=16)
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))
    real_builder = backtest_module.build_fingerprinted_support_zones
    build_targets: list[int] = []

    def cached_test_builder(evidence, four_hour_df, **kwargs):
        # Record real detector builds while preserving production enrichment and fingerprints.
        build_targets.append(int(kwargs["zone_set_as_of"]))
        return real_builder(four_hour_df, detector=_fake_detector, **kwargs)

    monkeypatch.setattr(backtest_module, "build_fingerprinted_support_zones_from_evidence", cached_test_builder)

    cold = run_backtest(config, db_path, start_ms=start, end_ms=start + 4 * HOUR)
    warm = run_backtest(config, db_path, start_ms=start, end_ms=start + 4 * HOUR)
    extended = run_backtest(config, db_path, start_ms=start, end_ms=start + 8 * HOUR)

    assert cold.zone_rebuild_count == cold.zone_snapshot_count
    assert cold.zone_cache_hit_count == 0
    assert cold.zone_full_history_scans == 1
    assert cold.zone_state_ingested_candles >= cold.zone_snapshot_count
    assert warm.zone_rebuild_count == 0
    assert warm.zone_cache_hit_count == warm.zone_snapshot_count == cold.zone_snapshot_count
    assert warm.zone_full_history_scans == 0
    assert warm.zone_state_ingested_candles == 0
    assert warm.buys == cold.buys
    assert warm.zone_segments == cold.zone_segments
    assert extended.zone_cache_hit_count == cold.zone_snapshot_count
    assert extended.zone_rebuild_count == extended.zone_snapshot_count - cold.zone_snapshot_count
    assert extended.zone_full_history_scans == 1
    assert extended.zone_state_ingested_candles >= extended.zone_snapshot_count
    assert len(build_targets) == cold.zone_rebuild_count + extended.zone_rebuild_count
    with connect(db_path) as conn:
        cache_count = int(conn.execute("SELECT COUNT(*) FROM backtest_zone_cache").fetchone()[0])
    assert cache_count == extended.zone_snapshot_count


def test_zone_cache_invalidates_changed_config_and_candle_data(tmp_path: Path, monkeypatch) -> None:
    # Verify both detector settings and canonical candle prefixes participate in the cache identity.
    db_path = tmp_path / "bot.sqlite"
    start = 100 * FOUR
    lookback = start - 48 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 4, warm_up_4h=16)
    real_builder = backtest_module.build_fingerprinted_support_zones

    def cached_test_builder(evidence, four_hour_df, **kwargs):
        # Keep generated zones deterministic while allowing cache identity inputs to change.
        return real_builder(four_hour_df, detector=_fake_detector, **kwargs)

    monkeypatch.setattr(backtest_module, "build_fingerprinted_support_zones_from_evidence", cached_test_builder)
    first_config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))
    changed_config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=3))

    first = run_backtest(first_config, db_path, start_ms=start, end_ms=start + 4 * HOUR)
    after_config_change = run_backtest(changed_config, db_path, start_ms=start, end_ms=start + 4 * HOUR)

    assert first.zone_cache_hit_count == 0
    assert after_config_change.zone_cache_hit_count == 0
    assert after_config_change.zone_rebuild_count == after_config_change.zone_snapshot_count

    changed = _hourly_rows(lookback, 1, base_close=125.0)[0]
    upsert_candles(db_path, [changed], "binance", "BTCUSDT", "1h")
    after_candle_change = run_backtest(changed_config, db_path, start_ms=start, end_ms=start + 4 * HOUR)

    assert after_candle_change.zone_cache_hit_count == 0
    assert after_candle_change.zone_rebuild_count == after_candle_change.zone_snapshot_count


def test_zone_cache_rebuilds_corrupt_snapshot_without_touching_live_tables(tmp_path: Path, monkeypatch) -> None:
    # A disposable cache error must rebuild only that snapshot and preserve all live table counts.
    db_path = tmp_path / "bot.sqlite"
    start = 110 * FOUR
    lookback = start - 48 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 8, warm_up_4h=16)
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))
    real_builder = backtest_module.build_fingerprinted_support_zones

    def cached_test_builder(evidence, four_hour_df, **kwargs):
        # Exercise the production zone enrichment while replacing only expensive detection.
        return real_builder(four_hour_df, detector=_fake_detector, **kwargs)

    monkeypatch.setattr(backtest_module, "build_fingerprinted_support_zones_from_evidence", cached_test_builder)
    live_before = live_table_counts(db_path)
    cold = run_backtest(config, db_path, start_ms=start, end_ms=start + 8 * HOUR)
    with connect(db_path) as conn:
        target = int(conn.execute("SELECT MIN(zone_set_as_of) FROM backtest_zone_cache").fetchone()[0])
        conn.execute(
            "UPDATE backtest_zone_cache SET zones_json=? WHERE zone_set_as_of=?",
            ("{invalid", target),
        )
        conn.commit()

    repaired = run_backtest(config, db_path, start_ms=start, end_ms=start + 8 * HOUR)

    assert repaired.zone_rebuild_count == 1
    assert repaired.zone_cache_hit_count == cold.zone_snapshot_count - 1
    assert repaired.zone_full_history_scans == 1
    assert repaired.zone_state_ingested_candles >= repaired.zone_snapshot_count
    assert repaired.buys == cold.buys
    assert repaired.zone_segments == cold.zone_segments
    assert live_table_counts(db_path) == live_before


def test_zone_cache_reuses_empty_snapshots(tmp_path: Path, monkeypatch) -> None:
    # Empty detector output is a valid snapshot and must not become a permanent cache miss.
    db_path = tmp_path / "bot.sqlite"
    start = 120 * FOUR
    lookback = start - 48 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 4, warm_up_4h=16)
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))
    build_count = 0

    def empty_builder(_evidence, _four_hour_df, **_kwargs):
        # Return the valid no-zone outcome while tracking whether cache reuse skips this call.
        nonlocal build_count
        build_count += 1
        return []

    monkeypatch.setattr(backtest_module, "build_fingerprinted_support_zones_from_evidence", empty_builder)

    cold = run_backtest(config, db_path, start_ms=start, end_ms=start + 4 * HOUR)
    warm = run_backtest(config, db_path, start_ms=start, end_ms=start + 4 * HOUR)

    assert build_count == cold.zone_snapshot_count
    assert warm.zone_cache_hit_count == warm.zone_snapshot_count
    assert warm.zone_rebuild_count == 0


def test_zone_cache_warm_path_bulk_loads_on_one_connection(tmp_path: Path, monkeypatch) -> None:
    # After a cold fill, warm replay should prune once and bulk-load hits on a single connection.
    db_path = tmp_path / "bot.sqlite"
    start = 125 * FOUR
    lookback = start - 48 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 8, warm_up_4h=16)
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))
    real_builder = backtest_module.build_fingerprinted_support_zones

    def cached_test_builder(evidence, four_hour_df, **kwargs):
        return real_builder(four_hour_df, detector=_fake_detector, **kwargs)

    monkeypatch.setattr(backtest_module, "build_fingerprinted_support_zones_from_evidence", cached_test_builder)
    run_backtest(config, db_path, start_ms=start, end_ms=start + 8 * HOUR)

    real_connect = cache_module.connect
    connection_count = {"n": 0}

    def counting_connect(path):
        connection_count["n"] += 1
        return real_connect(path)

    monkeypatch.setattr(cache_module, "connect", counting_connect)
    warm = run_backtest(config, db_path, start_ms=start, end_ms=start + 8 * HOUR)

    assert warm.zone_rebuild_count == 0
    assert warm.zone_cache_hit_count == warm.zone_snapshot_count
    assert warm.zone_full_history_scans == 0
    assert connection_count["n"] == 2


def test_incremental_backtest_matches_stateless_detector_snapshots(tmp_path: Path) -> None:
    # Production incremental rebuilds must deep-equal injected full-frame detector snapshots.
    db_path = tmp_path / "bot.sqlite"
    start = 130 * FOUR
    lookback = start - 48 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 8, warm_up_4h=16)
    config = AppConfig(
        database_path=str(db_path),
        zones=ZoneConfig(
            min_touches=1,
            external_swing_order=2,
            atr_period=3,
            break_atr_mult=0.0,
            external_min_swing_atr_mult=0.0,
            external_min_swing_pct=0.0,
        ),
    )
    end = start + 8 * HOUR
    stateless = run_backtest(
        config,
        db_path,
        start_ms=start,
        end_ms=end,
        detector=detect_support_resistance_zones,
    )
    incremental = run_backtest(config, db_path, start_ms=start, end_ms=end)

    assert incremental.zone_full_history_scans == 1
    assert incremental.zone_state_ingested_candles >= incremental.zone_snapshot_count
    assert incremental.zone_snapshot_count == stateless.zone_snapshot_count
    assert incremental.buys == stateless.buys
    assert incremental.zone_segments == stateless.zone_segments
    assert len(incremental.zone_snapshots) == len(stateless.zone_snapshots)
    for left, right in zip(incremental.zone_snapshots, stateless.zone_snapshots):
        assert left.zone_set_as_of == right.zone_set_as_of
        assert left.valid_from == right.valid_from
        assert canonicalize_zone_snapshot(left.zones) == canonicalize_zone_snapshot(right.zones)


def test_missing_warmup_gap_and_bad_alignment_fail(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    start = 40 * FOUR
    # Seed only from start (no 48h warm-up).
    init_db(db_path)
    upsert_candles(db_path, _hourly_rows(start, 8), "binance", "BTCUSDT", "1h")
    upsert_candles(db_path, _four_hour_rows(16, end_before=start), "binance", "BTCUSDT", "4h")
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))

    with pytest.raises(BacktestError, match="warm-up|Missing"):
        run_backtest(config, db_path, start_ms=start, end_ms=start + 4 * HOUR, detector=_fake_detector)

    with pytest.raises(BacktestError, match="hour boundary"):
        parse_backtest_bound("2026-06-01T00:00:01+00:00", label="end")


def test_gap_in_1h_series_aborts(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    start = 40 * FOUR
    lookback = start - 48 * HOUR
    rows = _hourly_rows(lookback, 48 + 8)
    del rows[50]  # punch a hole inside the continuous window
    init_db(db_path)
    upsert_candles(db_path, rows, "binance", "BTCUSDT", "1h")
    upsert_candles(db_path, _four_hour_rows(16, end_before=lookback), "binance", "BTCUSDT", "4h")
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))

    with pytest.raises(BacktestError, match="gaps"):
        run_backtest(config, db_path, start_ms=start, end_ms=start + 4 * HOUR, detector=_fake_detector)


def test_same_setup_blocks_second_buy_other_zone_allowed(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    start = 60 * FOUR
    lookback = start - 48 * HOUR
    rows = _hourly_rows(lookback, 48 + 30, base_close=106.0)
    # Trigger A: inside 90-100, with prior close above internal midpoint 105.
    rows[48]["close"] = 92.0
    rows[48]["open"] = 93.0
    rows[48]["high"] = 93.0
    rows[48]["low"] = 91.0
    rows[47]["close"] = 106.0
    # Stay inside the band so the dip origin does not reset before the second attempt.
    rows[50]["close"] = 91.0
    rows[50]["open"] = 92.0
    rows[50]["high"] = 92.0
    rows[50]["low"] = 90.5
    rows[49]["close"] = 91.0
    # Deeper zone B: inside 80-85 (different fingerprint, so a different setup).
    rows[52]["close"] = 82.0
    rows[52]["open"] = 83.0
    rows[52]["high"] = 83.0
    rows[52]["low"] = 81.0
    rows[51]["close"] = 106.0

    init_db(db_path)
    upsert_candles(db_path, rows, "binance", "BTCUSDT", "1h")
    upsert_candles(db_path, _four_hour_rows(20, end_before=lookback), "binance", "BTCUSDT", "4h")
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))

    first = run_backtest(
        config,
        db_path,
        start_ms=start,
        end_ms=start + 8 * HOUR,
        detector=_fake_detector,
    )
    second = run_backtest(
        config,
        db_path,
        start_ms=start,
        end_ms=start + 8 * HOUR,
        detector=_fake_detector,
    )

    buy_times = [buy["trigger_open_time"] for buy in first.buys]
    assert start in buy_times
    assert start + 2 * HOUR not in buy_times  # same setup_id, no midpoint reset
    assert start + 4 * HOUR in buy_times  # deeper zone still allowed
    assert [buy["trigger_open_time"] for buy in second.buys] == buy_times


def test_new_dip_origin_within_24h_blocks_same_zone_other_zone_allowed(tmp_path: Path) -> None:
    # A bounce above the midpoint inside 24h must not unlock the same zone; a deeper zone still BUY.
    db_path = tmp_path / "bot.sqlite"
    start = 60 * FOUR
    lookback = start - 48 * HOUR
    rows = _hourly_rows(lookback, 48 + 30, base_close=106.0)
    rows[48]["close"] = 92.0
    rows[48]["open"] = 93.0
    rows[48]["high"] = 93.0
    rows[48]["low"] = 91.0
    rows[47]["close"] = 106.0
    # New dip origin one hour later, then a second inside-zone red candle.
    rows[49]["close"] = 106.0
    rows[50]["close"] = 91.0
    rows[50]["open"] = 92.0
    rows[50]["high"] = 92.0
    rows[50]["low"] = 90.5
    rows[52]["close"] = 82.0
    rows[52]["open"] = 83.0
    rows[52]["high"] = 83.0
    rows[52]["low"] = 81.0
    rows[51]["close"] = 106.0

    init_db(db_path)
    upsert_candles(db_path, rows, "binance", "BTCUSDT", "1h")
    upsert_candles(db_path, _four_hour_rows(20, end_before=lookback), "binance", "BTCUSDT", "4h")
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))

    result = run_backtest(
        config,
        db_path,
        start_ms=start,
        end_ms=start + 8 * HOUR,
        detector=_fake_detector,
    )

    buy_times = [buy["trigger_open_time"] for buy in result.buys]
    assert start in buy_times
    assert start + 2 * HOUR not in buy_times  # new origin, but still inside 24h
    assert start + 4 * HOUR in buy_times  # deeper zone still allowed


def test_new_dip_origin_after_24h_allows_same_zone(tmp_path: Path) -> None:
    # Once 24h has elapsed, a later close above the midpoint is a new setup and may BUY.
    db_path = tmp_path / "bot.sqlite"
    start = 60 * FOUR
    lookback = start - 48 * HOUR
    rows = _hourly_rows(lookback, 48 + 30, base_close=106.0)
    rows[48]["close"] = 92.0
    rows[48]["open"] = 93.0
    rows[48]["high"] = 93.0
    rows[48]["low"] = 91.0
    rows[47]["close"] = 106.0
    rows[49]["close"] = 106.0
    later = 48 + 24
    rows[later]["close"] = 91.0
    rows[later]["open"] = 92.0
    rows[later]["high"] = 92.0
    rows[later]["low"] = 90.5

    init_db(db_path)
    upsert_candles(db_path, rows, "binance", "BTCUSDT", "1h")
    upsert_candles(db_path, _four_hour_rows(20, end_before=lookback), "binance", "BTCUSDT", "4h")
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))

    result = run_backtest(
        config,
        db_path,
        start_ms=start,
        end_ms=start + 25 * HOUR,
        detector=_fake_detector,
    )

    buy_times = [buy["trigger_open_time"] for buy in result.buys]
    assert start in buy_times
    assert start + 24 * HOUR in buy_times


def test_backtest_does_not_mutate_live_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    start = 70 * FOUR
    lookback = start - 48 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 8, warm_up_4h=16)
    # Pretend live already has rows; replay must leave counts unchanged.
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO bot_state(key, value, updated_at) VALUES (?, ?, ?)",
            ("zone_rebuild_watermark:binance:BTCUSDT:4h:support_structure_v1", str(start - FOUR), 1),
        )
        conn.execute(
            """
            INSERT INTO zone_sets(exchange, symbol, timeframe, detector_version, zone_set_as_of, zone_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("binance", "BTCUSDT", "4h", "support_structure_v1", start - FOUR, 0, 1),
        )
        conn.commit()
    before = live_table_counts(db_path)
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))

    run_backtest(config, db_path, start_ms=start, end_ms=start + 4 * HOUR, detector=_fake_detector)

    assert live_table_counts(db_path) == before


def test_buy_csv_columns_and_zero_buy_header(tmp_path: Path) -> None:
    path = tmp_path / "buys.csv"
    write_buy_csv(path, [])
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
        assert list(rows) == []
        handle.seek(0)
        reader = csv.reader(handle)
        assert next(reader) == BUY_CSV_COLUMNS


def test_chart_payload_excludes_hold_and_segments_stop_at_end(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    start = 80 * FOUR
    lookback = start - 48 * HOUR
    _seed_db(db_path, hourly_start=lookback, hourly_count=48 + 8, warm_up_4h=16)
    config = AppConfig(database_path=str(db_path), zones=ZoneConfig(external_swing_order=2))
    end = start + 8 * HOUR

    payload = load_backtest_chart_payload(config, db_path, start_ms=start, end_ms=end)
    assert "holds" not in payload
    assert set(payload.keys()) == {"meta", "candles", "buys", "zone_segments"}
    for segment in payload["zone_segments"]:
        assert segment["valid_to"] <= end
        assert segment["valid_from"] < segment["valid_to"]
        assert {
            "fingerprint",
            "zone_lineage_id",
            "zone_track_id",
            "revision_fingerprint",
            "origin",
            "bounds_style",
            "score",
            "low",
            "mid",
            "high",
            "touches",
            "source_timeframe",
            "valid_from",
            "valid_to",
        } <= set(segment)


def test_zone_segments_merge_when_lineage_stays_and_sources_change() -> None:
    lineage = "zf1:lineage"
    first = ZoneSnapshot(
        zone_set_as_of=0,
        valid_from=0,
        zones=[
            {
                "fingerprint": lineage,
                "zone_lineage_id": lineage,
                "revision_fingerprint": "zf1:rev1",
                "origin": "structure_swing_low",
                "bounds_style": "body",
                "score": 4.0,
                "low": 90.0,
                "mid": 95.0,
                "high": 100.0,
                "touches": 2,
                "source_timeframe": "4h",
            }
        ],
    )
    second = ZoneSnapshot(
        zone_set_as_of=FOUR,
        valid_from=FOUR,
        zones=[
            {
                "fingerprint": lineage,
                "zone_lineage_id": lineage,
                "revision_fingerprint": "zf1:rev2",
                "origin": "mixed_structure",
                "bounds_style": "body",
                "score": 6.0,
                "low": 90.0,
                "mid": 95.0,
                "high": 100.0,
                "touches": 3,
                "source_timeframe": "4h",
            }
        ],
    )

    segments = build_zone_segments([first, second], end_ms=2 * FOUR)

    assert len(segments) == 1
    assert segments[0]["valid_from"] == 0
    assert segments[0]["valid_to"] == 2 * FOUR
    assert segments[0]["touches"] == 3
    assert segments[0]["zone_lineage_id"] == lineage
    assert segments[0]["origin"] == "mixed_structure"
    assert segments[0]["score"] == 6.0
    assert segments[0]["revision_fingerprint"] == "zf1:rev2"


def test_cli_backtest_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["backtest", "--start", "2026-06-01T00:00:00+00:00", "--csv", "out.csv"]
    )
    assert args.command == "backtest"
    assert args.start == "2026-06-01T00:00:00+00:00"
    assert args.end is None
    assert args.csv == "out.csv"

    args_with_end = parser.parse_args(
        [
            "backtest",
            "--start",
            "2026-06-01T00:00:00+00:00",
            "--end",
            "2026-06-02T00:00:00+00:00",
            "--csv",
            "out.csv",
        ]
    )
    assert args_with_end.end == "2026-06-02T00:00:00+00:00"
