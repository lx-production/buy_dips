from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..binance_client import BinanceSpotClient
from ..config import AppConfig
from ..db import connect, init_db, load_candles_df, upsert_candles
from ..utils import utc_ms
from .aggregate_4h import (
    aggregate_four_hour_bucket,
    aggregate_overdue_buckets,
    latest_overdue_bucket_open_time,
)
from .binance_hourly import fetch_closed_hourly_candles
from .constants import DETECTOR_VERSION, HOURLY_TIMEFRAME, ZONE_TIMEFRAME
from .signal import evaluate_support_close_v1
from .state_store import get_zone_rebuild_watermark, zone_rebuild_watermark_key
from .store import has_recent_zone_buy, has_setup_buy, insert_decision
from .zone_refresh import ZoneRefreshResult, refresh_zones


class TradingCycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class TradingCycleResult:
    decision_id: int
    decision: dict[str, Any]
    fetched_hourly_candles: int
    derived_four_hour_candles: int


def run_trade_once(
    config: AppConfig,
    database_path: str | Path,
    *,
    mode: str = "observe",
    now_ms: int | None = None,
    client: BinanceSpotClient | None = None,
    fetch: bool = True,
) -> TradingCycleResult:
    """Run the data/zone/decision portion of one idempotent hourly cycle."""
    if mode not in {"observe", "dry_run", "live"}:
        raise TradingCycleError(f"Unsupported mode: {mode}")
    if config.price_feed.timeframe != HOURLY_TIMEFRAME:
        raise TradingCycleError("The live price feed timeframe must be 1h")
    now_ms = utc_ms() if now_ms is None else int(now_ms)
    init_db(database_path)

    fetched = 0
    if fetch:
        fetched = fetch_closed_hourly_candles(
            database_path,
            exchange=config.exchange,
            symbol=config.symbol,
            limit=config.price_feed.fetch_limit,
            client=client,
        )

    hourly = load_candles_df(
        database_path,
        config.exchange,
        config.symbol,
        HOURLY_TIMEFRAME,
        only_closed=True,
    )
    if hourly.empty:
        raise TradingCycleError("No closed 1h candle exists after the hourly fetch")
    trigger = hourly.iloc[-1].to_dict()

    watermark_key = zone_rebuild_watermark_key(
        config.exchange,
        config.symbol,
        ZONE_TIMEFRAME,
        DETECTOR_VERSION,
    )
    with connect(database_path) as conn:
        watermark = get_zone_rebuild_watermark(conn, watermark_key)

    latest_due = latest_overdue_bucket_open_time(now_ms)
    # Revalidate the latest due bucket even when its snapshot watermark already exists.
    # Missing constituent 1h rows must always abort rather than trade on that snapshot.
    aggregate_four_hour_bucket(hourly, latest_due, now_ms=now_ms)
    derived = aggregate_overdue_buckets(hourly, now_ms=now_ms, after_open_time=watermark)
    if derived:
        upsert_candles(database_path, derived, config.exchange, config.symbol, ZONE_TIMEFRAME)

    four_hour = load_candles_df(
        database_path,
        config.exchange,
        config.symbol,
        ZONE_TIMEFRAME,
        only_closed=True,
    )
    if not four_hour.empty:
        four_hour = four_hour[four_hour["open_time"].astype("int64") <= latest_due].reset_index(drop=True)
    if four_hour.empty or int(four_hour.iloc[-1]["open_time"]) != latest_due:
        raise TradingCycleError("Latest overdue 4h bucket was not derived and persisted")

    refresh: ZoneRefreshResult = refresh_zones(
        database_path,
        four_hour,
        zone_config=config.zones,
        exchange=config.exchange,
        symbol=config.symbol,
    )
    with connect(database_path) as conn:
        decision = evaluate_support_close_v1(
            trigger,
            hourly,
            refresh.zones,
            zone_set_as_of=refresh.zone_set_as_of,
            setup_already_bought=lambda fingerprint, dip_origin: has_setup_buy(
                conn, fingerprint, dip_origin, int(trigger["open_time"])
            ),
            recent_zone_buy=lambda fingerprint: has_recent_zone_buy(
                conn,
                fingerprint,
                int(trigger["open_time"]),
                cooldown_hours=config.strategy.cooldown_hours,
            ),
            zones_rebuilt=refresh.rebuilt,
            mode=mode,
            strategy_version=config.strategy.version,
            config_version=config.strategy.config_version,
            dip_lookback_hours=config.strategy.dip_lookback_hours,
            below_zone_min_pct=config.strategy.below_zone_min_pct,
            inside_zone_max_pct=config.strategy.inside_zone_max_pct,
        )
        decision_id = insert_decision(
            conn,
            decision,
            exchange=config.exchange,
            symbol=config.symbol,
            timeframe=HOURLY_TIMEFRAME,
        )
        conn.commit()
    return TradingCycleResult(decision_id, decision, fetched, len(derived))
