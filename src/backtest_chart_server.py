from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig, load_config
from .trading.backtest import BacktestError, backtest_api_payload, parse_backtest_bound, run_backtest
from .utils import ms_to_iso, resolve_path


def build_parser() -> argparse.ArgumentParser:
    # Mirror the CLI backtest window so the chart always shows the same replay.
    parser = argparse.ArgumentParser(description="Serve a local backtest chart for support_close_v1 BUYs.")
    parser.add_argument("--config", default=None, help="Path to config YAML. Defaults to CONFIG_PATH or config.yaml.")
    parser.add_argument("--start", required=True, help="Inclusive ISO-8601 start on a UTC hour boundary.")
    parser.add_argument("--end", default=None, help="Exclusive ISO-8601 end on a UTC hour boundary.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", type=int, default=8001, help="Bind port.")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Run the offline replay once up front; refuse to bind if candle/zone data is invalid.
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    database_path = resolve_path(config.database_path)
    try:
        start_ms = parse_backtest_bound(args.start, label="start")
        end_ms = parse_backtest_bound(args.end, label="end") if args.end else None
        end_label = args.end or "latest closed 1h candle"
        print(f"Running backtest {args.start} -> {end_label}...", flush=True)
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
        f"cache_hits={result.zone_cache_hit_count} builds={result.zone_rebuild_count}"
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
    return _INDEX_HTML


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Backtest BUY Chart</title>
  <style>
    :root { color-scheme: dark; }
    html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; background: #090c10; color: #e6edf3; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    #app { position: fixed; inset: 0; }
    canvas { display: block; width: 100vw; height: 100vh; cursor: crosshair; }
    .hud { position: fixed; top: 18px; left: 22px; z-index: 2; padding: 12px 14px; border: 1px solid rgba(255,255,255,.08); border-radius: 12px; background: rgba(9,12,16,.78); backdrop-filter: blur(10px); max-width: min(420px, calc(100vw - 44px)); }
    .hud-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .title { font-size: 15px; font-weight: 700; letter-spacing: .03em; }
    .hud-toggle { width: 24px; height: 24px; margin: 0; padding: 0; border: 1px solid rgba(255,255,255,.14); border-radius: 7px; color: #c9d1d9; background: rgba(13,17,23,.88); font: 700 14px/1 ui-sans-serif, system-ui; cursor: pointer; }
    .hud.collapsed .hud-body { display: none; }
    .meta { margin-top: 6px; color: #8b949e; font-size: 12px; line-height: 1.45; }
    .legend { display: flex; gap: 12px; margin-top: 9px; color: #c9d1d9; font-size: 12px; }
    .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }
    .support { background: #2ea043; }
    .buy { background: #3fb950; }
    .controls { margin-top: 10px; }
    .controls button { color: #e6edf3; background: rgba(13,17,23,.88); border: 1px solid rgba(255,255,255,.14); border-radius: 7px; padding: 4px 10px; font: inherit; cursor: pointer; }
    .tooltip { position: fixed; z-index: 3; display: none; max-width: min(360px, calc(100vw - 24px)); padding: 10px 12px; border-radius: 10px; border: 1px solid rgba(255,255,255,.12); background: rgba(9,12,16,.92); color: #e6edf3; font-size: 12px; line-height: 1.45; pointer-events: none; white-space: pre-wrap; }
    .error { position: fixed; inset: auto 22px 22px 22px; padding: 12px 14px; border-radius: 10px; color: #ffdcd7; background: rgba(248,81,73,.14); border: 1px solid rgba(248,81,73,.35); font-size: 13px; display: none; }
  </style>
</head>
<body>
  <div id="app"><canvas id="chart"></canvas></div>
  <div class="hud" id="hud">
    <div class="hud-header">
      <div class="title" id="title">Backtest</div>
      <button type="button" class="hud-toggle" id="hud-toggle" title="Minimize panel" aria-label="Minimize panel" aria-expanded="true">−</button>
    </div>
    <div class="hud-body" id="hud-body">
      <div class="meta" id="meta">Loading backtest replay…</div>
      <div class="legend">
        <span><i class="dot support"></i>Support (valid window)</span>
        <span><i class="dot buy"></i>BUY</span>
      </div>
      <div class="controls">
        <button type="button" id="reset-view">Reset viewport</button>
      </div>
      <div class="meta">Wheel: time zoom. Shift+wheel or price axis: price zoom. Drag price axis to pan.</div>
    </div>
  </div>
  <div class="tooltip" id="tooltip"></div>
  <div class="error" id="error"></div>
  <script>
    const canvas = document.getElementById('chart');
    const ctx = canvas.getContext('2d');
    const hud = document.getElementById('hud');
    const title = document.getElementById('title');
    const meta = document.getElementById('meta');
    const error = document.getElementById('error');
    const tooltip = document.getElementById('tooltip');
    const hudToggle = document.getElementById('hud-toggle');
    const resetView = document.getElementById('reset-view');
    const HUD_COLLAPSED_KEY = 'backtestChartHudCollapsed';
    // Display-only band so far-away supports do not stretch the price axis.
    const VISIBLE_ZONE_MIN = 56000;
    const VISIBLE_ZONE_MAX = 70000;

    let chartData = null;
    let viewStart = 0;
    let viewEnd = 0;
    let priceView = null;
    let drag = null;

    function setHudCollapsed(collapsed) {
      hud.classList.toggle('collapsed', collapsed);
      hudToggle.textContent = collapsed ? '+' : '−';
      hudToggle.title = collapsed ? 'Expand panel' : 'Minimize panel';
      hudToggle.setAttribute('aria-label', hudToggle.title);
      hudToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      try { localStorage.setItem(HUD_COLLAPSED_KEY, collapsed ? '1' : '0'); } catch (_) {}
    }
    try { setHudCollapsed(localStorage.getItem(HUD_COLLAPSED_KEY) === '1'); } catch (_) { setHudCollapsed(false); }
    hudToggle.addEventListener('click', () => setHudCollapsed(!hud.classList.contains('collapsed')));

    async function load() {
      error.style.display = 'none';
      const response = await fetch('/api/backtest');
      if (!response.ok) throw new Error(`Backtest API failed: ${response.status}`);
      chartData = await response.json();
      if (chartData.holds) throw new Error('API payload unexpectedly contains HOLD decisions');
      title.textContent = `${chartData.meta.symbol} 1H backtest`;
      meta.textContent = [
        `${formatUtc(chartData.meta.start_ms)} → ${formatUtc(chartData.meta.end_ms)}`,
        `${chartData.meta.evaluated_candles} candles • ${chartData.meta.zone_snapshot_count} zone snapshots • ${chartData.meta.buy_count} BUY`,
        `${chartData.meta.zone_cache_hit_count} cache hits • ${chartData.meta.zone_rebuild_count} detector builds`
      ].join('\\n');
      resetViewport();
      draw();
    }

    function resetViewport() {
      viewStart = 0;
      viewEnd = chartData && chartData.candles ? chartData.candles.length : 0;
      priceView = null;
      draw();
    }

    function resize() {
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.floor(window.innerWidth * ratio);
      canvas.height = Math.floor(window.innerHeight * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      draw();
    }

    function isVisibleZone(zone) {
      return Number(zone.low) > VISIBLE_ZONE_MIN && Number(zone.high) < VISIBLE_ZONE_MAX;
    }

    function visibleCandles() {
      if (!chartData || !chartData.candles.length) return [];
      const start = Math.max(0, Math.min(viewStart, chartData.candles.length - 1));
      const end = Math.max(start + 1, Math.min(viewEnd, chartData.candles.length));
      return chartData.candles.slice(start, end).map((candle, offset) => ({
        ...candle,
        absoluteIndex: start + offset
      }));
    }

    function draw() {
      const width = window.innerWidth;
      const height = window.innerHeight;
      ctx.clearRect(0, 0, width, height);
      drawBackground(width, height);
      if (!chartData || !chartData.candles.length) {
        drawCentered('No closed candles in backtest window.');
        return;
      }
      const candles = visibleCandles();
      const firstTime = candles[0].time;
      const lastTime = candles[candles.length - 1].time + 3600000;
      const zones = (chartData.zone_segments || []).filter(zone =>
        zone.valid_to > firstTime &&
        zone.valid_from < lastTime &&
        isVisibleZone(zone)
      );
      const buys = (chartData.buys || []).filter(buy => {
        const t = buy.trigger_open_time;
        return t >= firstTime && t <= candles[candles.length - 1].time;
      });
      const prices = candles.flatMap(c => [c.high, c.low])
        .concat(zones.flatMap(z => [z.low, z.high]))
        .concat(buys.map(b => Number(b.trigger_close)));
      const minPrice = Math.min(...prices);
      const maxPrice = Math.max(...prices);
      const padding = Math.max((maxPrice - minPrice) * 0.08, 1);
      const autoMin = minPrice - padding;
      const autoMax = maxPrice + padding;
      const scale = {
        left: 56,
        right: 92,
        top: 38,
        bottom: 44,
        min: priceView ? priceView.min : autoMin,
        max: priceView ? priceView.max : autoMax,
        plotWidth: width - 148,
        plotHeight: height - 82,
        firstTime,
        lastTime
      };
      drawGrid(width, height, scale);
      ctx.save();
      ctx.beginPath();
      ctx.rect(scale.left, scale.top, scale.plotWidth, scale.plotHeight);
      ctx.clip();
      drawZoneSegments(zones, candles, scale);
      drawCandles(candles, scale);
      drawBuys(buys, candles, scale);
      ctx.restore();
      drawPriceAxis(scale, width);
      drawTimeAxis(candles, scale, height);
      canvas._scale = scale;
      canvas._candles = candles;
      canvas._zones = zones;
      canvas._buys = buys;
    }

    function xForIndex(index, count, scale) {
      const step = scale.plotWidth / count;
      return scale.left + step * index + step / 2;
    }

    function xForTime(time, candles, scale) {
      const index = candles.findIndex(c => c.time === time);
      if (index >= 0) return xForIndex(index, candles.length, scale);
      const step = scale.plotWidth / candles.length;
      const approx = candles.findIndex(c => c.time > time);
      const i = approx < 0 ? candles.length - 1 : Math.max(0, approx - 1);
      return xForIndex(i, candles.length, scale);
    }

    function yFor(price, scale) {
      return scale.top + ((scale.max - price) / (scale.max - scale.min)) * scale.plotHeight;
    }

    function priceForY(y, scale) {
      const ratio = Math.min(1, Math.max(0, (y - scale.top) / scale.plotHeight));
      return scale.max - ratio * (scale.max - scale.min);
    }

    function isPriceAxis(x, scale) {
      return x >= scale.left + scale.plotWidth;
    }

    function zoomPriceAt(clientY, factor) {
      const scale = canvas._scale;
      if (!scale) return;
      const rect = canvas.getBoundingClientRect();
      const y = clientY - rect.top;
      const ratio = Math.min(1, Math.max(0, (y - scale.top) / scale.plotHeight));
      const anchor = priceForY(y, scale);
      const nextRange = Math.max(10, (scale.max - scale.min) * factor);
      priceView = {
        max: anchor + ratio * nextRange,
        min: anchor - (1 - ratio) * nextRange
      };
      draw();
    }

    function panPrice(dy) {
      const scale = canvas._scale;
      if (!scale) return;
      const delta = dy * ((scale.max - scale.min) / scale.plotHeight);
      priceView = { min: scale.min + delta, max: scale.max + delta };
      draw();
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
        ctx.beginPath(); ctx.moveTo(scale.left, y); ctx.lineTo(width - scale.right, y); ctx.stroke();
      }
      for (let i = 0; i <= 8; i++) {
        const x = scale.left + (scale.plotWidth / 8) * i;
        ctx.beginPath(); ctx.moveTo(x, scale.top); ctx.lineTo(x, height - scale.bottom); ctx.stroke();
      }
    }

    function drawZoneSegments(zones, candles, scale) {
      for (const zone of zones) {
        const startX = Math.max(scale.left, xForTime(zone.valid_from, candles, scale) - (scale.plotWidth / candles.length) / 2);
        const endExclusive = Math.min(scale.lastTime, zone.valid_to);
        const endX = Math.min(scale.left + scale.plotWidth, xForTime(Math.max(zone.valid_from, endExclusive - 3600000), candles, scale) + (scale.plotWidth / candles.length) / 2);
        const width = Math.max(endX - startX, 2);
        const top = yFor(zone.high, scale);
        const bottom = yFor(zone.low, scale);
        const color = '46,160,67';
        ctx.fillStyle = `rgba(${color}, .14)`;
        ctx.strokeStyle = `rgba(${color}, .5)`;
        ctx.fillRect(startX, top, width, Math.max(bottom - top, 2));
        ctx.strokeRect(startX, top, width, Math.max(bottom - top, 2));
      }
    }

    function drawCandles(candles, scale) {
      const step = scale.plotWidth / candles.length;
      const bodyWidth = Math.max(2, Math.min(12, step * 0.62));
      candles.forEach((candle, index) => {
        const x = xForIndex(index, candles.length, scale);
        const openY = yFor(candle.open, scale);
        const closeY = yFor(candle.close, scale);
        const highY = yFor(candle.high, scale);
        const lowY = yFor(candle.low, scale);
        const up = candle.close >= candle.open;
        ctx.strokeStyle = up ? '#3fb950' : '#ff7b72';
        ctx.fillStyle = up ? '#3fb950' : '#ff7b72';
        ctx.beginPath(); ctx.moveTo(x, highY); ctx.lineTo(x, lowY); ctx.stroke();
        ctx.fillRect(x - bodyWidth / 2, Math.min(openY, closeY), bodyWidth, Math.max(Math.abs(closeY - openY), 1));
      });
    }

    function drawBuys(buys, candles, scale) {
      for (const buy of buys) {
        const x = xForTime(buy.trigger_open_time, candles, scale);
        const y = yFor(Number(buy.trigger_close), scale);
        const selected = (canvas._zones || []).find(z =>
          z.fingerprint === buy.selected_zone_fingerprint &&
          buy.trigger_open_time >= z.valid_from &&
          buy.trigger_open_time < z.valid_to
        );
        if (selected) {
          const top = yFor(selected.high, scale);
          const bottom = yFor(selected.low, scale);
          ctx.strokeStyle = 'rgba(63,185,80,.95)';
          ctx.lineWidth = 2;
          ctx.strokeRect(x - 10, top, 20, Math.max(bottom - top, 2));
          ctx.lineWidth = 1;
        }
        ctx.fillStyle = '#3fb950';
        ctx.beginPath();
        ctx.moveTo(x, y - 10);
        ctx.lineTo(x - 7, y + 6);
        ctx.lineTo(x + 7, y + 6);
        ctx.closePath();
        ctx.fill();
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
        ctx.fillText(formatUtcShort(candles[index].time), x, height - 18);
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

    function formatUtc(ms) {
      return new Date(ms).toISOString().replace('.000Z', 'Z');
    }

    function formatUtcShort(ms) {
      const d = new Date(ms);
      return d.toISOString().slice(5, 16).replace('T', ' ') + 'Z';
    }

    function shortFp(fp) {
      if (!fp) return 'n/a';
      return fp.length > 18 ? `${fp.slice(0, 10)}…${fp.slice(-6)}` : fp;
    }

    function hitTest(clientX, clientY) {
      const rect = canvas.getBoundingClientRect();
      const x = clientX - rect.left;
      const y = clientY - rect.top;
      const scale = canvas._scale;
      const candles = canvas._candles || [];
      const buys = canvas._buys || [];
      const zones = canvas._zones || [];
      if (!scale || !candles.length) return null;
      for (const buy of buys) {
        const bx = xForTime(buy.trigger_open_time, candles, scale);
        const by = yFor(Number(buy.trigger_close), scale);
        if (Math.hypot(x - bx, y - by) <= 10) return { type: 'buy', item: buy };
      }
      for (const zone of zones) {
        const startX = Math.max(scale.left, xForTime(zone.valid_from, candles, scale) - (scale.plotWidth / candles.length) / 2);
        const endX = Math.min(scale.left + scale.plotWidth, xForTime(Math.max(zone.valid_from, Math.min(scale.lastTime, zone.valid_to) - 3600000), candles, scale) + (scale.plotWidth / candles.length) / 2);
        const top = yFor(zone.high, scale);
        const bottom = yFor(zone.low, scale);
        if (x >= startX && x <= endX && y >= top && y <= bottom) return { type: 'zone', item: zone };
      }
      return null;
    }

    function showTooltip(hit, clientX, clientY) {
      if (!hit) {
        tooltip.style.display = 'none';
        return;
      }
      let text = '';
      if (hit.type === 'buy') {
        const b = hit.item;
        text = [
          `BUY ${b.trigger_time}`,
          `close ${formatPrice(b.trigger_close)}`,
          `entry ${b.entry_region}`,
          `zone ${formatPrice(b.zone_low)} / ${formatPrice(b.zone_mid)} / ${formatPrice(b.zone_high)}`,
          `higher ${shortFp(b.higher_zone_fingerprint)} low=${formatPrice(b.higher_zone_low)}`,
          `next-lower ${shortFp(b.next_lower_zone_fingerprint)} high=${formatPrice(b.next_lower_zone_high)}`,
          `midpoint ${formatPrice(b.internal_range_midpoint)}`,
          `below-zone % ${b.below_zone_pct == null ? 'n/a' : Number(b.below_zone_pct).toFixed(3)}`,
          `dip ${b.dip_origin_time} @ ${formatPrice(b.dip_origin_close)}`,
          `zone_set_as_of ${b.zone_set_as_of_iso || b.zone_set_as_of}`
        ].join('\\n');
      } else {
        const z = hit.item;
        text = [
          `zone ${formatPrice(z.low)}-${formatPrice(z.high)} mid ${formatPrice(z.mid)}`,
          `valid ${formatUtc(z.valid_from)} → ${formatUtc(z.valid_to)}`,
          `source ${z.source_timeframe} • touches ${z.touches}`,
          `fp ${shortFp(z.fingerprint)}`
        ].join('\\n');
      }
      tooltip.textContent = text;
      tooltip.style.display = 'block';
      const pad = 14;
      tooltip.style.left = Math.min(clientX + pad, window.innerWidth - tooltip.offsetWidth - 8) + 'px';
      tooltip.style.top = Math.min(clientY + pad, window.innerHeight - tooltip.offsetHeight - 8) + 'px';
    }

    canvas.addEventListener('mousemove', (event) => {
      if (drag) {
        if (drag.axis === 'price') {
          panPrice(event.clientY - drag.y);
          drag.y = event.clientY;
          return;
        }
        const dx = event.clientX - drag.x;
        const candlesPerPixel = Math.max(1, (drag.end - drag.start) / Math.max(1, window.innerWidth - 148));
        const shift = Math.round(-dx * candlesPerPixel);
        let nextStart = drag.start + shift;
        let nextEnd = drag.end + shift;
        if (nextStart < 0) { nextEnd -= nextStart; nextStart = 0; }
        if (nextEnd > chartData.candles.length) {
          nextStart -= (nextEnd - chartData.candles.length);
          nextEnd = chartData.candles.length;
        }
        viewStart = Math.max(0, nextStart);
        viewEnd = Math.max(viewStart + 1, nextEnd);
        draw();
        return;
      }
      showTooltip(hitTest(event.clientX, event.clientY), event.clientX, event.clientY);
    });
    canvas.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; });
    canvas.addEventListener('mousedown', (event) => {
      if (event.button !== 0) return;
      const rect = canvas.getBoundingClientRect();
      const scale = canvas._scale;
      const onPriceAxis = scale && isPriceAxis(event.clientX - rect.left, scale);
      drag = onPriceAxis
        ? { axis: 'price', y: event.clientY }
        : { axis: 'time', x: event.clientX, start: viewStart, end: viewEnd };
    });
    window.addEventListener('mouseup', () => { drag = null; });
    canvas.addEventListener('wheel', (event) => {
      if (!chartData || !chartData.candles.length) return;
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const scale = canvas._scale;
      if (!scale) return;
      const factor = event.deltaY < 0 ? 0.85 : 1.18;
      if (event.shiftKey || isPriceAxis(x, scale)) {
        zoomPriceAt(event.clientY, factor);
        return;
      }
      const ratio = Math.min(1, Math.max(0, (x - scale.left) / scale.plotWidth));
      const count = viewEnd - viewStart;
      let nextCount = Math.max(20, Math.min(chartData.candles.length, Math.round(count * factor)));
      let nextStart = Math.round(viewStart + ratio * (count - nextCount));
      nextStart = Math.max(0, Math.min(nextStart, chartData.candles.length - nextCount));
      viewStart = nextStart;
      viewEnd = nextStart + nextCount;
      draw();
    }, { passive: false });

    resetView.addEventListener('click', resetViewport);
    window.addEventListener('resize', resize);
    resize();
    load().catch((err) => {
      error.style.display = 'block';
      error.textContent = err.message;
      drawCentered('Unable to load backtest chart.');
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
