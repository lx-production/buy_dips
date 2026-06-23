from __future__ import annotations


def _classify_price_state(low: float, high: float, current_price: float, buffer_pct: float) -> str:
    if high < current_price * (1 - buffer_pct):
        return "support"
    if low > current_price * (1 + buffer_pct):
        return "resistance"
    return "active"
