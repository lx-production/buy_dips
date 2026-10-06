from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from src.config import AppConfig
from src.db import connect, init_db
from src.trading.constants import TRADE_AMOUNT_USDT_RAW
from src.trading.store import create_trade_execution, update_trade_execution
from src.trading.risk import RiskCheckError, check_gas_reserve, check_pre_execution_risk, check_wallet_funds


NOW = 1_800_000_000


def _insert_buy(conn: sqlite3.Connection, candle_open_time: int) -> int:
    """Insert one minimal BUY decision that may own an execution lifecycle."""
    cursor = conn.execute(
        """
        INSERT INTO decisions(
          created_at, exchange, symbol, timeframe, candle_open_time, candle_close_time,
          reference_close, zone_set_as_of, fingerprint_version, selected_zone_fingerprint,
          gate_results_json, zones_rebuilt, decision, reason_code, mode,
          strategy_version, config_version
        ) VALUES (?, 'binance', 'BTCUSDT', '1h', ?, ?, 1, 1000, 'zf1', ?,
                  '{}', 0, 'BUY', 'BUY_GATES_PASSED', 'live', 'support_close_v2', '1')
        """,
        (NOW, candle_open_time, candle_open_time + 1, f"zf1:{candle_open_time}"),
    )
    return int(cursor.lastrowid)


def _create_execution(conn: sqlite3.Connection, candle_open_time: int, *, mode: str = "live") -> int:
    """Create one execution row through the same idempotent store helper as the runner."""
    decision_id = _insert_buy(conn, candle_open_time)
    execution_id, created = create_trade_execution(conn, decision_id, mode, created_at=NOW)
    assert created is True
    return execution_id


def test_pause_file_preserves_buy_and_skips_execution(tmp_path) -> None:
    """An operator pause file must stop downstream work without changing the BUY decision."""
    database_path = tmp_path / "bot.sqlite"
    pause_file = tmp_path / "PAUSE_TRADING"
    pause_file.touch()
    init_db(database_path)
    config = AppConfig(risk={"pause_file": str(pause_file)})

    with connect(database_path) as conn:
        execution_id = _create_execution(conn, 1)
        with pytest.raises(RiskCheckError, match="PAUSE_FILE_PRESENT"):
            check_pre_execution_risk(conn, config, execution_id, now_s=NOW)


def test_in_flight_execution_blocks_a_second_execution(tmp_path) -> None:
    """Any unresolved lifecycle must prevent another decision from entering execution."""
    database_path = tmp_path / "bot.sqlite"
    init_db(database_path)
    config = AppConfig(risk={"pause_file": str(tmp_path / "missing-pause")})

    with connect(database_path) as conn:
        first_id = _create_execution(conn, 1)
        update_trade_execution(conn, first_id, status="pending", transaction_hash="0x" + "ab" * 32)
        second_id = _create_execution(conn, 2)
        with pytest.raises(RiskCheckError, match="UNRESOLVED_EXECUTION"):
            check_pre_execution_risk(conn, config, second_id, now_s=NOW)


def test_daily_live_limit_counts_signed_or_later_attempts(tmp_path) -> None:
    """One conservative live attempt in a UTC day must block the next."""
    database_path = tmp_path / "bot.sqlite"
    init_db(database_path)
    config = AppConfig(risk={"pause_file": str(tmp_path / "missing-pause")})

    with connect(database_path) as conn:
        execution_id = _create_execution(conn, 1)
        update_trade_execution(
            conn,
            execution_id,
            updated_at=NOW,
            status="confirmed",
            amount_in_raw=TRADE_AMOUNT_USDT_RAW,
        )
        current_id = _create_execution(conn, 10)
        with pytest.raises(RiskCheckError, match="DAILY_TRADE_LIMIT_REACHED"):
            check_pre_execution_risk(conn, config, current_id, now_s=NOW)


def test_prior_day_spend_does_not_block_the_next_trade(tmp_path) -> None:
    """Lifetime USDT spend is recorded but must not stop a later UTC day's BUY."""
    database_path = tmp_path / "bot.sqlite"
    init_db(database_path)
    config = AppConfig(risk={"pause_file": str(tmp_path / "missing-pause")})

    with connect(database_path) as conn:
        prior_id = _create_execution(conn, 1)
        update_trade_execution(conn, prior_id, status="confirmed", amount_in_raw=100_000_000)
        conn.execute("UPDATE trade_executions SET created_at=? WHERE id=?", (NOW - 86_400, prior_id))
        current_id = _create_execution(conn, 2)
        exposure = check_pre_execution_risk(conn, config, current_id, now_s=NOW)

    assert exposure.utc_day_trade_count == 0
    assert exposure.cumulative_spend_raw == 100_000_000


def test_wallet_and_gas_reserve_checks_use_integer_units() -> None:
    """Wallet checks must retain configured POL after a conservative gas budget."""
    config = AppConfig(risk={"min_pol_reserve": Decimal("0.01")})

    check_wallet_funds(config, usdt_balance_raw=TRADE_AMOUNT_USDT_RAW, pol_balance_raw=20_000_000_000_000_000)
    check_gas_reserve(
        config,
        pol_balance_raw=20_000_000_000_000_000,
        gas_price_wei=10_000_000_000,
        gas_limit=500_000,
        include_approval=False,
    )

    with pytest.raises(RiskCheckError, match="INSUFFICIENT_USDT_BALANCE"):
        check_wallet_funds(config, usdt_balance_raw=TRADE_AMOUNT_USDT_RAW - 1, pol_balance_raw=20_000_000_000_000_000)
    with pytest.raises(RiskCheckError, match="INSUFFICIENT_POL_GAS_RESERVE"):
        check_gas_reserve(
            config,
            pol_balance_raw=10_000_000_000_000_000,
            gas_price_wei=10_000_000_000,
            gas_limit=500_000,
            include_approval=False,
        )
