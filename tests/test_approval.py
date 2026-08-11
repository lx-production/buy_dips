from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.config import AppConfig
from src.trading.approval import (
    ApprovalBroadcastTimeout,
    ApprovalError,
    approve_trading,
    revoke_trading,
)
from src.trading.constants import CANARY_ALLOWANCE_USDT_RAW
from src.trading.contract_checks import ContractCheckResult


WALLET = "0x0000000000000000000000000000000000000001"
ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"


class _AllowanceCall:
    def __init__(self, contract: "_ApprovalContract") -> None:
        # Keep a live reference so post-receipt verification sees the updated mock state.
        self.contract = contract

    def call(self) -> int:
        # Return the current mocked allowance.
        return self.contract.allowance_raw


class _ApprovalFunction:
    def __init__(self, contract: "_ApprovalContract", amount_raw: int) -> None:
        # Capture the exact approval amount for simulation, building, and assertions.
        self.contract = contract
        self.amount_raw = amount_raw

    def call(self, context: dict[str, object]) -> bool:
        # Record the required eth_call simulation.
        assert context["from"] == WALLET
        self.contract.simulated.append(self.amount_raw)
        return True

    def estimate_gas(self, context: dict[str, object]) -> int:
        # Record the required gas estimate and return a deterministic safe value.
        assert context["from"] == WALLET
        self.contract.estimated.append(self.amount_raw)
        return self.contract.estimated_gas

    def build_transaction(self, values: dict[str, object]) -> dict[str, object]:
        # Return a transparent mock transaction for the local signer and fake broadcaster.
        return {**values, "approval_amount_raw": self.amount_raw}


class _Functions:
    def __init__(self, contract: "_ApprovalContract") -> None:
        # Bind the web3-style functions facade to mutable allowance state.
        self.contract = contract

    def approve(self, spender: str, amount_raw: int) -> _ApprovalFunction:
        # Record every requested approval target and amount.
        assert spender == ROUTER
        self.contract.requested.append(amount_raw)
        return _ApprovalFunction(self.contract, amount_raw)

    def allowance(self, owner: str, spender: str) -> _AllowanceCall:
        # Mock canonical allowance(owner, spender) reads.
        assert owner == WALLET
        assert spender == ROUTER
        return _AllowanceCall(self.contract)


class _ApprovalContract:
    def __init__(self, allowance_raw: int) -> None:
        # Hold all mocked ERC-20 approval state and instrumentation.
        self.allowance_raw = allowance_raw
        self.estimated_gas = 50_000
        self.requested: list[int] = []
        self.simulated: list[int] = []
        self.estimated: list[int] = []
        self.functions = _Functions(self)


class _Signer:
    def __init__(self) -> None:
        # Collect locally signed transaction dictionaries without real key material.
        self.transactions: list[dict[str, object]] = []

    def sign_transaction(self, transaction: dict[str, object]) -> SimpleNamespace:
        # Return the transaction as opaque fake raw bytes for the mock broadcaster.
        self.transactions.append(transaction)
        return SimpleNamespace(raw_transaction=transaction)


class _Eth:
    def __init__(self, contract: _ApprovalContract) -> None:
        # Model nonce, fee, broadcast, and receipt calls entirely in memory.
        self.contract = contract
        self.gas_price = 30_000_000_000
        self.nonce_tags: list[str] = []
        self.broadcasts = 0
        self.receipt_status = 1
        self.timeout = False

    def get_transaction_count(self, address: str, tag: str) -> int:
        # Require the pending nonce and issue one increasing nonce per transaction.
        assert address == WALLET
        self.nonce_tags.append(tag)
        return len(self.nonce_tags) - 1

    def send_raw_transaction(self, transaction: dict[str, object]) -> bytes:
        # Apply the mocked allowance at broadcast and return one deterministic public hash.
        self.broadcasts += 1
        self.contract.allowance_raw = int(transaction["approval_amount_raw"])
        return self.broadcasts.to_bytes(32, "big")

    def wait_for_transaction_receipt(self, _transaction_hash: bytes, timeout: int) -> dict[str, int]:
        # Return a configured receipt or emulate an unresolved post-broadcast timeout.
        assert timeout == 120
        if self.timeout:
            raise TimeoutError("mock timeout")
        return {"status": self.receipt_status}


class _Web3:
    def __init__(self, contract: _ApprovalContract) -> None:
        # Expose the minimal eth namespace used by the approval implementation.
        self.eth = _Eth(contract)


def _checked(initial_allowance: int) -> tuple[ContractCheckResult, _ApprovalContract, _Signer]:
    # Construct a validated-check result so tests isolate the signing boundary from RPC checks.
    contract = _ApprovalContract(initial_allowance)
    signer = _Signer()
    web3 = _Web3(contract)
    checked = ContractCheckResult(
        environment="dev",
        chain_id=137,
        wallet_address=WALLET,
        router_address=ROUTER,
        pol_balance_raw=10**18,
        usdt_balance_raw=CANARY_ALLOWANCE_USDT_RAW,
        allowance_raw=initial_allowance,
        web3=web3,
        account=signer,
        usdt_contract=contract,
    )
    return checked, contract, signer


@pytest.mark.parametrize(
    ("initial", "expected_amounts", "expected_action"),
    [
        (0, [CANARY_ALLOWANCE_USDT_RAW], "approved"),
        (3_000_000, [0, CANARY_ALLOWANCE_USDT_RAW], "approved"),
        (CANARY_ALLOWANCE_USDT_RAW, [], "already-approved"),
    ],
)
def test_approve_is_capped_and_zero_resets_when_needed(monkeypatch, initial, expected_amounts, expected_action) -> None:
    # Cover zero-to-cap, nonzero zero-reset, and exact-cap no-op paths.
    checked, contract, signer = _checked(initial)
    monkeypatch.setattr("src.trading.approval.run_contract_checks", lambda *_args, **_kwargs: checked)

    result = approve_trading(AppConfig())

    assert result.action == expected_action
    assert result.current_allowance_raw == CANARY_ALLOWANCE_USDT_RAW
    assert contract.requested == expected_amounts
    assert contract.simulated == expected_amounts
    assert contract.estimated == expected_amounts
    assert all(amount <= CANARY_ALLOWANCE_USDT_RAW for amount in contract.requested)
    assert checked.web3.eth.nonce_tags == ["pending"] * len(expected_amounts)
    assert len(signer.transactions) == len(expected_amounts)


@pytest.mark.parametrize(
    ("initial", "expected_amounts", "expected_action"),
    [(3_000_000, [0], "revoked"), (0, [], "already-revoked")],
)
def test_revoke_resets_nonzero_and_noops_at_zero(monkeypatch, initial, expected_amounts, expected_action) -> None:
    # Cover explicit revocation and the safe zero-allowance no-op.
    checked, contract, _signer = _checked(initial)
    monkeypatch.setattr("src.trading.approval.run_contract_checks", lambda *_args, **_kwargs: checked)

    result = revoke_trading(AppConfig())

    assert result.action == expected_action
    assert result.current_allowance_raw == 0
    assert contract.requested == expected_amounts


def test_approval_rejects_reverted_receipt(monkeypatch) -> None:
    # Refuse to report success when the mined transaction receipt has status zero.
    checked, _contract, _signer = _checked(0)
    checked.web3.eth.receipt_status = 0
    monkeypatch.setattr("src.trading.approval.run_contract_checks", lambda *_args, **_kwargs: checked)

    with pytest.raises(ApprovalError, match="reverted"):
        approve_trading(AppConfig())


def test_approval_timeout_reports_hash_and_never_retries(monkeypatch) -> None:
    # Preserve one unresolved transaction identity instead of consuming another nonce.
    checked, _contract, _signer = _checked(0)
    checked.web3.eth.timeout = True
    monkeypatch.setattr("src.trading.approval.run_contract_checks", lambda *_args, **_kwargs: checked)

    with pytest.raises(ApprovalBroadcastTimeout, match="transaction hash: 0x"):
        approve_trading(AppConfig())

    assert checked.web3.eth.broadcasts == 1
    assert checked.web3.eth.nonce_tags == ["pending"]


def test_approval_rejects_gas_estimate_above_configured_limit(monkeypatch) -> None:
    # Stop before signing when the simulated approval requires excessive gas.
    checked, contract, signer = _checked(0)
    contract.estimated_gas = 100_001
    monkeypatch.setattr("src.trading.approval.run_contract_checks", lambda *_args, **_kwargs: checked)

    with pytest.raises(ApprovalError, match="gas estimate"):
        approve_trading(AppConfig())

    assert signer.transactions == []
    assert checked.web3.eth.broadcasts == 0
