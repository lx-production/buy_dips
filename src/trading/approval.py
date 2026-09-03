from __future__ import annotations

from dataclasses import dataclass

from typing import Any

from web3 import Web3

from ..config import AppConfig
from .constants import CANARY_ALLOWANCE_USDT_RAW, POLYGON_CHAIN_ID
from .contract_checks import ContractCheckError, ContractCheckResult, run_contract_checks


class ApprovalError(RuntimeError):
    """Raised when an allowance transaction cannot be completed and verified safely."""


class ApprovalBroadcastTimeout(ApprovalError):
    """Raised after broadcast when receipt status is unknown and retrying would be unsafe."""


@dataclass(frozen=True)
class ApprovalResult:
    action: str
    previous_allowance_raw: int
    current_allowance_raw: int
    transaction_hashes: tuple[str, ...]


def approve_trading(
    config: AppConfig,
    *,
    password: str | None = None,
    web3: Any | None = None,
) -> ApprovalResult:
    # Run every contract check, zero-reset a prior allowance, then grant exactly the canary cap.
    try:
        checked = run_contract_checks(config, password=password, web3=web3)
        previous = checked.allowance_raw
        if previous == CANARY_ALLOWANCE_USDT_RAW:
            return ApprovalResult("already-approved", previous, previous, ())

        transaction_hashes: list[str] = []
        if previous != 0:
            transaction_hashes.append(_send_approval_transaction(config, checked, 0))
            if _read_allowance(checked) != 0:
                raise ApprovalError("USDT allowance did not reset to zero")
        transaction_hashes.append(_send_approval_transaction(config, checked, CANARY_ALLOWANCE_USDT_RAW))
        current = _read_allowance(checked)
        if current != CANARY_ALLOWANCE_USDT_RAW:
            raise ApprovalError("USDT allowance did not reach the 10 USDT canary cap")
        return ApprovalResult("approved", previous, current, tuple(transaction_hashes))
    except (ApprovalError, ContractCheckError):
        raise
    except Exception as exc:
        raise ApprovalError("Approval flow failed; inspect the current allowance before retrying") from exc


def revoke_trading(
    config: AppConfig,
    *,
    password: str | None = None,
    web3: Any | None = None,
) -> ApprovalResult:
    # Run every contract check and send one zero approval only when an allowance currently exists.
    try:
        checked = run_contract_checks(config, password=password, web3=web3)
        previous = checked.allowance_raw
        if previous == 0:
            return ApprovalResult("already-revoked", 0, 0, ())
        transaction_hash = _send_approval_transaction(config, checked, 0)
        current = _read_allowance(checked)
        if current != 0:
            raise ApprovalError("USDT allowance did not revoke to zero")
        return ApprovalResult("revoked", previous, current, (transaction_hash,))
    except (ApprovalError, ContractCheckError):
        raise
    except Exception as exc:
        raise ApprovalError("Revocation failed; inspect the current allowance before retrying") from exc


def ensure_swap_allowance(
    config: AppConfig,
    checked: ContractCheckResult,
    router_address: str,
    amount_raw: int,
) -> ApprovalResult:
    """Top up only the validated quote amount, using USDT's zero-reset flow when required."""
    router = Web3.to_checksum_address(router_address)
    allowed = {Web3.to_checksum_address(address) for address in config.execution.router_allowlist}
    if router not in allowed:
        raise ApprovalError("Quote router is outside the configured allowlist")
    if amount_raw <= 0 or amount_raw > CANARY_ALLOWANCE_USDT_RAW:
        raise ApprovalError("Swap approval amount is outside the canary cap")
    previous = _read_allowance(checked, router)
    if previous >= amount_raw:
        return ApprovalResult("already-sufficient", previous, previous, ())

    transaction_hashes: list[str] = []
    if previous != 0:
        transaction_hashes.append(_send_approval_transaction(config, checked, 0, router))
        if _read_allowance(checked, router) != 0:
            raise ApprovalError("USDT allowance did not reset to zero")
    transaction_hashes.append(_send_approval_transaction(config, checked, amount_raw, router))
    current = _read_allowance(checked, router)
    if current < amount_raw:
        raise ApprovalError("USDT allowance remains below the quote amount")
    return ApprovalResult("topped-up", previous, current, tuple(transaction_hashes))


def _send_approval_transaction(
    config: AppConfig,
    checked: ContractCheckResult,
    amount_raw: int,
    router_address: str | None = None,
) -> str:
    # Simulate, estimate, locally sign, broadcast once, and require a successful mined receipt.
    if amount_raw < 0 or amount_raw > CANARY_ALLOWANCE_USDT_RAW:
        raise ApprovalError("Approval amount is outside the permitted canary values")
    if checked.chain_id != POLYGON_CHAIN_ID:
        raise ApprovalError("Refusing to sign an approval for the wrong chain")
    router = checked.router_address if router_address is None else Web3.to_checksum_address(router_address)
    function = checked.usdt_contract.functions.approve(router, amount_raw)
    transaction_context = {"from": checked.wallet_address}
    function.call(transaction_context)
    estimated_gas = int(function.estimate_gas(transaction_context))
    if estimated_gas <= 0 or estimated_gas > config.execution.max_approval_gas:
        raise ApprovalError("Approval gas estimate exceeds the configured limit")
    padded_gas = min(config.execution.max_approval_gas, (estimated_gas * 120 + 99) // 100)
    transaction = function.build_transaction(
        {
            "from": checked.wallet_address,
            "nonce": checked.web3.eth.get_transaction_count(checked.wallet_address, "pending"),
            "chainId": POLYGON_CHAIN_ID,
            "gas": padded_gas,
            "gasPrice": checked.web3.eth.gas_price,
        }
    )
    signed = checked.account.sign_transaction(transaction)
    raw_transaction = getattr(signed, "raw_transaction", None)
    if raw_transaction is None:
        raw_transaction = getattr(signed, "rawTransaction", None)
    if raw_transaction is None:
        raise ApprovalError("Local signer did not return a raw transaction")
    transaction_hash = checked.web3.eth.send_raw_transaction(raw_transaction)
    rendered_hash = _render_transaction_hash(transaction_hash)
    try:
        receipt = checked.web3.eth.wait_for_transaction_receipt(
            transaction_hash,
            timeout=config.execution.receipt_timeout_seconds,
        )
    except Exception as exc:
        # Any post-broadcast polling failure leaves state unresolved, so surface the hash and never retry.
        raise ApprovalBroadcastTimeout(
            f"Approval broadcast but receipt timed out; transaction hash: {rendered_hash}"
        ) from exc
    status = receipt.get("status") if hasattr(receipt, "get") else getattr(receipt, "status", None)
    if int(status or 0) != 1:
        raise ApprovalError(f"Approval transaction reverted: {rendered_hash}")
    return rendered_hash


def _read_allowance(
    checked: ContractCheckResult,
    router_address: str | None = None,
) -> int:
    # Re-read the canonical USDT allowance after each mined state transition.
    router = checked.router_address if router_address is None else Web3.to_checksum_address(router_address)
    return int(
        checked.usdt_contract.functions.allowance(
            checked.wallet_address,
            router,
        ).call()
    )


def _render_transaction_hash(value: Any) -> str:
    # Convert bytes-like or HexBytes hashes to one public 0x-prefixed identifier.
    rendered = value.hex() if hasattr(value, "hex") else str(value)
    return rendered if rendered.startswith("0x") else f"0x{rendered}"
