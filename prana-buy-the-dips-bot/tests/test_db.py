from __future__ import annotations

from src.db import candle_count, init_db, load_candles_df, upsert_candles


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
