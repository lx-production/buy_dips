from __future__ import annotations

import json
import time
import argparse

from pathlib import Path
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from typing import Any

from .elapsed_ticker import ElapsedTicker
from .utils import ms_to_iso, resolve_path
from .config import AppConfig, load_config
from .trading.backtest import BacktestError, backtest_api_payload, parse_backtest_bound, run_backtest


_INDEX_HTML_PATH = Path(__file__).with_name("backtest_chart.html")


def build_parser() -> argparse.ArgumentParser:
    # Mirror the CLI backtest window so the chart always shows the same replay.
    parser = argparse.ArgumentParser(description="Serve a local backtest chart for support_close_v1 BUYs.")
    parser.add_argument("--config", default=None, help="Path to config YAML. Defaults to CONFIG_PATH or config.yaml.")
    parser.add_argument("--start", required=True, help="Inclusive ISO-8601 start on a UTC hour boundary.")
    parser.add_argument("--end", default=None, help="Exclusive ISO-8601 end on a UTC hour boundary.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", type=int, default=8001, help="Bind port.")
    return parser


def main(argv: list[str] | None = None, *, started_at: float | None = None) -> int:
    """Replay once, bind the chart port, and print wall time from process start to ready."""
    # CLI entry may pass started_at from before importing this module (true command cold start).
    if started_at is None:
        started_at = time.perf_counter()
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    database_path = resolve_path(config.database_path)
    try:
        start_ms = parse_backtest_bound(args.start, label="start")
        end_ms = parse_backtest_bound(args.end, label="end") if args.end else None
        end_label = args.end or "latest closed 1h candle"
        print(f"Running backtest {args.start} -> {end_label}...", flush=True)
        with ElapsedTicker(started_at):
            result = run_backtest(config, database_path, start_ms=start_ms, end_ms=end_ms)
            payload = backtest_api_payload(result, config=config)
    except (BacktestError, Exception) as exc:
        print(f"Backtest chart failed to start: {exc}")
        return 2

    handler = _make_handler(payload)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving backtest chart at http://{args.host}:{args.port}")
    print(
        f"Range {ms_to_iso(result.start_ms)} -> {ms_to_iso(result.end_ms)} | "
        f"BUY={result.buy_count} zones={result.zone_snapshot_count} "
        f"cache_hits={result.zone_cache_hit_count} builds={result.zone_rebuild_count} "
        f"ingested={result.zone_state_ingested_candles} scans={result.zone_full_history_scans}"
    )
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped backtest chart server.")
    finally:
        server.server_close()
    return 0


def load_backtest_chart_payload(
    config: AppConfig,
    database_path: str | Path,
    *,
    start_ms: int,
    end_ms: int | None = None,
) -> dict[str, Any]:
    """Helper for tests: run replay and return the chart API payload shape."""
    result = run_backtest(config, database_path, start_ms=start_ms, end_ms=end_ms)
    return backtest_api_payload(result, config=config)


def _make_handler(payload: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    index_html = _build_index_html()
    cached = payload

    class BacktestChartHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send_text(index_html, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/backtest":
                # Cached replay result only — never re-run mid-request and never include HOLD.
                self._send_json(cached)
                return
            self.send_error(404, "Not found")

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}")

        def _send_json(self, body_payload: dict[str, Any]) -> None:
            body = json.dumps(body_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, content_type: str) -> None:
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return BacktestChartHandler


def _build_index_html() -> str:
    # Load the Lightweight Charts page from disk; replay data still comes from GET /api/backtest.
    return _INDEX_HTML_PATH.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
