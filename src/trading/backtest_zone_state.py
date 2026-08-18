from __future__ import annotations

from typing import Any

import pandas as pd

from ..config import ZoneConfig
from ..zones import IncrementalZoneDetectorError, IncrementalZoneDetectorState


class BacktestIncrementalZoneState:
    """Ingest closed 4h history once and snapshot evidence at later watermarks.

    The first `advance_to` walks every bar from the start of history through that
    watermark. Later calls only feed candles that have not been ingested yet.
    """

    # Capture detector knobs and copy closed 4h history. Constructing state is one full scan.
    def __init__(self, zone_config: ZoneConfig, four_hour_history: pd.DataFrame) -> None:
        self._state = IncrementalZoneDetectorState(zone_config)
        self._candles = _closed_four_hour_mappings(four_hour_history)
        self._next_index = 0
        self.ingested_candles = 0
        self.full_history_scans = 1

    # Ingest remaining closed 4h bars through `watermark`, never skipping ahead.
    def advance_to(self, watermark: int) -> None:
        target = int(watermark)
        while self._next_index < len(self._candles):
            candle = self._candles[self._next_index]
            open_time = int(candle["open_time"])
            if open_time > target:
                break
            self._state.advance(candle)
            self.ingested_candles += 1
            self._next_index += 1
        if self._next_index == 0 or int(self._candles[self._next_index - 1]["open_time"]) != target:
            raise IncrementalZoneDetectorError("4h history does not include the target zone_set_as_of candle")

    # Freeze detector evidence at the current watermark for full materialization.
    def snapshot_evidence(self, zone_set_as_of: int) -> Any:
        return self._state.snapshot_evidence(int(zone_set_as_of))


def _closed_four_hour_mappings(four_hour_history: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert the pre-aggregated 4h frame into closed-candle mappings for incremental ingest."""
    if four_hour_history is None or four_hour_history.empty:
        return []
    ordered = four_hour_history.sort_values("open_time").reset_index(drop=True)
    candles: list[dict[str, Any]] = []
    for row in ordered.itertuples(index=False):
        candles.append(
            {
                "open_time": int(row.open_time),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "is_closed": int(getattr(row, "is_closed", 1)),
            }
        )
    return candles
