from __future__ import annotations

import os
import yaml
from decimal import Decimal
from typing import Any, Literal
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .trading.constants import POLYGON_CHAIN_ID, SWAP_ROUTER_02_ADDRESSES


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
    below_zone_min_pct: float = 0.50
    inside_zone_max_pct: float = 0.70


class WalletConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keystore_path: str = "data/wallet/trader-dev.json"
    expected_address: str | None = None
    password_env: str = "KEYSTORE_PASSWORD"

    @field_validator("expected_address")
    @classmethod
    def validate_expected_address(cls, value: str | None) -> str | None:
        # Normalize a configured public address once so all later comparisons use checksum form.
        if value is None:
            return None
        from web3 import Web3

        if not Web3.is_address(value):
            raise ValueError("wallet.expected_address must be a valid EVM address")
        return Web3.to_checksum_address(value)


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chain_id: int = POLYGON_CHAIN_ID
    rpc_url_env: str = "POLYGON_RPC_URL"
    quote_base_url: str = "https://prana.triethocduongpho.net"
    router_allowlist: list[str] = Field(default_factory=lambda: sorted(SWAP_ROUTER_02_ADDRESSES))
    live_enabled: bool = False
    max_approval_gas: int = 100_000
    receipt_timeout_seconds: int = 120

    @field_validator("router_allowlist")
    @classmethod
    def validate_router_allowlist(cls, values: list[str]) -> list[str]:
        # Reject invalid or mutable router choices before any RPC or signing work can start.
        from web3 import Web3

        if not values:
            raise ValueError("execution.router_allowlist must not be empty")
        immutable = {address.lower() for address in SWAP_ROUTER_02_ADDRESSES}
        normalized: list[str] = []
        for value in values:
            if not Web3.is_address(value) or value.lower() not in immutable:
                raise ValueError(f"Router is not in the immutable allowlist: {value}")
            checksum = Web3.to_checksum_address(value)
            if checksum not in normalized:
                normalized.append(checksum)
        return normalized

    @field_validator("max_approval_gas", "receipt_timeout_seconds")
    @classmethod
    def validate_positive_execution_limits(cls, value: int) -> int:
        # Fail closed on limits that would disable gas or receipt-timeout protection.
        if value <= 0:
            raise ValueError("Execution limits must be positive")
        return value


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trade_amount_usdt: Decimal = Decimal("1")
    max_cumulative_usdt: Decimal = Decimal("10")


class AppConfig(BaseModel):
    exchange: str = "binance"
    symbol: str = "BTCUSDT"
    timeframe: str = "4h"
    database_path: str = "data/prana_buy_the_dips.sqlite"
    zones: ZoneConfig = Field(default_factory=ZoneConfig)
    price_feed: PriceFeedConfig = Field(default_factory=PriceFeedConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    environment: Literal["dev", "prod"] = "dev"
    wallet: WalletConfig = Field(default_factory=WalletConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)

    @model_validator(mode="after")
    def validate_wallet_execution_safety(self) -> "AppConfig":
        # Lock canary configuration and prevent a development machine from entering live mode.
        if self.execution.chain_id != POLYGON_CHAIN_ID:
            raise ValueError(f"execution.chain_id must be {POLYGON_CHAIN_ID}")
        if self.risk.trade_amount_usdt != Decimal("1"):
            raise ValueError("risk.trade_amount_usdt must be exactly 1")
        if self.risk.max_cumulative_usdt < Decimal("0"):
            raise ValueError("risk.max_cumulative_usdt must not be negative")
        if self.risk.max_cumulative_usdt > Decimal("10"):
            raise ValueError("risk.max_cumulative_usdt must not exceed 10")
        if self.environment == "dev":
            if "trader-prod" in str(Path(self.wallet.keystore_path)).lower():
                raise ValueError("A dev config cannot use a trader-prod keystore")
            quote_host = (urlparse(self.execution.quote_base_url).hostname or "").lower()
            if quote_host in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("A dev config cannot use the production loopback quote host")
            if self.execution.live_enabled:
                raise ValueError("A dev config cannot enable live trading")
        return self


def load_config(path: str | Path | None = None) -> AppConfig:
    # Load non-secret YAML settings after optional local environment variables.
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
