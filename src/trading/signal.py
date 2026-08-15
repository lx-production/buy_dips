from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

import pandas as pd

from .constants import COOLDOWN_24H_MS, FINGERPRINT_VERSION, LOOKBACK_48H_MS, STRATEGY_VERSION


BUY = "BUY"
HOLD = "HOLD"
REASON_CODES = frozenset(
    {
        "CLOSE_NOT_BELOW_OPEN",
        "CLOSE_OUTSIDE_ENTRY_REGION",
        "CLOSE_NOT_BELOW_ZONE_MID",
        "NO_HIGHER_ZONE",
        "NO_RECENT_CLOSE_ABOVE_INTERNAL_MID",
        "NO_LOWER_ZONE",
        "BELOW_ZONE_OUT_OF_BAND",
        "RECENT_BUY_IN_24H",
        "BUY_GATES_PASSED",
    }
)

PriorBuyLookup = Callable[[str, int, int], bool]


class DecisionInputError(ValueError):
    pass


def evaluate_support_close_v1(
    trigger_candle: Mapping[str, Any],
    hourly_candles: pd.DataFrame,
    zones: Iterable[Mapping[str, Any]],
    *,
    zone_set_as_of: int,
    prior_buy_exists: PriorBuyLookup | None = None,
    zones_rebuilt: bool = False,
    mode: str = "observe",
    strategy_version: str = STRATEGY_VERSION,
    config_version: str = "1",
) -> dict[str, Any]:
    """Evaluate the one gate-based dip-to-support flow on a closed 1h candle.

    The first gate requires a red trigger candle (`close < open`) so a green
    reclaim into a higher zone cannot BUY. Later gates then select the nearest
    support and apply the inside-below-70 / below-zone-band entry regions.
    """
    # backtest is allowed in the pure engine only; decisions.mode schema stays observe/dry_run/live.
    if mode not in {"observe", "dry_run", "live", "backtest"}:
        raise DecisionInputError(f"Unsupported decision mode: {mode}")
    trigger_open_time = _required_int(trigger_candle, "open_time")
    trigger_close_time = _required_int(trigger_candle, "close_time")
    close = _decimal(trigger_candle.get("close"), "trigger close")
    open_price = _decimal(trigger_candle.get("open"), "trigger open")
    ordered_zones = [dict(zone) for zone in zones]

    payload: dict[str, Any] = {
        "candle_open_time": trigger_open_time,
        "candle_close_time": trigger_close_time,
        "reference_close": float(close),
        "zone_set_as_of": int(zone_set_as_of),
        "fingerprint_version": FINGERPRINT_VERSION,
        "selected_zone_fingerprint": None,
        "selected_zone_low": None,
        "selected_zone_high": None,
        "selected_zone_mid": None,
        "selected_zone_source_time": None,
        "selected_source_open_times": None,
        "entry_region": None,
        "higher_zone_fingerprint": None,
        "higher_zone_low": None,
        "higher_zone_high": None,
        "next_lower_zone_fingerprint": None,
        "next_lower_zone_low": None,
        "next_lower_zone_high": None,
        "internal_range_low": None,
        "internal_range_high": None,
        "internal_range_midpoint": None,
        "below_zone_band_low": None,
        "below_zone_band_high": None,
        "below_zone_pct": None,
        "lookback_start_time": None,
        "lookback_end_time": trigger_open_time,
        "dip_origin_open_time": None,
        "dip_origin_close": None,
        "recent_buy_in_24h": False,
        "gate_results": {
            "red_candle": False,
            "entry_region": False,
            "higher_zone": False,
            "dip_origin": False,
            "per_zone_cooldown": False,
        },
        "zones_rebuilt": bool(zones_rebuilt),
        "mode": mode,
        "strategy_version": strategy_version,
        "config_version": str(config_version),
        "sanitized_error": None,
    }

    # Buy the dip only. A green or doji candle is rejected before zone selection.
    if close >= open_price:
        return _finish(payload, HOLD, "CLOSE_NOT_BELOW_OPEN")
    payload["gate_results"]["red_candle"] = True

    containing = [zone for zone in ordered_zones if _low(zone) <= close <= _high(zone)]
    selected: dict[str, Any] | None = None
    if containing:
        selected = max(containing, key=_low)
        _set_selected(payload, selected)
        # Inside-zone entry: 0% = zone.low, 100% = zone.high. Close must be strictly below 70%.
        if close >= _inside_zone_70_level(selected):
            return _finish(payload, HOLD, "CLOSE_NOT_BELOW_ZONE_MID")
        payload["entry_region"] = "inside_below_70"
        payload["gate_results"]["entry_region"] = True
    else:
        above = [zone for zone in ordered_zones if _low(zone) > close]
        if not above:
            return _finish(payload, HOLD, "CLOSE_OUTSIDE_ENTRY_REGION")
        selected = min(above, key=_low)
        _set_selected(payload, selected)
        lower = [zone for zone in ordered_zones if _high(zone) < _low(selected)]
        if not lower:
            return _finish(payload, HOLD, "NO_LOWER_ZONE")
        next_lower = max(lower, key=_high)
        _set_adjacent(payload, "next_lower", next_lower)
        gap_low = _high(next_lower)
        gap_high = _low(selected)
        below_pct = (close - gap_low) / (gap_high - gap_low)
        payload["below_zone_band_low"] = float(gap_low)
        payload["below_zone_band_high"] = float(gap_high)
        payload["below_zone_pct"] = float(below_pct)
        # Immediately-below entry: close must sit in the 50%–100% portion of (next_lower.high → zone.low).
        if not (Decimal("0.50") <= below_pct <= Decimal("1.0") and close < _low(selected)):
            return _finish(payload, HOLD, "BELOW_ZONE_OUT_OF_BAND")
        payload["entry_region"] = "below_zone_band"
        payload["gate_results"]["entry_region"] = True

    higher = [zone for zone in ordered_zones if _low(zone) > _high(selected)]
    if not higher:
        return _finish(payload, HOLD, "NO_HIGHER_ZONE")
    higher_zone = min(higher, key=_low)
    _set_adjacent(payload, "higher", higher_zone)
    internal_low = _high(selected)
    internal_high = _low(higher_zone)
    midpoint = (internal_low + internal_high) / Decimal("2")
    payload["internal_range_low"] = float(internal_low)
    payload["internal_range_high"] = float(internal_high)
    payload["internal_range_midpoint"] = float(midpoint)
    payload["gate_results"]["higher_zone"] = True

    zone_source_time = _required_zone_source_time(selected)
    lookback_start = max(trigger_open_time - LOOKBACK_48H_MS, zone_source_time)
    payload["lookback_start_time"] = lookback_start
    dip_origin = nearest_close_above_midpoint(
        hourly_candles,
        midpoint=midpoint,
        lookback_start_time=lookback_start,
        trigger_open_time=trigger_open_time,
    )
    if dip_origin is None:
        return _finish(payload, HOLD, "NO_RECENT_CLOSE_ABOVE_INTERNAL_MID")
    payload["dip_origin_open_time"] = int(dip_origin["open_time"])
    payload["dip_origin_close"] = float(dip_origin["close"])
    payload["gate_results"]["dip_origin"] = True

    fingerprint = str(selected["fingerprint"])
    cooldown_start = trigger_open_time - COOLDOWN_24H_MS
    recent_buy = bool(prior_buy_exists(fingerprint, cooldown_start, trigger_open_time)) if prior_buy_exists else False
    payload["recent_buy_in_24h"] = recent_buy
    payload["gate_results"]["per_zone_cooldown"] = not recent_buy
    if recent_buy:
        return _finish(payload, HOLD, "RECENT_BUY_IN_24H")
    return _finish(payload, BUY, "BUY_GATES_PASSED")


def nearest_close_above_midpoint(
    hourly_candles: pd.DataFrame,
    *,
    midpoint: Decimal | float,
    lookback_start_time: int,
    trigger_open_time: int,
) -> dict[str, Any] | None:
    if hourly_candles is None or hourly_candles.empty:
        return None
    required = {"open_time", "close"}
    if not required.issubset(hourly_candles.columns):
        return None
    candidates = hourly_candles.copy()
    if "is_closed" in candidates.columns:
        candidates = candidates[candidates["is_closed"].astype(int) == 1]
    candidates = candidates[
        (candidates["open_time"].astype("int64") >= int(lookback_start_time))
        & (candidates["open_time"].astype("int64") < int(trigger_open_time))
    ].sort_values("open_time", ascending=False)
    threshold = _decimal(midpoint, "internal range midpoint")
    for _, row in candidates.iterrows():
        candidate_close = _decimal(row["close"], "hourly close")
        if candidate_close > threshold:
            return {"open_time": int(row["open_time"]), "close": float(candidate_close)}
    return None


def _set_selected(payload: dict[str, Any], zone: Mapping[str, Any]) -> None:
    fingerprint = _required_fingerprint(zone)
    source_open_times = zone.get("source_open_times")
    if not isinstance(source_open_times, list) or not source_open_times:
        raise DecisionInputError("Selected zone is missing persisted source_open_times")
    payload.update(
        {
            "selected_zone_fingerprint": fingerprint,
            "selected_zone_low": float(_low(zone)),
            "selected_zone_high": float(_high(zone)),
            "selected_zone_mid": float(_mid(zone)),
            "selected_zone_source_time": _required_zone_source_time(zone),
            "selected_source_open_times": [int(value) for value in source_open_times],
        }
    )


def _set_adjacent(payload: dict[str, Any], prefix: str, zone: Mapping[str, Any]) -> None:
    payload[f"{prefix}_zone_fingerprint"] = _required_fingerprint(zone)
    payload[f"{prefix}_zone_low"] = float(_low(zone))
    payload[f"{prefix}_zone_high"] = float(_high(zone))


def _finish(payload: dict[str, Any], decision: str, reason_code: str) -> dict[str, Any]:
    if reason_code not in REASON_CODES:
        raise AssertionError(f"Unknown support_close_v1 reason: {reason_code}")
    payload["decision"] = decision
    payload["reason_code"] = reason_code
    return payload


def _required_fingerprint(zone: Mapping[str, Any]) -> str:
    fingerprint = zone.get("fingerprint")
    version = zone.get("fingerprint_version")
    if version != FINGERPRINT_VERSION or not isinstance(fingerprint, str) or not fingerprint.startswith("zf1:"):
        raise DecisionInputError("Zone is missing its persisted zf1 fingerprint")
    return fingerprint


def _required_zone_source_time(zone: Mapping[str, Any]) -> int:
    value = zone.get("zone_source_time")
    if value is None:
        raise DecisionInputError("Zone is missing persisted zone_source_time")
    return int(value)


def _low(zone: Mapping[str, Any]) -> Decimal:
    return _decimal(zone.get("low"), "zone low")


def _high(zone: Mapping[str, Any]) -> Decimal:
    return _decimal(zone.get("high"), "zone high")


def _mid(zone: Mapping[str, Any]) -> Decimal:
    return _decimal(zone.get("mid"), "zone mid")


def _inside_zone_70_level(zone: Mapping[str, Any]) -> Decimal:
    """Return the 70% price of the zone span. 0% is zone.low, 100% is zone.high."""
    return _low(zone) + Decimal("0.70") * (_high(zone) - _low(zone))


def _decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise DecisionInputError(f"Invalid {label}: {value!r}") from exc
    if not parsed.is_finite():
        raise DecisionInputError(f"Invalid {label}: {value!r}")
    return parsed


def _required_int(values: Mapping[str, Any], key: str) -> int:
    if key not in values or isinstance(values[key], bool):
        raise DecisionInputError(f"Trigger candle is missing integer {key}")
    try:
        return int(values[key])
    except (TypeError, ValueError) as exc:
        raise DecisionInputError(f"Trigger candle is missing integer {key}") from exc
