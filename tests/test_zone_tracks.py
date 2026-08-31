from __future__ import annotations

from typing import Any

import pytest

from src.trading.zone_tracks import ZONE_TRACK_RETIRE_MISSES, ZoneTrackState


FOUR = 14_400_000


# Build one detector-shaped support dict so track tests do not depend on the full finder.
def _zone(
    low: float,
    high: float,
    *,
    origin: str = "structure_swing_low",
    bounds_style: str = "body",
    source_timeframe: str = "4h",
    score: float = 4.0,
    touches: int = 2,
    revision: str = "zf1:rev1",
    lineage: str = "zf1:lineage",
) -> dict[str, Any]:
    mid = (low + high) / 2.0
    return {
        "origin": origin,
        "bounds_style": bounds_style,
        "source_timeframe": source_timeframe,
        "low": low,
        "high": high,
        "mid": mid,
        "width": high - low,
        "score": score,
        "touches": touches,
        "fingerprint": lineage,
        "zone_lineage_id": lineage,
        "revision_fingerprint": revision,
        "source_indexes": [0],
        "source_open_times": [0],
        "zone_source_time": 0,
    }


def test_bootstrap_publishes_first_snapshot_immediately() -> None:
    state = ZoneTrackState()
    zone = _zone(90, 100)
    published = state.advance([zone], zone_set_as_of=0)

    assert len(published) == 1
    assert published[0]["low"] == 90
    assert published[0]["high"] == 100
    assert published[0]["fingerprint"] == published[0]["zone_track_id"]
    assert published[0]["zone_lineage_id"] == "zf1:lineage"
    assert published[0]["fingerprint"] != published[0]["zone_lineage_id"]


def test_new_candidate_needs_two_consecutive_snapshots_to_activate() -> None:
    state = ZoneTrackState()
    incumbent = _zone(90, 100)
    state.advance([incumbent], zone_set_as_of=0)
    new = _zone(5000, 5100, lineage="zf1:new")

    after_first = state.advance([incumbent, new], zone_set_as_of=FOUR)
    assert all(abs(float(zone["low"]) - 5000) > 1 for zone in after_first)

    after_second = state.advance([incumbent, new], zone_set_as_of=2 * FOUR)
    assert any(abs(float(zone["low"]) - 5000) < 1 for zone in after_second)


def test_active_track_survives_two_misses_and_retires_on_the_third() -> None:
    state = ZoneTrackState()
    zone = _zone(90, 100)
    state.advance([zone], zone_set_as_of=0)

    assert state.advance([], zone_set_as_of=FOUR)
    assert state.advance([], zone_set_as_of=2 * FOUR)
    assert state.advance([], zone_set_as_of=3 * FOUR) == []
    assert state.retire_count == 1
    assert ZONE_TRACK_RETIRE_MISSES == 3


def test_bounds_only_move_after_two_consecutive_new_levels() -> None:
    state = ZoneTrackState()
    first = _zone(90, 100, lineage="zf1:a")
    drifted = _zone(92, 102, lineage="zf1:b", revision="zf1:rev2")
    published = state.advance([first], zone_set_as_of=0)
    track_id = published[0]["zone_track_id"]

    still_old = state.advance([drifted], zone_set_as_of=FOUR)
    assert still_old[0]["low"] == 90
    assert still_old[0]["high"] == 100
    assert still_old[0]["zone_track_id"] == track_id

    moved = state.advance([drifted], zone_set_as_of=2 * FOUR)
    assert moved[0]["low"] == 92
    assert moved[0]["high"] == 102
    assert moved[0]["zone_track_id"] == track_id
    assert moved[0]["fingerprint"] == track_id
    assert moved[0]["zone_lineage_id"] == "zf1:b"


def test_single_flicker_does_not_activate_or_move_bounds() -> None:
    state = ZoneTrackState()
    first = _zone(90, 100)
    flicker = _zone(5000, 5100, lineage="zf1:flicker")
    state.advance([first], zone_set_as_of=0)
    state.advance([first, flicker], zone_set_as_of=FOUR)
    back = state.advance([first], zone_set_as_of=2 * FOUR)

    assert len(back) == 1
    assert back[0]["low"] == 90
    assert back[0]["high"] == 100


def test_challenger_replaces_incumbent_after_two_consecutive_wins() -> None:
    state = ZoneTrackState()
    body = _zone(90, 100, bounds_style="body", origin="structure_swing_low")
    local = _zone(92, 102, bounds_style="local_reaction", origin="local_reaction_support", lineage="zf1:local")
    first = state.advance([body], zone_set_as_of=0)
    incumbent_id = first[0]["zone_track_id"]

    still_body = state.advance([local], zone_set_as_of=FOUR)
    assert still_body[0]["zone_track_id"] == incumbent_id
    assert still_body[0]["bounds_style"] == "body"

    replaced = state.advance([local], zone_set_as_of=2 * FOUR)
    assert len(replaced) == 1
    assert replaced[0]["bounds_style"] == "local_reaction"
    assert replaced[0]["zone_track_id"] != incumbent_id
    assert state.replace_count == 1


def test_restore_payload_continues_hysteresis_without_lookahead() -> None:
    state = ZoneTrackState()
    zone = _zone(90, 100)
    state.advance([zone], zone_set_as_of=0)
    state.advance([], zone_set_as_of=FOUR)
    restored = ZoneTrackState.from_payload(state.to_payload())
    still_active = restored.advance([], zone_set_as_of=2 * FOUR)
    gone = restored.advance([], zone_set_as_of=3 * FOUR)

    assert still_active[0]["low"] == 90
    assert gone == []


def test_duplicate_or_past_watermark_is_rejected() -> None:
    state = ZoneTrackState()
    zone = _zone(90, 100)
    state.advance([zone], zone_set_as_of=FOUR)
    with pytest.raises(ValueError, match="watermark"):
        state.advance([zone], zone_set_as_of=FOUR)
