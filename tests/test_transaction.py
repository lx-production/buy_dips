from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from web3 import Web3

from src.trading.constants import POLYGON_PRANA_ADDRESS
from src.trading.contract_checks import ContractCheckResult
from src.trading.models import SwapTransaction, ValidatedSwapQuote
from src.trading.transaction import broadcast_signed_swap, prepare_signed_swap, reconcile_swap, simulate_swap, wait_for_swap_receipt


NOW = 1_730_000_000
WALLET = "0x0000000000000000000000000000000000000001"
ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"


class _Signer:
    def __init__(self) -> None:
        """Capture the exact transaction passed to the local signer."""
        self.transactions: list[dict[str, object]] = []

    def sign_transaction(self, transaction: dict[str, object]) -> SimpleNamespace:
        """Return deterministic raw bytes and their locally derived hash."""
        self.transactions.append(transaction)
        raw = b"signed-swap"
        return SimpleNamespace(raw_transaction=raw, hash=Web3.keccak(raw))


class _Eth:
    def __init__(self) -> None:
        """Model the web3 transaction surface entirely in memory."""
        self.gas_price = 30_000_000_000
        self.calls: list[dict[str, object]] = []
        self.estimates: list[dict[str, object]] = []
        self.sent: list[bytes] = []
        self.receipt: dict[str, object] | None = None

    def call(self, transaction: dict[str, object]) -> bytes:
        """Record eth_call for the exact quote transaction."""
        self.calls.append(transaction)
        return b""

    def estimate_gas(self, transaction: dict[str, object]) -> int:
        """Record estimate_gas and return one safe deterministic estimate."""
        self.estimates.append(transaction)
        return 100_000

    def get_transaction_count(self, address: str, tag: str) -> int:
        """Reserve one pending nonce for the checked wallet."""
        assert address == WALLET
        assert tag == "pending"
        return 7

    def send_raw_transaction(self, raw: bytes) -> bytes:
        """Broadcast the exact signed bytes and return their matching hash."""
        self.sent.append(raw)
        return bytes(Web3.keccak(raw))

    def wait_for_transaction_receipt(self, transaction_hash: str, timeout: int) -> dict[str, object]:
        """Return the configured mined receipt."""
        assert transaction_hash.startswith("0x")
        assert timeout == 120
        assert self.receipt is not None
        return self.receipt

    def get_transaction_receipt(self, _transaction_hash: str) -> dict[str, object] | None:
        """Return a receipt when mined or None while pending."""
        return self.receipt


def _checked() -> ContractCheckResult:
    """Build a contract-check result with an in-memory signer and provider."""
    web3 = SimpleNamespace(eth=_Eth())
    return ContractCheckResult(
        environment="dev",
        chain_id=137,
        wallet_address=WALLET,
        router_address=ROUTER,
        pol_balance_raw=10**18,
        usdt_balance_raw=10_000_000,
        allowance_raw=10_000_000,
        web3=web3,
        account=_Signer(),
        usdt_contract=SimpleNamespace(),
    )


def _quote() -> ValidatedSwapQuote:
    """Build one already validated quote for transaction boundary tests."""
    return ValidatedSwapQuote(
        token_in_symbol="USDT",
        token_out_symbol="PRANA",
        amount_in=Decimal("1"),
        amount_in_raw=1_000_000,
        recipient=WALLET,
        slippage_bps=50,
        chain_id=137,
        amount_out=Decimal("12.5"),
        amount_out_raw=12_500_000_000,
        minimum_amount_out=Decimal("12.4"),
        router_address=ROUTER,
        transaction=SwapTransaction(to=ROUTER, data="0x1234", value=0),
        deadline=NOW + 180,
        verification_version=2,
        verification_expires_at=NOW + 180,
    )


def test_simulate_sign_and_broadcast_use_exact_quote_transaction() -> None:
    """Simulation and signing must not rebuild or alter quote calldata and target."""
    checked = _checked()
    quote = _quote()

    simulation = simulate_swap(checked, quote, max_swap_gas=200_000)
    signed = prepare_signed_swap(
        checked,
        quote,
        simulation,
        minimum_deadline_seconds=30,
        now_s=NOW,
    )
    transaction_hash = broadcast_signed_swap(checked, signed)

    expected_context = {
        "from": WALLET,
        "to": ROUTER,
        "data": "0x1234",
        "value": 0,
    }
    assert checked.web3.eth.calls == [expected_context]
    assert checked.web3.eth.estimates == [expected_context]
    assert checked.account.transactions[0]["nonce"] == 7
    assert checked.account.transactions[0]["gas"] == 120_000
    assert transaction_hash == signed.transaction_hash
    assert checked.web3.eth.sent == [b"signed-swap"]


def test_receipt_reconciliation_decodes_prana_received_by_wallet() -> None:
    """A successful receipt should expose only public receipt fields and actual PRANA output."""
    checked = _checked()
    transfer_topic = Web3.keccak(text="Transfer(address,address,uint256)")
    recipient_topic = bytes.fromhex("00" * 12 + WALLET[2:])
    checked.web3.eth.receipt = {
        "status": 1,
        "blockNumber": 123,
        "gasUsed": 90_000,
        "logs": [
            {
                "address": POLYGON_PRANA_ADDRESS,
                "topics": [transfer_topic, bytes(32), recipient_topic],
                "data": f"0x{12_500_000_000:064x}",
            }
        ],
    }

    waited = wait_for_swap_receipt(
        checked,
        "0x" + "ab" * 32,
        recipient=WALLET,
        timeout_seconds=120,
    )
    reconciled = reconcile_swap(
        checked.web3,
        waited.transaction_hash,
        recipient=WALLET,
    )

    assert waited.status == "confirmed"
    assert waited.actual_prana_output_raw == 12_500_000_000
    assert reconciled == waited


def test_reconcile_returns_pending_without_broadcasting_replacement() -> None:
    """A missing receipt remains pending and never calls send_raw_transaction."""
    checked = _checked()

    receipt = reconcile_swap(
        checked.web3,
        "0x" + "cd" * 32,
        recipient=WALLET,
    )

    assert receipt.status == "pending"
    assert checked.web3.eth.sent == []
