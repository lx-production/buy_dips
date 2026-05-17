from __future__ import annotations

from src.chart_server import _visible_support_zones


def test_visible_support_zones_keep_below_price_and_nearest_two_above() -> None:
    zones = [
        _zone(80.0, 85.0, touches=4),
        _zone(70.0, 75.0, touches=3),
        _zone(102.0, 107.0, touches=2),
        _zone(110.0, 115.0, touches=8),
        _zone(130.0, 135.0, touches=10),
    ]

    visible = _visible_support_zones(zones, current_price=100.0)

    assert [(zone["low"], zone["high"]) for zone in visible] == [
        (80.0, 85.0),
        (70.0, 75.0),
        (102.0, 107.0),
        (110.0, 115.0),
    ]


def test_visible_support_zones_keep_price_touching_zone() -> None:
    zones = [_zone(98.0, 103.0), _zone(104.0, 109.0), _zone(120.0, 125.0)]

    visible = _visible_support_zones(zones, current_price=100.0, above_count=1)

    assert [(zone["low"], zone["high"]) for zone in visible] == [(98.0, 103.0), (104.0, 109.0)]


def _zone(low: float, high: float, touches: int = 1) -> dict:
    return {"low": low, "high": high, "touches": touches}
