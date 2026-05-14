from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .utils import utc_ms


BINANCE_SPOT_BASE_URL = "https://api.binance.com"


class BinanceClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class BinanceSpotClient:
    base_url: str = BINANCE_SPOT_BASE_URL
    timeout: float = 15.0

    def fetch_klines(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "4h",
        limit: int = 1000,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("Binance kline limit must be between 1 and 1000")

        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time

        url = f"{self.base_url}/api/v3/klines"
        try:
            import requests

            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise BinanceClientError(f"Failed to fetch Binance klines: {exc}") from exc

        data = response.json()
        if not isinstance(data, list):
            raise BinanceClientError(f"Unexpected Binance response: {data}")

        now_ms = utc_ms()
        parsed: list[dict[str, Any]] = []
        for item in data:
            parsed.append(
                {
                    "open_time": int(item[0]),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                    "close_time": int(item[6]),
                    "is_closed": 1 if int(item[6]) <= now_ms else 0,
                }
            )
        return parsed
