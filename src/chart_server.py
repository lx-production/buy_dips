from __future__ import annotations

import json
import argparse

from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from typing import Any

from .config import AppConfig, load_config
from .db import load_candles_df
from .utils import resolve_path
from .zones import (
    _average_true_range,
    _coerce_ohlc,
    _find_structure_pivots,
    _label_structure_pivots,
    aggregate_ohlc_to_daily,
    detect_support_resistance_zones,
)


DEFAULT_LIMIT = 400
ALL_CANDLES_LIMIT = "all"
# Display-only: hide cheap historical supports; detector still sees every zone.
VISIBLE_ZONE_MIN = 57000
VISIBLE_SUPPORT_ZONES_ABOVE_PRICE = 2
# Inclusive UTC open_time for the default 4h helper view (2026-06-01 07:00 +07:00).
CHART_VISIBLE_START_MS = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
_INDEX_HTML_PATH = Path(__file__).with_name("chart.html")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a local fullscreen BTCUSDT 4H chart.")
    parser.add_argument("--config", default=None, help="Path to config YAML. Defaults to CONFIG_PATH or config.yaml.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Visible candle limit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    database_path = resolve_path(config.database_path)
    handler = _make_handler(config=config, database_path=database_path, default_limit=args.limit)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving chart at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped chart server.")
    finally:
        server.server_close()
    return 0


def load_chart_payload(
    config: AppConfig,
    database_path: str | Path,
    limit: int | None = DEFAULT_LIMIT,
    timeframe: str | None = None,
    start_ms: int | None = None,
) -> dict[str, Any]:
    selected_timeframe = _normalize_timeframe(timeframe, config.timeframe)
    df = _load_chart_candles_df(
        database_path=database_path,
        config=config,
        selected_timeframe=selected_timeframe,
    )
    zone_df = _load_zone_candles_df(config=config, database_path=database_path)
    if df.empty:
        return {
            "exchange": config.exchange,
            "symbol": config.symbol,
            "timeframe": selected_timeframe,
            "candles": [],
            "zones": {"support": [], "all": []},
            "pivots": [],
            "current_price": None,
            "total_candles": 0,
        }

    current_price = float(df.iloc[-1]["close"])
    zone_config = config.zones
    zones = _detect_chart_zones(df=zone_df, current_price=current_price, zone_config=zone_config)
    support_zones = _visible_support_zones(zones["support"], current_price)
    visible_df, visible_start_index = _visible_candle_frame(df, start_ms=start_ms, limit=limit)
    pivots = _chart_pivots(
        df=df,
        visible_start_index=visible_start_index,
        internal_swing_order=zone_config.internal_swing_order,
        atr_period=zone_config.atr_period,
        show_internal_pivots=zone_config.show_internal_pivots,
    )
    candles = [
        {
            "time": int(row.open_time),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume) if row.volume is not None else None,
        }
        for row in visible_df.itertuples(index=False)
    ]
    return {
        "exchange": config.exchange,
        "symbol": config.symbol,
        "timeframe": selected_timeframe,
        "candles": candles,
        "zones": {"support": support_zones, "all": support_zones},
        "pivots": pivots,
        "current_price": current_price,
        "total_candles": len(df),
    }


def _detect_chart_zones(df: Any, current_price: float, zone_config: Any) -> dict[str, list[dict[str, Any]]]:
    if df.empty:
        return {"support": [], "all": []}
    return detect_support_resistance_zones(
        df,
        min_touches=zone_config.min_touches,
        current_price=current_price,
        buffer_pct=zone_config.role_buffer_pct,
        external_swing_order=zone_config.external_swing_order,
        atr_period=zone_config.atr_period,
        break_atr_mult=zone_config.break_atr_mult,
        near_price_gap_fill_edge_clearance=zone_config.near_price_gap_fill_edge_clearance,
        near_price_gap_fill_midpoint_spacing=zone_config.near_price_gap_fill_midpoint_spacing,
        near_price_gap_fill_min_touches=zone_config.near_price_gap_fill_min_touches,
        external_min_swing_atr_mult=zone_config.external_min_swing_atr_mult,
        external_min_swing_pct=zone_config.external_min_swing_pct,
    )


def _load_zone_candles_df(config: AppConfig, database_path: str | Path) -> Any:
    config_timeframe = _normalize_timeframe(config.timeframe, config.timeframe)
    return load_candles_df(
        database_path=database_path,
        exchange=config.exchange,
        symbol=config.symbol,
        timeframe=config_timeframe,
        only_closed=True,
        limit=None,
    )


def _load_chart_candles_df(config: AppConfig, database_path: str | Path, selected_timeframe: str) -> Any:
    df = load_candles_df(
        database_path=database_path,
        exchange=config.exchange,
        symbol=config.symbol,
        timeframe=selected_timeframe,
        only_closed=True,
        limit=None,
    )
    config_timeframe = _normalize_timeframe(config.timeframe, config.timeframe)
    if not df.empty or selected_timeframe != "1d" or config_timeframe == selected_timeframe:
        return df

    base_df = load_candles_df(
        database_path=database_path,
        exchange=config.exchange,
        symbol=config.symbol,
        timeframe=config_timeframe,
        only_closed=True,
        limit=None,
    )
    return _aggregate_candles_to_daily(base_df)


def _aggregate_candles_to_daily(df: Any) -> Any:
    return aggregate_ohlc_to_daily(df, min_bars_per_day=1)


def _chart_pivots(
    df: Any,
    visible_start_index: int,
    internal_swing_order: int,
    atr_period: int,
    show_internal_pivots: bool,
) -> list[dict[str, Any]]:
    """Build optional debug pivot markers for the chart (internal only; external pivots are not shown)."""
    if not show_internal_pivots:
        return []

    ohlc = _coerce_ohlc(df)
    if ohlc is None:
        return []

    highs = ohlc["high"].to_numpy(dtype=float)
    lows = ohlc["low"].to_numpy(dtype=float)
    closes = ohlc["close"].to_numpy(dtype=float)
    atr = _average_true_range(highs=highs, lows=lows, closes=closes, period=atr_period)
    internal_pivots = _find_structure_pivots(ohlc, internal_swing_order, atr, "internal")
    _label_structure_pivots(internal_pivots)

    time_values = df["open_time"].tolist() if "open_time" in df.columns else list(range(len(ohlc)))
    pivots = []
    for pivot in internal_pivots:
        if pivot.index < visible_start_index:
            continue
        pivots.append(
            {
                "index": int(pivot.index),
                "visible_index": int(pivot.index - visible_start_index),
                "time": int(time_values[pivot.index]),
                "kind": pivot.kind,
                "term": pivot.term,
                "role": pivot.structure_role,
                "wick_price": float(pivot.wick_price),
                "body_price": float(pivot.body_price),
            }
        )
    return sorted(pivots, key=lambda item: (item["visible_index"], item["term"], item["kind"]))


def _visible_support_zones(
    support_zones: list[dict[str, Any]],
    current_price: float,
    min_low: float = VISIBLE_ZONE_MIN,
    above_count: int = VISIBLE_SUPPORT_ZONES_ABOVE_PRICE,
) -> list[dict[str, Any]]:
    """Filter detected zones for chart display only; detection algo stays unchanged."""
    price = float(current_price)
    eligible = [zone for zone in support_zones if float(zone["low"]) > float(min_low)]
    # Keep every eligible support at/below price; only the nearest N above price.
    below_or_touching = sorted(
        [zone for zone in eligible if float(zone["low"]) <= price],
        key=lambda zone: float(zone["low"]),
    )
    above = sorted(
        [zone for zone in eligible if float(zone["low"]) > price],
        key=lambda zone: (float(zone["low"]) - price, -float(zone.get("score", 0.0)), -int(zone["touches"])),
    )
    return below_or_touching + above[: max(0, int(above_count))]


def _visible_candle_frame(df: Any, *, start_ms: int | None, limit: int | None) -> tuple[Any, int]:
    """Slice plotted candles by inclusive start and optional tail limit.

    Zone detection still uses the full closed series; this only chooses what the chart draws.
    """
    visible = df
    if start_ms is not None:
        visible = visible[visible["open_time"].astype("int64") >= int(start_ms)]
    if limit is not None and not visible.empty:
        visible = visible.tail(max(1, int(limit)))
    if visible.empty:
        return visible, len(df)
    first_open = int(visible.iloc[0]["open_time"])
    matches = df.index[df["open_time"].astype("int64") == first_open]
    visible_start_index = int(matches[0]) if len(matches) else 0
    return visible, visible_start_index


def _make_handler(config: AppConfig, database_path: Path, default_limit: int) -> type[BaseHTTPRequestHandler]:
    index_html = _build_index_html(default_limit)

    class ChartHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send_text(index_html, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/chart":
                query = parse_qs(parsed.query)
                limit = _parse_limit(query.get("limit", [str(default_limit)])[0], default_limit)
                timeframe = query.get("timeframe", [config.timeframe])[0]
                payload = load_chart_payload(
                    config=config,
                    database_path=database_path,
                    limit=limit,
                    timeframe=timeframe,
                    start_ms=_parse_start_ms(query.get("start_ms", [""])[0]),
                )
                self._send_json(payload)
                return
            self.send_error(404, "Not found")

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}")

        def _send_json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
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

    return ChartHandler


def _parse_start_ms(raw: str) -> int | None:
    """Parse an optional UTC-ms query bound; ignore empty or invalid values."""
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_limit(raw: str, fallback: int) -> int | None:
    if raw.strip().lower() == ALL_CANDLES_LIMIT:
        return None
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return min(max(value, 50), 2000)


def _normalize_timeframe(raw: str | None, fallback: str) -> str:
    value = (raw or "").strip().lower()
    return value or fallback.strip().lower()


def _build_index_html(default_limit: int) -> str:
    """Load the Lightweight Charts page from disk and inject view placeholders.

    Candle and zone data still come from GET /api/chart, not from this HTML.
    """
    template = _INDEX_HTML_PATH.read_text(encoding="utf-8")
    return template.replace("__LIMIT__", str(default_limit)).replace("__START_MS__", str(CHART_VISIBLE_START_MS))


if __name__ == "__main__":
    raise SystemExit(main())
