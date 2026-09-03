from __future__ import annotations

from copy import deepcopy

import pytest

from src.config import AppConfig
from src.trading.prana_swap import DEV_QUOTE_BASE_URL, QuoteError, fetch_swap_quote, validate_swap_quote


NOW = 1_730_000_000
WALLET = "0x0000000000000000000000000000000000000001"
ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"


def _payload() -> dict[str, object]:
    """Build one complete valid in-house quote response fixture."""
    return {
        "request": {
            "tokenInSymbol": "USDT",
            "tokenOutSymbol": "PRANA",
            "amountIn": "1",
            "amountInRaw": "1000000",
            "recipient": WALLET,
            "slippageBps": 50,
            "chainId": 137,
        },
        "amountOut": "12.5",
        "amountOutRaw": "12500000000",
        "minimumAmountOut": "12.4",
        "routerAddress": ROUTER,
        "transaction": {
            "to": ROUTER,
            "data": "0x1234",
            "value": "0",
        },
        "deadline": NOW + 180,
        "verification": {
            "version": 2,
            "token": "opaque-and-never-persisted",
            "expiresAt": NOW + 180,
        },
    }


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        """Store one deterministic response payload for the fake HTTP session."""
        self.payload = payload

    def raise_for_status(self) -> None:
        """Model a successful HTTP status."""
        return None

    def json(self) -> dict[str, object]:
        """Return the configured JSON response."""
        return self.payload


class _Session:
    def __init__(self, payload: dict[str, object]) -> None:
        """Capture the exact outbound quote request without network access."""
        self.payload = payload
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> _Response:
        """Record one POST and return a valid fake response."""
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def test_fetch_quote_sends_origin_free_locked_request() -> None:
    """The adapter must send the UI-compatible body without an Origin header."""
    session = _Session(_payload())

    quote = fetch_swap_quote(AppConfig(), WALLET, session=session, now_s=NOW)

    url, request = session.calls[0]
    assert url == f"{DEV_QUOTE_BASE_URL}/api/swap/quote"
    assert request["headers"] == {"Content-Type": "application/json"}
    assert "Origin" not in request["headers"]
    assert request["json"] == {
        "tokenInSymbol": "USDT",
        "tokenOutSymbol": "PRANA",
        "amountIn": "1",
        "recipient": WALLET,
        "slippageBps": 50,
    }
    assert quote.amount_in_raw == 1_000_000
    assert quote.transaction.data == "0x1234"


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("request", "tokenInSymbol"), "USDC", "tokenInSymbol"),
        (("request", "amountIn"), "2", "amountIn"),
        (("request", "recipient"), "0x0000000000000000000000000000000000000002", "recipient"),
        (("request", "chainId"), 1, "chainId"),
        (("transaction", "to"), "0x0000000000000000000000000000000000000002", "transaction.to"),
        (("transaction", "data"), "0x", "calldata"),
        (("transaction", "value"), "1", "value"),
        (("verification", "version"), 1, "verification version"),
        (("verification", "token"), "", "verification token"),
        (("verification", "expiresAt"), NOW + 10, "verification"),
        (("root", "routerAddress"), "0x0000000000000000000000000000000000000002", "allowlisted"),
        (("root", "deadline"), NOW + 10, "deadline"),
    ],
)
def test_validate_quote_rejects_mismatched_execution_fields(path, value, message) -> None:
    """Every response field that pins execution must fail closed when changed."""
    payload = deepcopy(_payload())
    if path[0] == "root":
        payload[path[1]] = value
    else:
        payload[path[0]][path[1]] = value

    with pytest.raises(QuoteError, match=message):
        validate_swap_quote(payload, AppConfig(), WALLET, now_s=NOW)


def test_validate_quote_rejects_unpinned_dev_host_before_http() -> None:
    """A development run cannot silently switch to another quote server."""
    config = AppConfig(execution={"quote_base_url": "https://example.com"})
    session = _Session(_payload())

    with pytest.raises(QuoteError, match="pinned host"):
        fetch_swap_quote(config, WALLET, session=session, now_s=NOW)

    assert session.calls == []
