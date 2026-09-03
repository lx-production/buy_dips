from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from typing import Any

import requests
from web3 import Web3

from ..config import AppConfig
from .constants import POLYGON_CHAIN_ID, QUOTE_TOKEN_IN_SYMBOL, QUOTE_TOKEN_OUT_SYMBOL, TRADE_AMOUNT_USDT_RAW
from .models import SwapTransaction, ValidatedSwapQuote


DEV_QUOTE_BASE_URL = "https://prana.triethocduongpho.net"
PROD_QUOTE_BASE_URL = "http://127.0.0.1:4173"
QUOTE_PATH = "/api/swap/quote"
MAX_QUOTE_BODY_BYTES = 2_048


class QuoteError(RuntimeError):
    """Raised when a quote request or response cannot be trusted."""


def fetch_swap_quote(
    config: AppConfig,
    recipient: str,
    *,
    session: Any | None = None,
    now_s: int | None = None,
) -> ValidatedSwapQuote:
    """Request one in-house USDT→PRANA quote and validate every execution-bound field."""
    base_url = _validate_quote_base_url(config)
    recipient_address = _checksum_address(recipient, "recipient")
    body = {
        "tokenInSymbol": QUOTE_TOKEN_IN_SYMBOL,
        "tokenOutSymbol": QUOTE_TOKEN_OUT_SYMBOL,
        "amountIn": "1",
        "recipient": recipient_address,
        "slippageBps": config.execution.slippage_bps,
    }
    encoded_body = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(encoded_body) > MAX_QUOTE_BODY_BYTES:
        raise QuoteError("Quote request exceeds the 2 KB body limit")

    http = requests if session is None else session
    try:
        response = http.post(
            f"{base_url}{QUOTE_PATH}",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=config.execution.quote_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        # Keep upstream response bodies and transport details out of persisted/operator errors.
        raise QuoteError("Quote request failed") from exc
    return validate_swap_quote(
        payload,
        config,
        recipient_address,
        now_s=int(time.time()) if now_s is None else int(now_s),
    )


def validate_swap_quote(
    payload: Any,
    config: AppConfig,
    recipient: str,
    *,
    now_s: int,
) -> ValidatedSwapQuote:
    """Validate response echoes, router calldata, amounts, deadline, and verification metadata."""
    _validate_quote_base_url(config)
    if not isinstance(payload, dict):
        raise QuoteError("Quote response must be a JSON object")
    request = _required_mapping(payload, "request")
    transaction = _required_mapping(payload, "transaction")
    verification = _required_mapping(payload, "verification")

    if request.get("tokenInSymbol") != QUOTE_TOKEN_IN_SYMBOL:
        raise QuoteError("Quote tokenInSymbol does not match USDT")
    if request.get("tokenOutSymbol") != QUOTE_TOKEN_OUT_SYMBOL:
        raise QuoteError("Quote tokenOutSymbol does not match PRANA")
    if request.get("amountIn") != "1":
        raise QuoteError('Quote amountIn must exactly equal "1"')
    amount_in_raw = _positive_int(request.get("amountInRaw"), "request.amountInRaw")
    if amount_in_raw != TRADE_AMOUNT_USDT_RAW:
        raise QuoteError("Quote amountInRaw does not equal 1 USDT")
    response_recipient = _checksum_address(request.get("recipient"), "request.recipient")
    if response_recipient != _checksum_address(recipient, "recipient"):
        raise QuoteError("Quote recipient does not match the bot wallet")
    slippage_bps = _positive_int(request.get("slippageBps"), "request.slippageBps")
    if slippage_bps != config.execution.slippage_bps:
        raise QuoteError("Quote slippageBps does not match configuration")
    chain_id = _positive_int(request.get("chainId"), "request.chainId")
    if chain_id != POLYGON_CHAIN_ID or chain_id != config.execution.chain_id:
        raise QuoteError("Quote chainId does not match Polygon")

    router_address = _checksum_address(payload.get("routerAddress"), "routerAddress")
    allowed_routers = {Web3.to_checksum_address(address) for address in config.execution.router_allowlist}
    if router_address not in allowed_routers:
        raise QuoteError("Quote routerAddress is not allowlisted")
    transaction_to = _checksum_address(transaction.get("to"), "transaction.to")
    if transaction_to != router_address:
        raise QuoteError("Quote transaction.to does not match routerAddress")
    calldata = _validate_calldata(transaction.get("data"))
    value = _non_negative_int(transaction.get("value"), "transaction.value")
    if value != 0:
        raise QuoteError("USDT quote transaction.value must be zero")

    amount_out = _positive_decimal(payload.get("amountOut"), "amountOut")
    amount_out_raw = _positive_int(payload.get("amountOutRaw"), "amountOutRaw")
    minimum_amount_out = _positive_decimal(payload.get("minimumAmountOut"), "minimumAmountOut")
    if minimum_amount_out > amount_out:
        raise QuoteError("minimumAmountOut cannot exceed amountOut")

    deadline = _unix_seconds(payload.get("deadline"), "deadline")
    minimum_usable_time = int(now_s) + config.execution.quote_min_deadline_seconds
    if deadline <= minimum_usable_time:
        raise QuoteError("Quote deadline is expired or too close")

    verification_version = _positive_int(verification.get("version"), "verification.version")
    if verification_version != 2:
        raise QuoteError("Unsupported quote verification version")
    token = verification.get("token")
    if not isinstance(token, str) or not token.strip():
        raise QuoteError("Quote verification token is missing")
    verification_expires_at = _parse_expiry(verification.get("expiresAt"))
    if verification_expires_at <= minimum_usable_time:
        raise QuoteError("Quote verification is expired or too close")

    return ValidatedSwapQuote(
        token_in_symbol=QUOTE_TOKEN_IN_SYMBOL,
        token_out_symbol=QUOTE_TOKEN_OUT_SYMBOL,
        amount_in=Decimal("1"),
        amount_in_raw=amount_in_raw,
        recipient=response_recipient,
        slippage_bps=slippage_bps,
        chain_id=chain_id,
        amount_out=amount_out,
        amount_out_raw=amount_out_raw,
        minimum_amount_out=minimum_amount_out,
        router_address=router_address,
        transaction=SwapTransaction(to=transaction_to, data=calldata, value=value),
        deadline=deadline,
        verification_version=verification_version,
        verification_expires_at=verification_expires_at,
    )


def _validate_quote_base_url(config: AppConfig) -> str:
    """Pin dev to the public HTTPS host and prod to the local route server."""
    candidate = config.execution.quote_base_url.rstrip("/")
    expected = DEV_QUOTE_BASE_URL if config.environment == "dev" else PROD_QUOTE_BASE_URL
    parsed = urlparse(candidate)
    if candidate != expected or parsed.path or parsed.query or parsed.fragment:
        raise QuoteError(f"{config.environment} quote_base_url is not the pinned host")
    return candidate


def _required_mapping(payload: dict[str, Any], field: str) -> dict[str, Any]:
    """Return a required nested JSON object or fail with a field-safe message."""
    value = payload.get(field)
    if not isinstance(value, dict):
        raise QuoteError(f"Quote {field} must be an object")
    return value


def _checksum_address(value: Any, field: str) -> str:
    """Validate an EVM address and normalize it for exact comparisons."""
    if not isinstance(value, str) or not Web3.is_address(value):
        raise QuoteError(f"Quote {field} is not a valid address")
    return Web3.to_checksum_address(value)


def _validate_calldata(value: Any) -> str:
    """Require non-empty, even-length hexadecimal router calldata."""
    if not isinstance(value, str) or not value.startswith("0x") or len(value) <= 2:
        raise QuoteError("Quote transaction.data must contain calldata")
    hexadecimal = value[2:]
    if len(hexadecimal) % 2:
        raise QuoteError("Quote transaction.data must have an even hex length")
    try:
        bytes.fromhex(hexadecimal)
    except ValueError as exc:
        raise QuoteError("Quote transaction.data must be hexadecimal") from exc
    return value


def _positive_decimal(value: Any, field: str) -> Decimal:
    """Parse a positive finite decimal from a JSON string without binary floats."""
    if not isinstance(value, str):
        raise QuoteError(f"Quote {field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise QuoteError(f"Quote {field} is not a decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise QuoteError(f"Quote {field} must be positive")
    return parsed


def _positive_int(value: Any, field: str) -> int:
    """Parse a strictly positive integer while rejecting booleans and fractional values."""
    parsed = _non_negative_int(value, field)
    if parsed <= 0:
        raise QuoteError(f"Quote {field} must be positive")
    return parsed


def _non_negative_int(value: Any, field: str) -> int:
    """Parse a non-negative base-10 integer from the API's number or string form."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise QuoteError(f"Quote {field} must be an integer")
    rendered = str(value)
    if not rendered.isdigit():
        raise QuoteError(f"Quote {field} must be a non-negative integer")
    return int(rendered)


def _unix_seconds(value: Any, field: str) -> int:
    """Parse a Unix-seconds timestamp and reject likely millisecond timestamps."""
    parsed = _positive_int(value, field)
    if parsed >= 100_000_000_000:
        raise QuoteError(f"Quote {field} must use Unix seconds")
    return parsed


def _parse_expiry(value: Any) -> int:
    """Accept verification expiry as Unix seconds or an ISO-8601 timestamp."""
    if isinstance(value, bool):
        raise QuoteError("Quote verification.expiresAt is invalid")
    if isinstance(value, int) or isinstance(value, str) and value.isdigit():
        return _unix_seconds(value, "verification.expiresAt")
    if not isinstance(value, str):
        raise QuoteError("Quote verification.expiresAt is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuoteError("Quote verification.expiresAt is invalid") from exc
    if parsed.tzinfo is None:
        raise QuoteError("Quote verification.expiresAt must include a timezone")
    return int(parsed.astimezone(timezone.utc).timestamp())
