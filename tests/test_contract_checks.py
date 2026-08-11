from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.config import AppConfig
from src.trading.constants import POLYGON_PRANA_ADDRESS, POLYGON_USDT_ADDRESS
from src.trading.contract_checks import ContractCheckError, run_contract_checks
from src.trading.wallet import create_encrypted_keystore


@dataclass
class _Call:
    value: object

    def call(self) -> object:
        # Return one deterministic mocked contract result without network access.
        return self.value


class _TokenFunctions:
    def __init__(self, symbol: str, decimals: int, balance: int = 0, allowance: int = 0) -> None:
        # Store mocked ERC-20 metadata and wallet state for strict contract checks.
        self._symbol = symbol
        self._decimals = decimals
        self._balance = balance
        self._allowance = allowance

    def symbol(self) -> _Call:
        # Mock symbol().
        return _Call(self._symbol)

    def decimals(self) -> _Call:
        # Mock decimals().
        return _Call(self._decimals)

    def balanceOf(self, _address: str) -> _Call:
        # Mock balanceOf(address).
        return _Call(self._balance)

    def allowance(self, _owner: str, _spender: str) -> _Call:
        # Mock allowance(owner, spender).
        return _Call(self._allowance)


class _Contract:
    def __init__(self, functions: _TokenFunctions) -> None:
        # Match the small web3 contract surface used by the checker.
        self.functions = functions


class _Eth:
    def __init__(self) -> None:
        # Default to valid Polygon contracts and metadata, all fully in memory.
        self.chain_id = 137
        self.empty_code_address: str | None = None
        self.usdt = _Contract(_TokenFunctions("USDT0", 6, balance=2_000_000, allowance=3_000_000))
        self.prana = _Contract(_TokenFunctions("PRANA", 9))

    def get_code(self, address: str) -> bytes:
        # Return empty bytecode only for the address selected by a test.
        return b"" if self.empty_code_address == address.lower() else b"\x60\x00"

    def contract(self, address: str, abi: object) -> _Contract:
        # Route immutable token addresses to their mocked ERC-20 contracts.
        del abi
        if address.lower() == POLYGON_USDT_ADDRESS.lower():
            return self.usdt
        if address.lower() == POLYGON_PRANA_ADDRESS.lower():
            return self.prana
        raise AssertionError(f"Unexpected token contract: {address}")

    def get_balance(self, _address: str) -> int:
        # Mock the native POL wallet balance.
        return 10**18


class _Web3:
    def __init__(self) -> None:
        # Expose a minimal connected Web3 facade with mocked eth calls.
        self.eth = _Eth()

    def is_connected(self) -> bool:
        # Report a successful provider connection without opening a socket.
        return True


def _config_and_wallet(tmp_path) -> tuple[AppConfig, str]:
    # Create a temporary encrypted signer and pin its public address in config.
    path = tmp_path / "wallet" / "trader-dev.json"
    password = "test-password"
    address = create_encrypted_keystore(path, password)
    config = AppConfig(wallet={"keystore_path": str(path), "expected_address": address})
    return config, password


def test_contract_checks_validate_chain_contracts_tokens_and_balances(tmp_path) -> None:
    # Cover the complete success path while every RPC and contract response is mocked.
    config, password = _config_and_wallet(tmp_path)

    checked = run_contract_checks(config, password=password, web3=_Web3())

    assert checked.chain_id == 137
    assert checked.usdt_balance_raw == 2_000_000
    assert checked.pol_balance_raw == 10**18
    assert checked.allowance_raw == 3_000_000


def test_contract_checks_reject_wrong_chain(tmp_path) -> None:
    # Abort before signer use when an RPC endpoint reports a non-Polygon chain.
    config, password = _config_and_wallet(tmp_path)
    web3 = _Web3()
    web3.eth.chain_id = 1

    with pytest.raises(ContractCheckError, match="chain ID"):
        run_contract_checks(config, password=password, web3=web3)


@pytest.mark.parametrize("target", [POLYGON_USDT_ADDRESS, POLYGON_PRANA_ADDRESS, "router"])
def test_contract_checks_reject_empty_contract_bytecode(tmp_path, target: str) -> None:
    # Require deployed code at both token addresses and the configured router.
    config, password = _config_and_wallet(tmp_path)
    web3 = _Web3()
    address = config.execution.router_allowlist[0] if target == "router" else target
    web3.eth.empty_code_address = address.lower()

    with pytest.raises(ContractCheckError, match="empty bytecode"):
        run_contract_checks(config, password=password, web3=web3)


@pytest.mark.parametrize(
    ("token", "field", "value", "message"),
    [
        ("usdt", "_symbol", "FAKE", "USDT symbol"),
        ("usdt", "_decimals", 18, "USDT decimals"),
        ("prana", "_symbol", "FAKE", "PRANA symbol"),
        ("prana", "_decimals", 18, "PRANA decimals"),
    ],
)
def test_contract_checks_reject_wrong_token_metadata(tmp_path, token: str, field: str, value: object, message: str) -> None:
    # Fail closed when either immutable token deployment reports unexpected metadata.
    config, password = _config_and_wallet(tmp_path)
    web3 = _Web3()
    setattr(getattr(web3.eth, token).functions, field, value)

    with pytest.raises(ContractCheckError, match=message):
        run_contract_checks(config, password=password, web3=web3)


def test_contract_checks_redact_provider_exception_details(tmp_path) -> None:
    # Prevent a provider exception from echoing an RPC URL that embeds a secret API key.
    config, password = _config_and_wallet(tmp_path)
    web3 = _Web3()

    def explode() -> bool:
        # Simulate a provider library leaking its endpoint in a low-level exception.
        raise RuntimeError("https://rpc.example/super-secret-api-key")

    web3.is_connected = explode
    with pytest.raises(ContractCheckError) as error:
        run_contract_checks(config, password=password, web3=web3)

    assert "super-secret-api-key" not in str(error.value)
