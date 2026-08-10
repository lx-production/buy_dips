from __future__ import annotations

import pandas as pd

from src.trading.signal import evaluate_support_close_v1


HOUR = 3_600_000


def _zone(low: float, high: float, suffix: str, source_time: int = 0) -> dict[str, object]:
    return {
        "low": low,
        "high": high,
        "mid": (low + high) / 2,
        "fingerprint_version": "zf1",
        "fingerprint": f"zf1:{suffix}",
        "source_open_times": [source_time],
        "zone_source_time": source_time,
    }


def _hourly(trigger_time: int, previous: list[tuple[int, float]], trigger_close: float) -> pd.DataFrame:
    rows = [
        {"open_time": open_time, "close_time": open_time + HOUR - 1, "close": close, "is_closed": 1}
        for open_time, close in previous
    ]
    rows.append(
        {"open_time": trigger_time, "close_time": trigger_time + HOUR - 1, "close": trigger_close, "is_closed": 1}
    )
    return pd.DataFrame(rows)


def test_inside_below_mid_uses_nearest_qualifying_prior_close() -> None:
    trigger_time = 100 * HOUR
    candles = _hourly(
        trigger_time,
        [(trigger_time - 3 * HOUR, 108), (trigger_time - 2 * HOUR, 106), (trigger_time - HOUR, 104)],
        92,
    )
    zones = [_zone(80, 85, "lower"), _zone(90, 100, "selected"), _zone(110, 120, "higher")]

    decision = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(), candles, zones, zone_set_as_of=96 * HOUR
    )

    assert decision["decision"] == "BUY"
    assert decision["reason_code"] == "BUY_GATES_PASSED"
    assert decision["entry_region"] == "inside_below_mid"
    assert decision["dip_origin_open_time"] == trigger_time - 2 * HOUR


def test_below_zone_uses_70_to_100_percent_band() -> None:
    trigger_time = 100 * HOUR
    candles = _hourly(trigger_time, [(trigger_time - HOUR, 106)], 89)
    zones = [_zone(80, 85, "lower"), _zone(90, 100, "selected"), _zone(110, 120, "higher")]

    decision = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(), candles, zones, zone_set_as_of=96 * HOUR
    )

    assert decision["decision"] == "BUY"
    assert decision["entry_region"] == "below_zone_band"
    assert decision["below_zone_pct"] == 0.8
    assert decision["next_lower_zone_fingerprint"] == "zf1:lower"


def test_cooldown_is_for_selected_zone_only() -> None:
    trigger_time = 100 * HOUR
    candles = _hourly(trigger_time, [(trigger_time - HOUR, 106)], 92)
    zones = [_zone(90, 100, "selected"), _zone(110, 120, "higher")]

    same_zone = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(),
        candles,
        zones,
        zone_set_as_of=96 * HOUR,
        prior_buy_exists=lambda fingerprint, _start, _end: fingerprint == "zf1:selected",
    )
    other_zone = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(),
        candles,
        zones,
        zone_set_as_of=96 * HOUR,
        prior_buy_exists=lambda fingerprint, _start, _end: fingerprint == "zf1:other",
    )

    assert same_zone["reason_code"] == "RECENT_BUY_IN_24H"
    assert other_zone["decision"] == "BUY"


def test_inside_at_or_above_mid_is_hold() -> None:
    trigger_time = 100 * HOUR
    candles = _hourly(trigger_time, [(trigger_time - HOUR, 106)], 95)
    zones = [_zone(90, 100, "selected"), _zone(110, 120, "higher")]

    decision = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(), candles, zones, zone_set_as_of=96 * HOUR
    )

    assert decision["reason_code"] == "CLOSE_NOT_BELOW_ZONE_MID"
