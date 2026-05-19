from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd


PivotKind = Literal["high", "low"]
SwingTerm = Literal["internal", "external"]
STRUCTURE_ZONE_WIDTH = 500.0


@dataclass
class StructurePivot:
    index: int
    kind: PivotKind
    price: float
    body_price: float
    atr: float
    term: SwingTerm
    structure_role: str | None = None


def detect_support_resistance_zones(
    df: pd.DataFrame,
    zone_tolerance_pct: float = 0.0045,
    min_touches: int = 2,
    current_price: float | None = None,
    buffer_pct: float = 0.0015,
    internal_swing_order: int = 2,
    external_swing_order: int = 5,
    atr_period: int = 14,
    break_atr_mult: float = 0.2,
    external_min_swing_atr_mult: float = 4.0,
    external_min_swing_pct: float = 2.5,
) -> dict[str, list[dict[str, Any]]]:
    return detect_support_resistance_zones_structure_v1(
        df,
        zone_tolerance_pct=zone_tolerance_pct,
        min_touches=min_touches,
        current_price=current_price,
        buffer_pct=buffer_pct,
        internal_swing_order=internal_swing_order,
        external_swing_order=external_swing_order,
        atr_period=atr_period,
        break_atr_mult=break_atr_mult,
        external_min_swing_atr_mult=external_min_swing_atr_mult,
        external_min_swing_pct=external_min_swing_pct,
    )


def detect_support_resistance_zones_structure_v1(
    df: pd.DataFrame,
    internal_swing_order: int = 2,
    external_swing_order: int = 5,
    atr_period: int = 14,
    external_min_swing_atr_mult: float = 4.0,
    external_min_swing_pct: float = 2.5,
    zone_tolerance_pct: float = 0.0045,
    min_touches: int = 2,
    current_price: float | None = None,
    buffer_pct: float = 0.0015,
    break_atr_mult: float = 0.2,
) -> dict[str, list[dict[str, Any]]]:
    """Detect buy-relevant support zones from repeated prominent swing lows.

    The return shape intentionally keeps the legacy support/resistance/active
    buckets so the DB, chart, and signal code can keep their existing contracts.
    Resistance and active zones are no longer produced.
    """
    _ = internal_swing_order, zone_tolerance_pct, buffer_pct, break_atr_mult
    empty = {"support": [], "resistance": [], "active": [], "all": []}
    ohlc = _coerce_ohlc(df)
    if ohlc is None:
        return empty

    order = max(1, int(external_swing_order))
    if len(ohlc) < (order * 2 + 1):
        return empty

    highs = ohlc["high"].to_numpy(dtype=float)
    lows = ohlc["low"].to_numpy(dtype=float)
    closes = ohlc["close"].to_numpy(dtype=float)
    atr = _average_true_range(highs=highs, lows=lows, closes=closes, period=atr_period)
    if current_price is None:
        current_price = float(closes[-1])

    raw_external_pivots = _find_structure_pivots(ohlc, order, atr, "external")
    external_pivots = _filter_prominent_structure_pivots(
        raw_external_pivots,
        min_swing_atr_mult=external_min_swing_atr_mult,
        min_swing_pct=external_min_swing_pct,
    )
    _label_structure_pivots(external_pivots)

    support_pivots = [pivot for pivot in external_pivots if pivot.kind == "low"]
    support = _build_support_zones_from_swing_lows(
        support_pivots,
        zone_width=STRUCTURE_ZONE_WIDTH,
        min_touches=min_touches,
        current_price=float(current_price),
    )
    support = sorted(support, key=lambda zone: _zone_distance_sort_key(zone, float(current_price)))
    return {"support": support, "resistance": [], "active": [], "all": support}


def _build_support_zones_from_swing_lows(
    pivots: list[StructurePivot],
    zone_width: float,
    min_touches: int,
    current_price: float,
) -> list[dict[str, Any]]:
    clusters: list[list[StructurePivot]] = []
    for pivot in sorted(pivots, key=lambda item: (float(item.body_price), item.index)):
        placed = False
        for cluster in clusters:
            prices = [float(item.body_price) for item in cluster] + [float(pivot.body_price)]
            if max(prices) - min(prices) <= float(zone_width):
                cluster.append(pivot)
                placed = True
                break
        if not placed:
            clusters.append([pivot])

    zones = [
        _support_zone_from_swing_low_cluster(cluster, zone_width)
        for cluster in clusters
        if len({pivot.index for pivot in cluster}) >= max(1, int(min_touches))
    ]
    return [zone for zone in zones if float(zone["low"]) <= float(current_price)]


def _support_zone_from_swing_low_cluster(cluster: list[StructurePivot], zone_width: float) -> dict[str, Any]:
    cluster = sorted(cluster, key=lambda item: (item.index, float(item.body_price)))
    source_closes = [float(pivot.body_price) for pivot in cluster]
    source_indexes = [int(pivot.index) for pivot in cluster]
    low, high = _fixed_structure_zone_bounds(source_closes, zone_width=zone_width)
    mid = (low + high) / 2.0
    width = high - low
    return {
        "origin": "structure_swing_low",
        "role": "support",
        "low": float(low),
        "high": float(high),
        "mid": float(mid),
        "width": float(width),
        "width_pct": float(width / mid * 100.0) if mid else 0.0,
        "touches": len(source_indexes),
        "source_closes": source_closes,
        "source_indexes": source_indexes,
        "score": float(len(source_indexes)),
        "structure_role": _structure_cluster_role(cluster),
        "last_touch_index": max(source_indexes),
        "zone_width": float(zone_width),
    }


def _fixed_structure_zone_bounds(prices: list[float], zone_width: float = STRUCTURE_ZONE_WIDTH) -> tuple[float, float]:
    high = max(float(price) for price in prices)
    return high - float(zone_width), high


def _structure_cluster_role(cluster: list[StructurePivot]) -> str:
    roles = [pivot.structure_role for pivot in cluster if pivot.structure_role]
    if not roles:
        return "unknown"
    if len(set(roles)) == 1:
        return roles[0]
    return "mixed"


def _zone_distance_sort_key(zone: dict[str, Any], current_price: float) -> tuple[float, float, int]:
    low = float(zone["low"])
    high = float(zone["high"])
    price = float(current_price)
    if low <= price <= high:
        distance = 0.0
    elif price < low:
        distance = low - price
    else:
        distance = price - high
    return (distance, -float(zone.get("score", 0.0)), -int(zone["touches"]))


def _coerce_ohlc(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None:
        return None
    required = ["open", "high", "low", "close"]
    if any(column not in df.columns for column in required):
        return None
    ohlc = df[required].apply(pd.to_numeric, errors="coerce").dropna().reset_index(drop=True)
    if ohlc.empty:
        return None
    return ohlc


def _average_true_range(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    if len(closes) == 0:
        return np.array([], dtype=float)
    true_ranges = np.empty(len(closes), dtype=float)
    true_ranges[0] = highs[0] - lows[0]
    for idx in range(1, len(closes)):
        true_ranges[idx] = max(
            highs[idx] - lows[idx],
            abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        )
    return pd.Series(true_ranges).rolling(window=max(1, int(period)), min_periods=1).mean().to_numpy(dtype=float)


def _find_structure_pivots(
    ohlc: pd.DataFrame,
    swing_order: int,
    atr: np.ndarray,
    term: SwingTerm,
) -> list[StructurePivot]:
    order = max(1, int(swing_order))
    if len(ohlc) < (order * 2 + 1):
        return []

    highs = ohlc["high"].to_numpy(dtype=float)
    lows = ohlc["low"].to_numpy(dtype=float)
    opens = ohlc["open"].to_numpy(dtype=float)
    closes = ohlc["close"].to_numpy(dtype=float)
    pivots: list[StructurePivot] = []
    for idx in range(order, len(ohlc) - order):
        high_window = highs[idx - order : idx + order + 1]
        low_window = lows[idx - order : idx + order + 1]
        body_high = max(float(opens[idx]), float(closes[idx]))
        body_low = min(float(opens[idx]), float(closes[idx]))
        if highs[idx] == float(np.max(high_window)) and np.count_nonzero(high_window == highs[idx]) == 1:
            pivots.append(
                StructurePivot(
                    index=idx,
                    kind="high",
                    price=float(highs[idx]),
                    body_price=body_high,
                    atr=float(atr[idx]),
                    term=term,
                )
            )
        if lows[idx] == float(np.min(low_window)) and np.count_nonzero(low_window == lows[idx]) == 1:
            pivots.append(
                StructurePivot(
                    index=idx,
                    kind="low",
                    price=float(lows[idx]),
                    body_price=body_low,
                    atr=float(atr[idx]),
                    term=term,
                )
            )
    return sorted(pivots, key=lambda pivot: (pivot.index, 0 if pivot.kind == "low" else 1))


def _filter_prominent_structure_pivots(
    pivots: list[StructurePivot],
    min_swing_atr_mult: float,
    min_swing_pct: float,
) -> list[StructurePivot]:
    atr_mult = max(0.0, float(min_swing_atr_mult))
    pct = max(0.0, float(min_swing_pct))
    if not pivots or (atr_mult == 0.0 and pct == 0.0):
        return list(pivots)

    prominent: list[StructurePivot] = []
    for pivot in sorted(pivots, key=lambda item: (item.index, 0 if item.kind == "low" else 1)):
        if not prominent:
            prominent.append(pivot)
            continue

        previous = prominent[-1]
        if pivot.kind == previous.kind:
            if _is_more_extreme_structure_pivot(pivot, previous):
                prominent[-1] = pivot
            continue

        min_move = max(
            _structure_pivot_min_move(previous, atr_mult=atr_mult, pct=pct),
            _structure_pivot_min_move(pivot, atr_mult=atr_mult, pct=pct),
        )
        if abs(float(pivot.price) - float(previous.price)) >= min_move:
            prominent.append(pivot)

    return prominent


def _is_more_extreme_structure_pivot(candidate: StructurePivot, current: StructurePivot) -> bool:
    if candidate.kind == "high":
        return candidate.price > current.price
    return candidate.price < current.price


def _structure_pivot_min_move(pivot: StructurePivot, atr_mult: float, pct: float) -> float:
    atr_move = abs(float(pivot.atr)) * atr_mult
    pct_move = abs(float(pivot.price)) * pct / 100.0
    return max(atr_move, pct_move)


def _label_structure_pivots(pivots: list[StructurePivot]) -> None:
    previous_high: float | None = None
    previous_low: float | None = None
    for pivot in pivots:
        if pivot.kind == "high":
            if previous_high is None:
                pivot.structure_role = "H"
            else:
                pivot.structure_role = "HH" if pivot.price > previous_high else "LH"
            previous_high = pivot.price
        else:
            if previous_low is None:
                pivot.structure_role = "L"
            else:
                pivot.structure_role = "HL" if pivot.price > previous_low else "LL"
            previous_low = pivot.price
