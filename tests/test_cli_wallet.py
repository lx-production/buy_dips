from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from src.cli import main
from src.config import AppConfig
from src.db import upsert_candles
from src.trading.contract_checks import ContractCheckResult
from src.trading.runner import run_trade_once


HOUR = 3_600_000
FOUR_HOURS = 4 * HOUR


def _write_config(path, wallet_path, expected_address: str | None = None, database_path=None) -> None:
    # Write only non-secret test configuration to a temporary YAML file.
    database_line = f"database_path: {database_path}\n" if database_path is not None else ""
    expected = "null" if expected_address is None else f'"{expected_address}"'
    path.write_text(
        f"{database_line}environment: dev\nwallet:\n  keystore_path: {wallet_path}\n  expected_address: {expected}\n",
        encoding="utf-8",
    )


def test_wallet_create_and_status_print_only_public_address(monkeypatch, capsys, tmp_path) -> None:
    # Exercise both CLI paths while ensuring the environment password never reaches output.
    config_path = tmp_path / "config.yaml"
    wallet_path = tmp_path / "wallet" / "trader-dev.json"
    _write_config(config_path, wallet_path)
    monkeypatch.setenv("KEYSTORE_PASSWORD", "cli-super-secret")

    assert main(["--config", str(config_path), "wallet-create"]) == 0
    created = capsys.readouterr()
    assert created.out.startswith("Wallet address: 0x")
    assert created.err == ""
    address = created.out.strip().split(": ", 1)[1]
    _write_config(config_path, wallet_path, expected_address=address)

    assert main(["--config", str(config_path), "wallet-status"]) == 0
    status = capsys.readouterr()
    assert status.out == f"Wallet address: {address}\n"
    assert status.err == ""
    assert "cli-super-secret" not in created.out + created.err + status.out + status.err


def test_wallet_status_wrong_password_redacts_secret(monkeypatch, capsys, tmp_path) -> None:
    # Return a generic decrypt failure without echoing the wrong password fixture.
    config_path = tmp_path / "config.yaml"
    wallet_path = tmp_path / "wallet" / "trader-dev.json"
    _write_config(config_path, wallet_path)
    monkeypatch.setenv("KEYSTORE_PASSWORD", "correct-password")
    assert main(["--config", str(config_path), "wallet-create"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("KEYSTORE_PASSWORD", "wrong-secret-password")

    assert main(["--config", str(config_path), "wallet-status"]) == 2
    output = capsys.readouterr()
    assert "wrong-secret-password" not in output.out + output.err
    assert "decryption failed" in output.err


def test_trade_check_prints_allowlisted_summary_without_rpc_url(monkeypatch, capsys, tmp_path) -> None:
    # Limit successful trade-check output to public chain, wallet, balance, router, and live state.
    config_path = tmp_path / "config.yaml"
    wallet_path = tmp_path / "wallet" / "trader-dev.json"
    _write_config(config_path, wallet_path)
    checked = ContractCheckResult(
        environment="dev",
        chain_id=137,
        wallet_address="0x0000000000000000000000000000000000000001",
        router_address="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        pol_balance_raw=10**18,
        usdt_balance_raw=1_000_000,
        allowance_raw=0,
        web3=SimpleNamespace(),
        account=SimpleNamespace(),
        usdt_contract=SimpleNamespace(),
    )
    monkeypatch.setattr("src.cli.run_contract_checks", lambda _config: checked)
    monkeypatch.setenv("POLYGON_RPC_URL", "https://rpc.example/secret-api-key")

    assert main(["--config", str(config_path), "trade-check"]) == 0
    output = capsys.readouterr()
    assert "USDT balance: 1" in output.out
    assert "POL balance: 1" in output.out
    assert "Router: verified" in output.out
    assert "secret-api-key" not in output.out + output.err


def _four_hour_history(count: int) -> pd.DataFrame:
    # Build enough closed 4h history for the existing deterministic zone refresh path.
    return pd.DataFrame(
        [
            {
                "open_time": index * FOUR_HOURS,
                "close_time": (index + 1) * FOUR_HOURS - 1,
                "open": 100 + index,
                "high": 110 + index,
                "low": 90 + index,
                "close": 105 + index,
                "volume": 1,
                "is_closed": 1,
            }
            for index in range(count)
        ]
    )


def _hourly(bucket: int) -> pd.DataFrame:
    # Build exactly four closed 1h candles for the latest overdue aggregation bucket.
    return pd.DataFrame(
        [
            {
                "open_time": bucket + index * HOUR,
                "close_time": bucket + (index + 1) * HOUR - 1,
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "volume": 10,
                "is_closed": 1,
            }
            for index in range(4)
        ]
    )


def test_observe_runner_never_loads_wallet_or_calls_rpc(monkeypatch, tmp_path) -> None:
    # Guard the data-only observe cycle against future accidental wallet or Polygon dependencies.
    database_path = tmp_path / "bot.sqlite"
    upsert_candles(database_path, _four_hour_history(11).to_dict("records"), "binance", "BTCUSDT", "4h")
    upsert_candles(database_path, _hourly(11 * FOUR_HOURS).to_dict("records"), "binance", "BTCUSDT", "1h")

    def forbidden(*_args, **_kwargs):
        # Fail immediately if observe crosses either prohibited external boundary.
        raise AssertionError("observe touched wallet or Polygon RPC")

    monkeypatch.setattr("src.trading.wallet.load_local_account", forbidden)
    monkeypatch.setattr("src.trading.contract_checks.create_web3", forbidden)

    result = run_trade_once(AppConfig(database_path=str(database_path)), database_path, now_ms=12 * FOUR_HOURS, fetch=False)

    assert result.decision_id > 0
