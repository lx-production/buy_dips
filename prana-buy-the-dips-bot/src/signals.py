from __future__ import annotations

from typing import Any

import pandas as pd

from .config import AppConfig


def generate_buy_the_dips_signal(df: pd.DataFrame, zones: dict[str, list[dict[str, Any]]], config: AppConfig) -> dict[str, Any]:
    if df is None or df.empty or "close" not in df.columns:
        return {
            "price": 0.0,
            "decision": "HOLD",
            "signal_score": 0.0,
            "reason": "No closed candle data available.",
            "metadata": {"components": [], "paper_mode": True},
        }

    closed_df = df.copy()
    if "is_closed" in closed_df.columns:
        closed_df = closed_df[closed_df["is_closed"].astype(int) == 1]
    if closed_df.empty:
        return {
            "price": 0.0,
            "decision": "HOLD",
            "signal_score": 0.0,
            "reason": "No closed candle data available.",
            "metadata": {"components": [], "paper_mode": True},
        }

    price = float(closed_df.iloc[-1]["close"])
    previous_price = float(closed_df.iloc[-2]["close"]) if len(closed_df) >= 2 else None
    latest_close_time = int(closed_df.iloc[-1]["close_time"]) if "close_time" in closed_df.columns else None

    active_zones = zones.get("active", [])
    supports = [zone for zone in zones.get("support", []) if zone["high"] < price or zone["low"] <= price]
    resistances = [zone for zone in zones.get("resistance", []) if zone["low"] > price or zone["high"] >= price]
    nearest_support = max(supports, key=lambda z: z["high"], default=None)
    nearest_resistance = min(resistances, key=lambda z: z["low"], default=None)

    distance_to_support_pct = None
    if nearest_support is not None:
        distance_to_support_pct = max(0.0, (price - float(nearest_support["high"])) / price * 100.0)
    distance_to_resistance_pct = None
    if nearest_resistance is not None:
        distance_to_resistance_pct = max(0.0, (float(nearest_resistance["low"]) - price) / price * 100.0)

    score = 0.0
    components: list[dict[str, Any]] = []

    inside_active = any(float(zone["low"]) <= price <= float(zone["high"]) for zone in active_zones)
    if inside_active:
        score += 20
        components.append({"points": 20, "reason": "Price is inside an active support/resistance zone."})

    near_support = False
    signal_config = config.signals
    if nearest_support is not None and distance_to_support_pct is not None:
        if distance_to_support_pct <= signal_config.near_support_pct_tight:
            score += 35
            near_support = True
            components.append({"points": 35, "reason": "Price is within 0.25% of nearest support."})
        elif distance_to_support_pct <= signal_config.near_support_pct_medium:
            score += 25
            near_support = True
            components.append({"points": 25, "reason": "Price is within 0.50% of nearest support."})
        elif distance_to_support_pct <= signal_config.near_support_pct_loose:
            score += 15
            near_support = True
            components.append({"points": 15, "reason": "Price is within 1.00% of nearest support."})

        if price < float(nearest_support["low"]):
            score -= 30
            components.append({"points": -30, "reason": "Price closed below nearest support low."})

    if nearest_resistance is not None and distance_to_resistance_pct is not None:
        if distance_to_resistance_pct <= signal_config.near_resistance_pct:
            score -= 10
            components.append({"points": -10, "reason": "Nearest resistance is within 0.50%."})

    if previous_price is not None and price > previous_price and near_support:
        score += 10
        components.append({"points": 10, "reason": "Latest 4H close is higher than previous 4H close while near support."})

    recent_high = _recent_close_high(closed_df, signal_config.dip_lookback_candles)
    recent_dip_pct = 0.0
    if recent_high and recent_high > 0:
        recent_dip_pct = max(0.0, (recent_high - price) / recent_high * 100.0)
        if recent_dip_pct >= signal_config.dip_threshold_3_pct:
            score += 30
            components.append({"points": 30, "reason": "Recent dip from 20-candle close high is at least 8%."})
        elif recent_dip_pct >= signal_config.dip_threshold_2_pct:
            score += 20
            components.append({"points": 20, "reason": "Recent dip from 20-candle close high is at least 5%."})
        elif recent_dip_pct >= signal_config.dip_threshold_1_pct:
            score += 10
            components.append({"points": 10, "reason": "Recent dip from 20-candle close high is at least 3%."})

    decision = _decision(score)
    if not components:
        components.append({"points": 0, "reason": "No buy conditions matched; paper signal remains HOLD."})
    reason = "; ".join(component["reason"] for component in components)

    return {
        "price": price,
        "decision": decision,
        "signal_score": score,
        "nearest_support_low": nearest_support["low"] if nearest_support else None,
        "nearest_support_high": nearest_support["high"] if nearest_support else None,
        "nearest_resistance_low": nearest_resistance["low"] if nearest_resistance else None,
        "nearest_resistance_high": nearest_resistance["high"] if nearest_resistance else None,
        "distance_to_support_pct": distance_to_support_pct,
        "distance_to_resistance_pct": distance_to_resistance_pct,
        "reason": reason,
        "metadata": {
            "paper_mode": True,
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
            "active_zones": active_zones,
            "recent_close_high": recent_high,
            "recent_dip_pct": recent_dip_pct,
            "components": components,
            "latest_candle_close_time": latest_close_time,
        },
    }


def _recent_close_high(df: pd.DataFrame, lookback: int) -> float | None:
    if df.empty:
        return None
    window = df.tail(max(1, lookback))
    return float(window["close"].max())


def _decision(score: float) -> str:
    if score < 50:
        return "HOLD"
    if score < 70:
        return "ALERT_ONLY"
    if score < 85:
        return "PREPARE_MANUAL_REVIEW"
    return "STRONG_BUY_SIGNAL"
