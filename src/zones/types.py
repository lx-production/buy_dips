from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PivotKind = Literal["high", "low"]
SwingTerm = Literal["internal", "external"]
BoundsStyle = Literal["body", "support_floor"]

STRUCTURE_ZONE_WIDTH = 500.0
STRUCTURE_MACRO_GAP = 300.0
STRUCTURE_MACRO_MAX_SOURCE_SPAN = 2000.0
STRUCTURE_IMPORTANT_ZONE_SPACING = 1000.0
STRUCTURE_SUPPORT_FLOOR_RETEST_WIDTH_MULT = 0.2
STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP = 4000.0
STRUCTURE_STAIR_STEP_MAX_INSERTIONS = 6


@dataclass
class StructurePivot:
    index: int
    kind: PivotKind
    price: float
    body_price: float
    atr: float
    term: SwingTerm
    structure_role: str | None = None


@dataclass
class SupportCandidate:
    price: float
    index: int
    origin: str # reason for candidate creation
    structure_role: str
    bounds_style: BoundsStyle = "body"
    broken_index: int | None = None
