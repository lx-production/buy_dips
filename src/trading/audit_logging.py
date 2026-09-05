from __future__ import annotations

import re
import sys
import json
import logging

from pathlib import Path
from datetime import datetime
from logging.handlers import RotatingFileHandler

from typing import Any

from ..utils import UTC_PLUS_7
from ..config import LoggingConfig


LOGGER_NAME = "buy_dips.trading"
REDACTED = "[REDACTED]"
_URL_PATTERN = re.compile(r"https?://[^\s\"']+")
_LONG_HEX_PATTERN = re.compile(r"^0x[0-9a-fA-F]{67,}$")
_SENSITIVE_KEYS = {
    "api_key",
    "calldata",
    "decrypted_key",
    "mnemonic",
    "password",
    "private_key",
    "raw_transaction",
    "rpc_url",
    "secret",
    "signed_transaction",
    "token",
    "verification_token",
}


class _JsonFormatter(logging.Formatter):
    """Render one compact JSON object per audit event."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize only standard audit fields plus an already-sanitized payload."""
        payload = {
            # Operator-facing clock: same UTC+7 string as `*_utc7` / `ms_to_utc7`.
            "timestamp": datetime.fromtimestamp(record.created, UTC_PLUS_7).strftime("%Y-%m-%d %H:%M:%S +07:00"),
            "level": record.levelname,
            "event": record.getMessage(),
            **getattr(record, "audit_fields", {}),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def configure_audit_logger(config: LoggingConfig) -> logging.Logger:
    """Configure JSON stdout plus a size-rotating file without duplicate handlers."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, config.level))
    logger.propagate = False

    for handler in list(logger.handlers):
        # Only replace handlers owned by this module, leaving test capture handlers intact.
        if getattr(handler, "_buy_dips_audit_handler", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = _JsonFormatter()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler._buy_dips_audit_handler = True
    logger.addHandler(stdout_handler)

    file_path = Path(config.file_path)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=config.max_bytes,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler._buy_dips_audit_handler = True
        logger.addHandler(file_handler)
        file_path.chmod(0o600)
    except OSError:
        # Keep the systemd/stdout audit trail available when the file path is unavailable.
        logger.warning("audit_file_unavailable", extra={"audit_fields": {}})
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Write one structured event after recursively removing secret-bearing values."""
    logger.info(event, extra={"audit_fields": sanitize_for_audit(fields)})


def sanitize_for_audit(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secret keys, byte payloads, URLs, and long signed/calldata hex."""
    normalized_key = "" if key is None else key.lower().replace("-", "_").replace(".", "_")
    if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith("_password"):
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): sanitize_for_audit(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_for_audit(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return REDACTED
    if isinstance(value, str):
        if _URL_PATTERN.search(value) or _LONG_HEX_PATTERN.fullmatch(value):
            return REDACTED
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)
