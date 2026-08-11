from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from eth_account.signers.local import LocalAccount
from web3 import Web3

from ..config import AppConfig
from .constants import (
    ERC20_ABI,
    EXPECTED_PRANA_ONCHAIN_SYMBOL,
    EXPECTED_USDT_ONCHAIN_SYMBOLS,
    POLYGON_CHAIN_ID,
    POLYGON_PRANA_ADDRESS,
    POLYGON_USDT_ADDRESS,
    PRANA_DECIMALS,
    SWAP_ROUTER_02_ADDRESSES,
    USDT_DECIMALS,
)
from .wallet import WalletError, load_local_account, resolve_keystore_password


class ContractCheckError(RuntimeError):
    """Raised when the configured Polygon wallet or contracts fail validation."""


@dataclass(frozen=True)
class ContractCheckResult:
    environment: str
    chain_id: int
    wallet_address: str
    router_address: str
    pol_balance_raw: int
    usdt_balance_raw: int
    allowance_raw: int
    web3: Any = field(repr=False)
    account: LocalAccount = field(repr=False)
    usdt_contract: Any = field(repr=False)


def resolve_rpc_url(variable_name: str, environ: dict[str, str] | None = None) -> str:
    # Resolve the RPC endpoint without ever including its potentially secret value in errors.
    source = os.environ if environ is None else environ
    rpc_url = source.get(variable_name, "").strip()
    if not rpc_url:
        raise ContractCheckError(f"Polygon RPC URL is unavailable; set {variable_name}")
    return rpc_url


def create_web3(rpc_url: str, *, timeout_seconds: int = 10) -> Web3:
    # Build a short-timeout HTTP client so safety checks fail promptly when Polygon is unavailable.
    provider = Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout_seconds})
    return Web3(provider)


def run_contract_checks(
    config: AppConfig,
    *,
    password: str | None = None,
    web3: Any | None = None,
) -> ContractCheckResult:
    # Redact provider-level exception details because an RPC URL can contain an API key.
    try:
        return _run_contract_checks(config, password=password, web3=web3)
    except (ContractCheckError, WalletError):
        raise
    except Exception as exc:
        raise ContractCheckError("Polygon contract check failed") from exc


def _run_contract_checks(
    config: AppConfig,
    *,
    password: str | None,
    web3: Any | None,
) -> ContractCheckResult:
    # Verify network, signer, bytecode, token metadata, balances, and allowance in fail-closed order.
    w3 = web3
    if w3 is None:
        rpc_url = resolve_rpc_url(config.execution.rpc_url_env)
        w3 = create_web3(rpc_url)
    if not w3.is_connected():
        raise ContractCheckError("Polygon RPC connection failed")
    chain_id = int(w3.eth.chain_id)
    if chain_id != POLYGON_CHAIN_ID or chain_id != config.execution.chain_id:
        raise ContractCheckError(f"Unexpected chain ID: {chain_id}")

    resolved_password = password
    if resolved_password is None:
        resolved_password = resolve_keystore_password(config.wallet.password_env)
    account = load_local_account(
        config.wallet.keystore_path,
        resolved_password,
        expected_address=config.wallet.expected_address,
    )
    wallet_address = Web3.to_checksum_address(account.address)
    usdt_address = Web3.to_checksum_address(POLYGON_USDT_ADDRESS)
    prana_address = Web3.to_checksum_address(POLYGON_PRANA_ADDRESS)
    routers = [Web3.to_checksum_address(address) for address in config.execution.router_allowlist]
    immutable_routers = {address.lower() for address in SWAP_ROUTER_02_ADDRESSES}
    if any(address.lower() not in immutable_routers for address in routers):
        raise ContractCheckError("Configured router is outside the immutable allowlist")

    for label, address in [("USDT", usdt_address), ("PRANA", prana_address), *[("router", item) for item in routers]]:
        if not _has_bytecode(w3.eth.get_code(address)):
            raise ContractCheckError(f"{label} contract has empty bytecode")

    usdt_contract = w3.eth.contract(address=usdt_address, abi=ERC20_ABI)
    prana_contract = w3.eth.contract(address=prana_address, abi=ERC20_ABI)
    usdt_symbol = _normalize_symbol(usdt_contract.functions.symbol().call())
    usdt_decimals = int(usdt_contract.functions.decimals().call())
    prana_symbol = _normalize_symbol(prana_contract.functions.symbol().call())
    prana_decimals = int(prana_contract.functions.decimals().call())
    if usdt_symbol not in EXPECTED_USDT_ONCHAIN_SYMBOLS:
        raise ContractCheckError(f"Unexpected USDT symbol: {usdt_symbol}")
    if usdt_decimals != USDT_DECIMALS:
        raise ContractCheckError(f"Unexpected USDT decimals: {usdt_decimals}")
    if prana_symbol != EXPECTED_PRANA_ONCHAIN_SYMBOL:
        raise ContractCheckError(f"Unexpected PRANA symbol: {prana_symbol}")
    if prana_decimals != PRANA_DECIMALS:
        raise ContractCheckError(f"Unexpected PRANA decimals: {prana_decimals}")

    router_address = routers[0]
    pol_balance = int(w3.eth.get_balance(wallet_address))
    usdt_balance = int(usdt_contract.functions.balanceOf(wallet_address).call())
    allowance = int(usdt_contract.functions.allowance(wallet_address, router_address).call())
    return ContractCheckResult(
        environment=config.environment,
        chain_id=chain_id,
        wallet_address=wallet_address,
        router_address=router_address,
        pol_balance_raw=pol_balance,
        usdt_balance_raw=usdt_balance,
        allowance_raw=allowance,
        web3=w3,
        account=account,
        usdt_contract=usdt_contract,
    )


def format_token_amount(raw_amount: int, decimals: int) -> str:
    # Render integer token units exactly without introducing binary floating-point rounding.
    rendered = format(Decimal(raw_amount) / (Decimal(10) ** decimals), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _has_bytecode(code: Any) -> bool:
    # Treat all standard representations of an empty eth_getCode response as missing code.
    if code is None:
        return False
    if isinstance(code, str):
        return code.lower() not in {"", "0x"}
    return len(bytes(code)) > 0


def _normalize_symbol(value: Any) -> str:
    # Normalize common string and fixed-bytes ERC-20 metadata responses for strict comparison.
    if isinstance(value, bytes):
        try:
            return value.rstrip(b"\x00").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractCheckError("Token symbol is not valid UTF-8") from exc
    if not isinstance(value, str):
        raise ContractCheckError("Token symbol is not a string")
    return value
