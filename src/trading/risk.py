from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from web3 import Web3

from ..config import AppConfig
from .constants import POLYGON_CHAIN_ID, TRADE_AMOUNT_USDT_RAW
from .store import get_live_exposure, has_other_in_flight_execution


PROD_QUOTE_BASE_URL = "http://127.0.0.1:4173"


class LiveModeNotAllowed(RuntimeError):
    """Raised when any independent live-trading safety gate is not satisfied."""


class RiskCheckError(RuntimeError):
    """Raised with a safe reason code when downstream execution must be skipped."""

    def __init__(self, code: str) -> None:
        """Keep persisted/logged risk failures stable and free of secret values."""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LiveExposure:
    """Describe conservative live canary usage before reserving another trade."""

    utc_day_trade_count: int
    utc_day_spend_raw: int
    cumulative_spend_raw: int


def assert_live_mode_allowed(
    config: AppConfig,
    wallet_address: str,
    confirmation: str | None,
) -> None:
    # Require production config, pinned wallet, loopback quote host, and wallet-specific confirmation.
    if config.environment != "prod":
        raise LiveModeNotAllowed("Live mode requires environment=prod")
    if not config.execution.live_enabled:
        raise LiveModeNotAllowed("Live mode is disabled in configuration")
    if not Web3.is_address(wallet_address):
        raise LiveModeNotAllowed("Loaded wallet address is invalid")
    loaded_address = Web3.to_checksum_address(wallet_address)
    expected_address = config.wallet.expected_address
    if expected_address is None or loaded_address != Web3.to_checksum_address(expected_address):
        raise LiveModeNotAllowed("Loaded wallet does not match wallet.expected_address")
    parsed_quote_url = urlparse(config.execution.quote_base_url)
    normalized_quote_url = config.execution.quote_base_url.rstrip("/")
    if (
        normalized_quote_url != PROD_QUOTE_BASE_URL
        or parsed_quote_url.hostname != "127.0.0.1"
        or parsed_quote_url.port != 4173
    ):
        raise LiveModeNotAllowed("Live mode requires the pinned production loopback quote host")
    expected_confirmation = f"polygon:{POLYGON_CHAIN_ID}:{loaded_address}"
    if confirmation != expected_confirmation:
        raise LiveModeNotAllowed("Live wallet confirmation is missing or does not match")


def check_pre_execution_risk(
    conn: sqlite3.Connection,
    config: AppConfig,
    execution_id: int,
    *,
    now_s: int,
) -> LiveExposure:
    """Apply pause, in-flight, and live canary caps before external execution work.

    Dry runs obey the operational pause and in-flight lock, but they do not
    consume or get blocked by live-spend caps because they never sign.
    """
    if Path(config.risk.pause_file).exists():
        raise RiskCheckError("PAUSE_FILE_PRESENT")
    if has_other_in_flight_execution(conn, execution_id):
        raise RiskCheckError("UNRESOLVED_EXECUTION")

    exposure_row = get_live_exposure(conn, now_s=now_s, exclude_execution_id=execution_id)
    exposure = LiveExposure(
        utc_day_trade_count=exposure_row["utc_day_trade_count"],
        utc_day_spend_raw=exposure_row["utc_day_spend_raw"],
        cumulative_spend_raw=exposure_row["cumulative_spend_raw"],
    )
    execution = conn.execute(
        "SELECT mode FROM trade_executions WHERE id=?",
        (int(execution_id),),
    ).fetchone()
    if execution is None:
        raise RiskCheckError("EXECUTION_NOT_FOUND")
    if execution["mode"] != "live":
        return exposure

    if exposure.utc_day_trade_count >= config.risk.max_trades_per_utc_day:
        raise RiskCheckError("DAILY_TRADE_LIMIT_REACHED")
    max_cumulative_raw = _usdt_to_raw(config.risk.max_cumulative_usdt)
    if exposure.cumulative_spend_raw + TRADE_AMOUNT_USDT_RAW > max_cumulative_raw:
        raise RiskCheckError("CUMULATIVE_SPEND_LIMIT_REACHED")
    return exposure


def check_wallet_funds(config: AppConfig, *, usdt_balance_raw: int, pol_balance_raw: int) -> None:
    """Require the exact trade balance and untouched minimum POL reserve."""
    if usdt_balance_raw < TRADE_AMOUNT_USDT_RAW:
        raise RiskCheckError("INSUFFICIENT_USDT_BALANCE")
    if pol_balance_raw < _pol_to_wei(config.risk.min_pol_reserve):
        raise RiskCheckError("INSUFFICIENT_POL_RESERVE")


def check_gas_reserve(
    config: AppConfig,
    *,
    pol_balance_raw: int,
    gas_price_wei: int,
    gas_limit: int,
    include_approval: bool,
) -> None:
    """Keep the configured POL reserve after a conservative swap/approval gas budget."""
    if gas_price_wei <= 0 or gas_limit <= 0:
        raise RiskCheckError("INVALID_GAS_ESTIMATE")
    approval_gas = config.execution.max_approval_gas if include_approval else 0
    required = _pol_to_wei(config.risk.min_pol_reserve) + gas_price_wei * (gas_limit + approval_gas)
    if pol_balance_raw < required:
        raise RiskCheckError("INSUFFICIENT_POL_GAS_RESERVE")


def _usdt_to_raw(amount: Decimal) -> int:
    """Convert configured USDT to exact six-decimal integer units."""
    return int(amount * Decimal(1_000_000))


def _pol_to_wei(amount: Decimal) -> int:
    """Convert configured POL to exact eighteen-decimal integer units."""
    return int(amount * Decimal(10**18))
