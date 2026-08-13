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
    # Serve the Lightweight Charts page; replay data still comes from GET /api/backtest.
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
    #app, #chart { position: fixed; inset: 0; }
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
  <script src="https://unpkg.com/lightweight-charts@5.0.8/dist/lightweight-charts.standalone.production.js"></script>
</head>
<body>
  <div id="app"><div id="chart"></div></div>
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
      <div class="meta">Times are UTC+7. Scroll to zoom time. Drag plot to pan. Drag / scroll the price axis to scale price.</div>
    </div>
  </div>
  <div class="tooltip" id="tooltip"></div>
  <div class="error" id="error"></div>
  <script>
    const container = document.getElementById('chart');
    const hud = document.getElementById('hud');
    const title = document.getElementById('title');
    const meta = document.getElementById('meta');
    const error = document.getElementById('error');
    const tooltip = document.getElementById('tooltip');
    const hudToggle = document.getElementById('hud-toggle');
    const resetView = document.getElementById('reset-view');
    const HUD_COLLAPSED_KEY = 'backtestChartHudCollapsed';
    // Display-only band so far-away supports do not clutter the pane.
    const VISIBLE_ZONE_MIN = 56000;
    const VISIBLE_ZONE_MAX = 70000;

    let chartData = null;
    let visibleZones = [];
    let visibleBuys = [];
    let chart = null;
    let candleSeries = null;

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

    function toSec(ms) {
      return Math.floor(Number(ms) / 1000);
    }

    function isVisibleZone(zone) {
      return Number(zone.low) > VISIBLE_ZONE_MIN && Number(zone.high) < VISIBLE_ZONE_MAX;
    }

    // Map a UTC-ms bound onto the bar edge. valid_to is exclusive.
    function zoneEdgeX(timeScale, timeMs, which, spacing) {
      const sec = toSec(timeMs);
      const x = timeScale.timeToCoordinate(sec);
      if (x !== null) return x - spacing / 2;
      if (which === 'end') {
        const prev = timeScale.timeToCoordinate(sec - 3600);
        if (prev !== null) return prev + spacing / 2;
      }
      return null;
    }

    class ZoneBandsRenderer {
      constructor(boxes) {
        this._boxes = boxes;
      }
      draw(target) {
        target.useMediaCoordinateSpace((scope) => {
          const ctx = scope.context;
          for (const box of this._boxes) {
            const x = Math.min(box.x1, box.x2);
            const y = Math.min(box.y1, box.y2);
            const width = Math.max(Math.abs(box.x2 - box.x1), 1);
            const height = Math.max(Math.abs(box.y2 - box.y1), 1);
            ctx.fillStyle = 'rgba(46,160,67,0.16)';
            ctx.strokeStyle = box.highlight ? 'rgba(63,185,80,0.95)' : 'rgba(46,160,67,0.55)';
            ctx.lineWidth = box.highlight ? 2 : 1;
            ctx.fillRect(x, y, width, height);
            ctx.strokeRect(x, y, width, height);
          }
        });
      }
    }

    class ZoneBandsPaneView {
      constructor(source) {
        this._source = source;
        this._boxes = [];
      }
      zOrder() {
        return 'bottom';
      }
      update() {
        const host = this._source._chart;
        const series = this._source._series;
        if (!host || !series) {
          this._boxes = [];
          return;
        }
        const timeScale = host.timeScale();
        const spacing = timeScale.options().barSpacing || 6;
        this._boxes = [];
        for (const zone of this._source._zones) {
          const x1 = zoneEdgeX(timeScale, zone.valid_from, 'start', spacing);
          const x2 = zoneEdgeX(timeScale, zone.valid_to, 'end', spacing);
          const y1 = series.priceToCoordinate(Number(zone.high));
          const y2 = series.priceToCoordinate(Number(zone.low));
          if (x1 === null || x2 === null || y1 === null || y2 === null) continue;
          this._boxes.push({ x1, x2, y1, y2, highlight: Boolean(zone.highlight) });
        }
      }
      renderer() {
        return new ZoneBandsRenderer(this._boxes);
      }
    }

    // One primitive draws every time-bounded support band so zoom/pan stay on the library.
    class ZoneBandsPrimitive {
      constructor(zones) {
        this._zones = zones;
        this._views = [new ZoneBandsPaneView(this)];
        this._chart = null;
        this._series = null;
      }
      attached(param) {
        this._chart = param.chart;
        this._series = param.series;
      }
      detached() {
        this._chart = null;
        this._series = null;
      }
      updateAllViews() {
        this._views[0].update();
      }
      paneViews() {
        return this._views;
      }
    }

    function formatPrice(value) {
      if (value === null || value === undefined) return 'n/a';
      return Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
    }

    const UTC7_OFFSET_MS = 7 * 60 * 60 * 1000;

    // Display-only: shift the UTC instant into UTC+7 wall time. Replay math stays UTC.
    function formatUtc7(ms, withSeconds) {
      if (ms === null || ms === undefined || !Number.isFinite(Number(ms))) return 'n/a';
      const shifted = new Date(Number(ms) + UTC7_OFFSET_MS);
      const stamp = shifted.toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, '');
      return (withSeconds === false ? stamp.slice(0, 16) : stamp) + ' +07:00';
    }

    function formatUtc7Tick(time, tickMarkType) {
      const shifted = new Date(Number(time) * 1000 + UTC7_OFFSET_MS);
      const year = shifted.getUTCFullYear();
      const month = String(shifted.getUTCMonth() + 1).padStart(2, '0');
      const day = String(shifted.getUTCDate()).padStart(2, '0');
      const hour = String(shifted.getUTCHours()).padStart(2, '0');
      const minute = String(shifted.getUTCMinutes()).padStart(2, '0');
      if (tickMarkType === 0) return String(year);
      if (tickMarkType === 1) return `${year}-${month}`;
      if (tickMarkType === 2) return `${month}-${day}`;
      if (tickMarkType === 4) return `${hour}:${minute}:00`;
      return `${month}-${day} ${hour}:${minute}`;
    }

    function shortFp(fp) {
      if (!fp) return 'n/a';
      return fp.length > 18 ? `${fp.slice(0, 10)}…${fp.slice(-6)}` : fp;
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
          `BUY ${formatUtc7(b.trigger_open_time)}`,
          `close ${formatPrice(b.trigger_close)}`,
          `entry ${b.entry_region}`,
          `zone ${formatPrice(b.zone_low)} / ${formatPrice(b.zone_mid)} / ${formatPrice(b.zone_high)}`,
          `higher ${shortFp(b.higher_zone_fingerprint)} low=${formatPrice(b.higher_zone_low)}`,
          `next-lower ${shortFp(b.next_lower_zone_fingerprint)} high=${formatPrice(b.next_lower_zone_high)}`,
          `midpoint ${formatPrice(b.internal_range_midpoint)}`,
          `below-zone % ${b.below_zone_pct == null ? 'n/a' : Number(b.below_zone_pct).toFixed(3)}`,
          `dip ${formatUtc7(b.dip_origin_open_time)} @ ${formatPrice(b.dip_origin_close)}`,
          `zone_set_as_of ${formatUtc7(b.zone_set_as_of)}`
        ].join('\\n');
      } else {
        const z = hit.item;
        text = [
          `zone ${formatPrice(z.low)}-${formatPrice(z.high)} mid ${formatPrice(z.mid)}`,
          `valid ${formatUtc7(z.valid_from)} → ${formatUtc7(z.valid_to)}`,
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

    function hitAt(timeSec, price) {
      const timeMs = Number(timeSec) * 1000;
      const buy = visibleBuys.find((item) => item.trigger_open_time === timeMs);
      if (buy) return { type: 'buy', item: buy };
      if (price === null || price === undefined) return null;
      const zone = visibleZones.find((item) =>
        timeMs >= item.valid_from && timeMs < item.valid_to &&
        price >= Number(item.low) && price <= Number(item.high)
      );
      return zone ? { type: 'zone', item: zone } : null;
    }

    function resetViewport() {
      if (!chart) return;
      chart.timeScale().fitContent();
      chart.priceScale('right').applyOptions({ autoScale: true });
    }

    function requireLightweightCharts() {
      const lib = window.LightweightCharts;
      if (!lib || typeof lib.createChart !== 'function') {
        throw new Error('Lightweight Charts failed to load. The backtest page needs network once to fetch the CDN script.');
      }
      return lib;
    }

    function renderChart(data) {
      const lib = requireLightweightCharts();
      const candles = (data.candles || []).map((candle) => ({
        time: toSec(candle.time),
        open: Number(candle.open),
        high: Number(candle.high),
        low: Number(candle.low),
        close: Number(candle.close)
      }));
      if (!candles.length) throw new Error('No closed candles in backtest window.');

      visibleBuys = data.buys || [];
      visibleZones = (data.zone_segments || []).filter(isVisibleZone).map((zone) => ({
        ...zone,
        highlight: visibleBuys.some((buy) =>
          buy.selected_zone_fingerprint === zone.fingerprint &&
          buy.trigger_open_time >= zone.valid_from &&
          buy.trigger_open_time < zone.valid_to
        )
      }));

      if (chart) {
        chart.remove();
        chart = null;
        candleSeries = null;
      }
      chart = lib.createChart(container, {
        autoSize: true,
        layout: {
          background: { color: '#090c10' },
          textColor: '#8b949e',
          attributionLogo: false
        },
        grid: {
          vertLines: { color: 'rgba(139,148,158,.15)' },
          horzLines: { color: 'rgba(139,148,158,.15)' }
        },
        crosshair: { mode: lib.CrosshairMode ? lib.CrosshairMode.Normal : 0 },
        rightPriceScale: { borderColor: 'rgba(255,255,255,.12)', scaleMargins: { top: 0.08, bottom: 0.08 } },
        timeScale: {
          borderColor: 'rgba(255,255,255,.12)',
          timeVisible: true,
          secondsVisible: false,
          tickMarkFormatter: (time, tickMarkType) => formatUtc7Tick(time, tickMarkType)
        },
        localization: {
          locale: 'en-GB',
          timeFormatter: (time) => formatUtc7(Number(time) * 1000),
          priceFormatter: (price) => formatPrice(price)
        }
      });
      candleSeries = chart.addSeries(lib.CandlestickSeries, {
        upColor: '#3fb950',
        downColor: '#ff7b72',
        borderUpColor: '#3fb950',
        borderDownColor: '#ff7b72',
        wickUpColor: '#3fb950',
        wickDownColor: '#ff7b72'
      });
      candleSeries.setData(candles);
      candleSeries.attachPrimitive(new ZoneBandsPrimitive(visibleZones));

      const markers = visibleBuys.map((buy) => ({
        time: toSec(buy.trigger_open_time),
        position: 'belowBar',
        color: '#3fb950',
        shape: 'arrowUp',
        text: 'BUY'
      }));
      if (typeof lib.createSeriesMarkers === 'function') {
        lib.createSeriesMarkers(candleSeries, markers);
      } else if (typeof candleSeries.setMarkers === 'function') {
        candleSeries.setMarkers(markers);
      }

      chart.subscribeCrosshairMove((param) => {
        if (!param.point || param.time === undefined || param.time === null) {
          showTooltip(null);
          return;
        }
        const price = candleSeries.coordinateToPrice(param.point.y);
        const rect = container.getBoundingClientRect();
        showTooltip(hitAt(param.time, price), rect.left + param.point.x, rect.top + param.point.y);
      });
      resetViewport();
    }

    async function load() {
      error.style.display = 'none';
      const response = await fetch('/api/backtest');
      if (!response.ok) throw new Error(`Backtest API failed: ${response.status}`);
      chartData = await response.json();
      if (chartData.holds) throw new Error('API payload unexpectedly contains HOLD decisions');
      title.textContent = `${chartData.meta.symbol} 1H backtest`;
      meta.textContent = [
        `${formatUtc7(chartData.meta.start_ms)} → ${formatUtc7(chartData.meta.end_ms)}`,
        `${chartData.meta.evaluated_candles} candles • ${chartData.meta.zone_snapshot_count} zone snapshots • ${chartData.meta.buy_count} BUY`,
        `${chartData.meta.zone_cache_hit_count} cache hits • ${chartData.meta.zone_rebuild_count} detector builds`
      ].join('\\n');
      renderChart(chartData);
    }

    resetView.addEventListener('click', resetViewport);
    load().catch((err) => {
      error.style.display = 'block';
      error.textContent = err.message;
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
