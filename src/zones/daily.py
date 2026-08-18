from __future__ import annotations

from typing import Any

from .factory import Zone, _make_support_zone
from .timeframes import aggregate_ohlc_to_daily
from .ohlc import _average_true_range, _coerce_ohlc
from .types import STRUCTURE_ADJACENT_ZONE_MIN_GAP, StructurePivot
from .pivots import _filter_prominent_structure_pivots, _find_structure_pivots, _label_structure_pivots


DAILY_ZONE_MIN_BARS_PER_DAY = 6
DAILY_ZONE_SCORE_BONUS = 100.0


# Find prominent daily swing pivots from completed UTC days. Zone dicts are built later.
def _extract_daily_structure_pivots(
    df: Any,
    *,
    external_swing_order: int,
    atr_period: int,
    external_min_swing_atr_mult: float,
    external_min_swing_pct: float,
) -> list[StructurePivot]:
    daily_df = aggregate_ohlc_to_daily(df, min_bars_per_day=DAILY_ZONE_MIN_BARS_PER_DAY)
    ohlc = _coerce_ohlc(daily_df)
    if ohlc is None:
        return []

    bars_each_side = max(1, int(external_swing_order))
    if len(ohlc) < (bars_each_side * 2 + 1):
        return []

    highs = ohlc["high"].to_numpy(dtype=float)
    lows = ohlc["low"].to_numpy(dtype=float)
    closes = ohlc["close"].to_numpy(dtype=float)
    atr = _average_true_range(highs=highs, lows=lows, closes=closes, period=atr_period)
    raw_daily_pivots = _find_structure_pivots(ohlc, bars_each_side, atr, "external")
    daily_pivots = _filter_prominent_structure_pivots(
        raw_daily_pivots,
        min_swing_atr_mult=external_min_swing_atr_mult,
        min_swing_pct=external_min_swing_pct,
    )
    _label_structure_pivots(daily_pivots)
    return daily_pivots


# Turn extracted daily low pivots into fixed-width body-support zones.
def _daily_body_support_zones_from_pivots(
    daily_pivots: list[StructurePivot],
    *,
    zone_width: float,
    current_price: float,
    buffer_pct: float,
) -> list[Zone]:
    zones: list[Zone] = []
    for pivot in daily_pivots:
        if pivot.kind != "low":
            continue

        high = float(pivot.body_price)
        low = high - float(zone_width)
        zone = _make_support_zone(
            origin="daily_body_support",
            bounds_style="body",
            low=low,
            high=high,
            width=float(zone_width),
            touches=1,
            source_closes=[high],
            source_indexes=[int(pivot.index)],
            score=DAILY_ZONE_SCORE_BONUS + 2.0,
            structure_role=pivot.structure_role or "L",
            broken_index=None,
            zone_width=zone_width,
            current_price=current_price,
            buffer_pct=buffer_pct,
        )
        zone["source_timeframe"] = "1d"
        zones.append(zone)

    return zones


# Build body-style support zones from daily swing lows after aggregating intraday OHLC.
def _build_daily_body_support_zones(
    df: Any,
    *,
    zone_width: float,
    current_price: float,
    buffer_pct: float,
    external_swing_order: int,
    atr_period: int,
    external_min_swing_atr_mult: float,
    external_min_swing_pct: float,
) -> list[Zone]:
    daily_pivots = _extract_daily_structure_pivots(
        df,
        external_swing_order=external_swing_order,
        atr_period=atr_period,
        external_min_swing_atr_mult=external_min_swing_atr_mult,
        external_min_swing_pct=external_min_swing_pct,
    )
    return _daily_body_support_zones_from_pivots(
        daily_pivots,
        zone_width=zone_width,
        current_price=current_price,
        buffer_pct=buffer_pct,
    )


# Merge daily zones into the main list, replacing overlapping zones that daily can supersede
# and a flipped-resistance body zone immediately above the daily support.
def _overlay_daily_support_zones(zones: list[Zone], daily_zones: list[Zone]) -> list[Zone]:
    if not daily_zones:
        return [dict(zone) for zone in zones]

    selected = [dict(zone) for zone in zones]
    for daily_zone in sorted(daily_zones, key=lambda zone: (float(zone["low"]), float(zone["high"]))):
        overlaps = [index for index, zone in enumerate(selected) if _zones_overlap(zone, daily_zone)]
        if not overlaps:
            adjacent_upper = _nearest_replaceable_upper_zone(selected, daily_zone)
            if adjacent_upper is not None:
                selected.pop(adjacent_upper)
            selected.append(dict(daily_zone))
            continue

        if any(not _daily_zone_can_replace(selected[index]) for index in overlaps):
            continue

        selected = [zone for index, zone in enumerate(selected) if index not in overlaps]
        selected.append(dict(daily_zone))

    return sorted(selected, key=lambda zone: float(zone["low"]))


# Find the nearest 4H flipped-resistance body band when it crowds a daily support from above.
def _nearest_replaceable_upper_zone(zones: list[Zone], daily_zone: Zone) -> int | None:
    nearest: tuple[float, int] | None = None
    daily_high = float(daily_zone["high"])
    for index, zone in enumerate(zones):
        gap = float(zone["low"]) - daily_high
        if gap < 0.0 or gap >= STRUCTURE_ADJACENT_ZONE_MIN_GAP:
            continue
        if zone.get("bounds_style", "body") != "body" or str(zone.get("origin")) != "flipped_resistance":
            continue
        candidate = (gap, index)
        if nearest is None or candidate < nearest:
            nearest = candidate
    return None if nearest is None else nearest[1]


# True when a daily overlay may replace this zone (daily, mixed_structure; not local_reaction or pinned wick floors).
def _daily_zone_can_replace(zone: Zone) -> bool:
    if zone.get("source_timeframe") == "1d":
        return True
    if zone.get("bounds_style") == "local_reaction":
        return False
    if str(zone.get("origin")) == "persistent_wick_floor":
        return False
    return str(zone.get("origin")) == "mixed_structure"


# True when two zones share any price range between their low and high bounds.
def _zones_overlap(first: Zone, second: Zone) -> bool:
    return max(float(first["low"]), float(second["low"])) <= min(float(first["high"]), float(second["high"]))
