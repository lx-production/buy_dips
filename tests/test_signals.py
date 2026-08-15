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


def _hourly(
    trigger_time: int,
    previous: list[tuple[int, float]],
    trigger_close: float,
    trigger_open: float | None = None,
) -> pd.DataFrame:
    """Build closed 1h rows. Default trigger is red (`open > close`) so later gates can be tested alone."""
    open_price = trigger_close + 1.0 if trigger_open is None else trigger_open
    rows = [
        {"open_time": open_time, "close_time": open_time + HOUR - 1, "close": close, "is_closed": 1}
        for open_time, close in previous
    ]
    rows.append(
        {
            "open_time": trigger_time,
            "close_time": trigger_time + HOUR - 1,
            "open": open_price,
            "close": trigger_close,
            "is_closed": 1,
        }
    )
    return pd.DataFrame(rows)


def test_inside_zone_uses_nearest_qualifying_prior_close() -> None:
    # close=92 is 20% of zone 90–100, so it is inside the full 0%–100% band.
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
    assert decision["entry_region"] == "inside_zone"
    assert decision["dip_origin_open_time"] == trigger_time - 2 * HOUR


def test_below_zone_uses_50_to_100_percent_band() -> None:
    # Gap is 85 → 90. close=89 sits at 80% of that gap, inside the 50%–100% band.
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


def test_below_zone_at_50_percent_is_buy() -> None:
    # close=87.5 is exactly 50% of the 85 → 90 gap, so it is the inclusive lower edge.
    trigger_time = 100 * HOUR
    candles = _hourly(trigger_time, [(trigger_time - HOUR, 106)], 87.5)
    zones = [_zone(80, 85, "lower"), _zone(90, 100, "selected"), _zone(110, 120, "higher")]

    decision = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(), candles, zones, zone_set_as_of=96 * HOUR
    )

    assert decision["decision"] == "BUY"
    assert decision["entry_region"] == "below_zone_band"
    assert decision["below_zone_pct"] == 0.5


def test_below_zone_below_50_percent_is_hold() -> None:
    # close=87 is 40% of the 85 → 90 gap, so it is outside the 50%–100% band.
    trigger_time = 100 * HOUR
    candles = _hourly(trigger_time, [(trigger_time - HOUR, 106)], 87)
    zones = [_zone(80, 85, "lower"), _zone(90, 100, "selected"), _zone(110, 120, "higher")]

    decision = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(), candles, zones, zone_set_as_of=96 * HOUR
    )

    assert decision["decision"] == "HOLD"
    assert decision["reason_code"] == "BELOW_ZONE_OUT_OF_BAND"
    assert decision["below_zone_pct"] == 0.4


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


def test_inside_at_80_percent_is_buy() -> None:
    # close=98 is exactly 80% of zone 90–100; the full inside band now includes this level.
    trigger_time = 100 * HOUR
    candles = _hourly(trigger_time, [(trigger_time - HOUR, 106)], 98)
    zones = [_zone(90, 100, "selected"), _zone(110, 120, "higher")]

    decision = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(), candles, zones, zone_set_as_of=96 * HOUR
    )

    assert decision["decision"] == "BUY"
    assert decision["entry_region"] == "inside_zone"
    assert decision["reason_code"] == "BUY_GATES_PASSED"


def test_inside_at_zone_high_is_buy() -> None:
    # close=100 is 100% of zone 90–100 (zone.high), so it still qualifies for inside_zone.
    trigger_time = 100 * HOUR
    candles = _hourly(trigger_time, [(trigger_time - HOUR, 106)], 100)
    zones = [_zone(90, 100, "selected"), _zone(110, 120, "higher")]

    decision = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(), candles, zones, zone_set_as_of=96 * HOUR
    )

    assert decision["decision"] == "BUY"
    assert decision["entry_region"] == "inside_zone"


def test_mar5_floor_78_percent_close_is_buy() -> None:
    # June 6 01:00 UTC+7 close 59395.99 is 78.2% of the pinned 59005-59505 floor.
    trigger_time = 100 * HOUR
    candles = _hourly(trigger_time, [(trigger_time - HOUR, 61000)], 59395.99)
    zones = [_zone(59005.0, 59505.0, "mar5"), _zone(60123.73, 60623.73, "higher")]

    decision = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(), candles, zones, zone_set_as_of=96 * HOUR
    )

    assert decision["decision"] == "BUY"
    assert decision["entry_region"] == "inside_zone"
    assert decision["reason_code"] == "BUY_GATES_PASSED"


def test_green_trigger_candle_holds_before_zone_gates() -> None:
    # Same close as the inside-zone BUY case, but open is lower so the candle is green.
    trigger_time = 100 * HOUR
    candles = _hourly(
        trigger_time,
        [(trigger_time - 3 * HOUR, 108), (trigger_time - 2 * HOUR, 106), (trigger_time - HOUR, 104)],
        92,
        trigger_open=90,
    )
    zones = [_zone(80, 85, "lower"), _zone(90, 100, "selected"), _zone(110, 120, "higher")]

    decision = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(), candles, zones, zone_set_as_of=96 * HOUR
    )

    assert decision["decision"] == "HOLD"
    assert decision["reason_code"] == "CLOSE_NOT_BELOW_OPEN"
    assert decision["entry_region"] is None
    assert decision["gate_results"]["red_candle"] is False


def test_doji_trigger_candle_is_hold() -> None:
    # close == open is not a red candle, so it cannot BUY.
    trigger_time = 100 * HOUR
    candles = _hourly(trigger_time, [(trigger_time - HOUR, 106)], 92, trigger_open=92)
    zones = [_zone(90, 100, "selected"), _zone(110, 120, "higher")]

    decision = evaluate_support_close_v1(
        candles.iloc[-1].to_dict(), candles, zones, zone_set_as_of=96 * HOUR
    )

    assert decision["decision"] == "HOLD"
    assert decision["reason_code"] == "CLOSE_NOT_BELOW_OPEN"
