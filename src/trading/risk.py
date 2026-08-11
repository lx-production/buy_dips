from __future__ import annotations

from urllib.parse import urlparse

from web3 import Web3

from ..config import AppConfig
from .constants import POLYGON_CHAIN_ID


PROD_QUOTE_BASE_URL = "http://127.0.0.1:4173"


class LiveModeNotAllowed(RuntimeError):
    """Raised when any independent live-trading safety gate is not satisfied."""


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
