from __future__ import annotations

import json

from datetime import datetime

from src.utils import UTC_PLUS_7
from src.config import LoggingConfig
from src.trading.audit_logging import REDACTED, configure_audit_logger, log_event


def test_structured_logger_redacts_secret_fields_from_stdout_and_file(capsys, tmp_path) -> None:
    """Passwords, keys, RPC URLs, signed bytes, and verification tokens must never render."""
    log_path = tmp_path / "trading.jsonl"
    logger = configure_audit_logger(LoggingConfig(file_path=str(log_path)))
    secrets = {
        "password": "wallet-password",
        "private_key": "private-key-value",
        "rpc_url": "https://rpc.example.test/?api_key=secret",
        "raw_transaction": b"signed-transaction-bytes",
        "verification": {"token": "opaque-verification-token"},
    }

    log_event(
        logger,
        "quote_validated",
        cycle_id="cycle-1",
        selected_zone_fingerprint="zf1:selected",
        token_in_symbol="USDT",
        secrets=secrets,
    )

    stdout = capsys.readouterr().out
    file_output = log_path.read_text(encoding="utf-8")
    for secret in (
        "wallet-password",
        "private-key-value",
        "rpc.example.test",
        "signed-transaction-bytes",
        "opaque-verification-token",
    ):
        assert secret not in stdout
        assert secret not in file_output
    payload = json.loads(file_output)
    assert payload["event"] == "quote_validated"
    assert payload["cycle_id"] == "cycle-1"
    assert payload["selected_zone_fingerprint"] == "zf1:selected"
    assert payload["token_in_symbol"] == "USDT"
    assert payload["secrets"]["password"] == REDACTED
    parsed = datetime.strptime(payload["timestamp"], "%Y-%m-%d %H:%M:%S +07:00").replace(tzinfo=UTC_PLUS_7)
    assert parsed.tzinfo == UTC_PLUS_7


def test_structured_logger_does_not_duplicate_owned_handlers(tmp_path) -> None:
    """Reconfiguration must replace stdout/file handlers instead of duplicating each event."""
    config = LoggingConfig(file_path=str(tmp_path / "trading.jsonl"))

    first = configure_audit_logger(config)
    second = configure_audit_logger(config)

    assert first is second
    assert len([handler for handler in second.handlers if getattr(handler, "_buy_dips_audit_handler", False)]) == 2
