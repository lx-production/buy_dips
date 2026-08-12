from __future__ import annotations

import json
import sqlite3

import pytest

from src.db import candle_count, connect, init_db, load_candles_df, upsert_candles


def _candle(close: float = 100.0) -> dict[str, float | int]:
    return {
        "open_time": 1_700_000_000_000,
        "close_time": 1_700_014_399_999,
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "close": close,
        "volume": 12.5,
        "is_closed": 1,
    }


def test_init_db_and_duplicate_upsert_keeps_one_row(tmp_path) -> None:
    db_path = tmp_path / "bot.sqlite"
    init_db(db_path)

    assert upsert_candles(db_path, [_candle(100.0)], "binance", "BTCUSDT", "4h") == 1
    assert upsert_candles(db_path, [_candle(101.0)], "binance", "BTCUSDT", "4h") == 1

    assert candle_count(db_path) == 1
    df = load_candles_df(db_path, "binance", "BTCUSDT", "4h")
    assert len(df) == 1
    assert float(df.iloc[0]["close"]) == 101.0


def test_insert_and_query_candles(tmp_path) -> None:
    db_path = tmp_path / "bot.sqlite"
    candles = [_candle(100.0), {**_candle(102.0), "open_time": 1_700_014_400_000, "close_time": 1_700_028_799_999}]

    upsert_candles(db_path, candles, "binance", "BTCUSDT", "4h")
    df = load_candles_df(db_path, "binance", "BTCUSDT", "4h")

    assert list(df["close"]) == [100.0, 102.0]
    assert list(df["symbol"]) == ["BTCUSDT", "BTCUSDT"]


def test_readable_views_convert_all_timestamps_without_changing_canonical_values(tmp_path) -> None:
    """Verify millisecond, second, JSON, and watermark timestamps across every readable view."""
    db_path = tmp_path / "bot.sqlite"
    init_db(db_path)
    expected_zero_utc7 = "1970-01-01 07:00:00 +07:00"
    expected_one_second_utc7 = "1970-01-01 07:00:01 +07:00"

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO candles(
              exchange, symbol, timeframe, open_time, close_time, open, high, low, close,
              volume, is_closed, fetched_at
            ) VALUES ('binance', 'BTCUSDT', '1h', 1000, 1000, 1, 1, 1, 1, NULL, 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO zones(
              created_at, exchange, symbol, timeframe, detector_version, zone_set_as_of,
              fingerprint_version, fingerprint, source_timeframe, source_open_times_json,
              zone_source_time, origin, role, bounds_style, low, high, mid, width, width_pct,
              touches, source_closes_json, source_indexes_json, metadata_json
            ) VALUES (
              1, 'binance', 'BTCUSDT', '4h', 'detector-v1', 1000,
              'zf1', 'zf1:test', '4h', '[0,1000]',
              1000, 'test', 'support', 'body', 1, 2, 1.5, 1, 50,
              2, '[1,2]', '[0,1]', NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO zone_sets(
              exchange, symbol, timeframe, detector_version, zone_set_as_of, zone_count, created_at
            ) VALUES ('binance', 'BTCUSDT', '4h', 'detector-v1', 1000, 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO decisions(
              created_at, exchange, symbol, timeframe, candle_open_time, candle_close_time,
              reference_close, zone_set_as_of, fingerprint_version, selected_zone_source_time,
              selected_source_open_times_json, lookback_start_time, lookback_end_time,
              dip_origin_open_time, gate_results_json, zones_rebuilt, decision, reason_code,
              mode, strategy_version, config_version
            ) VALUES (
              1, 'binance', 'BTCUSDT', '1h', 1000, 1000,
              1, 1000, 'zf1', NULL,
              '[0,1000]', NULL, 1000,
              NULL, '{}', 0, 'HOLD', 'CLOSE_OUTSIDE_ENTRY_REGION',
              'observe', 'strategy-v1', 'config-v1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO bot_state(key, value, updated_at)
            VALUES ('zone_rebuild_watermark:binance:BTCUSDT:4h:detector-v1', '1000', 1)
            """
        )

        candle = conn.execute("SELECT * FROM candles_readable").fetchone()
        assert candle["open_time"] == 1000
        assert candle["close_time"] == 1000
        assert candle["fetched_at"] == 1
        assert candle["open_time_utc7"] == expected_one_second_utc7
        assert candle["close_time_utc7"] == expected_one_second_utc7
        assert candle["fetched_at_utc7"] == expected_one_second_utc7

        zone = conn.execute("SELECT * FROM zones_readable").fetchone()
        assert zone["created_at"] == 1
        assert zone["zone_set_as_of"] == 1000
        assert zone["source_open_times_json"] == "[0,1000]"
        assert zone["zone_source_time"] == 1000
        assert zone["created_at_utc7"] == expected_one_second_utc7
        assert zone["zone_set_as_of_utc7"] == expected_one_second_utc7
        assert json.loads(zone["source_open_times_json_utc7"]) == [
            expected_zero_utc7,
            expected_one_second_utc7,
        ]
        assert zone["zone_source_time_utc7"] == expected_one_second_utc7

        zone_set = conn.execute("SELECT * FROM zone_sets_readable").fetchone()
        assert zone_set["zone_set_as_of"] == 1000
        assert zone_set["created_at"] == 1
        assert zone_set["zone_set_as_of_utc7"] == expected_one_second_utc7
        assert zone_set["created_at_utc7"] == expected_one_second_utc7

        decision = conn.execute("SELECT * FROM decisions_readable").fetchone()
        assert decision["created_at"] == 1
        for canonical_column in (
            "candle_open_time",
            "candle_close_time",
            "zone_set_as_of",
            "lookback_end_time",
        ):
            assert decision[canonical_column] == 1000
        for readable_column in (
            "created_at_utc7",
            "candle_open_time_utc7",
            "candle_close_time_utc7",
            "zone_set_as_of_utc7",
            "lookback_end_time_utc7",
        ):
            assert decision[readable_column] == expected_one_second_utc7
        assert decision["selected_source_open_times_json"] == "[0,1000]"
        assert json.loads(decision["selected_source_open_times_json_utc7"]) == [
            expected_zero_utc7,
            expected_one_second_utc7,
        ]
        assert decision["selected_zone_source_time_utc7"] is None
        assert decision["lookback_start_time_utc7"] is None
        assert decision["dip_origin_open_time_utc7"] is None

        conn.execute("UPDATE decisions SET selected_source_open_times_json = NULL")
        decision_with_null_json = conn.execute("SELECT * FROM decisions_readable").fetchone()
        assert decision_with_null_json["selected_source_open_times_json"] is None
        assert decision_with_null_json["selected_source_open_times_json_utc7"] is None

        state = conn.execute("SELECT * FROM bot_state_readable").fetchone()
        assert state["value"] == "1000"
        assert state["updated_at"] == 1
        assert state["value_utc7"] == expected_one_second_utc7
        assert state["updated_at_utc7"] == expected_one_second_utc7

        with pytest.raises(sqlite3.OperationalError, match="view"):
            conn.execute("DELETE FROM candles_readable")


def test_readable_views_are_added_to_an_existing_database(tmp_path) -> None:
    """Verify rerunning init_db adds views without recreating or backfilling canonical rows."""
    db_path = tmp_path / "bot.sqlite"
    init_db(db_path)
    upsert_candles(db_path, [_candle()], "binance", "BTCUSDT", "4h")

    with connect(db_path) as conn:
        conn.execute("DROP VIEW candles_readable")
        canonical_before = dict(conn.execute("SELECT * FROM candles").fetchone())

    init_db(db_path)

    with connect(db_path) as conn:
        canonical_after = dict(conn.execute("SELECT * FROM candles").fetchone())
        readable_count = conn.execute("SELECT COUNT(*) FROM candles_readable").fetchone()[0]

    assert canonical_after == canonical_before
    assert readable_count == 1
