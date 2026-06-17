from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PivotKind = Literal["high", "low"]
SwingTerm = Literal["internal", "external"]
BoundsStyle = Literal["body", "support_floor"] # how the zone edges are anchored

STRUCTURE_ZONE_WIDTH = 500.0
STRUCTURE_MACRO_GAP = 300.0
STRUCTURE_MACRO_MAX_SOURCE_SPAN = 2000.0
STRUCTURE_IMPORTANT_ZONE_SPACING = 1000.0
STRUCTURE_ADJACENT_ZONE_MIN_GAP = 650.0
STRUCTURE_ADJACENT_STRONGER_TOUCH_MARGIN = 3
STRUCTURE_BODY_FLOOR_BRIDGE_MAX_GAP = 1000.0
STRUCTURE_SUPPORT_FLOOR_RETEST_WIDTH_MULT = 0.2 # 20% of zone width
STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP = 4000.0
STRUCTURE_STAIR_STEP_MAX_INSERTIONS = 6


@dataclass
class StructurePivot:
    index: int
    kind: PivotKind
    price: float # wick price
    body_price: float
    atr: float
    term: SwingTerm
    structure_role: str | None = None # H, HH, L, LL, etc.


@dataclass
class SupportCandidate:
    price: float # zone anchor price — body by default; wick when origin is structure_swing_low_wick
    index: int
    origin: str # reasons for candidates creation (structure_swing_low, flipped_resistance, structure_swing_low_wick, structure_swing_low_body_floor)
    structure_role: str
    bounds_style: BoundsStyle = "body"
    broken_index: int | None = None
