from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def utc_seconds() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def ms_to_iso(ms: int | None) -> str:
    if ms is None:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


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
