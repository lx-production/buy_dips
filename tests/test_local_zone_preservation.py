from __future__ import annotations

from itertools import permutations

import pytest

from src.trading.zone_tracks import ZoneTrackState
from src.zones.reactions import _select_local_reaction_zones


# Reproduce the three bands from September 9 without depending on a local database.
def _bands() -> list[dict]:
    zones = []
    for low, high, origin in (
        (77962.49, 78118.0, "local_reaction_support"),
        (78410.98, 78539.14, "local_retested_flip_support"),
        (79100.01, 79581.99, "local_reaction_support"),
    ):
        mid = (low + high) / 2
        zones.append({
            "low": low, "high": high, "mid": mid, "width": high - low,
            "width_pct": (high - low) / mid * 100,
            "origin": origin, "bounds_style": "local_reaction",
            "source_timeframe": "4h", "score": 7.0, "touches": 3,
        })
    return zones


# A flip sharing both slots must leave the two distinct shelves intact in any input order.
def test_middle_flip_cannot_collapse_two_distinct_local_shelves() -> None:
    lower, flip, upper = _bands()
    for candidates in permutations((lower, flip, upper)):
        assert _select_local_reaction_zones(list(candidates), 500) == [lower, upper]


# Repeated rebuilds must retain the actual old bounds and cooldown identities beyond retirement.
def test_preserved_shelves_survive_repeated_track_rebuilds_and_restore() -> None:
    lower, flip, upper = _bands()
    state = ZoneTrackState()
    initial = state.advance([lower, upper], zone_set_as_of=0)
    for step in range(1, 7):
        state = ZoneTrackState.from_payload(state.to_payload())
        candidates = _select_local_reaction_zones([lower, flip, upper], 500)
        current = state.advance(candidates, zone_set_as_of=step * 14_400_000)
        assert current == initial
    assert state.retire_count == state.replace_count == state.switch_count == 0


# A single nearby shelf still follows the existing retested-flip preference.
@pytest.mark.parametrize("shelf_index", [0, 2])
def test_flip_can_still_replace_one_local_shelf(shelf_index: int) -> None:
    bands = _bands()
    flip = bands[1]
    assert _select_local_reaction_zones([bands[shelf_index], flip], 500) == [flip]


# Thin rejected bands and duplicate evidence cannot invent a second protected ladder step.
@pytest.mark.parametrize("invalid_second", ["thin", "duplicate"])
def test_only_two_valid_distinct_shelves_block_a_flip(invalid_second: str) -> None:
    lower, flip, upper = _bands()
    if invalid_second == "thin":
        upper["high"] = upper["low"] + 50
        upper["width"] = 50
        upper["mid"] = upper["low"] + 25
    else:
        upper = dict(lower)
    assert _select_local_reaction_zones([lower, flip, upper], 500) == [flip]
