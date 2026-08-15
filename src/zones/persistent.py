from __future__ import annotations

from .types import StructurePivot
from .factory import Zone, _make_support_zone

PERSISTENT_WICK_FLOOR_ORIGIN = "persistent_wick_floor"


# Pin a fixed $500 wick-floor from each confirmed local swing-low with a long wick.
def _build_persistent_wick_floor_zones(
    raw_external_pivots: list[StructurePivot],
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> list[Zone]:
    """Create lasting wick-floor shelves from closed 4H swing lows.

    Rebuilds are stateless, so this uses raw local swing lows rather than the
    current prominent set. A later deeper low must not erase an earlier shelf.
    Bounds freeze at first print: low = wick, high = wick + zone_width. One
    touch is enough; later overlays keep the oldest overlapping floor.
    """
    width = float(zone_width)
    zones: list[Zone] = []
    for pivot in sorted(raw_external_pivots, key=lambda item: item.index):
        if pivot.kind != "low":
            continue
        wick = float(pivot.wick_price)
        body_low = float(pivot.body_price)
        # Skip ordinary candles; only pin dumps whose wick hangs at least one zone width below the body.
        if body_low - wick < width:
            continue
        zones.append(
            _make_support_zone(
                origin=PERSISTENT_WICK_FLOOR_ORIGIN,
                bounds_style="support_floor",
                low=wick,
                high=wick + width,
                width=width,
                touches=1,
                source_closes=[wick],
                source_indexes=[int(pivot.index)],
                score=2.0,
                structure_role=pivot.structure_role or "L",
                broken_index=None,
                zone_width=width,
                current_price=current_price,
                buffer_pct=buffer_pct,
            )
        )
    return zones


# Insert pinned wick floors after merge/daily so those steps cannot absorb them.
def _overlay_persistent_wick_floors(zones: list[Zone], persistent_zones: list[Zone]) -> list[Zone]:
    """Keep the oldest overlapping persistent floor and drop any swing band it covers.

    Persistent floors are not clustered, macro-merged, or replaced by daily
    overlay. A later dump that prints a nearby wick (for example 59130 vs 59005)
    must not move the original shelf.
    """
    kept_persistent: list[Zone] = []
    for zone in sorted(persistent_zones, key=_persistent_floor_age):
        if any(_zones_overlap(zone, previous) for previous in kept_persistent):
            continue
        kept_persistent.append(dict(zone))

    selected = [
        dict(zone)
        for zone in zones
        if str(zone.get("origin")) != PERSISTENT_WICK_FLOOR_ORIGIN
        and not any(_zones_overlap(zone, pinned) for pinned in kept_persistent)
    ]
    selected.extend(kept_persistent)
    return sorted(selected, key=lambda zone: float(zone["low"]))


# Older source candle wins when two persistent floors overlap.
def _persistent_floor_age(zone: Zone) -> tuple[int, float]:
    indexes = [int(index) for index in zone.get("source_indexes") or []]
    first_index = min(indexes) if indexes else 0
    return (first_index, float(zone["low"]))


# True when two zones share any price range between their low and high bounds.
def _zones_overlap(first: Zone, second: Zone) -> bool:
    return max(float(first["low"]), float(second["low"])) <= min(float(first["high"]), float(second["high"]))
