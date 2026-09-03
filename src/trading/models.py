from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from typing import Any


@dataclass(frozen=True)
class SwapTransaction:
    to: str
    data: str
    value: int


@dataclass(frozen=True)
class ValidatedSwapQuote:
    token_in_symbol: str
    token_out_symbol: str
    amount_in: Decimal
    amount_in_raw: int
    recipient: str
    slippage_bps: int
    chain_id: int
    amount_out: Decimal
    amount_out_raw: int
    minimum_amount_out: Decimal
    router_address: str
    transaction: SwapTransaction
    deadline: int
    verification_version: int
    verification_expires_at: int


@dataclass(frozen=True)
class SwapSimulation:
    gas_estimate: int
    gas_limit: int


@dataclass(frozen=True)
class SignedSwap:
    nonce: int
    transaction_hash: str
    raw_transaction: Any = field(repr=False)


@dataclass(frozen=True)
class SwapReceipt:
    status: str
    transaction_hash: str
    block_number: int | None = None
    gas_used: int | None = None
    actual_prana_output_raw: int | None = None
