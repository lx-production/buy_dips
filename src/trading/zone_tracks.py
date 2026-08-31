from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any, Literal

from .constants import DETECTOR_VERSION, EXCHANGE, SYMBOL
from ..zones.postprocess import _support_zones_share_ladder_slot
from .zone_identity import canonical_bounds_style, canonical_price, make_zone_track_id


ZONE_TRACK_ACTIVATE_CONFIRMS = 2
ZONE_TRACK_RETIRE_MISSES = 3
ZONE_TRACK_REPLACE_CONFIRMS = 2

TrackStatus = Literal["pending", "active"]


@dataclass
class ZoneTrack:
    """One causal support shelf that can outlive a single detector snapshot."""

    track_id: str
    source_timeframe: str
    bounds_style: str
    status: TrackStatus
    published: dict[str, Any] | None = None
    last_candidate: dict[str, Any] | None = None
    pending_bounds: dict[str, Any] | None = None
    challenger: dict[str, Any] | None = None
    appear_count: int = 0
    miss_count: int = 0
    bounds_confirm_count: int = 0
    replace_count: int = 0

    # Zone used to match this track against the current detector ladder.
    @property
    def matching_zone(self) -> dict[str, Any] | None:
        if self.published is not None:
            return self.published
        return self.last_candidate


@dataclass
class ZoneTrackState:
    """Keep detector candidates, then publish a sticky ladder with 2-confirm / 3-miss hysteresis.

    The detector still rebuilds winners every 4h. This layer is causal: it only
    sees the current snapshot's candidates plus prior tracks. The first snapshot
    of a run bootstraps every candidate to active so a replay start (or first live
    rebuild after a version bump) does not wait two bars for shelves that already
    exist at that watermark. Later new shelves need two consecutive appearances.
    """

    activate_confirms: int = ZONE_TRACK_ACTIVATE_CONFIRMS
    retire_misses: int = ZONE_TRACK_RETIRE_MISSES
    replace_confirms: int = ZONE_TRACK_REPLACE_CONFIRMS
    exchange: str = EXCHANGE
    symbol: str = SYMBOL
    detector_version: str = DETECTOR_VERSION
    tracks: list[ZoneTrack] = field(default_factory=list)
    bootstrapped: bool = False
    last_watermark: int | None = None
    activate_count: int = 0
    retire_count: int = 0
    replace_count: int = 0
    switch_count: int = 0

    # Advance tracks with this watermark's detector candidates and return active published zones.
    def advance(self, candidates: list[dict[str, Any]], *, zone_set_as_of: int) -> list[dict[str, Any]]:
        watermark = int(zone_set_as_of)
        if self.last_watermark is not None and watermark <= self.last_watermark:
            raise ValueError("zone track state cannot move to a past or duplicate watermark")
        snapshot = [dict(zone) for zone in candidates]
        if not self.bootstrapped:
            self._bootstrap(snapshot, watermark)
            return self.active_zones()
        self._advance_from_candidates(snapshot, watermark)
        return self.active_zones()

    # Published active tracks, sorted low to high like the detector support list.
    def active_zones(self) -> list[dict[str, Any]]:
        zones = [dict(track.published) for track in self.tracks if track.status == "active" and track.published is not None]
        return sorted(zones, key=lambda zone: (float(zone["low"]), str(zone.get("fingerprint", ""))))

    # JSON payload stored in live bot_state so the next 4h rebuild continues the same tracks.
    def to_payload(self) -> dict[str, Any]:
        return {
            "bootstrapped": self.bootstrapped,
            "last_watermark": self.last_watermark,
            "activate_count": self.activate_count,
            "retire_count": self.retire_count,
            "replace_count": self.replace_count,
            "switch_count": self.switch_count,
            "tracks": [_track_to_payload(track) for track in self.tracks],
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any] | None,
        *,
        exchange: str = EXCHANGE,
        symbol: str = SYMBOL,
        detector_version: str = DETECTOR_VERSION,
    ) -> ZoneTrackState:
        """Restore tracks from live bot_state. Missing or empty payload starts a fresh bootstrap."""
        state = cls(exchange=exchange, symbol=symbol, detector_version=detector_version)
        if not payload:
            return state
        state.bootstrapped = bool(payload.get("bootstrapped", False))
        raw_watermark = payload.get("last_watermark")
        state.last_watermark = None if raw_watermark is None else int(raw_watermark)
        state.activate_count = int(payload.get("activate_count", 0))
        state.retire_count = int(payload.get("retire_count", 0))
        state.replace_count = int(payload.get("replace_count", 0))
        state.switch_count = int(payload.get("switch_count", 0))
        state.tracks = [_track_from_payload(item) for item in payload.get("tracks") or []]
        return state

    # First snapshot: every candidate is already on the ladder at this watermark, so publish now.
    def _bootstrap(self, candidates: list[dict[str, Any]], watermark: int) -> None:
        self.tracks = []
        for candidate in candidates:
            track = self._new_track(candidate, status="active")
            track.appear_count = self.activate_confirms
            track.published = _publish_zone(candidate, track.track_id)
            self.tracks.append(track)
            self.activate_count += 1
        self.bootstrapped = True
        self.last_watermark = watermark

    # Match, confirm, challenge, and retire using only this snapshot plus prior track state.
    def _advance_from_candidates(self, candidates: list[dict[str, Any]], watermark: int) -> None:
        matches, challengers, leftovers = _assign_candidates(self.tracks, candidates)
        kept: list[ZoneTrack] = []
        matched_ids = set(matches)
        challenged_ids = set(challengers)

        for track in self.tracks:
            track_key = id(track)
            if track_key in matches:
                self._on_match(track, matches[track_key])
                kept.append(track)
                continue
            if track_key in challenged_ids:
                result = self._on_challenger(track, challengers[track_key])
                if result is None:
                    continue
                if result is not track:
                    self.replace_count += 1
                    self.switch_count += 1
                kept.append(result)
                continue
            if self._on_miss(track):
                kept.append(track)

        for candidate in leftovers:
            kept.append(self._new_track(candidate, status="pending"))

        self.tracks = kept
        self.last_watermark = watermark

    # Family match: refresh metadata, and only move bounds after consecutive confirmation.
    def _on_match(self, track: ZoneTrack, candidate: dict[str, Any]) -> None:
        track.last_candidate = dict(candidate)
        track.miss_count = 0
        track.challenger = None
        track.replace_count = 0
        if track.status == "pending":
            track.appear_count += 1
            if track.appear_count >= self.activate_confirms:
                self._activate(track, candidate)
            return
        if track.published is None:
            self._activate(track, candidate)
            return
        if _same_bounds(track.published, candidate):
            track.pending_bounds = None
            track.bounds_confirm_count = 0
            track.published = _overlay_candidate_metadata(track.published, candidate)
            return
        if track.pending_bounds is not None and _same_bounds(track.pending_bounds, candidate):
            track.bounds_confirm_count += 1
            track.pending_bounds = dict(candidate)
            if track.bounds_confirm_count >= self.replace_confirms:
                track.published = _publish_zone(candidate, track.track_id)
                track.pending_bounds = None
                track.bounds_confirm_count = 0
                self.switch_count += 1
            return
        track.pending_bounds = dict(candidate)
        track.bounds_confirm_count = 1

    # A different family occupied the same ladder slot. Replace only after consecutive wins.
    def _on_challenger(self, track: ZoneTrack, candidate: dict[str, Any]) -> ZoneTrack | None:
        track.miss_count += 1
        if track.challenger is not None and _same_challenger(track.challenger, candidate):
            track.replace_count += 1
        else:
            track.challenger = dict(candidate)
            track.replace_count = 1
        if track.replace_count >= self.replace_confirms:
            self.retire_count += 1
            replacement = self._new_track(candidate, status="active")
            replacement.appear_count = self.activate_confirms
            replacement.published = _publish_zone(candidate, replacement.track_id)
            self.activate_count += 1
            return replacement
        if track.miss_count >= self.retire_misses:
            self.retire_count += 1
            return None
        return track if track.status == "active" else None

    # True when the unmatched track should stay in state (still within the miss window).
    def _on_miss(self, track: ZoneTrack) -> bool:
        if track.status != "active":
            return False
        track.miss_count += 1
        track.challenger = None
        track.replace_count = 0
        track.pending_bounds = None
        track.bounds_confirm_count = 0
        if track.miss_count >= self.retire_misses:
            self.retire_count += 1
            return False
        return True

    # Build a track whose id is frozen from this candidate's exact first bounds.
    def _new_track(self, candidate: dict[str, Any], *, status: TrackStatus) -> ZoneTrack:
        source_timeframe = str(candidate.get("source_timeframe", "4h"))
        bounds_style = canonical_bounds_style(candidate.get("bounds_style", "body"))
        track_id = make_zone_track_id(
            low=candidate.get("low"),
            high=candidate.get("high"),
            source_timeframe=source_timeframe,
            bounds_style=bounds_style,
            exchange=self.exchange,
            symbol=self.symbol,
            detector_version=self.detector_version,
        )
        return ZoneTrack(
            track_id=track_id,
            source_timeframe=source_timeframe,
            bounds_style=bounds_style,
            status=status,
            last_candidate=dict(candidate),
            appear_count=1 if status == "pending" else self.activate_confirms,
        )

    # Flip a pending track to active and stamp the stable track id onto the published zone.
    def _activate(self, track: ZoneTrack, candidate: dict[str, Any]) -> None:
        track.status = "active"
        track.published = _publish_zone(candidate, track.track_id)
        track.pending_bounds = None
        track.bounds_confirm_count = 0
        self.activate_count += 1


# Pair current candidates onto existing tracks, then leftover candidates become new pending tracks.
def _assign_candidates(
    tracks: list[ZoneTrack],
    candidates: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], list[dict[str, Any]]]:
    remaining: list[dict[str, Any] | None] = [dict(zone) for zone in candidates]
    matches: dict[int, dict[str, Any]] = {}
    ordered = sorted(tracks, key=lambda track: (0 if track.status == "active" else 1, _track_mid(track)))
    for track in ordered:
        if track.matching_zone is None:
            continue
        best_index: int | None = None
        best_rank: tuple[float, float] | None = None
        for index, candidate in enumerate(remaining):
            if candidate is None or not _same_family(track, candidate):
                continue
            if not _share_slot(track.matching_zone, candidate):
                continue
            rank = (abs(_zone_mid(candidate) - _zone_mid(track.matching_zone)), -_zone_score(candidate))
            if best_rank is None or rank < best_rank:
                best_index = index
                best_rank = rank
        if best_index is None:
            continue
        matched = remaining[best_index]
        if matched is None:
            continue
        matches[id(track)] = matched
        remaining[best_index] = None

    unmatched_active = [
        track
        for track in tracks
        if track.status == "active" and track.published is not None and id(track) not in matches
    ]
    leftovers: list[dict[str, Any]] = []
    challengers: dict[int, dict[str, Any]] = {}
    for candidate in remaining:
        if candidate is None:
            continue
        target = _closest_slot_track(unmatched_active, candidate)
        if target is None:
            leftovers.append(candidate)
            continue
        challengers[id(target)] = candidate
        unmatched_active.remove(target)
    return matches, challengers, leftovers


# True when timeframe and bounds_style match, so this candidate can continue the same track.
def _same_family(track: ZoneTrack, candidate: dict[str, Any]) -> bool:
    source_timeframe = str(candidate.get("source_timeframe", "4h"))
    bounds_style = canonical_bounds_style(candidate.get("bounds_style", "body"))
    return track.source_timeframe == source_timeframe and track.bounds_style == bounds_style


# True when two bands count as one $650-edge / $1000-midpoint ladder step.
def _share_slot(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return _support_zones_share_ladder_slot(first, second)


# Pick the unmatched active track whose published band shares this candidate's slot.
def _closest_slot_track(tracks: list[ZoneTrack], candidate: dict[str, Any]) -> ZoneTrack | None:
    ranked: list[tuple[float, ZoneTrack]] = []
    for track in tracks:
        if track.published is None or not _share_slot(track.published, candidate):
            continue
        ranked.append((abs(_zone_mid(track.published) - _zone_mid(candidate)), track))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


# Copy detector fields onto the sticky published band, then stamp cooldown identity as the track id.
def _publish_zone(candidate: dict[str, Any], track_id: str) -> dict[str, Any]:
    published = dict(candidate)
    published["zone_track_id"] = track_id
    published["fingerprint"] = track_id
    return _with_derived_bounds(published)


# Keep sticky low/high, but take the latest score, origin, touches, and source evidence.
def _overlay_candidate_metadata(published: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    merged = dict(published)
    for key in (
        "origin",
        "score",
        "touches",
        "source_closes",
        "source_indexes",
        "source_open_times",
        "zone_source_time",
        "revision_fingerprint",
        "structure_role",
        "price_state",
        "last_touch_index",
        "broken_index",
        "zone_set_as_of",
    ):
        if key in candidate:
            merged[key] = candidate[key]
    merged["zone_track_id"] = published["zone_track_id"]
    merged["fingerprint"] = published["zone_track_id"]
    return _with_derived_bounds(merged)


# Recompute mid/width from the published edges so a metadata overlay cannot drift the band.
def _with_derived_bounds(zone: dict[str, Any]) -> dict[str, Any]:
    low = float(zone["low"])
    high = float(zone["high"])
    mid = (low + high) / 2.0
    width = high - low
    zone["low"] = low
    zone["high"] = high
    zone["mid"] = mid
    zone["width"] = width
    if mid:
        zone["width_pct"] = float(width / mid * 100.0)
    return zone


# Exact-bound compare using the same price quantum as lineage hashes.
def _same_bounds(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return canonical_price(first["low"]) == canonical_price(second["low"]) and canonical_price(first["high"]) == canonical_price(second["high"])


# Challenger identity includes family, so a body stair and a local band are different replacements.
def _same_challenger(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if not _same_bounds(first, second):
        return False
    if str(first.get("source_timeframe", "4h")) != str(second.get("source_timeframe", "4h")):
        return False
    return canonical_bounds_style(first.get("bounds_style", "body")) == canonical_bounds_style(second.get("bounds_style", "body"))


def _zone_mid(zone: dict[str, Any]) -> float:
    if "mid" in zone:
        return float(zone["mid"])
    return (float(zone["low"]) + float(zone["high"])) / 2.0


def _zone_score(zone: dict[str, Any]) -> float:
    return float(zone.get("score", 0.0))


def _track_mid(track: ZoneTrack) -> float:
    zone = track.matching_zone
    if zone is None:
        return 0.0
    return _zone_mid(zone)


def _track_to_payload(track: ZoneTrack) -> dict[str, Any]:
    return {
        "track_id": track.track_id,
        "source_timeframe": track.source_timeframe,
        "bounds_style": track.bounds_style,
        "status": track.status,
        "published": track.published,
        "last_candidate": track.last_candidate,
        "pending_bounds": track.pending_bounds,
        "challenger": track.challenger,
        "appear_count": track.appear_count,
        "miss_count": track.miss_count,
        "bounds_confirm_count": track.bounds_confirm_count,
        "replace_count": track.replace_count,
    }


def _track_from_payload(payload: dict[str, Any]) -> ZoneTrack:
    return ZoneTrack(
        track_id=str(payload["track_id"]),
        source_timeframe=str(payload.get("source_timeframe", "4h")),
        bounds_style=canonical_bounds_style(payload.get("bounds_style", "body")),
        status="active" if payload.get("status") == "active" else "pending",
        published=dict(payload["published"]) if payload.get("published") else None,
        last_candidate=dict(payload["last_candidate"]) if payload.get("last_candidate") else None,
        pending_bounds=dict(payload["pending_bounds"]) if payload.get("pending_bounds") else None,
        challenger=dict(payload["challenger"]) if payload.get("challenger") else None,
        appear_count=int(payload.get("appear_count", 0)),
        miss_count=int(payload.get("miss_count", 0)),
        bounds_confirm_count=int(payload.get("bounds_confirm_count", 0)),
        replace_count=int(payload.get("replace_count", 0)),
    )
