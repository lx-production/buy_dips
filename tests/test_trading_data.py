from __future__ import annotations

import pytest
import pandas as pd

from src.config import AppConfig, ZoneConfig
from src.trading.runner import run_trade_once
from src.trading.zone_refresh import refresh_zones
from src.zones.timeframes import aggregate_ohlc_to_daily
from src.db import connect, init_db, load_candles_df, upsert_candles
from src.trading.binance_hourly import HourlyFeedError, fetch_closed_hourly_candles
from src.trading.aggregate_4h import OverdueIncompleteFourHourError, aggregate_four_hour_bucket
from src.trading.zone_identity import fingerprint_zone, make_zone_fingerprint, make_zone_lineage_id
from src.trading.state_store import get_zone_rebuild_watermark, set_zone_rebuild_watermark, validate_zone_rebuild_watermark, zone_rebuild_watermark_key


HOUR = 3_600_000
FOUR_HOURS = 4 * HOUR


class _HourlyClient:
    """Return fixed Binance rows and record the requested market parameters."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        """Store deterministic rows for an isolated hourly-feed test."""
        self.rows = rows
        self.calls: list[dict[str, object]] = []

    def fetch_klines(self, **kwargs: object) -> list[dict[str, object]]:
        """Capture the fetch arguments and return the configured rows."""
        self.calls.append(kwargs)
        return self.rows


def _hourly(bucket: int, count: int = 4) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open_time": bucket + index * HOUR,
                "close_time": bucket + (index + 1) * HOUR - 1,
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "volume": 10,
                "is_closed": 1,
            }
            for index in range(count)
        ]
    )


def _four_hour_history(count: int = 12) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open_time": index * FOUR_HOURS,
                "close_time": (index + 1) * FOUR_HOURS - 1,
                "open": 100 + index,
                "high": 110 + index,
                "low": 90 + index,
                "close": 105 + index,
                "volume": 1,
                "is_closed": 1,
            }
            for index in range(count)
        ]
    )


def test_hourly_fetch_requests_only_1h_and_persists_only_closed_rows(tmp_path) -> None:
    """The live feed must request 1h BTCUSDT data and discard the open candle."""
    closed = _hourly(0, count=1).iloc[0].to_dict()
    open_candle = _hourly(HOUR, count=1).iloc[0].to_dict()
    open_candle["is_closed"] = 0
    client = _HourlyClient([closed, open_candle])
    db_path = tmp_path / "bot.sqlite"

    inserted = fetch_closed_hourly_candles(db_path, limit=72, client=client)
    stored = load_candles_df(db_path, "binance", "BTCUSDT", "1h", only_closed=False)

    assert client.calls == [{"symbol": "BTCUSDT", "interval": "1h", "limit": 72}]
    assert inserted == 1
    assert stored["open_time"].tolist() == [0]
    assert stored["timeframe"].tolist() == ["1h"]


def test_hourly_fetch_fails_closed_when_binance_returns_no_closed_rows(tmp_path) -> None:
    """An open-only response must not create candle rows or permit a stale cycle."""
    open_candle = _hourly(0, count=1).iloc[0].to_dict()
    open_candle["is_closed"] = 0

    with pytest.raises(HourlyFeedError, match="no closed 1h"):
        fetch_closed_hourly_candles(tmp_path / "bot.sqlite", client=_HourlyClient([open_candle]))


def test_aggregate_closed_four_hour_bucket() -> None:
    result = aggregate_four_hour_bucket(_hourly(0), 0, now_ms=FOUR_HOURS)

    assert result == {
        "open_time": 0,
        "close_time": FOUR_HOURS - 1,
        "open": 100.0,
        "high": 105.0,
        "low": 99.0,
        "close": 104.0,
        "volume": 40.0,
        "is_closed": 1,
    }


def test_overdue_incomplete_four_hour_bucket_aborts() -> None:
    with pytest.raises(OverdueIncompleteFourHourError):
        aggregate_four_hour_bucket(_hourly(0, count=3), 0, now_ms=FOUR_HOURS)


def test_zf1_is_deterministic_and_ignores_detector_labels() -> None:
    first = make_zone_fingerprint(
        low=90, high=100, source_open_times=[FOUR_HOURS, 0, FOUR_HOURS], source_timeframe="4h"
    )
    second = make_zone_fingerprint(
        low="90.000000000", high="100.00000000", source_open_times=[0, FOUR_HOURS], source_timeframe="4h"
    )

    assert first == second
    assert first.startswith("zf1:")


def test_lineage_stays_put_when_revision_gains_a_touch() -> None:
    four_hour = _four_hour_history(12)
    daily = aggregate_ohlc_to_daily(four_hour, min_bars_per_day=6)
    base = {
        "low": 90,
        "high": 100,
        "source_timeframe": "4h",
        "bounds_style": "body",
        "origin": "test",
    }

    first = fingerprint_zone({**base, "source_indexes": [2]}, four_hour_df=four_hour, daily_df=daily)
    second = fingerprint_zone({**base, "source_indexes": [2, 3]}, four_hour_df=four_hour, daily_df=daily)
    other_style = make_zone_lineage_id(low=90, high=100, source_timeframe="4h", bounds_style="local_reaction")

    assert first["fingerprint"] == first["zone_lineage_id"] == second["zone_lineage_id"]
    assert first["revision_fingerprint"] != second["revision_fingerprint"]
    assert first["fingerprint"] != first["revision_fingerprint"]
    assert other_style != first["zone_lineage_id"]


def test_daily_zone_sources_resolve_against_daily_frame() -> None:
    four_hour = _four_hour_history(12)
    daily = aggregate_ohlc_to_daily(four_hour, min_bars_per_day=6)
    zone = {
        "low": 90,
        "high": 100,
        "source_timeframe": "1d",
        "source_indexes": [1],
        "origin": "daily_body_support",
    }

    result = fingerprint_zone(zone, four_hour_df=four_hour, daily_df=daily)

    assert result["source_open_times"] == [86_400_000]
    assert result["zone_source_time"] == 86_400_000
    assert result["fingerprint"] == result["zone_lineage_id"]
    assert result["revision_fingerprint"].startswith("zf1:")


def test_zone_snapshot_and_watermark_commit_together(tmp_path) -> None:
    db_path = tmp_path / "bot.sqlite"
    four_hour = _four_hour_history()
    calls = 0

    def detector(_df, **_kwargs):
        nonlocal calls
        calls += 1
        zone = {
            "origin": "test",
            "role": "support",
            "bounds_style": "body",
            "low": 90,
            "high": 100,
            "mid": 95,
            "width": 10,
            "width_pct": 10 / 95 * 100,
            "touches": 1,
            "source_closes": [100],
            "source_indexes": [2],
        }
        return {"support": [zone], "all": [zone]}

    first = refresh_zones(db_path, four_hour, zone_config=ZoneConfig(), detector=detector, now_s=123)
    second = refresh_zones(db_path, four_hour, zone_config=ZoneConfig(), detector=detector, now_s=124)

    assert first.rebuilt is True
    assert second.rebuilt is False
    assert calls == 1
    assert first.zones[0]["fingerprint"].startswith("zf1:")
    assert first.zones[0]["fingerprint"] == first.zones[0]["zone_track_id"]
    assert first.zones[0]["fingerprint"] != first.zones[0]["zone_lineage_id"]
    assert first.zones[0]["revision_fingerprint"].startswith("zf1:")
    with connect(db_path) as conn:
        key = zone_rebuild_watermark_key()
        assert get_zone_rebuild_watermark(conn, key) == int(four_hour.iloc[-1]["open_time"])
        manifest = conn.execute("SELECT zone_count FROM zone_sets").fetchone()
        assert manifest["zone_count"] == 1


def test_watermark_requires_complete_manifest(tmp_path) -> None:
    db_path = tmp_path / "bot.sqlite"
    init_db(db_path)
    key = zone_rebuild_watermark_key()
    with connect(db_path) as conn:
        set_zone_rebuild_watermark(conn, key, FOUR_HOURS, 123)
        conn.commit()
        with pytest.raises(RuntimeError, match="manifest"):
            validate_zone_rebuild_watermark(
                conn,
                key=key,
                latest_completed_open_time=FOUR_HOURS,
            )


def test_observe_runner_persists_one_decision_after_deriving_latest_4h(tmp_path) -> None:
    db_path = tmp_path / "bot.sqlite"
    history = _four_hour_history(11)
    due_hourly = _hourly(11 * FOUR_HOURS)
    upsert_candles(db_path, history.to_dict("records"), "binance", "BTCUSDT", "4h")
    upsert_candles(db_path, due_hourly.to_dict("records"), "binance", "BTCUSDT", "1h")
    config = AppConfig(database_path=str(db_path))

    result = run_trade_once(config, db_path, now_ms=12 * FOUR_HOURS, fetch=False)

    # Seeded 1h bars are green (close > open), so the red-candle gate holds first.
    assert result.derived_four_hour_candles == 1
    assert result.decision["reason_code"] == "CLOSE_NOT_BELOW_OPEN"
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM decisions").fetchone()["count"] == 1
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signals'").fetchone() is None
