# Visual Backtest cho BUY và Support Zones

## Tóm tắt

- Hoàn thiện offline backtest `support_close_v1`, BUY CSV và một chart server riêng.
- Backtest dùng lại chính decision engine và detector hiện có, không đọc/ghi `decisions`, `zones` hay `bot_state` live.
- Chỉ các quyết định `BUY` được xuất ra CLI/CSV/API/chart. `HOLD` vẫn được tính nội bộ để replay đúng nhưng bị loại khỏi output.
- Chart dùng nến 1h, support zone theo đúng thời gian hiệu lực, có zoom/pan/hover và không dùng zone tương lai.

## Trạng thái: đã implement

- Replay: [src/trading/backtest.py](../../src/trading/backtest.py)
- Shared zone build: `build_fingerprinted_support_zones` trong [src/trading/zone_refresh.py](../../src/trading/zone_refresh.py)
- CLI: `python3 -m src.cli backtest --start <ISO> [--end <ISO>] --csv <path>`
- Chart: [scripts/serve_backtest_chart.py](../../scripts/serve_backtest_chart.py) → [src/backtest_chart_server.py](../../src/backtest_chart_server.py)
- Tests: [tests/test_backtest.py](../../tests/test_backtest.py)
- Docs: README offline backtest + chart sections; live plan backtest todo marked completed

## Thay đổi chính (đã khóa / đã ship)

- Replay engine trong `src/trading/backtest.py`:
  - Nhận `start` inclusive và `end` exclusive; `start` bắt buộc, `end` mặc định sau cây 1h đóng cuối cùng.
  - Thời gian đầu vào phải là ISO-8601 có timezone và nằm trên biên giờ UTC.
  - Yêu cầu đủ dữ liệu 1h liên tục từ `start - 48h`; thiếu nến hoặc bucket 4h không đủ bốn nến thì abort.
  - `start` có thể nằm trên bất kỳ biên giờ UTC; bucket 4h derive đầu tiên là bucket đầu tiên được phủ đủ bốn nến 1h.
  - Dùng 4h lịch sử trước vùng 1h làm detector warm-up; từ vùng có 1h trở đi luôn derive 4h từ bốn nến 1h và không dùng 4h tương lai.
  - Mỗi bucket 4h được aggregate đúng một lần cho cả replay, sau đó frame as-of chỉ được cắt lại khi watermark 4h tiến lên.
  - Khởi tạo zone snapshot tại cây trigger đầu tiên, sau đó rebuild trong memory đúng lúc một 4h mới hoàn tất.
  - Helper thuần dùng chung với live zone refresh: `build_fingerprinted_support_zones`.
  - Gọi `evaluate_support_close_v1(..., mode="backtest")`; mode này có trong pure engine nhưng không thêm vào schema `decisions`.
  - Cooldown BUY là danh sách in-memory theo fingerprint, bắt đầu rỗng tại `start`; không persist kết quả.
  - Trả về `BacktestResult` gồm nến trong khoảng hiển thị, danh sách BUY, zone snapshot/segment và thống kê `evaluated_candles`, `zone_rebuild_count`, `buy_count`.

- CLI / CSV / visual server: như mô tả ở README.

## Hành vi chart

- Ban đầu fit toàn bộ khoảng backtest; wheel zoom theo vị trí con trỏ, kéo ngang để pan, có nút reset viewport.
- Vẽ support zone chỉ trong thời gian snapshot của chúng có hiệu lực, và chỉ các band có `low > 56000` và `high < 70000` (lọc hiển thị; API vẫn trả đủ segment).
- Vẽ BUY bằng marker xanh tại `trigger_close`; selected zone tại BUY được nhấn mạnh.
- Hover BUY / zone như đã khóa; không có marker/bảng/tooltip dành cho no-BUY.

## Kiểm thử và tiêu chí chấp nhận

- Covered by `tests/test_backtest.py` + full pytest green.
- Synthetic replay: rebuild timing, warm-up/gap/alignment failures, per-zone cooldown, live-table immutability, CSV header-only for zero BUY, API payload without HOLD, CLI parser for start/end/default end.

## Giả định đã khóa

- Chart và quyết định dùng timeframe 1h; mốc thời gian hiển thị theo UTC để khớp Binance bucket.
- `start` là ranh giới bắt đầu một replay độc lập: prior-BUY state rỗng tại đó, còn 48h trước `start` chỉ dùng cho dip-origin lookback.
- Backtest là signal-only: không PnL, sell, quote, slippage, gas, wallet hoặc transaction simulation.
- Không thêm thư viện chart bên ngoài; tiếp tục dùng canvas hiện tại.
