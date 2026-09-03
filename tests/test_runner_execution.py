from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from src.config import AppConfig
from src.db import connect, init_db
from src.trading.approval import ApprovalResult
from src.trading.models import SignedSwap, SwapReceipt, SwapSimulation, SwapTransaction, ValidatedSwapQuote
from src.trading.runner import _run_execution


WALLET = "0x0000000000000000000000000000000000000001"
ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"


def _insert_decision(database_path) -> int:
    """Insert one BUY decision that is eligible for downstream execution."""
    init_db(database_path)
    with connect(database_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO decisions(
              created_at, exchange, symbol, timeframe, candle_open_time, candle_close_time,
              reference_close, zone_set_as_of, fingerprint_version, selected_zone_fingerprint,
              gate_results_json, zones_rebuilt, decision, reason_code, mode,
              strategy_version, config_version
            ) VALUES (
              1, 'binance', 'BTCUSDT', '1h', 1000, 1999,
              1, 1000, 'zf1', 'zf1:selected',
              '{}', 0, 'BUY', 'BUY_GATES_PASSED', 'live',
              'support_close_v2', '1'
            )
            """
        )
        conn.commit()
        return int(cursor.lastrowid)


def _quote() -> ValidatedSwapQuote:
    """Build one valid quote result without invoking HTTP."""
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
        deadline=2_000_000_000,
        verification_version=2,
        verification_expires_at=2_000_000_000,
    )


def _checked() -> SimpleNamespace:
    """Build the public checked-wallet fields used by execution orchestration."""
    return SimpleNamespace(
        wallet_address=WALLET,
        usdt_balance_raw=10_000_000,
        pol_balance_raw=10**18,
        allowance_raw=10_000_000,
        web3=SimpleNamespace(eth=SimpleNamespace(gas_price=1_000_000_000)),
    )


def test_live_execution_reserves_hash_before_broadcast_and_is_idempotent(monkeypatch, tmp_path) -> None:
    """Live mode must commit the local hash first and never broadcast again on rerun."""
    database_path = tmp_path / "bot.sqlite"
    decision_id = _insert_decision(database_path)
    broadcasts = 0

    monkeypatch.setattr("src.trading.runner.run_contract_checks", lambda *_args, **_kwargs: _checked())
    monkeypatch.setattr("src.trading.runner.assert_live_mode_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("src.trading.runner.fetch_swap_quote", lambda *_args, **_kwargs: _quote())
    monkeypatch.setattr(
        "src.trading.runner.ensure_swap_allowance",
        lambda *_args, **_kwargs: ApprovalResult("already-sufficient", 1_000_000, 1_000_000, ()),
    )
    monkeypatch.setattr(
        "src.trading.runner.simulate_swap",
        lambda *_args, **_kwargs: SwapSimulation(gas_estimate=100_000, gas_limit=120_000),
    )
    monkeypatch.setattr(
        "src.trading.runner.prepare_signed_swap",
        lambda *_args, **_kwargs: SignedSwap(7, "0x" + "ab" * 32, b"signed"),
    )

    def broadcast(_checked_result, signed: SignedSwap) -> str:
        """Assert SQLite already owns the nonce/hash before touching the RPC broadcaster."""
        nonlocal broadcasts
        broadcasts += 1
        with connect(database_path) as conn:
            row = conn.execute("SELECT status, nonce, transaction_hash FROM trade_executions").fetchone()
        assert row["status"] == "signed"
        assert row["nonce"] == 7
        assert row["transaction_hash"] == signed.transaction_hash
        return signed.transaction_hash

    monkeypatch.setattr("src.trading.runner.broadcast_signed_swap", broadcast)
    monkeypatch.setattr(
        "src.trading.runner.wait_for_swap_receipt",
        lambda *_args, **_kwargs: SwapReceipt(
            status="confirmed",
            transaction_hash="0x" + "ab" * 32,
            block_number=123,
            gas_used=90_000,
            actual_prana_output_raw=12_500_000_000,
        ),
    )
    config = AppConfig()

    execution_id, status = _run_execution(
        config,
        database_path,
        decision_id,
        mode="live",
        password=None,
        web3=None,
        quote_session=None,
        live_confirmation=None,
    )
    duplicate_id, duplicate_status = _run_execution(
        config,
        database_path,
        decision_id,
        mode="live",
        password=None,
        web3=None,
        quote_session=None,
        live_confirmation=None,
    )

    assert status == "confirmed"
    assert duplicate_status == "confirmed"
    assert duplicate_id == execution_id
    assert broadcasts == 1


def test_dry_run_stops_after_quote_and_simulation(monkeypatch, tmp_path) -> None:
    """Dry-run mode must persist simulation without approval, signing, or broadcast."""
    database_path = tmp_path / "bot.sqlite"
    decision_id = _insert_decision(database_path)
    monkeypatch.setattr("src.trading.runner.run_contract_checks", lambda *_args, **_kwargs: _checked())
    monkeypatch.setattr("src.trading.runner.fetch_swap_quote", lambda *_args, **_kwargs: _quote())
    monkeypatch.setattr(
        "src.trading.runner.simulate_swap",
        lambda *_args, **_kwargs: SwapSimulation(gas_estimate=100_000, gas_limit=120_000),
    )

    def forbidden(*_args, **_kwargs):
        """Fail if dry-run crosses a signing boundary."""
        raise AssertionError("dry_run attempted a signing operation")

    monkeypatch.setattr("src.trading.runner.ensure_swap_allowance", forbidden)
    monkeypatch.setattr("src.trading.runner.prepare_signed_swap", forbidden)
    monkeypatch.setattr("src.trading.runner.broadcast_signed_swap", forbidden)

    _execution_id, status = _run_execution(
        AppConfig(),
        database_path,
        decision_id,
        mode="dry_run",
        password=None,
        web3=None,
        quote_session=None,
        live_confirmation=None,
    )

    assert status == "simulated"
    with connect(database_path) as conn:
        row = conn.execute("SELECT status, transaction_hash FROM trade_executions").fetchone()
    assert row["status"] == "simulated"
    assert row["transaction_hash"] is None


def test_pause_file_persists_execution_skip_before_wallet_access(monkeypatch, tmp_path) -> None:
    """A paused BUY must persist a skip reason without loading a wallet or calling Polygon."""
    database_path = tmp_path / "bot.sqlite"
    decision_id = _insert_decision(database_path)
    pause_file = tmp_path / "PAUSE_TRADING"
    pause_file.touch()
    config = AppConfig(
        risk={"pause_file": str(pause_file)},
        logging={"file_path": str(tmp_path / "trading.jsonl")},
    )

    def forbidden(*_args, **_kwargs):
        """Fail if a pre-execution pause crosses the wallet/RPC boundary."""
        raise AssertionError("paused execution touched wallet or Polygon")

    monkeypatch.setattr("src.trading.runner.run_contract_checks", forbidden)

    execution_id, status = _run_execution(
        config,
        database_path,
        decision_id,
        mode="live",
        password=None,
        web3=None,
        quote_session=None,
        live_confirmation=None,
    )

    with connect(database_path) as conn:
        decision = conn.execute("SELECT decision, reason_code FROM decisions WHERE id=?", (decision_id,)).fetchone()
        execution = conn.execute("SELECT status, reason FROM trade_executions WHERE id=?", (execution_id,)).fetchone()
    assert decision["decision"] == "BUY"
    assert decision["reason_code"] == "BUY_GATES_PASSED"
    assert execution["status"] == "skipped"
    assert execution["reason"] == "PAUSE_FILE_PRESENT"
    assert status == "skipped"
