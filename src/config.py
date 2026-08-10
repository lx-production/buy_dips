from __future__ import annotations

import os
import yaml
from typing import Any
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ZoneConfig(BaseModel):
    min_touches: int = 2
    role_buffer_pct: float = 0.0015
    internal_swing_order: int = 2
    external_swing_order: int = 5
    atr_period: int = 14
    break_atr_mult: float = 0.2
    external_min_swing_atr_mult: float = 4.0
    external_min_swing_pct: float = 2.5
    show_internal_pivots: bool = False


class PriceFeedConfig(BaseModel):
    timeframe: str = "1h"
    fetch_limit: int = 168


class StrategyConfig(BaseModel):
    version: str = "support_close_v1"
    config_version: str = "1"
    dip_lookback_hours: int = 48
    cooldown_hours: int = 24
    below_zone_min_pct: float = 0.70


class AppConfig(BaseModel):
    exchange: str = "binance"
    symbol: str = "BTCUSDT"
    timeframe: str = "4h"
    database_path: str = "data/prana_buy_the_dips.sqlite"
    zones: ZoneConfig = Field(default_factory=ZoneConfig)
    price_feed: PriceFeedConfig = Field(default_factory=PriceFeedConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    load_dotenv()
    config_path = Path(path or os.getenv("CONFIG_PATH", "config.yaml"))
    payload: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"Config file must contain a mapping: {config_path}")
            payload = loaded
    return AppConfig.model_validate(payload)
