# Visual Backtest cho BUY và Support Zones

## Tóm tắt

- Hoàn thiện offline backtest `support_close_v1`, BUY CSV và một chart server riêng.
- Backtest dùng lại chính decision engine và detector hiện có, không đọc/ghi `decisions`, `zones` hay `bot_state` live.
- Zone snapshots của backtest được cache trong bảng `backtest_zone_cache` riêng; không dùng chung persistence với zone live.
- Chỉ các quyết định `BUY` được xuất ra CLI/CSV/API/chart. `HOLD` vẫn được tính nội bộ để replay đúng nhưng bị loại khỏi output.
- Chart dùng nến 1h, support zone theo đúng thời gian hiệu lực, có zoom/pan/hover và không dùng zone tương lai.

## Trạng thái: đã implement

- Replay: [src/trading/backtest.py](../../src/trading/backtest.py)
- Shared zone build: `build_fingerprinted_support_zones` / `build_fingerprinted_support_zones_from_evidence` trong [src/trading/zone_refresh.py](../../src/trading/zone_refresh.py)
- CLI: `python3 -m src.cli backtest --start <ISO> [--end <ISO>] --csv <path>`
- Chart: [scripts/serve_backtest_chart.py](../../scripts/serve_backtest_chart.py) → [src/backtest_chart_server.py](../../src/backtest_chart_server.py) (page: [src/backtest_chart.html](../../src/backtest_chart.html))
- Tests: [tests/test_backtest.py](../../tests/test_backtest.py)
- Docs: README offline backtest + chart sections; live plan backtest todo marked completed

## Thay đổi chính (đã khóa / đã ship)

- Replay engine trong `src/trading/backtest.py`:
  - Nhận `start` inclusive và `end` exclusive; `start` bắt buộc, `end` mặc định sau cây 1h đóng cuối cùng.
  - Thời gian đầu vào phải là ISO-8601 có timezone và nằm trên biên giờ UTC.
  - Yêu cầu đủ dữ liệu 1h liên tục từ `start - dip_lookback_hours` (mặc định 48h); thiếu nến hoặc bucket 4h không đủ bốn nến thì abort.
  - `start` có thể nằm trên bất kỳ biên giờ UTC; bucket 4h derive đầu tiên là bucket đầu tiên được phủ đủ bốn nến 1h.
  - Dùng 4h lịch sử trước vùng 1h làm detector warm-up; từ vùng có 1h trở đi luôn derive 4h từ bốn nến 1h và không dùng 4h tương lai.
  - Mỗi bucket 4h được aggregate đúng một lần cho cả replay, sau đó frame as-of chỉ được cắt lại khi watermark 4h tiến lên.
  - Cold cache miss ingest 4h history một lần vào `IncrementalZoneDetectorState`, rồi `snapshot_evidence` + full materialization tại từng watermark thiếu. Fingerprint reuse revision không đổi (chỉ cập nhật `zone_set_as_of`). Warm path (mọi snapshot đều hit) không khởi tạo state.
  - Mỗi zone snapshot được cache theo watermark cùng hash config detector, source code và prefix dữ liệu 4h. Cache stale/hỏng tự rebuild và overwrite; range mở rộng chỉ build watermark mới. Replay bulk-load cache hits bằng một SQLite connection.
  - Khởi tạo zone snapshot tại cây trigger đầu tiên, sau đó rebuild trong memory đúng lúc một 4h mới hoàn tất.
  - Helper thuần dùng chung với live zone refresh: `build_fingerprinted_support_zones` (stateless / injected detector) và `build_fingerprinted_support_zones_from_evidence` (incremental backtest).
  - Gọi `evaluate_support_close_v1(..., mode="backtest")`; mode này có trong pure engine nhưng không thêm vào schema `decisions`.
  - Setup đã BUY là danh sách in-memory theo `zone_lineage_id` (`fingerprint`) + `dip_origin_open_time` (và `trigger_open_time` cho cooldown 24h), bắt đầu rỗng tại `start`; không persist kết quả. Lineage không đổi khi zone thêm touch, nên cooldown/chart không reset chỉ vì evidence mới. Trong 24h cùng zone bị chặn dù có dip origin mới. Sau 24h zone chỉ reset khi có dip origin mới (close trên `internal_range_midpoint`).
  - Trả về `BacktestResult` gồm nến trong khoảng hiển thị, danh sách BUY, zone snapshot/segment và thống kê `evaluated_candles`, `zone_snapshot_count`, `zone_cache_hit_count`, `zone_rebuild_count`, `zone_state_ingested_candles`, `zone_full_history_scans`, `buy_count`.

- CLI / CSV / visual server: như mô tả ở README.

## Hành vi chart

- Backtest chart và chart 4h live (`serve_chart.py`) đều dùng TradingView Lightweight Charts (CDN).
- Ban đầu fit toàn bộ khoảng backtest; zoom/pan thời gian và giá do thư viện; nút reset viewport gọi `fitContent`.
- Vẽ support zone bằng một series primitive (hình chữ nhật `valid_from` → `valid_to`, `low` → `high`); chỉ các band có `low > 57000` và `high < 75000` (lọc hiển thị; API vẫn trả đủ segment).
- Vẽ BUY bằng marker xanh tại `trigger_close`; selected zone tại BUY được nhấn mạnh.
- Hover nến hiện OHLC; hover BUY / zone vẫn append chi tiết như đã khóa. Không có marker/bảng/tooltip dành cho no-BUY.

## Kiểm thử và tiêu chí chấp nhận

- Covered by `tests/test_backtest.py` + full pytest green.
- Synthetic replay: rebuild timing, warm-up/gap/alignment failures, cache cold/warm/extended range, config/candle invalidation, corrupt-cache repair, same-setup block / 24h same-zone block with new dip origin / other-zone still allowed / new origin after 24h allowed, live-table immutability, CSV header-only for zero BUY, API payload without HOLD, CLI parser for start/end/default end.

## Giả định đã khóa

- Chart và quyết định dùng timeframe 1h; replay/API giữ Unix UTC, còn nhãn trục / HUD / hover của backtest chart hiển thị UTC+7.
- `start` là ranh giới bắt đầu một replay độc lập: prior-setup state rỗng tại đó, còn `dip_lookback_hours` trước `start` chỉ dùng cho dip-origin lookback.
- Backtest là signal-only: không PnL, sell, quote, slippage, gas, wallet hoặc transaction simulation.
- Cả backtest chart và chart 4h live đều dùng Lightweight Charts + một rectangle primitive cho zone bands. Backtest band bị cắt theo `valid_from`/`valid_to`; live band trải suốt cửa sổ nến đang xem.
