from __future__ import annotations

from dataclasses import dataclass

from typing import Literal

import numpy as np
import pandas as pd


PivotKind = Literal["high", "low"]
SwingTerm = Literal["internal", "external"]
BoundsStyle = Literal["body", "support_floor", "local_reaction"] # how the zone edges are anchored

STRUCTURE_ZONE_WIDTH = 500.0
STRUCTURE_PERSISTENT_WICK_MIN_PCT = 2.0 # wick must hang this % of wick price below the body
STRUCTURE_MACRO_GAP = 300.0
STRUCTURE_MACRO_MAX_SOURCE_SPAN = 2000.0
STRUCTURE_IMPORTANT_ZONE_SPACING = 1000.0
STRUCTURE_ADJACENT_ZONE_MIN_GAP = 650.0
STRUCTURE_ADJACENT_STRONGER_TOUCH_MARGIN = 3
STRUCTURE_BODY_FLOOR_BRIDGE_MAX_GAP = 1000.0
STRUCTURE_SUPPORT_FLOOR_RETEST_WIDTH_MULT = 0.2 # 20% of zone width
STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP = 4000.0 # early staircase only; final fill uses zone_width + 2 * $650
STRUCTURE_STAIR_STEP_MAX_INSERTIONS = 6
STRUCTURE_LOCAL_REACTION_LOOKBACK_BARS = 150
STRUCTURE_SPLIT_REJECTION_MAX_RETEST_BARS = 4
STRUCTURE_SPLIT_REJECTION_MIN_WICK_WIDTH_MULT = 2.0
STRUCTURE_SPLIT_REJECTION_MIN_RETEST_WIDTH_MULT = 0.2


@dataclass
class StructurePivot:
    index: int
    kind: PivotKind
    wick_price: float
    body_price: float
    atr: float
    term: SwingTerm
    structure_role: str | None = None # H, HH, L, LL, etc.


@dataclass
class SupportCandidate:
    price: float # zone anchor price — body by default; wick when origin is structure_swing_low_wick
    index: int
    origin: str # reasons for candidates creation
    structure_role: str
    bounds_style: BoundsStyle = "body"
    broken_index: int | None = None


# Snapshot of detector features at one watermark. Materialization reads only this bag.
@dataclass(eq=False)
class ZoneDetectorEvidence:
    ohlc: pd.DataFrame # canonical numeric OHLC view used by rejection overlays
    closes: np.ndarray # close series aligned with ohlc row indexes
    current_price: float # last close, or the caller override captured at extract time
    raw_external_pivots: list[StructurePivot]
    external_pivots: list[StructurePivot] # prominent external subset
    internal_pivots: list[StructurePivot]
    daily_pivots: list[StructurePivot] # prominent daily swings, already structure-labeled
    first_reclaim_indexes: dict[tuple[SwingTerm, int], int] # (term, pivot index) -> first close-through bar
