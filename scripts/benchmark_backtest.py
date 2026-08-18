from __future__ import annotations

import sys
import json
import time
import sqlite3
import argparse
import tempfile

from pathlib import Path

from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

from src.db import connect  # noqa: E402
from src.config import AppConfig  # noqa: E402
from src.utils import ms_to_iso  # noqa: E402
from src.trading.backtest import parse_backtest_bound, run_backtest  # noqa: E402

DEFAULT_DATABASE = PROJECT_ROOT / "data" / "prana_buy_the_dips.sqlite"
DEFAULT_START = "2026-06-01T00:00:00+00:00"
DEFAULT_END = "2026-08-13T06:00:00+00:00"


# Copy the source SQLite file into a temp dir without opening .env, wallet, or logs.
def copy_source_database(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Source database does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dest_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()


# Drop copied cache rows so the first replay on the temp DB is a true cold run.
def clear_backtest_zone_cache(database_path: Path) -> None:
    with connect(database_path) as conn:
        conn.execute("DELETE FROM backtest_zone_cache")
        conn.commit()


# Load zone/strategy YAML only. Never call load_config(), which reads .env.
def load_benchmark_config(config_path: Path | None, database_path: Path) -> AppConfig:
    payload: dict[str, Any] = {}
    if config_path is not None:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file must contain a mapping: {config_path}")
        payload = loaded
    payload["database_path"] = str(database_path)
    return AppConfig.model_validate(payload)


# Run one isolated replay and return timing plus the cache counters the CLI already prints.
def run_timed_backtest(config: AppConfig, database_path: Path, start_ms: int, end_ms: int) -> dict[str, Any]:
    started = time.perf_counter()
    result = run_backtest(config, database_path, start_ms=start_ms, end_ms=end_ms)
    elapsed = time.perf_counter() - started
    return {
        "elapsed_seconds": round(elapsed, 3),
        "evaluated_candles": result.evaluated_candles,
        "zone_snapshot_count": result.zone_snapshot_count,
        "zone_rebuild_count": result.zone_rebuild_count,
        "zone_cache_hit_count": result.zone_cache_hit_count,
        "buy_count": result.buy_count,
    }


# Build the stable JSON/text report for one cold+warm pair on the same temp copy.
def build_benchmark_report(
    *,
    source_database: Path,
    start_ms: int,
    end_ms: int,
    cold: dict[str, Any],
    warm: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "source_database": str(source_database),
        "range_start": ms_to_iso(start_ms),
        "range_end": ms_to_iso(end_ms),
        "evaluated_candles": cold["evaluated_candles"],
        "zone_snapshot_count": cold["zone_snapshot_count"],
        "buy_count": cold["buy_count"],
        "cold": {
            "elapsed_seconds": cold["elapsed_seconds"],
            "zone_rebuild_count": cold["zone_rebuild_count"],
            "zone_cache_hit_count": cold["zone_cache_hit_count"],
        },
        "warm": None
        if warm is None
        else {
            "elapsed_seconds": warm["elapsed_seconds"],
            "zone_rebuild_count": warm["zone_rebuild_count"],
            "zone_cache_hit_count": warm["zone_cache_hit_count"],
        },
    }


def format_benchmark_report(report: dict[str, Any]) -> str:
    # Keep a short human-readable block above the JSON object.
    warm = report.get("warm") or {}
    lines = [
        "Backtest zone benchmark",
        f"source_database: {report['source_database']}",
        f"range: {report['range_start']} -> {report['range_end']} (end exclusive)",
        f"evaluated_candles: {report['evaluated_candles']}",
        f"zone_snapshot_count: {report['zone_snapshot_count']}",
        f"buy_count: {report['buy_count']}",
        (
            "cold: "
            f"elapsed_seconds={report['cold']['elapsed_seconds']} "
            f"rebuilds={report['cold']['zone_rebuild_count']} "
            f"hits={report['cold']['zone_cache_hit_count']}"
        ),
    ]
    if warm:
        lines.append(
            "warm: "
            f"elapsed_seconds={warm['elapsed_seconds']} "
            f"rebuilds={warm['zone_rebuild_count']} "
            f"hits={warm['zone_cache_hit_count']}"
        )
    lines.append(json.dumps(report, indent=2, sort_keys=True))
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    # Keep flags local to the benchmark so `src.cli` / load_config() are never imported.
    parser = argparse.ArgumentParser(
        description="Benchmark cold/warm backtest zone snapshots on a temporary database copy."
    )
    parser.add_argument(
        "--database",
        default=str(DEFAULT_DATABASE),
        help="Source SQLite path to copy. The original cache is never written.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional YAML with zone/strategy settings. Does not load .env.",
    )
    parser.add_argument("--start", default=DEFAULT_START, help="Inclusive ISO-8601 start with timezone")
    parser.add_argument("--end", default=DEFAULT_END, help="Exclusive ISO-8601 end with timezone")
    parser.add_argument("--json", dest="json_path", default=None, help="Write the JSON report to this path")
    parser.add_argument("--skip-warm", action="store_true", help="Run only the cold pass on the temp copy")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Isolate all cache writes on a temp copy so production backtest_zone_cache stays untouched.
    parser = build_parser()
    args = parser.parse_args(argv)
    source = Path(args.database).expanduser()
    if not source.is_absolute():
        source = (PROJECT_ROOT / source).resolve()
    else:
        source = source.resolve()
    config_path = Path(args.config).expanduser().resolve() if args.config else None
    if config_path is not None and not config_path.is_file():
        print(f"Config file does not exist: {config_path}", file=sys.stderr)
        return 2

    try:
        start_ms = parse_backtest_bound(args.start, label="start")
        end_ms = parse_backtest_bound(args.end, label="end")
        with tempfile.TemporaryDirectory(prefix="backtest-zone-bench-") as tmp:
            temp_db = Path(tmp) / "benchmark.sqlite"
            copy_source_database(source, temp_db)
            clear_backtest_zone_cache(temp_db)
            config = load_benchmark_config(config_path, temp_db)
            cold = run_timed_backtest(config, temp_db, start_ms, end_ms)
            warm = None if args.skip_warm else run_timed_backtest(config, temp_db, start_ms, end_ms)
            report = build_benchmark_report(
                source_database=source,
                start_ms=start_ms,
                end_ms=end_ms,
                cold=cold,
                warm=warm,
            )
    except Exception as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 2

    text = format_benchmark_report(report)
    sys.stdout.write(text)
    if args.json_path:
        json_path = Path(args.json_path).expanduser()
        if not json_path.is_absolute():
            json_path = (Path.cwd() / json_path).resolve()
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
