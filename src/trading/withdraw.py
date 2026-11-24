from __future__ import annotations

import sqlite3

from pathlib import Path
from dataclasses import dataclass

from typing import Any

from web3 import Web3
from web3.exceptions import TimeExhausted

from ..config import AppConfig
from .wallet import WalletError
from ..utils import resolve_path
from .contract_checks import ContractCheckError, ContractCheckResult, run_contract_checks
from .constants import EXPECTED_PRANA_ONCHAIN_SYMBOL, POLYGON_CHAIN_ID, POLYGON_PRANA_ADDRESS, PRANA_DECIMALS


# Only this address may receive a full-balance withdrawal. A CLI typo cannot redirect the tokens.
WITHDRAW_PRANA_DESTINATION = "0x696b00596F553FcF6F98EeBfD58F48d2645D7E1b"
MAX_PRANA_TRANSFER_GAS = 100_000

# Separate from the trading ABI so wallet checks and approvals cannot call transfer.
_PRANA_TRANSFER_ABI = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


class WithdrawError(RuntimeError):
    """Raised when a PRANA withdrawal must stop before or after signing."""


class WithdrawBroadcastTimeout(WithdrawError):
    """Raised when the transfer was broadcast but the receipt is still unknown."""


@dataclass(frozen=True)
class WithdrawalResult:
    action: str
    wallet_address: str
    destination: str
    balance_raw: int
    pol_balance_raw: int
    confirmation: str
    transaction_hash: str | None = None
    gas_limit: int | None = None
    gas_price_wei: int | None = None


def withdraw_all_prana(
    config: AppConfig,
    *,
    destination: str,
    broadcast: bool,
    confirmation: str | None = None,
    password: str | None = None,
    web3: Any | None = None,
) -> WithdrawalResult:
    # Move the prod wallet's whole PRANA balance, or preview that transfer without signing.
    try:
        return _withdraw_all_prana(
            config,
            destination=destination,
            broadcast=broadcast,
            confirmation=confirmation,
            password=password,
            web3=web3,
        )
    except (WithdrawError, ContractCheckError, WalletError):
        raise
    except Exception as exc:
        raise WithdrawError("PRANA withdrawal failed") from exc


def _withdraw_all_prana(
    config: AppConfig,
    *,
    destination: str,
    broadcast: bool,
    confirmation: str | None,
    password: str | None,
    web3: Any | None,
) -> WithdrawalResult:
    # Require the prod keystore, the pinned recipient, and a successful receipt before reporting success.
    _assert_prod_config(config)
    pinned_destination = _pinned_destination(destination)
    checked = run_contract_checks(config, password=password, web3=web3)
    if checked.chain_id != POLYGON_CHAIN_ID:
        raise WithdrawError("Refusing to withdraw PRANA on the wrong chain")
    if pinned_destination == checked.wallet_address:
        raise WithdrawError("Refusing to withdraw PRANA to the prod wallet itself")

    confirmation_text = _confirmation(checked.wallet_address, pinned_destination)
    prana = _prana_contract(checked)
    nonce = None
    if broadcast:
        # Capture the confirmed nonce before the balance read so a concurrent send cannot advance it silently.
        nonce = checked.web3.eth.get_transaction_count(checked.wallet_address, "latest")
    balance_raw = int(prana.functions.balanceOf(checked.wallet_address).call())
    if balance_raw == 0:
        # An empty wallet must not build, sign, or broadcast a zero-value transfer.
        return WithdrawalResult(
            "empty",
            checked.wallet_address,
            pinned_destination,
            0,
            checked.pol_balance_raw,
            confirmation_text,
        )

    if broadcast:
        if not Path(config.risk.pause_file).is_file():
            raise WithdrawError("Create the configured pause file and stop the live timer before broadcasting")
        if confirmation != confirmation_text:
            raise WithdrawError("Withdrawal confirmation is missing or does not match")
        _assert_no_in_flight_execution(config)

    function, gas_limit, gas_price_wei = _prepare_transfer(checked, prana, pinned_destination, balance_raw)
    if nonce is None:
        return WithdrawalResult(
            "preview",
            checked.wallet_address,
            pinned_destination,
            balance_raw,
            checked.pol_balance_raw,
            confirmation_text,
            gas_limit=gas_limit,
            gas_price_wei=gas_price_wei,
        )

    transaction_hash = _send_transfer(config, checked, function, nonce, gas_limit, gas_price_wei)
    try:
        remaining_raw = int(prana.functions.balanceOf(checked.wallet_address).call())
    except Exception as exc:
        raise WithdrawError(
            f"Transfer mined but the remaining PRANA balance could not be read; transaction hash: {transaction_hash}"
        ) from exc
    if remaining_raw != 0:
        raise WithdrawError(
            f"Transfer mined but PRANA still remains; transaction hash: {transaction_hash}"
        )
    return WithdrawalResult(
        "withdrawn",
        checked.wallet_address,
        pinned_destination,
        balance_raw,
        checked.pol_balance_raw,
        confirmation_text,
        transaction_hash=transaction_hash,
        gas_limit=gas_limit,
        gas_price_wei=gas_price_wei,
    )


def _assert_prod_config(config: AppConfig) -> None:
    # Block canary and any config whose keystore filename is not the production wallet.
    if config.environment != "prod":
        raise WithdrawError("PRANA withdrawal requires environment=prod")
    if "trader-prod" not in Path(config.wallet.keystore_path).name.lower():
        raise WithdrawError("PRANA withdrawal requires the trader-prod keystore")


def _pinned_destination(destination: str) -> str:
    # Normalize the input before comparing it to the one operator-selected recipient.
    if not Web3.is_address(destination):
        raise WithdrawError("Withdrawal destination is not a valid address")
    checksum = Web3.to_checksum_address(destination)
    if checksum != WITHDRAW_PRANA_DESTINATION:
        raise WithdrawError("Destination is not the pinned withdrawal address")
    return checksum


def _confirmation(wallet_address: str, destination: str) -> str:
    # Bind the acknowledgment to this chain, the decrypted prod wallet, and the pinned recipient.
    return f"withdraw-all-prana:{POLYGON_CHAIN_ID}:{wallet_address}:{destination}"


def _prana_contract(checked: ContractCheckResult) -> Any:
    # Re-read the canonical PRANA token before trusting a balance that will be transferred in full.
    address = Web3.to_checksum_address(POLYGON_PRANA_ADDRESS)
    contract = checked.web3.eth.contract(address=address, abi=_PRANA_TRANSFER_ABI)
    symbol = contract.functions.symbol().call()
    decimals = int(contract.functions.decimals().call())
    if symbol != EXPECTED_PRANA_ONCHAIN_SYMBOL:
        raise WithdrawError(f"Unexpected PRANA symbol: {symbol}")
    if decimals != PRANA_DECIMALS:
        raise WithdrawError(f"Unexpected PRANA decimals: {decimals}")
    return contract


def _assert_no_in_flight_execution(config: AppConfig) -> None:
    # Refuse while an earlier buy may still be waiting on the same wallet nonce.
    database_path = resolve_path(config.database_path)
    if not database_path.is_file():
        raise WithdrawError("Prod database is missing; refusing to broadcast")
    try:
        connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise WithdrawError("Could not read trade executions; refusing to broadcast") from exc
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'trade_executions'"
        ).fetchone()
        if table is None:
            raise WithdrawError("trade_executions is missing; refusing to broadcast")
        row = connection.execute(
            """
            SELECT id, status FROM trade_executions
            WHERE status IN (
                'started', 'risk_checked', 'quoted', 'allowance_ready', 'signed', 'broadcast', 'pending'
            )
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error as exc:
        raise WithdrawError("Could not read trade executions; refusing to broadcast") from exc
    finally:
        connection.close()
    if row is not None:
        raise WithdrawError(f"Refusing to broadcast while trade execution {int(row[0])} is {row[1]}")


def _prepare_transfer(
    checked: ContractCheckResult,
    prana: Any,
    destination: str,
    balance_raw: int,
) -> tuple[Any, int, int]:
    # Simulate the exact full-balance transfer and stop when gas or POL cannot cover it.
    if balance_raw <= 0:
        raise WithdrawError("PRANA transfer amount must be positive")
    function = prana.functions.transfer(destination, balance_raw)
    context = {"from": checked.wallet_address}
    try:
        accepted = function.call(context)
        if accepted is True:
            estimated_gas = int(function.estimate_gas(context))
    except Exception as exc:
        raise WithdrawError("PRANA transfer simulation failed") from exc
    if accepted is not True:
        raise WithdrawError("PRANA transfer simulation returned false")
    if estimated_gas <= 0 or estimated_gas > MAX_PRANA_TRANSFER_GAS:
        raise WithdrawError("PRANA transfer gas estimate exceeds the limit")
    gas_limit = min(MAX_PRANA_TRANSFER_GAS, (estimated_gas * 120 + 99) // 100)
    gas_price_wei = int(checked.web3.eth.gas_price)
    if gas_price_wei <= 0:
        raise WithdrawError("Polygon gas price is unavailable")
    if checked.pol_balance_raw < gas_limit * gas_price_wei:
        raise WithdrawError("POL balance cannot pay the PRANA transfer gas")
    return function, gas_limit, gas_price_wei


def _send_transfer(
    config: AppConfig,
    checked: ContractCheckResult,
    function: Any,
    nonce: int,
    gas_limit: int,
    gas_price_wei: int,
) -> str:
    # Sign locally, broadcast once, and require a mined success before the caller re-reads the balance.
    if checked.chain_id != POLYGON_CHAIN_ID:
        raise WithdrawError("Refusing to sign a PRANA transfer for the wrong chain")
    if checked.web3.eth.get_transaction_count(checked.wallet_address, "pending") != nonce:
        raise WithdrawError("Wallet nonce changed or a transaction is pending; refusing to sign")
    # The bound contract function fills in the canonical PRANA contract address.
    transaction = function.build_transaction(
        {
            "from": checked.wallet_address,
            "value": 0,
            "nonce": nonce,
            "chainId": POLYGON_CHAIN_ID,
            "gas": gas_limit,
            "gasPrice": gas_price_wei,
        }
    )
    signed = checked.account.sign_transaction(transaction)
    raw_transaction = getattr(signed, "raw_transaction", None)
    if raw_transaction is None:
        raw_transaction = getattr(signed, "rawTransaction", None)
    if raw_transaction is None:
        raise WithdrawError("Local signer did not return a raw transaction")
    signed_hash = getattr(signed, "hash", None)
    rendered_hash = _render_transaction_hash(Web3.keccak(bytes(raw_transaction)) if signed_hash is None else signed_hash)
    try:
        transaction_hash = checked.web3.eth.send_raw_transaction(raw_transaction)
    except Exception as exc:
        # The RPC may have accepted the transaction before the response was lost.
        raise WithdrawError(f"PRANA broadcast outcome unknown; transaction hash: {rendered_hash}") from exc
    if _render_transaction_hash(transaction_hash).lower() != rendered_hash.lower():
        raise WithdrawError(f"RPC returned a different PRANA transfer hash; signed transaction hash: {rendered_hash}")
    try:
        receipt = checked.web3.eth.wait_for_transaction_receipt(
            transaction_hash,
            timeout=config.execution.receipt_timeout_seconds,
        )
    except (TimeExhausted, TimeoutError) as exc:
        raise WithdrawBroadcastTimeout(
            f"PRANA transfer broadcast but receipt timed out; transaction hash: {rendered_hash}"
        ) from exc
    except Exception as exc:
        raise WithdrawError(f"PRANA receipt lookup failed; transaction hash: {rendered_hash}") from exc
    status = receipt.get("status") if hasattr(receipt, "get") else getattr(receipt, "status", None)
    if int(status or 0) != 1:
        raise WithdrawError(f"PRANA transfer reverted: {rendered_hash}")
    return rendered_hash


def _render_transaction_hash(value: Any) -> str:
    # Convert bytes-like or HexBytes hashes to one public 0x-prefixed identifier.
    rendered = value.hex() if hasattr(value, "hex") else str(value)
    return rendered if rendered.startswith("0x") else f"0x{rendered}"
