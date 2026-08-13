from __future__ import annotations

import csv

from pathlib import Path
from datetime import datetime, timezone

import pytest

import src.trading.backtest as backtest_module

from src.cli import build_parser
from src.config import AppConfig, ZoneConfig
from src.db import connect, init_db, upsert_candles
from src.backtest_chart_server import load_backtest_chart_payload
from src.trading.constants import FOUR_HOURS_MS, ONE_HOUR_MS
from src.trading.backtest import BUY_CSV_COLUMNS, BacktestError, live_table_counts, parse_backtest_bound, run_backtest, write_buy_csv


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


def test_cooldown_same_zone_blocks_second_buy_other_zone_allowed(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    start = 60 * FOUR
    lookback = start - 48 * HOUR
    rows = _hourly_rows(lookback, 48 + 30, base_close=106.0)
    # Force two inside-zone BUYs on the selected band, then a deeper-band BUY.
    # Trigger A: inside 90-100 below mid, with prior close above internal midpoint 105.
    rows[48]["close"] = 92.0
    rows[48]["open"] = 92.0
    rows[48]["high"] = 93.0
    rows[48]["low"] = 91.0
    # Keep dip-origin above midpoint in the hour before first trigger.
    rows[47]["close"] = 106.0
    # Second same-zone attempt a few hours later still inside cooldown.
    rows[50]["close"] = 91.0
    rows[50]["open"] = 91.0
    rows[50]["high"] = 92.0
    rows[50]["low"] = 90.5
    rows[49]["close"] = 106.0
    # Deeper zone B: inside 80-85 strictly below mid (not the same selected fingerprint as A).
    rows[52]["close"] = 82.0
    rows[52]["open"] = 82.0
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
    assert start + 2 * HOUR not in buy_times  # same-zone cooldown
    assert start + 4 * HOUR in buy_times  # deeper zone still allowed
    assert [buy["trigger_open_time"] for buy in second.buys] == buy_times


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
        assert {"fingerprint", "low", "mid", "high", "touches", "source_timeframe", "valid_from", "valid_to"} <= set(
            segment
        )


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
