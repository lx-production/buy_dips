from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import AppConfig, load_config
from .db import load_candles_df
from .utils import resolve_path
from .zones import (
    _average_true_range,
    _coerce_ohlc,
    _filter_prominent_structure_pivots,
    _find_structure_pivots,
    _label_structure_pivots,
    detect_support_resistance_zones,
)


DEFAULT_LIMIT = 500
VISIBLE_SUPPORT_ZONES_ABOVE_PRICE = 2


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


def load_chart_payload(config: AppConfig, database_path: str | Path, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    df = load_candles_df(
        database_path=database_path,
        exchange=config.exchange,
        symbol=config.symbol,
        timeframe=config.timeframe,
        only_closed=True,
        limit=None,
    )
    if df.empty:
        return {
            "exchange": config.exchange,
            "symbol": config.symbol,
            "timeframe": config.timeframe,
            "candles": [],
            "zones": {"support": [], "all": []},
            "pivots": [],
            "current_price": None,
        }

    current_price = float(df.iloc[-1]["close"])
    zone_config = config.zones
    zones = detect_support_resistance_zones(
        df,
        min_touches=zone_config.min_touches,
        current_price=current_price,
        internal_swing_order=zone_config.internal_swing_order,
        external_swing_order=zone_config.external_swing_order,
        atr_period=zone_config.atr_period,
        external_min_swing_atr_mult=zone_config.external_min_swing_atr_mult,
        external_min_swing_pct=zone_config.external_min_swing_pct,
    )
    support_zones = _visible_support_zones(zones["support"], current_price)
    visible_df = df.tail(max(1, int(limit)))
    visible_start_index = max(0, len(df) - len(visible_df))
    pivots = _chart_pivots(
        df=df,
        visible_start_index=visible_start_index,
        internal_swing_order=zone_config.internal_swing_order,
        external_swing_order=zone_config.external_swing_order,
        atr_period=zone_config.atr_period,
        external_min_swing_atr_mult=zone_config.external_min_swing_atr_mult,
        external_min_swing_pct=zone_config.external_min_swing_pct,
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
        "timeframe": config.timeframe,
        "candles": candles,
        "zones": {"support": support_zones, "all": support_zones},
        "pivots": pivots,
        "current_price": current_price,
    }


def _chart_pivots(
    df: Any,
    visible_start_index: int,
    internal_swing_order: int,
    external_swing_order: int,
    atr_period: int,
    external_min_swing_atr_mult: float,
    external_min_swing_pct: float,
    show_internal_pivots: bool,
) -> list[dict[str, Any]]:
    ohlc = _coerce_ohlc(df)
    if ohlc is None:
        return []

    highs = ohlc["high"].to_numpy(dtype=float)
    lows = ohlc["low"].to_numpy(dtype=float)
    closes = ohlc["close"].to_numpy(dtype=float)
    atr = _average_true_range(highs=highs, lows=lows, closes=closes, period=atr_period)
    internal_pivots = _find_structure_pivots(ohlc, internal_swing_order, atr, "internal") if show_internal_pivots else []
    raw_external_pivots = _find_structure_pivots(ohlc, external_swing_order, atr, "external")
    external_pivots = _filter_prominent_structure_pivots(
        raw_external_pivots,
        min_swing_atr_mult=external_min_swing_atr_mult,
        min_swing_pct=external_min_swing_pct,
    )
    _label_structure_pivots(internal_pivots)
    _label_structure_pivots(external_pivots)

    time_values = df["open_time"].tolist() if "open_time" in df.columns else list(range(len(ohlc)))
    pivots = []
    for pivot in [*internal_pivots, *external_pivots]:
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
                "price": float(pivot.price),
                "body_price": float(pivot.body_price),
            }
        )
    return sorted(pivots, key=lambda item: (item["visible_index"], item["term"], item["kind"]))


def _visible_support_zones(
    support_zones: list[dict[str, Any]],
    current_price: float,
    above_count: int = VISIBLE_SUPPORT_ZONES_ABOVE_PRICE,
) -> list[dict[str, Any]]:
    price = float(current_price)
    below_or_touching = [zone for zone in support_zones if float(zone["low"]) <= price]
    above = sorted(
        [zone for zone in support_zones if float(zone["low"]) > price],
        key=lambda zone: (float(zone["low"]) - price, -float(zone.get("score", 0.0)), -int(zone["touches"])),
    )
    return below_or_touching + above[: max(0, int(above_count))]


def _make_handler(config: AppConfig, database_path: Path, default_limit: int) -> type[BaseHTTPRequestHandler]:
    class ChartHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send_text(INDEX_HTML, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/chart":
                query = parse_qs(parsed.query)
                limit = _parse_limit(query.get("limit", [str(default_limit)])[0], default_limit)
                payload = load_chart_payload(config=config, database_path=database_path, limit=limit)
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


def _parse_limit(raw: str, fallback: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return min(max(value, 50), 2000)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BTCUSDT 4H Zones</title>
  <style>
    :root { color-scheme: dark; }
    html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; background: #090c10; color: #e6edf3; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    #app { position: fixed; inset: 0; }
    canvas { display: block; width: 100vw; height: 100vh; }
    .hud { position: fixed; top: 18px; left: 22px; z-index: 2; padding: 12px 14px; border: 1px solid rgba(255,255,255,.08); border-radius: 12px; background: rgba(9,12,16,.72); backdrop-filter: blur(10px); box-shadow: 0 12px 40px rgba(0,0,0,.32); }
    .title { font-size: 15px; font-weight: 700; letter-spacing: .04em; }
    .meta { margin-top: 4px; color: #8b949e; font-size: 12px; }
    .legend { display: flex; gap: 12px; margin-top: 9px; color: #c9d1d9; font-size: 12px; }
    .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }
    .support { background: #2ea043; }
    .internal { background: #79c0ff; }
    .external { background: #d2a8ff; }
    .controls { display: flex; gap: 12px; margin-top: 10px; color: #c9d1d9; font-size: 12px; }
    .toggle { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; }
    .toggle input { width: 14px; height: 14px; margin: 0; accent-color: #d2a8ff; }
    .error { position: fixed; inset: auto 22px 22px 22px; padding: 12px 14px; border-radius: 10px; color: #ffdcd7; background: rgba(248,81,73,.14); border: 1px solid rgba(248,81,73,.35); font-size: 13px; display: none; }
  </style>
</head>
<body>
  <div id="app"><canvas id="chart"></canvas></div>
  <div class="hud">
    <div class="title" id="title">BTCUSDT 4H</div>
    <div class="meta" id="meta">Loading SQLite candles and zones…</div>
    <div class="legend">
      <span><i class="dot support"></i>Support</span>
      <span><i class="dot external"></i>Prominent external pivots</span>
    </div>
    <div class="controls">
      <label class="toggle" title="Show or hide prominent external swing points">
        <input type="checkbox" id="toggle-external-pivots" checked>
        External pivots
      </label>
    </div>
  </div>
  <div class="error" id="error"></div>
  <script>
    const canvas = document.getElementById('chart');
    const ctx = canvas.getContext('2d');
    const title = document.getElementById('title');
    const meta = document.getElementById('meta');
    const error = document.getElementById('error');
    const toggleExternalPivots = document.getElementById('toggle-external-pivots');
    let chartData = null;

    async function load() {
      const response = await fetch('/api/chart?limit=600');
      if (!response.ok) throw new Error(`Chart API failed: ${response.status}`);
      chartData = await response.json();
      title.textContent = `${chartData.symbol} ${chartData.timeframe.toUpperCase()}`;
      meta.textContent = `${chartData.candles.length} closed candles • last close ${formatPrice(chartData.current_price)}`;
      draw();
    }

    function resize() {
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.floor(window.innerWidth * ratio);
      canvas.height = Math.floor(window.innerHeight * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      draw();
    }

    function draw() {
      const width = window.innerWidth;
      const height = window.innerHeight;
      ctx.clearRect(0, 0, width, height);
      drawBackground(width, height);
      if (!chartData || chartData.candles.length === 0) {
        drawCentered('No closed candles found in SQLite.');
        return;
      }
      const candles = chartData.candles;
      const zones = chartData.zones.support || [];
      const prices = candles.flatMap(candle => [candle.high, candle.low]).concat(zones.flatMap(zone => [zone.low, zone.high]));
      const minPrice = Math.min(...prices);
      const maxPrice = Math.max(...prices);
      const padding = Math.max((maxPrice - minPrice) * 0.08, 1);
      const scale = {
        left: 56,
        right: 92,
        top: 38,
        bottom: 44,
        min: minPrice - padding,
        max: maxPrice + padding,
        plotWidth: width - 148,
        plotHeight: height - 82
      };
      drawGrid(width, height, scale);
      drawZones(zones, width, scale);
      drawCandles(candles, scale);
      drawPivots(visiblePivots(chartData.pivots || []), candles, scale);
      drawPriceAxis(scale, width);
      drawTimeAxis(candles, scale, height);
    }

    function visiblePivots(pivots) {
      if (toggleExternalPivots.checked) return pivots;
      return pivots.filter(pivot => pivot.term !== 'external');
    }

    function yFor(price, scale) {
      return scale.top + ((scale.max - price) / (scale.max - scale.min)) * scale.plotHeight;
    }

    function drawBackground(width, height) {
      const gradient = ctx.createLinearGradient(0, 0, 0, height);
      gradient.addColorStop(0, '#0d1117');
      gradient.addColorStop(1, '#05070a');
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, width, height);
    }

    function drawGrid(width, height, scale) {
      ctx.strokeStyle = 'rgba(139,148,158,.15)';
      ctx.lineWidth = 1;
      for (let i = 0; i <= 6; i++) {
        const y = scale.top + (scale.plotHeight / 6) * i;
        ctx.beginPath();
        ctx.moveTo(scale.left, y);
        ctx.lineTo(width - scale.right, y);
        ctx.stroke();
      }
      for (let i = 0; i <= 8; i++) {
        const x = scale.left + (scale.plotWidth / 8) * i;
        ctx.beginPath();
        ctx.moveTo(x, scale.top);
        ctx.lineTo(x, height - scale.bottom);
        ctx.stroke();
      }
    }

    function drawZones(zones, width, scale) {
      for (const zone of zones) {
        const top = yFor(zone.high, scale);
        const bottom = yFor(zone.low, scale);
        const color = '46,160,67';
        ctx.fillStyle = `rgba(${color}, .16)`;
        ctx.strokeStyle = `rgba(${color}, .55)`;
        ctx.fillRect(scale.left, top, scale.plotWidth, Math.max(bottom - top, 2));
        ctx.strokeRect(scale.left, top, scale.plotWidth, Math.max(bottom - top, 2));
        ctx.fillStyle = `rgba(${color}, .95)`;
        ctx.font = '12px ui-sans-serif, system-ui';
        ctx.textAlign = 'left';
        ctx.fillText(`support ${formatPrice(zone.low)}-${formatPrice(zone.high)} (${zone.touches})`, scale.left + 8, top - 5);
      }
    }

    function drawCandles(candles, scale) {
      const step = scale.plotWidth / candles.length;
      const bodyWidth = Math.max(2, Math.min(12, step * 0.62));
      candles.forEach((candle, index) => {
        const x = scale.left + step * index + step / 2;
        const openY = yFor(candle.open, scale);
        const closeY = yFor(candle.close, scale);
        const highY = yFor(candle.high, scale);
        const lowY = yFor(candle.low, scale);
        const up = candle.close >= candle.open;
        ctx.strokeStyle = up ? '#3fb950' : '#ff7b72';
        ctx.fillStyle = up ? '#3fb950' : '#ff7b72';
        ctx.beginPath();
        ctx.moveTo(x, highY);
        ctx.lineTo(x, lowY);
        ctx.stroke();
        const bodyTop = Math.min(openY, closeY);
        const bodyHeight = Math.max(Math.abs(closeY - openY), 1);
        ctx.fillRect(x - bodyWidth / 2, bodyTop, bodyWidth, bodyHeight);
      });
    }

    function drawPivots(pivots, candles, scale) {
      if (!pivots.length || !candles.length) return;
      const step = scale.plotWidth / candles.length;
      for (const pivot of pivots) {
        const visibleIndex = Number(pivot.visible_index);
        if (!Number.isFinite(visibleIndex) || visibleIndex < 0 || visibleIndex >= candles.length) continue;

        const x = scale.left + step * visibleIndex + step / 2;
        const isHigh = pivot.kind === 'high';
        const isExternal = pivot.term === 'external';
        const priceY = yFor(pivot.price, scale);
        const markerY = isHigh ? priceY - 7 : priceY + 7;
        const labelY = isHigh
          ? priceY - (isExternal ? 31 : 18)
          : priceY + (isExternal ? 39 : 26);
        const label = `${isExternal ? 'external' : 'internal'} ${pivot.role || (isHigh ? 'H' : 'L')}`;
        const color = isExternal ? '#d2a8ff' : '#79c0ff';
        const fill = isExternal ? 'rgba(210,168,255,.16)' : 'rgba(121,192,255,.16)';

        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = isExternal ? 1.6 : 1;
        ctx.beginPath();
        if (isHigh) {
          ctx.moveTo(x, priceY - 2);
          ctx.lineTo(x - 4, markerY);
          ctx.lineTo(x + 4, markerY);
        } else {
          ctx.moveTo(x, priceY + 2);
          ctx.lineTo(x - 4, markerY);
          ctx.lineTo(x + 4, markerY);
        }
        ctx.closePath();
        ctx.stroke();

        ctx.font = isExternal ? '700 11px ui-sans-serif, system-ui' : '10px ui-sans-serif, system-ui';
        const metrics = ctx.measureText(label);
        const padX = 4;
        const boxWidth = metrics.width + padX * 2;
        const boxHeight = isExternal ? 16 : 14;
        const boxX = Math.max(scale.left, Math.min(x - boxWidth / 2, scale.left + scale.plotWidth - boxWidth));
        const boxY = Math.max(scale.top, Math.min(labelY - boxHeight / 2, scale.top + scale.plotHeight - boxHeight));
        ctx.fillStyle = 'rgba(9,12,16,.78)';
        ctx.fillRect(boxX, boxY, boxWidth, boxHeight);
        ctx.strokeStyle = fill;
        ctx.strokeRect(boxX, boxY, boxWidth, boxHeight);
        ctx.fillStyle = color;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(label, boxX + boxWidth / 2, boxY + boxHeight / 2);
        ctx.textBaseline = 'alphabetic';
      }
    }

    function drawPriceAxis(scale, width) {
      ctx.fillStyle = '#8b949e';
      ctx.font = '12px ui-sans-serif, system-ui';
      ctx.textAlign = 'left';
      for (let i = 0; i <= 6; i++) {
        const price = scale.max - ((scale.max - scale.min) / 6) * i;
        ctx.fillText(formatPrice(price), width - scale.right + 12, yFor(price, scale) + 4);
      }
    }

    function drawTimeAxis(candles, scale, height) {
      ctx.fillStyle = '#8b949e';
      ctx.font = '12px ui-sans-serif, system-ui';
      ctx.textAlign = 'center';
      for (let i = 0; i <= 4; i++) {
        const index = Math.min(candles.length - 1, Math.floor((candles.length - 1) * (i / 4)));
        const x = scale.left + scale.plotWidth * (i / 4);
        ctx.fillText(formatDate(candles[index].time), x, height - 18);
      }
    }

    function drawCentered(message) {
      ctx.fillStyle = '#8b949e';
      ctx.font = '16px ui-sans-serif, system-ui';
      ctx.textAlign = 'center';
      ctx.fillText(message, window.innerWidth / 2, window.innerHeight / 2);
    }

    function formatPrice(value) {
      if (value === null || value === undefined) return 'n/a';
      return Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
    }

    function formatDate(value) {
      return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }

    window.addEventListener('resize', resize);
    toggleExternalPivots.addEventListener('change', draw);
    resize();
    load().catch((err) => {
      error.style.display = 'block';
      error.textContent = err.message;
      drawCentered('Unable to load chart data.');
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
