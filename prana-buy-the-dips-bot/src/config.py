from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ZoneConfig(BaseModel):
    swing_order: int = 5
    lookahead: int = 6
    min_reversal_pct: float = 0.008
    zone_tolerance_pct: float = 0.0045
    min_touches: int = 2
    max_zone_width_pct: float = 0.018
    role_buffer_pct: float = 0.0015


class SignalConfig(BaseModel):
    near_support_pct_tight: float = 0.25
    near_support_pct_medium: float = 0.50
    near_support_pct_loose: float = 1.00
    near_resistance_pct: float = 0.50
    dip_lookback_candles: int = 20
    dip_threshold_1_pct: float = 3.0
    dip_threshold_2_pct: float = 5.0
    dip_threshold_3_pct: float = 8.0


class AppConfig(BaseModel):
    exchange: str = "binance"
    symbol: str = "BTCUSDT"
    timeframe: str = "4h"
    database_path: str = "data/prana_buy_the_dips.sqlite"
    zones: ZoneConfig = Field(default_factory=ZoneConfig)
    signals: SignalConfig = Field(default_factory=SignalConfig)


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
