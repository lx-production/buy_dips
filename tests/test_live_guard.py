from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import AppConfig
from src.trading.risk import LiveModeNotAllowed, assert_live_mode_allowed


WALLET = "0x0000000000000000000000000000000000000001"
OTHER_WALLET = "0x0000000000000000000000000000000000000002"


def _prod_config(**execution_overrides: object) -> AppConfig:
    # Build the smallest production config that can satisfy every pure live gate.
    execution = {
        "quote_base_url": "http://127.0.0.1:4173",
        "live_enabled": True,
        **execution_overrides,
    }
    return AppConfig(
        environment="prod",
        wallet={"keystore_path": "data/wallet/trader-prod.json", "expected_address": WALLET},
        execution=execution,
    )


def test_dev_always_rejects_live_even_with_matching_confirmation() -> None:
    # Confirm a development environment cannot be unlocked by a copied confirmation value.
    config = AppConfig(wallet={"expected_address": WALLET})

    with pytest.raises(LiveModeNotAllowed, match="environment=prod"):
        assert_live_mode_allowed(config, WALLET, f"polygon:137:{WALLET}")


def test_prod_accepts_only_all_matching_live_gates() -> None:
    # Accept the exact production wallet, enabled flag, loopback quote host, and confirmation tuple.
    config = _prod_config()

    assert_live_mode_allowed(config, WALLET, f"polygon:137:{WALLET}")


@pytest.mark.parametrize(
    ("config", "wallet", "confirmation", "message"),
    [
        (_prod_config(live_enabled=False), WALLET, f"polygon:137:{WALLET}", "disabled"),
        (_prod_config(quote_base_url="https://prana.triethocduongpho.net"), WALLET, f"polygon:137:{WALLET}", "loopback"),
        (_prod_config(), OTHER_WALLET, f"polygon:137:{OTHER_WALLET}", "expected_address"),
        (_prod_config(), WALLET, "polygon:137:wrong", "confirmation"),
    ],
)
def test_prod_rejects_each_missing_live_gate(config, wallet, confirmation, message) -> None:
    # Prove each independent gate fails closed rather than relying on the others.
    with pytest.raises(LiveModeNotAllowed, match=message):
        assert_live_mode_allowed(config, wallet, confirmation)


@pytest.mark.parametrize(
    "payload",
    [
        {"environment": "dev", "wallet": {"keystore_path": "data/wallet/trader-prod.json"}},
        {"execution": {"chain_id": 1}},
        {"execution": {"router_allowlist": ["0x0000000000000000000000000000000000000001"]}},
        {"execution": {"quote_base_url": "http://127.0.0.1:4173"}},
        {"risk": {"trade_amount_usdt": "2"}},
        {"risk": {"max_cumulative_usdt": "10.01"}},
        {"execution": {"live_enabled": True}},
        {"wallet": {"password": "must-not-be-in-yaml"}},
        {"execution": {"rpc_url": "must-not-be-in-yaml"}},
    ],
)
def test_config_rejects_unsafe_wallet_execution_values(payload) -> None:
    # Lock immutable Polygon, router, canary, secret-source, and development safety settings.
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)
