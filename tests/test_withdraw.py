from __future__ import annotations

import sqlite3

from pathlib import Path
from types import SimpleNamespace

import pytest

from web3 import Web3

from src.cli import main
from src.config import AppConfig
from src.trading.constants import POLYGON_PRANA_ADDRESS
from src.trading.contract_checks import ContractCheckResult
from src.trading.withdraw import WITHDRAW_PRANA_DESTINATION, WithdrawBroadcastTimeout, WithdrawError, withdraw_all_prana


WALLET = Web3.to_checksum_address("0x0000000000000000000000000000000000000001")
PRANA = Web3.to_checksum_address(POLYGON_PRANA_ADDRESS)
BALANCE_RAW = 123_000_000_000
TX_HASH = (1).to_bytes(32, "big")


class _Call:
    def __init__(self, fn) -> None:
        # Store the read so each web3-style call sees the contract's current state.
        self.fn = fn

    def call(self, _context: dict[str, object] | None = None):
        # Return the latest mocked token metadata or balance.
        return self.fn()


class _Transfer:
    def __init__(self, contract: "_PranaContract", amount_raw: int) -> None:
        # Remember the exact amount the withdrawal asked the token to transfer.
        self.contract = contract
        self.amount_raw = amount_raw

    def call(self, context: dict[str, object]) -> bool:
        # Record the required eth_call and fail only when the test requests a simulation error.
        assert context["from"] == WALLET
        self.contract.simulated.append(self.amount_raw)
        if self.contract.fail_simulation:
            raise RuntimeError("mock simulation failure")
        return self.contract.transfer_accepted

    def estimate_gas(self, context: dict[str, object]) -> int:
        # Record the gas estimate for the same full balance that was simulated.
        assert context["from"] == WALLET
        self.contract.estimated.append(self.amount_raw)
        return self.contract.estimated_gas

    def build_transaction(self, values: dict[str, object]) -> dict[str, object]:
        # Use the actual Web3.py builder so the test exercises contract binding and ABI encoding.
        self.contract.built.append(self.amount_raw)
        function = self.contract.bound_contract.functions.transfer(WITHDRAW_PRANA_DESTINATION, self.amount_raw)
        return function.build_transaction(values)


class _Functions:
    def __init__(self, contract: "_PranaContract") -> None:
        # Bind the token facade to the mutable balance.
        self.contract = contract

    def symbol(self) -> _Call:
        # Return the mocked on-chain symbol.
        return _Call(lambda: self.contract.symbol)

    def decimals(self) -> _Call:
        # Return the mocked on-chain decimals.
        return _Call(lambda: self.contract.decimals)

    def balanceOf(self, owner: str) -> _Call:
        # Read the wallet balance the withdrawal is about to move.
        assert owner == WALLET
        return _Call(lambda: self.contract.balance_raw)

    def transfer(self, recipient: str, amount_raw: int) -> _Transfer:
        # Accept only the pinned recipient and the full balance captured for this call.
        assert recipient == WITHDRAW_PRANA_DESTINATION
        self.contract.requested.append(amount_raw)
        return _Transfer(self.contract, amount_raw)


class _PranaContract:
    def __init__(self, balance_raw: int) -> None:
        # Hold the mocked PRANA state and every transfer attempt.
        self.balance_raw = balance_raw
        self.symbol = "PRANA"
        self.decimals = 9
        self.estimated_gas = 60_000
        self.fail_simulation = False
        self.transfer_accepted = True
        self.requested: list[int] = []
        self.simulated: list[int] = []
        self.estimated: list[int] = []
        self.built: list[int] = []
        self.functions = _Functions(self)


class _Signer:
    def __init__(self) -> None:
        # Collect locally signed transactions without using key material.
        self.transactions: list[dict[str, object]] = []

    def sign_transaction(self, transaction: dict[str, object]) -> SimpleNamespace:
        # Return the transaction as the fake raw payload the broadcaster expects.
        self.transactions.append(transaction)
        return SimpleNamespace(raw_transaction=transaction, hash=TX_HASH)


class _Eth:
    def __init__(self, contract: _PranaContract) -> None:
        # Model the token binding, nonce, fee, broadcast, and receipt in memory.
        self.token = contract
        self.gas_price = 30_000_000_000
        self.nonce_tags: list[str] = []
        self.pending_nonce = 0
        self.broadcasts = 0
        self.receipt_status = 1
        self.timeout = False
        self.fail_broadcast = False

    def contract(self, *, address: str, abi: list[dict[str, object]]) -> _PranaContract:
        # Return the mocked PRANA token only when the withdrawal targets the canonical contract.
        assert address == PRANA
        assert any(item.get("name") == "transfer" for item in abi)
        self.token.bound_contract = Web3().eth.contract(address=address, abi=abi)
        return self.token

    def get_transaction_count(self, address: str, tag: str) -> int:
        # Report whether another transaction advanced the pending nonce beyond the confirmed nonce.
        assert address == WALLET
        self.nonce_tags.append(tag)
        return self.pending_nonce if tag == "pending" else 0

    def send_raw_transaction(self, transaction: dict[str, object]) -> bytes:
        # Apply a successful full-balance debit only when the receipt will also succeed.
        self.broadcasts += 1
        assert transaction["nonce"] == 0
        assert transaction["value"] == 0
        assert transaction["chainId"] == 137
        assert transaction["to"] == PRANA
        function, args = self.token.bound_contract.decode_function_input(transaction["data"])
        assert function.fn_name == "transfer"
        assert args == {"to": WITHDRAW_PRANA_DESTINATION, "amount": BALANCE_RAW}
        if self.fail_broadcast:
            raise TimeoutError("mock broadcast response lost")
        if not self.timeout and self.receipt_status == 1:
            self.token.balance_raw = 0
        return TX_HASH

    def wait_for_transaction_receipt(self, _transaction_hash: bytes, timeout: int) -> dict[str, int]:
        # Return the configured receipt, or leave the transfer unresolved.
        assert timeout == 120
        if self.timeout:
            raise TimeoutError("mock timeout")
        return {"status": self.receipt_status}


class _Web3:
    def __init__(self, contract: _PranaContract) -> None:
        # Expose the eth namespace used by the withdrawal.
        self.eth = _Eth(contract)


def _checked(contract: _PranaContract, signer: _Signer, *, pol_balance_raw: int = 10**18) -> ContractCheckResult:
    # Build a contract-check result so these tests do not touch Polygon or a keystore.
    return ContractCheckResult(
        environment="prod",
        chain_id=137,
        wallet_address=WALLET,
        router_address="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        pol_balance_raw=pol_balance_raw,
        usdt_balance_raw=0,
        allowance_raw=0,
        web3=_Web3(contract),
        account=signer,
        usdt_contract=SimpleNamespace(),
    )


def _config(tmp_path: Path, *, pause: bool = True, keystore: str = "data/wallet/trader-prod.json") -> AppConfig:
    # Point the prod config at a temporary pause file and a database with no open buy.
    pause_path = tmp_path / "PAUSE_TRADING"
    if pause:
        pause_path.write_text("", encoding="utf-8")
    database_path = tmp_path / "prod.sqlite"
    _prepare_db(database_path, status=None)
    return AppConfig(
        environment="prod",
        database_path=str(database_path),
        wallet={"keystore_path": keystore, "expected_address": WALLET},
        risk={"pause_file": str(pause_path)},
    )


def _prepare_db(path: Path, status: str | None) -> None:
    # Create the smallest execution table the withdrawal lock needs to read.
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE trade_executions (id INTEGER PRIMARY KEY, status TEXT)")
    if status is not None:
        connection.execute("INSERT INTO trade_executions (status) VALUES (?)", (status,))
    connection.commit()
    connection.close()


def _confirmation() -> str:
    # Build the exact acknowledgment the preview prints and broadcast requires.
    return f"withdraw-all-prana:137:{WALLET}:{WITHDRAW_PRANA_DESTINATION}"


def test_withdrawal_rejects_non_prod_config() -> None:
    # A dev or canary config must fail before any RPC or signing work.
    with pytest.raises(WithdrawError, match="environment=prod"):
        withdraw_all_prana(AppConfig(), destination=WITHDRAW_PRANA_DESTINATION, broadcast=False)


def test_withdrawal_rejects_non_prod_keystore(tmp_path: Path) -> None:
    # Production mode still has to point at the trader-prod keystore filename.
    config = _config(tmp_path, keystore="data/wallet/trader-dev.json")

    with pytest.raises(WithdrawError, match="trader-prod"):
        withdraw_all_prana(config, destination=WITHDRAW_PRANA_DESTINATION, broadcast=False)


def test_withdrawal_rejects_any_other_destination(tmp_path: Path) -> None:
    # The command cannot be retargeted by passing a different --to address.
    config = _config(tmp_path)

    with pytest.raises(WithdrawError, match="pinned"):
        withdraw_all_prana(
            config,
            destination="0x0000000000000000000000000000000000000002",
            broadcast=True,
        )


def test_preview_reads_full_balance_without_signing(monkeypatch, tmp_path: Path) -> None:
    # Preview estimates gas and prints the confirmation, but it must not sign or broadcast.
    contract = _PranaContract(BALANCE_RAW)
    signer = _Signer()
    checked = _checked(contract, signer)
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)

    result = withdraw_all_prana(
        _config(tmp_path, pause=False),
        destination=WITHDRAW_PRANA_DESTINATION,
        broadcast=False,
    )

    assert result.action == "preview"
    assert result.balance_raw == BALANCE_RAW
    assert result.destination == WITHDRAW_PRANA_DESTINATION
    assert result.confirmation == _confirmation()
    assert result.transaction_hash is None
    assert contract.requested == [BALANCE_RAW]
    assert signer.transactions == []
    assert checked.web3.eth.broadcasts == 0


def test_broadcast_sends_entire_balance_once(monkeypatch, tmp_path: Path) -> None:
    # The signed transfer uses the whole balance, nonce pending, and leaves the wallet at zero.
    contract = _PranaContract(BALANCE_RAW)
    signer = _Signer()
    checked = _checked(contract, signer)
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)

    result = withdraw_all_prana(
        _config(tmp_path),
        destination=WITHDRAW_PRANA_DESTINATION.lower(),
        broadcast=True,
        confirmation=_confirmation(),
    )

    assert result.action == "withdrawn"
    assert result.transaction_hash == "0x" + TX_HASH.hex()
    assert contract.balance_raw == 0
    assert signer.transactions[0]["to"] == PRANA
    assert signer.transactions[0]["value"] == 0
    assert contract.built == [BALANCE_RAW]
    assert checked.web3.eth.nonce_tags == ["latest", "pending"]
    assert checked.web3.eth.broadcasts == 1


def test_pending_wallet_nonce_stops_before_signing(monkeypatch, tmp_path: Path) -> None:
    # An outstanding wallet transaction must block even when the execution table is clear.
    contract = _PranaContract(BALANCE_RAW)
    signer = _Signer()
    checked = _checked(contract, signer)
    checked.web3.eth.pending_nonce = 1
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)

    with pytest.raises(WithdrawError, match="transaction is pending"):
        withdraw_all_prana(
            _config(tmp_path), destination=WITHDRAW_PRANA_DESTINATION, broadcast=True, confirmation=_confirmation()
        )

    assert signer.transactions == []
    assert checked.web3.eth.broadcasts == 0


def test_false_transfer_simulation_stops_before_signing(monkeypatch, tmp_path: Path) -> None:
    # An ERC-20 call can reject a transfer by returning false without reverting.
    contract = _PranaContract(BALANCE_RAW)
    contract.transfer_accepted = False
    signer = _Signer()
    checked = _checked(contract, signer)
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)

    with pytest.raises(WithdrawError, match="simulation returned false"):
        withdraw_all_prana(
            _config(tmp_path), destination=WITHDRAW_PRANA_DESTINATION, broadcast=True, confirmation=_confirmation()
        )

    assert contract.estimated == []
    assert signer.transactions == []


def test_lost_broadcast_response_reports_signed_hash(monkeypatch, tmp_path: Path) -> None:
    # The operator can look up the original hash even when the RPC fails to return it.
    contract = _PranaContract(BALANCE_RAW)
    signer = _Signer()
    checked = _checked(contract, signer)
    checked.web3.eth.fail_broadcast = True
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)

    with pytest.raises(WithdrawError, match=f"broadcast outcome unknown; transaction hash: 0x{TX_HASH.hex()}"):
        withdraw_all_prana(
            _config(tmp_path), destination=WITHDRAW_PRANA_DESTINATION, broadcast=True, confirmation=_confirmation()
        )

    assert checked.web3.eth.broadcasts == 1


def test_broadcast_requires_pause_confirmation_and_clear_queue(monkeypatch, tmp_path: Path) -> None:
    # Each broadcast gate stops the transfer before a signature exists.
    contract = _PranaContract(BALANCE_RAW)
    signer = _Signer()
    checked = _checked(contract, signer)
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)
    config = _config(tmp_path, pause=False)

    with pytest.raises(WithdrawError, match="pause file"):
        withdraw_all_prana(config, destination=WITHDRAW_PRANA_DESTINATION, broadcast=True, confirmation=_confirmation())

    pause_path = Path(config.risk.pause_file)
    pause_path.write_text("", encoding="utf-8")
    with pytest.raises(WithdrawError, match="confirmation"):
        withdraw_all_prana(config, destination=WITHDRAW_PRANA_DESTINATION, broadcast=True, confirmation="wrong")

    connection = sqlite3.connect(config.database_path)
    connection.execute("INSERT INTO trade_executions (status) VALUES ('broadcast')")
    connection.commit()
    connection.close()
    with pytest.raises(WithdrawError, match="trade execution"):
        withdraw_all_prana(config, destination=WITHDRAW_PRANA_DESTINATION, broadcast=True, confirmation=_confirmation())

    assert signer.transactions == []
    assert checked.web3.eth.broadcasts == 0


def test_empty_balance_does_not_simulate_or_sign(monkeypatch, tmp_path: Path) -> None:
    # There is nothing to move, so the command stops after reading the balance.
    contract = _PranaContract(0)
    signer = _Signer()
    checked = _checked(contract, signer)
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)

    result = withdraw_all_prana(
        _config(tmp_path),
        destination=WITHDRAW_PRANA_DESTINATION,
        broadcast=True,
        confirmation=_confirmation(),
    )

    assert result.action == "empty"
    assert result.balance_raw == 0
    assert contract.requested == []
    assert signer.transactions == []


def test_timeout_reports_hash_without_a_second_broadcast(monkeypatch, tmp_path: Path) -> None:
    # Keep the unresolved hash visible and do not queue another transfer behind it.
    contract = _PranaContract(BALANCE_RAW)
    signer = _Signer()
    checked = _checked(contract, signer)
    checked.web3.eth.timeout = True
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)

    with pytest.raises(WithdrawBroadcastTimeout, match="transaction hash: 0x"):
        withdraw_all_prana(
            _config(tmp_path),
            destination=WITHDRAW_PRANA_DESTINATION,
            broadcast=True,
            confirmation=_confirmation(),
        )

    assert checked.web3.eth.broadcasts == 1
    assert contract.balance_raw == BALANCE_RAW


def test_reverted_receipt_is_not_reported_as_withdrawn(monkeypatch, tmp_path: Path) -> None:
    # A mined failure must surface the hash and leave the mocked balance untouched.
    contract = _PranaContract(BALANCE_RAW)
    signer = _Signer()
    checked = _checked(contract, signer)
    checked.web3.eth.receipt_status = 0
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)

    with pytest.raises(WithdrawError, match="reverted"):
        withdraw_all_prana(
            _config(tmp_path),
            destination=WITHDRAW_PRANA_DESTINATION,
            broadcast=True,
            confirmation=_confirmation(),
        )

    assert contract.balance_raw == BALANCE_RAW


def test_withdrawal_stops_when_pol_cannot_pay_gas(monkeypatch, tmp_path: Path) -> None:
    # A preview must stop before signing when the wallet cannot pay the transfer fee.
    contract = _PranaContract(BALANCE_RAW)
    signer = _Signer()
    checked = _checked(contract, signer, pol_balance_raw=1)
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)

    with pytest.raises(WithdrawError, match="POL balance"):
        withdraw_all_prana(_config(tmp_path), destination=WITHDRAW_PRANA_DESTINATION, broadcast=False)

    assert signer.transactions == []
    assert checked.web3.eth.broadcasts == 0


def test_withdrawal_stops_when_gas_estimate_exceeds_cap(monkeypatch, tmp_path: Path) -> None:
    # Refuse a transfer whose simulated gas is above the fixed cap, before any signature.
    contract = _PranaContract(BALANCE_RAW)
    contract.estimated_gas = 100_001
    signer = _Signer()
    checked = _checked(contract, signer)
    monkeypatch.setattr("src.trading.withdraw.run_contract_checks", lambda *_args, **_kwargs: checked)

    with pytest.raises(WithdrawError, match="gas estimate"):
        withdraw_all_prana(
            _config(tmp_path),
            destination=WITHDRAW_PRANA_DESTINATION,
            broadcast=True,
            confirmation=_confirmation(),
        )

    assert signer.transactions == []
    assert checked.web3.eth.broadcasts == 0


def test_cli_preview_prints_balance_and_confirmation(monkeypatch, capsys, tmp_path: Path) -> None:
    # The CLI preview path must pass the typed address through and must not request a broadcast.
    config_path = tmp_path / "config.prod.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: prod",
                "wallet:",
                "  keystore_path: data/wallet/trader-prod.json",
                f'  expected_address: "{WALLET}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_withdraw(_config, *, destination, broadcast, confirmation):
        # Capture the CLI arguments and return a preview result with no transaction hash.
        seen["destination"] = destination
        seen["broadcast"] = broadcast
        seen["confirmation"] = confirmation
        return SimpleNamespace(
            action="preview",
            wallet_address=WALLET,
            destination=WITHDRAW_PRANA_DESTINATION,
            balance_raw=BALANCE_RAW,
            pol_balance_raw=10**18,
            confirmation=_confirmation(),
            transaction_hash=None,
            gas_limit=72_000,
            gas_price_wei=30_000_000_000,
        )

    monkeypatch.setattr("src.cli.withdraw_all_prana", fake_withdraw)
    monkeypatch.setenv("WITHDRAW_PRANA_CONFIRMATION", "should-not-be-required-for-preview")

    assert main(["--config", str(config_path), "withdraw-prana", "--to", WITHDRAW_PRANA_DESTINATION]) == 0
    output = capsys.readouterr()
    assert seen == {
        "destination": WITHDRAW_PRANA_DESTINATION,
        "broadcast": False,
        "confirmation": "should-not-be-required-for-preview",
    }
    assert "Action: preview" in output.out
    assert "PRANA balance: 123" in output.out
    assert f"Confirmation: {_confirmation()}" in output.out
    assert "Broadcast: no" in output.out
    assert "should-not-be-required-for-preview" not in output.out
