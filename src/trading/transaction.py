from __future__ import annotations

import time

from typing import Any

from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound

from .constants import POLYGON_CHAIN_ID, POLYGON_PRANA_ADDRESS
from .contract_checks import ContractCheckResult
from .models import SignedSwap, SwapReceipt, SwapSimulation, ValidatedSwapQuote


class TransactionError(RuntimeError):
    """Raised when a swap cannot be safely simulated, signed, sent, or reconciled."""


def simulate_swap(
    checked: ContractCheckResult,
    quote: ValidatedSwapQuote,
    *,
    max_swap_gas: int,
) -> SwapSimulation:
    """Run eth_call and estimate_gas against the exact validated quote transaction."""
    _validate_checked_quote(checked, quote)
    transaction = _transaction_context(checked, quote)
    try:
        checked.web3.eth.call(transaction)
        estimated_gas = int(checked.web3.eth.estimate_gas(transaction))
    except Exception as exc:
        raise TransactionError("Swap simulation failed") from exc
    if estimated_gas <= 0 or estimated_gas > max_swap_gas:
        raise TransactionError("Swap gas estimate exceeds the configured limit")
    gas_limit = min(max_swap_gas, (estimated_gas * 120 + 99) // 100)
    return SwapSimulation(gas_estimate=estimated_gas, gas_limit=gas_limit)


def prepare_signed_swap(
    checked: ContractCheckResult,
    quote: ValidatedSwapQuote,
    simulation: SwapSimulation,
    *,
    minimum_deadline_seconds: int,
    now_s: int | None = None,
) -> SignedSwap:
    """Reserve the pending nonce and sign the exact quote transaction before broadcast."""
    _validate_checked_quote(checked, quote)
    current_time = int(time.time()) if now_s is None else int(now_s)
    if quote.deadline <= current_time + minimum_deadline_seconds:
        raise TransactionError("Quote deadline is too close to sign")
    try:
        nonce = int(checked.web3.eth.get_transaction_count(checked.wallet_address, "pending"))
        transaction = {
            **_transaction_context(checked, quote),
            "nonce": nonce,
            "chainId": POLYGON_CHAIN_ID,
            "gas": simulation.gas_limit,
            "gasPrice": checked.web3.eth.gas_price,
        }
        signed = checked.account.sign_transaction(transaction)
    except Exception as exc:
        raise TransactionError("Swap signing failed") from exc
    raw_transaction = getattr(signed, "raw_transaction", None)
    if raw_transaction is None:
        raw_transaction = getattr(signed, "rawTransaction", None)
    if raw_transaction is None:
        raise TransactionError("Local signer did not return a raw transaction")
    signed_hash = getattr(signed, "hash", None)
    transaction_hash = _render_transaction_hash(
        Web3.keccak(bytes(raw_transaction)) if signed_hash is None else signed_hash
    )
    return SignedSwap(nonce=nonce, transaction_hash=transaction_hash, raw_transaction=raw_transaction)


def broadcast_signed_swap(checked: ContractCheckResult, signed: SignedSwap) -> str:
    """Broadcast one previously persisted signed intent and verify its transaction hash."""
    try:
        sent_hash = checked.web3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as exc:
        raise TransactionError("Swap broadcast failed; reconcile the reserved hash before retrying") from exc
    rendered_hash = _render_transaction_hash(sent_hash)
    if rendered_hash.lower() != signed.transaction_hash.lower():
        raise TransactionError("RPC returned a different transaction hash")
    return rendered_hash


def wait_for_swap_receipt(
    checked: ContractCheckResult,
    transaction_hash: str,
    *,
    recipient: str,
    timeout_seconds: int,
) -> SwapReceipt:
    """Wait for a receipt, returning pending on timeout so the exact hash can be reconciled."""
    try:
        receipt = checked.web3.eth.wait_for_transaction_receipt(
            transaction_hash,
            timeout=timeout_seconds,
        )
    except (TimeExhausted, TimeoutError):
        return SwapReceipt(status="pending", transaction_hash=transaction_hash)
    except Exception as exc:
        raise TransactionError("Swap receipt lookup failed") from exc
    return _parse_swap_receipt(receipt, transaction_hash, recipient)


def reconcile_swap(web3: Any, transaction_hash: str, *, recipient: str) -> SwapReceipt:
    """Look up one reserved hash without sending a replacement transaction."""
    try:
        receipt = web3.eth.get_transaction_receipt(transaction_hash)
    except TransactionNotFound:
        return SwapReceipt(status="pending", transaction_hash=transaction_hash)
    except Exception as exc:
        raise TransactionError("Swap reconciliation failed") from exc
    if receipt is None:
        return SwapReceipt(status="pending", transaction_hash=transaction_hash)
    return _parse_swap_receipt(receipt, transaction_hash, recipient)


def _validate_checked_quote(checked: ContractCheckResult, quote: ValidatedSwapQuote) -> None:
    """Ensure contract checks and quote validation refer to the same Polygon intent."""
    if checked.chain_id != POLYGON_CHAIN_ID or quote.chain_id != POLYGON_CHAIN_ID:
        raise TransactionError("Swap intent is not pinned to Polygon")
    if Web3.to_checksum_address(checked.wallet_address) != quote.recipient:
        raise TransactionError("Swap quote recipient does not match the checked wallet")
    if Web3.to_checksum_address(quote.router_address) != quote.transaction.to:
        raise TransactionError("Swap router does not match the quote transaction target")


def _transaction_context(
    checked: ContractCheckResult,
    quote: ValidatedSwapQuote,
) -> dict[str, Any]:
    """Build the exact call/signing transaction from validated quote fields."""
    return {
        "from": checked.wallet_address,
        "to": quote.transaction.to,
        "data": quote.transaction.data,
        "value": quote.transaction.value,
    }


def _parse_swap_receipt(receipt: Any, transaction_hash: str, recipient: str) -> SwapReceipt:
    """Convert a Polygon receipt into a redacted swap outcome and decoded PRANA output."""
    status = _receipt_value(receipt, "status")
    block_number = _optional_int(_receipt_value(receipt, "blockNumber"))
    gas_used = _optional_int(_receipt_value(receipt, "gasUsed"))
    if int(status or 0) != 1:
        return SwapReceipt(
            status="reverted",
            transaction_hash=transaction_hash,
            block_number=block_number,
            gas_used=gas_used,
        )
    return SwapReceipt(
        status="confirmed",
        transaction_hash=transaction_hash,
        block_number=block_number,
        gas_used=gas_used,
        actual_prana_output_raw=_decode_prana_output(receipt, recipient),
    )


def _decode_prana_output(receipt: Any, recipient: str) -> int | None:
    """Sum PRANA Transfer logs received by the bot without persisting raw receipt data."""
    logs = _receipt_value(receipt, "logs") or []
    transfer_topic = _render_transaction_hash(
        Web3.keccak(text="Transfer(address,address,uint256)")
    ).lower()
    recipient_topic = f"0x{'0' * 24}{recipient[2:].lower()}"
    total = 0
    matched = False
    for log in logs:
        address = _receipt_value(log, "address")
        topics = _receipt_value(log, "topics") or []
        if not isinstance(address, str) or address.lower() != POLYGON_PRANA_ADDRESS.lower():
            continue
        rendered_topics = [_render_transaction_hash(topic).lower() for topic in topics]
        if len(rendered_topics) < 3 or rendered_topics[0] != transfer_topic or rendered_topics[2] != recipient_topic:
            continue
        data = _receipt_value(log, "data")
        try:
            amount = int(data, 16) if isinstance(data, str) else int.from_bytes(bytes(data), "big")
        except (TypeError, ValueError):
            continue
        total += amount
        matched = True
    return total if matched else None


def _receipt_value(value: Any, key: str) -> Any:
    """Read either mapping-style or attribute-style web3 receipt fields."""
    if hasattr(value, "get"):
        return value.get(key)
    return getattr(value, key, None)


def _optional_int(value: Any) -> int | None:
    """Normalize an optional receipt integer."""
    return None if value is None else int(value)


def _render_transaction_hash(value: Any) -> str:
    """Render bytes-like web3 hashes as one 0x-prefixed identifier."""
    rendered = value.hex() if hasattr(value, "hex") else str(value)
    return rendered if rendered.startswith("0x") else f"0x{rendered}"
