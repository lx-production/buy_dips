from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..binance_client import BinanceSpotClient
from ..db import upsert_candles
from .constants import EXCHANGE, HOURLY_TIMEFRAME, ONE_HOUR_MS, SYMBOL


class HourlyFeedError(RuntimeError):
    pass


def fetch_closed_hourly_candles(
    database_path: str | Path,
    *,
    exchange: str = EXCHANGE,
    symbol: str = SYMBOL,
    limit: int = 168,
    client: BinanceSpotClient | None = None,
) -> int:
    """Fetch only 1h klines and persist only candles Binance marks closed."""
    if exchange != EXCHANGE:
        raise HourlyFeedError(f"Unsupported hourly exchange: {exchange}")
    client = client or BinanceSpotClient()
    rows = client.fetch_klines(symbol=symbol, interval=HOURLY_TIMEFRAME, limit=limit)
    closed_rows = [row for row in rows if int(row.get("is_closed", 0)) == 1]
    if not closed_rows:
        raise HourlyFeedError("Binance returned no closed 1h candles")
    _validate_closed_hourly_rows(closed_rows)
    return upsert_candles(database_path, closed_rows, exchange, symbol, HOURLY_TIMEFRAME)


def _validate_closed_hourly_rows(rows: list[dict[str, Any]]) -> None:
    seen: set[int] = set()
    for row in rows:
        try:
            open_time = int(row["open_time"])
            close_time = int(row["close_time"])
            values = {name: float(row[name]) for name in ("open", "high", "low", "close")}
        except (KeyError, TypeError, ValueError) as exc:
            raise HourlyFeedError("Binance returned a malformed closed 1h candle") from exc
        if open_time < 0 or open_time % ONE_HOUR_MS != 0:
            raise HourlyFeedError(f"1h candle is not UTC aligned: {open_time}")
        if close_time != open_time + ONE_HOUR_MS - 1:
            raise HourlyFeedError(f"1h candle has an invalid close_time: {open_time}")
        if open_time in seen:
            raise HourlyFeedError(f"Binance returned a duplicate 1h candle: {open_time}")
        if any(not math.isfinite(value) for value in values.values()):
            raise HourlyFeedError(f"1h candle contains a non-finite OHLC value: {open_time}")
        if values["high"] < max(values["open"], values["close"]) or values["low"] > min(
            values["open"], values["close"]
        ):
            raise HourlyFeedError(f"1h candle contains inconsistent OHLC values: {open_time}")
        seen.add(open_time)
