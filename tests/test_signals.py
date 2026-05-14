from __future__ import annotations

import pandas as pd

from src.config import AppConfig
from src.signals import generate_buy_the_dips_signal


def _df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "close": closes,
            "close_time": [1_700_000_000_000 + i * 14_400_000 for i in range(len(closes))],
            "is_closed": [1] * len(closes),
        }
    )


def test_signal_scores_near_support() -> None:
    zones = {
        "support": [
            {
                "origin": "support_pivot",
                "role": "support",
                "low": 99.5,
                "high": 100.0,
                "mid": 99.75,
                "width": 0.5,
                "width_pct": 0.5,
                "touches": 3,
                "source_closes": [99.5, 100.0],
                "source_indexes": [1, 2],
            }
        ],
        "resistance": [],
        "active": [],
        "all": [],
    }

    signal = generate_buy_the_dips_signal(_df([106, 105, 103, 100.2]), zones, AppConfig())

    assert signal["signal_score"] >= 55
    assert signal["decision"] == "ALERT_ONLY"
    assert signal["distance_to_support_pct"] <= 0.25


def test_hold_generated_when_far_from_support() -> None:
    zones = {
        "support": [
            {
                "origin": "support_pivot",
                "role": "support",
                "low": 90.0,
                "high": 91.0,
                "mid": 90.5,
                "width": 1.0,
                "width_pct": 1.1,
                "touches": 2,
                "source_closes": [90.0, 91.0],
                "source_indexes": [1, 2],
            }
        ],
        "resistance": [],
        "active": [],
        "all": [],
    }

    signal = generate_buy_the_dips_signal(_df([100, 101, 102, 103]), zones, AppConfig())

    assert signal["decision"] == "HOLD"
    assert signal["signal_score"] < 50
    assert "HOLD" in signal["reason"]
