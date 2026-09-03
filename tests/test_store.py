from __future__ import annotations

import sqlite3

from src.db import connect, init_db
from src.trading.store import create_trade_execution, get_trade_execution, has_recent_zone_buy, has_setup_buy, update_trade_execution


HOUR = 3_600_000


def _insert_buy(
    conn: sqlite3.Connection,
    open_time: int,
    fingerprint: str,
    dip_origin: int,
) -> None:
    """Insert one BUY row so cooldown and setup lookups can be checked directly."""
    conn.execute(
        """
        INSERT INTO decisions(
          created_at, exchange, symbol, timeframe, candle_open_time, candle_close_time,
          reference_close, zone_set_as_of, fingerprint_version, selected_zone_fingerprint,
          dip_origin_open_time, gate_results_json, zones_rebuilt, decision, reason_code,
          mode, strategy_version, config_version
        ) VALUES (
          1, 'binance', 'BTCUSDT', '1h', ?, ?,
          1, 1000, 'zf1', ?,
          ?, '{}', 0, 'BUY', 'BUY_GATES_PASSED',
          'observe', 'strategy-v1', 'config-v1'
        )
        """,
        (open_time, open_time, fingerprint, dip_origin),
    )


def test_has_recent_zone_buy_is_per_zone_and_excludes_exact_24h(tmp_path) -> None:
    # Window is (trigger - 24h, trigger): the BUY 24h earlier and other zones do not block.
    db_path = tmp_path / "bot.sqlite"
    init_db(db_path)
    trigger = 100 * HOUR
    with connect(db_path) as conn:
        _insert_buy(conn, trigger - 2 * HOUR, "zf1:selected", trigger - 10 * HOUR)
        _insert_buy(conn, trigger - 24 * HOUR, "zf1:selected", trigger - 30 * HOUR)
        _insert_buy(conn, trigger - HOUR, "zf1:other", trigger - 5 * HOUR)
        conn.commit()

        assert has_recent_zone_buy(conn, "zf1:selected", trigger) is True
        assert has_recent_zone_buy(conn, "zf1:other", trigger) is True
        assert has_recent_zone_buy(conn, "zf1:selected", trigger + 22 * HOUR) is False
        assert has_recent_zone_buy(conn, "zf1:missing", trigger) is False


def test_has_setup_buy_ignores_time_and_other_origins(tmp_path) -> None:
    # Same fingerprint + dip origin stays blocked after 24h; a newer origin does not match.
    db_path = tmp_path / "bot.sqlite"
    init_db(db_path)
    origin = 70 * HOUR
    trigger = 100 * HOUR
    with connect(db_path) as conn:
        _insert_buy(conn, trigger - 30 * HOUR, "zf1:selected", origin)
        conn.commit()

        assert has_setup_buy(conn, "zf1:selected", origin, trigger) is True
        assert has_setup_buy(conn, "zf1:selected", origin + HOUR, trigger) is False
        assert has_setup_buy(conn, "zf1:other", origin, trigger) is False


def test_trade_execution_is_idempotent_and_stores_only_redacted_fields(tmp_path) -> None:
    """One decision gets one execution row whose safe lifecycle fields can be updated."""
    db_path = tmp_path / "bot.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        _insert_buy(conn, 100 * HOUR, "zf1:selected", 90 * HOUR)
        decision_id = int(conn.execute("SELECT id FROM decisions").fetchone()["id"])
        execution_id, created = create_trade_execution(conn, decision_id, "live", created_at=1)
        duplicate_id, duplicate_created = create_trade_execution(conn, decision_id, "live", created_at=2)
        update_trade_execution(
            conn,
            execution_id,
            updated_at=3,
            status="signed",
            nonce=7,
            transaction_hash="0x" + "ab" * 32,
            approval_transaction_hashes_json=["0xapproval"],
        )
        conn.commit()
        execution = get_trade_execution(conn, execution_id)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(trade_executions)")}

    assert created is True
    assert duplicate_created is False
    assert duplicate_id == execution_id
    assert execution["status"] == "signed"
    assert execution["nonce"] == 7
    assert "raw_calldata" not in columns
    assert "signed_transaction" not in columns
    assert "verification_token" not in columns
