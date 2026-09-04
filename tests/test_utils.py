from __future__ import annotations

from src.utils import ms_to_utc7


def test_ms_to_utc7_matches_readable_view_format() -> None:
    """Display strings must match SQLite `*_utc7` columns: wall clock plus a fixed +07:00 suffix."""
    assert ms_to_utc7(None) == "n/a"
    assert ms_to_utc7(1000) == "1970-01-01 07:00:01 +07:00"
    # 2026-09-04 00:00:00 UTC is a Binance 4h open_time (the sample journal watermark).
    assert ms_to_utc7(1788480000000) == "2026-09-04 07:00:00 +07:00"
