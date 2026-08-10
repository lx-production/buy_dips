Đúng, với mục tiêu của bạn thì backtest signal-only là đủ. Không cần PnL, forward return, PRANA price hay các tiêu chí kinh tế mình liệt kê trước đó.

Bản update này đã sửa tốt các lỗi logic chính:

- Path B chọn đúng zone gần nhất bằng `min(zone.low > close)` tại [plan:150](/home/prana/buy_dips/.cursor/plans/live_dip_execution_a7cf3b95.plan.md:150).
- Hai vùng entry dùng chung một định nghĩa dip.
- Dip origin dùng `1h close > internal midpoint`, không còn bị wick gây nhiễu.
- Trigger candle bị loại khỏi lookback.
- Cooldown 24h áp dụng cho cả inside-zone và below-zone.
- Không được bỏ qua zone gần nhất để chọn zone xa hơn.

Theo định nghĩa của bạn, đây đã là một dip hợp lệ: giá từng đóng trên midpoint của range trong vòng 48h, sau đó đóng tại phần dưới support hoặc ngay dưới support.

## Những điểm vẫn cần chốt để BUY locations chính xác

1. **Phải xác định rõ danh sách zone đầu vào**

Plan vẫn viết “active support zones” tại [plan:145](/home/prana/buy_dips/.cursor/plans/live_dip_execution_a7cf3b95.plan.md:145), trong khi detector hiện trả:

```python
{
    "support": [...],
    "active": [],
    "all": [...]
}
```

tại [detector.py:141](/home/prana/buy_dips/src/zones/detector.py:141).

Nên sửa plan thành:

```python
zones = detector_result["support"]
```

Không lọc bằng `price_state == "support"`, vì Path B cần nhìn thấy broken support đang nằm phía trên giá hiện tại.

2. **Viết công thức chính xác cho adjacent zones**

`selected` đã rõ, nhưng `higher_zone` và `next_lower_zone` vẫn chỉ nói “nearest”. Nên khóa công thức:

```python
higher_zone = min(
    (z for z in zones if z.low > selected.high),
    key=lambda z: z.low,
)

next_lower_zone = max(
    (z for z in zones if z.high < selected.low),
    key=lambda z: z.high,
)
```

Nếu có nhiều zone cùng chứa close, cũng nên quy định:

```python
selected = max(containing_zones, key=lambda z: z.low)
```

Detector hiện thường loại overlap, nhưng quy tắc này giúp replay không phụ thuộc thứ tự list.

3. **Current algo có thể BUY hai lần trên cùng một dip**

Giả sử dip origin xảy ra lúc `t0`, BUY lần đầu tại support, sau 24h giá vẫn ở support và dip origin vẫn còn trong cửa sổ 48h. Algo sẽ BUY lần hai.

Vì vậy semantics hiện tại là:

> Tối đa một BUY mỗi 24h, không phải một BUY cho mỗi dip.

Nếu đây là ý định DCA thì algo đang đúng. Nếu muốn mỗi dip chỉ BUY một lần, thay gate bằng:

```text
Không có BUY nào sau dip_origin.open_time
```

hoặc lưu `(selected_zone_fingerprint, dip_origin_open_time)` làm setup identity.

Ngoài ra cooldown hiện là global: BUY ở zone A sẽ chặn BUY ở zone B sâu hơn trong 24h. Cũng nên ghi rõ đây là chủ ý.

4. **`zone_source_time` chưa được định nghĩa đủ rõ**

Plan dùng nó làm floor tại [plan:163](/home/prana/buy_dips/.cursor/plans/live_dip_execution_a7cf3b95.plan.md:163), nhưng một zone có nhiều source indexes.

Nên định nghĩa:

```text
zone_source_time = max(all resolved source open_times)
```

Đặc biệt daily zone có `source_timeframe="1d"` tại [daily.py:72](/home/prana/buy_dips/src/zones/daily.py:72), nên source index của nó phải map vào daily dataframe, không phải 4H dataframe.

Nếu mapping sai, một dip origin có thể bị loại hoặc được nhận nhầm.

5. **Không được dùng stale zone khi 4H đáng lẽ đã đóng nhưng thiếu 1H candle**

Plan có test incomplete 4H, nhưng chưa nói rõ runner làm gì.

Quy tắc nên là:

- Trong lúc bucket 4H đang hình thành: dùng zone set cũ là đúng.
- Khi bucket đã hết thời gian nhưng thiếu một trong bốn candle 1H: abort cycle, không tạo decision.
- Không tiếp tục BUY bằng zone set trước watermark trong trường hợp này.

Điểm này trực tiếp ảnh hưởng việc BUY có đúng support snapshot tại thời điểm đó hay không.

6. **Backtest phải có state riêng**

Gate 24h đang scan `decisions` tại [plan:172](/home/prana/buy_dips/.cursor/plans/live_dip_execution_a7cf3b95.plan.md:172). Backtest không được đọc BUY từ `observe/live` hoặc từ lần backtest trước.

Nên dùng:

- Danh sách BUY in-memory riêng cho từng replay; hoặc
- `backtest_run_id` riêng.

Chạy lại cùng input phải cho ra chính xác cùng BUY timestamps.

## Output backtest nên đổi nhẹ

Vì mục tiêu là xem đã mua ở đâu, “short list” tại [plan:227](/home/prana/buy_dips/.cursor/plans/live_dip_execution_a7cf3b95.plan.md:227) chưa đủ. Nên xuất toàn bộ BUY, CSV ngay từ phase này, với các cột:

```text
trigger_time
trigger_close
entry_region
zone_low
zone_mid
zone_high
higher_zone_low
internal_range_midpoint
next_lower_zone_high
below_zone_pct
dip_origin_time
dip_origin_close
zone_set_as_of
```

Như vậy bạn nhìn mỗi row là xác nhận được ngay:

- Giá đã đi từ đâu xuống.
- Có thực sự đóng trên internal midpoint hay không.
- BUY nằm trong support hay dưới support.
- Nếu dưới support thì đúng band 70%–100% hay không.
- Zone được dùng là snapshot nào tại thời điểm BUY.

Cuối cùng, plan cũng cần thêm backfill lịch sử 1H rõ ràng. Code hiện tại chỉ hỗ trợ backfill `4h` tại [candles.py:12](/home/prana/buy_dips/src/candles.py:12), trong khi backtest yêu cầu nhiều tháng dữ liệu 1H.

Tóm lại: buy formula hiện đã coherent và không còn lỗi chọn sai Path B. Trước khi implement, mình chỉ coi các mục 1, 4, 5 và 6 là bắt buộc; mục 3 là quyết định chiến lược cần bạn xác nhận: **mỗi 24h một lần hay mỗi dip đúng một lần**.