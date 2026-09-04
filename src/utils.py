from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone

from typing import Any

UTC_PLUS_7 = timezone(timedelta(hours=7))


def utc_ms() -> int:
    """Return the current UTC time as Unix milliseconds."""
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def utc_seconds() -> int:
    """Return the current UTC time as Unix seconds."""
    return int(datetime.now(tz=timezone.utc).timestamp())


def ms_to_iso(ms: int | None) -> str:
    """Format Unix milliseconds as an ISO-8601 UTC timestamp, or n/a when missing."""
    if ms is None:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def ms_to_utc7(ms: int | None) -> str:
    """Format Unix milliseconds as a UTC+7 display string matching `*_readable` views."""
    if ms is None:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(UTC_PLUS_7).strftime("%Y-%m-%d %H:%M:%S +07:00")


def resolve_path(path: str | Path, base_dir: str | Path | None = None) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    root = Path(base_dir) if base_dir is not None else Path.cwd()
    return (root / candidate).resolve()


def json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    return value
