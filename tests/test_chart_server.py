from __future__ import annotations

import pandas as pd

from src.chart_server import _chart_pivots, _visible_support_zones, load_chart_payload
from src.config import AppConfig, ZoneConfig
from src.db import upsert_candles


def test_visible_support_zones_keep_below_price_and_nearest_two_above() -> None:
    zones = [
        _zone(80.0, 85.0, touches=4),
        _zone(70.0, 75.0, touches=3),
        _zone(102.0, 107.0, touches=2),
        _zone(110.0, 115.0, touches=8),
        _zone(130.0, 135.0, touches=10),
    ]

    visible = _visible_support_zones(zones, current_price=100.0)

    assert [(zone["low"], zone["high"]) for zone in visible] == [
        (80.0, 85.0),
        (70.0, 75.0),
        (102.0, 107.0),
        (110.0, 115.0),
    ]


def test_visible_support_zones_keep_price_touching_zone() -> None:
    zones = [_zone(98.0, 103.0), _zone(104.0, 109.0), _zone(120.0, 125.0)]

    visible = _visible_support_zones(zones, current_price=100.0, above_count=1)

    assert [(zone["low"], zone["high"]) for zone in visible] == [(98.0, 103.0), (104.0, 109.0)]


def test_chart_pivots_hide_internal_pivots_by_default() -> None:
    df = pd.DataFrame(
        {
            "open_time": list(range(9)),
            "open": [100, 110, 105, 112, 108, 120, 100, 115, 105],
            "high": [101, 111, 106, 113, 109, 121, 101, 116, 106],
            "low": [99, 109, 104, 111, 107, 119, 99, 114, 104],
            "close": [100, 110, 105, 112, 108, 120, 100, 115, 105],
        }
    )

    pivots = _chart_pivots(
        df,
        visible_start_index=0,
        internal_swing_order=1,
        external_swing_order=1,
        atr_period=3,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        show_internal_pivots=False,
    )

    assert pivots
    assert {pivot["term"] for pivot in pivots} == {"external"}


def test_chart_pivots_can_include_internal_debug_labels() -> None:
    df = pd.DataFrame(
        {
            "open_time": list(range(9)),
            "open": [100, 110, 105, 112, 108, 120, 100, 115, 105],
            "high": [101, 111, 106, 113, 109, 121, 101, 116, 106],
            "low": [99, 109, 104, 111, 107, 119, 99, 114, 104],
            "close": [100, 110, 105, 112, 108, 120, 100, 115, 105],
        }
    )

    pivots = _chart_pivots(
        df,
        visible_start_index=0,
        internal_swing_order=1,
        external_swing_order=1,
        atr_period=3,
        external_min_swing_atr_mult=0.0,
        external_min_swing_pct=0.0,
        show_internal_pivots=True,
    )

    assert {pivot["term"] for pivot in pivots} == {"external", "internal"}


def test_load_chart_payload_can_load_all_daily_candles(tmp_path) -> None:
    db_path = tmp_path / "bot.sqlite"
    config = AppConfig()
    daily_candles = [
        _candle(open_time=1_700_000_000_000, close_time=1_700_086_399_999, close=100.0),
        _candle(open_time=1_700_086_400_000, close_time=1_700_172_799_999, close=101.0),
        _candle(open_time=1_700_172_800_000, close_time=1_700_259_199_999, close=102.0),
    ]
    four_hour_candles = [_candle(open_time=1_700_000_000_000, close_time=1_700_014_399_999, close=90.0)]
    upsert_candles(db_path, daily_candles, config.exchange, config.symbol, "1d")
    upsert_candles(db_path, four_hour_candles, config.exchange, config.symbol, "4h")

    payload = load_chart_payload(config=config, database_path=db_path, limit=None, timeframe="1D")

    assert payload["timeframe"] == "1d"
    assert payload["total_candles"] == 3
    assert [candle["close"] for candle in payload["candles"]] == [100.0, 101.0, 102.0]


def test_load_chart_payload_aggregates_four_hour_candles_to_daily_when_needed(tmp_path) -> None:
    db_path = tmp_path / "bot.sqlite"
    config = AppConfig()
    four_hour_candles = [
        _candle(open_time=1_700_006_400_000, close_time=1_700_020_799_999, close=100.0),
        _candle(open_time=1_700_020_800_000, close_time=1_700_035_199_999, close=103.0),
        _candle(open_time=1_700_092_800_000, close_time=1_700_107_199_999, close=98.0),
    ]
    upsert_candles(db_path, four_hour_candles, config.exchange, config.symbol, "4h")

    payload = load_chart_payload(config=config, database_path=db_path, limit=None, timeframe="1d")

    assert payload["timeframe"] == "1d"
    assert payload["total_candles"] == 2
    assert [candle["open"] for candle in payload["candles"]] == [99.0, 97.0]
    assert [candle["high"] for candle in payload["candles"]] == [104.0, 99.0]
    assert [candle["low"] for candle in payload["candles"]] == [98.0, 96.0]
    assert [candle["close"] for candle in payload["candles"]] == [103.0, 98.0]


def test_daily_chart_view_uses_four_hour_zones(tmp_path) -> None:
    db_path = tmp_path / "bot.sqlite"
    config = AppConfig(
        zones=ZoneConfig(
            external_swing_order=2,
            atr_period=3,
            external_min_swing_atr_mult=0.0,
            external_min_swing_pct=0.0,
        )
    )
    four_hour_closes = [67000, 66500, 66000, 66800, 67500, 66900, 66400, 66380, 67000, 67600]
    daily_closes = [100000, 101000, 102000, 103000, 104000]
    upsert_candles(db_path, _candles_from_closes(four_hour_closes, step_ms=14_400_000), config.exchange, config.symbol, "4h")
    upsert_candles(db_path, _candles_from_closes(daily_closes, step_ms=86_400_000), config.exchange, config.symbol, "1d")

    four_hour_payload = load_chart_payload(config=config, database_path=db_path, limit=None, timeframe="4h")
    daily_payload = load_chart_payload(config=config, database_path=db_path, limit=None, timeframe="1d")

    four_hour_zones = [(zone["low"], zone["high"]) for zone in four_hour_payload["zones"]["support"]]
    daily_zones = [(zone["low"], zone["high"]) for zone in daily_payload["zones"]["support"]]
    assert daily_payload["timeframe"] == "1d"
    assert [candle["close"] for candle in daily_payload["candles"]] == [100000.0, 101000.0, 102000.0, 103000.0, 104000.0]
    assert daily_zones == four_hour_zones


def _zone(low: float, high: float, touches: int = 1) -> dict:
    return {"low": low, "high": high, "touches": touches}


def _candles_from_closes(closes: list[float], step_ms: int) -> list[dict[str, float | int]]:
    start_time = 1_700_000_000_000
    return [
        _candle(open_time=start_time + index * step_ms, close_time=start_time + (index + 1) * step_ms - 1, close=close)
        for index, close in enumerate(closes)
    ]


def _candle(open_time: int, close_time: int, close: float) -> dict[str, float | int]:
    return {
        "open_time": open_time,
        "close_time": close_time,
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "volume": 12.5,
        "is_closed": 1,
    }
