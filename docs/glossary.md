# Thuật ngữ PRANA Buy the Dips Bot

Từ điển ngắn cho các khái niệm trong repo. Engine hiện tại: detector `support_structure_v2`, quyết định `support_close_v2` (`evaluate_support_close_v2` trong `src/trading/signal.py`).

Bot **không** tự bán. CLI dùng cùng một cycle cho `observe`, `dry_run`, và `live`; chỉ BUY ở hai mode sau mới đi tiếp sang quote/execution.

---

## 1. Bức tranh lớn

**Buy the dips** — Mua khi giá vừa rơi xuống (hoặc sát dưới) một vùng hỗ trợ, sau khi trước đó đã đứng cao hơn. Không phải mua mọi lúc giá đỏ.

**Fail-closed** — Khi thiếu dữ liệu, watermark lỗi, zone không fingerprint được, hoặc 4h quá hạn mà thiếu nến 1h: **dừng cycle**, không ghi `HOLD` giả, không trade trên zone cũ.

**Canary** — Chạy thật với số tiền rất nhỏ để kiểm tra hệ thống: **1 USDT / lệnh**, tối đa **10 USDT** cộng dồn. Không phải chiến lược full size.

**Observe / dry_run / live** — Cùng một engine quyết định; khác nhau **sau** khi ra BUY:

- `observe` — chỉ ghi `decisions`, không mở ví/RPC.
- `dry_run` — thêm quote + `eth_call` + `estimate_gas`, không approve/ký/broadcast.
- `live` — có thể approve đúng amount quote, ký và broadcast sau khi qua live guard.

**Backtest** — Replay cùng engine trên nến đã lưu. Chỉ kiểm tra tín hiệu (BUY CSV + chart). Không PnL, không ví, không ghi `decisions` / `zones` / `bot_state` live.

**Phase 1 paper scorer** — Engine cũ (điểm số, bảng `signals`). Đã bỏ. Không còn `ALERT_ONLY` / `STRONG_BUY_SIGNAL`.

---

## 2. Thị trường, nến, thời gian

**Binance Spot `BTCUSDT`** — Nguồn giá. Cycle live **chỉ fetch 1h**; không gọi API 4h lúc trade.

**Nến đóng (closed candle)** — Bar đã kết thúc (`is_closed=1`). Detector và signal **không** dùng nến đang mở. Quyết định lấy `close` của 1h đóng mới nhất.

**Nến đỏ / xanh / doji** — Đỏ: `close < open`. Xanh hoặc doji (`close >= open`): HOLD ngay, **không chọn zone**.

**1h** — Timeframe tín hiệu. Mỗi cycle evaluate một nến 1h đóng.

**4h (Binance UTC)** — Timeframe detector. Bucket: `00 / 04 / 08 / 12 / 16 / 20` UTC. Một bar 4h đóng khi đủ **4** nến 1h đóng: open = nến đầu, high = max, low = min, close = nến cuối, volume = tổng.

**Derive 4h** — Tự gom 1h → 4h rồi upsert `candles` với `timeframe="4h"`. Backfill 4h trực tiếp từ Binance chỉ để làm nóng lịch sử.

**Bucket đang hình thành** — Đồng hồ vẫn trong 4h hiện tại. Giữ zone snapshot cũ — cố ý.

**4h quá hạn incomplete** — Hết giờ bucket rồi nhưng thiếu 1h thành phần. **Abort** cycle. Không quyết định trên zone pre-watermark.

**`open_time`** — Unix **milliseconds**, UTC, lúc bar mở. Logic so sánh luôn UTC. View `*_readable` thêm cột `*_utc7` chỉ để đọc.

**Look-ahead** — Dùng nến tương lai để xác nhận pivot. Repo **cấm**: pivot chỉ confirm khi đủ nến bên phải đã đóng.

**ATR (Wilder)** — Average True Range ~14 bar. Dùng lọc swing lớn và rule “reclaim” (close xuyên cao hơn `wick + 0.2 × ATR`).

---

## 3. Zone — kệ giá hỗ trợ

**Support zone / band / shelf** — Dải giá vài trăm USD (thường **$500**), không phải một mức. Giá hay nảy hoặc thủng nhẹ quanh kệ này.

**Ladder** — Danh sách zone sắp **low → high**. Một “bậc thang” một kệ. Zone **phía trên** giá vẫn nằm trong list (để vào lệnh *below-zone*).

**`support_structure_v2`** — Detector: 4h đóng → ladder. **Không** ra BUY/HOLD.

**`detect_support_resistance_zones`** — Entry public; alias của `detect_support_resistance_zones_structure_v2` trong `src/zones/detector.py`. Bên trong: extract evidence rồi materialize. Tên “resistance” lịch sử: output `support` và `all` là **cùng một list**.

**Extract / `ZoneDetectorEvidence`** — Túi dữ liệu: OHLC, ATR, pivot, reclaim. Chưa phải zone cuối.

**Materialize** — Từ evidence → cluster → overlay → spacing → list zone.

**Incremental detector** — `IncrementalZoneDetectorState`: nuốt từng 4h đóng (`advance`), `snapshot_evidence` ra cùng túi. Backtest cache-miss dùng cái này; live refresh vẫn extract full frame. Kết quả materialize phải giống nhau.

**Pivot** — Nến có high hoặc low **duy nhất** cực trị trong cửa sổ trái/phải. Trùng cực trị → không ai là pivot.

**External vs internal swing** — External: `external_swing_order` (mặc định 5 bar mỗi bên = 20h). Internal: `internal_swing_order` (mặc định 2). External = xương sống; internal = phản ứng gần, stair, retest.

**Prominent pivot** — External đã lọc: đảo chiều đủ lớn `max(4×ATR, 2.5% wick)`. Cùng phía thì giữ cái cực hơn.

**Structure labels** — `H` / `HH` / `LH`, `L` / `HL` / `LL` trên chuỗi pivot. Gắn lúc extract, không phải lý do BUY.

**Wick / body** — Wick = `high` hoặc `low`. Body high = `max(open,close)`, body low = `min(open,close)`.

**Reclaim / flipped resistance** — High pivot bị close sau **vượt** `wick + 0.2×ATR`. Kệ cũ đảo thành hỗ trợ. `broken_index` / first reclaim = bar reclaim **đầu tiên**, không dịch.

**Candidate** — Một mẩu bằng chứng (swing low, flipped high, wick floor) trước khi gom thành zone.

**Cluster / merge / bridge** — Gom giá gần nhau cùng `bounds_style` trong $500; macro-merge khe ≤ $300; body–floor bridge khi sàn wick nằm sát trên body.

**Min touches** — Số `(index, origin)` khác nhau tối thiểu (mặc định 2) để thành zone cấu trúc. Persistent floor được **1** touch.

**Families (ba họ)**

1. **Structural** — Swing 4h lớn; band cố định $500.
2. **Local** — Internal pivot ~150 bar gần; width biến thiên ≤ $500.
3. **Overlays** — Daily body, split-rejection, persistent wick floor.

Họ đứng cạnh nhau đến bước spacing cuối. Không được để local ngắn xóa structural sớm.

**Spacing / cùng bậc** — Hai band là **một slot** nếu khe cạnh **< $650** hoặc midpoint **< $1000**. Persistent thắng; không thì score → touches → hẹp hơn.

**Stair / gap-fill** — Lấp lỗ thang. Sớm: khe > $4000 dưới giá. Cuối: khe ≥ **$1800** (`$500 + 2×$650`). Near-price fallback: cùng hình, nhưng cần nhiều touch hơn (mặc định 4) cho **một** khe cắt giá hiện tại.

**`origin`** — Vì sao band tồn tại (nhãn, không phải điểm BUY):

- Cấu trúc: `structure_swing_low`, `flipped_resistance`, `structure_support_floor`, `mixed_structure`, `stair_step_flipped_resistance`
- Local: `local_reaction_support`, `local_retested_flip_support`
- Overlay: `wick_retest_support`, `body_rejection_support`, `daily_body_support`, `persistent_wick_floor`

**`bounds_style`** — Cách lấy low/high: `body` | `support_floor` | `local_reaction`.

**`price_state`** — Chỉ nhãn so với giá: `support` / `active` / `resistance` (±0.15%). **Không** lọc list trade. Zone “resistance” phía trên vẫn là mục tiêu below-zone.

**Persistent wick floor** — Low 4h có wick treo ≥ **2%** giá wick dưới body. Ghim `low = wick`, `high = wick + 500`, 1 touch, không bị merge/daily ăn. Overlay **cuối** để dump shelf không biến mất.

**Daily overlay** — Gom 4h → ngày UTC (cần ≥ 6 bar 4h đóng). Swing daily → zone `daily_body_support`, `source_timeframe="1d"`.

---

## 4. Định danh zone (sau detector)

**`source_indexes`** — Vị trí **hàng DataFrame** lúc detect. Chỉ đúng với frame đó. Daily map trên frame 1D, không phải 4h. Signal **không** map lại index.

**`source_open_times`** — `open_time` unique, sort, của các source. Persist JSON. Đây mới là identity ổn định.

**`zone_source_time`** — `max(source_open_times)`: touch **mới nhất** tạo zone. Sàn lookback dip: không lấy origin trước lúc kệ này tồn tại.

**`zf1`** — Scheme hash SHA-256, prefix `zf1:`.

**`zone_lineage_id`** — Hash **đúng** low/high + timeframe + `bounds_style` + exchange/symbol/detector. Thêm touch **không** đổi lineage. Đổi bound / style / detector → lineage mới. Audit.

**`revision_fingerprint`** — Giống lineage **cộng** `source_open_times`. Thêm evidence thì đổi. Cache / audit.

**`zone_track_id` / sticky tracks (`ZoneTrackState`)** — Lớp sau materialize: kệ **bền** qua nhiều snapshot 4h.

- Match slot: timeframe + `bounds_style` + $650/$1000.
- Kệ mới: **2** snapshot liên tiếp mới active.
- Active: chịu **2** miss, miss **3** thì retire.
- Challenger họ khác: thắng **2** snapshot mới thay.
- Bound chỉ dịch khi mức mới in **hai lần**.
- Snapshot đầu run: bootstrap mọi candidate → active (backtest start / version bump không chờ 2 bar).

**`fingerprint` (cột persist / cooldown)** — Sau tracks: **bằng `zone_track_id`**. Chart highlight và “cùng zone” dùng cái này, không dùng `low`/`high` gần đúng.

**`fingerprint_version`** — Hiện `"zf1"`.

**`zone_set_as_of`** — `open_time` 4h đóng mà snapshot được build. Mọi zone trong một rebuild chung một giá trị.

**Watermark** — `bot_state` key  
`zone_rebuild_watermark:binance:BTCUSDT:4h:support_structure_v2`  
Value = `open_time` 4h (ms) của snapshot hiện tại.

- Bằng latest 4h đóng → load snapshot, không detect lại.
- Nhỏ hơn latest → rebuild một lần + persist atomic.
- Lớn hơn / malformed / orphan → fail-closed.

Track JSON: key `zone_track_state:…`. Restore chỉ khi watermark trước **đúng một** bar 4h; lệch thì bootstrap.

**`zone_sets`** — Manifest: scope + `zone_set_as_of` + `zone_count`. Cần cả khi **0** zone (detector hợp lệ nhưng trống).

**`backtest_zone_cache`** — Snapshot zone cho replay. Invalid khi đổi config/code detector hoặc hash nến 4h. Xóa cache không đụng bảng live.

---

## 5. Decision engine (`support_close_v2`)

**Gate-based** — Không chấm điểm. Mỗi cycle **một** `decision` (`BUY` | `HOLD`) và **một** `reason_code`. Gate fail đầu tiên được lưu.

**Trigger candle** — Nến 1h đóng đang evaluate. Phải đỏ.

**Selected zone** — Kệ **gần nhất** mà `close` đã chạm:

- Nằm trong một/nhiều band → chọn zone có `low` cao nhất trong các band chứa `close`.
- Dưới mọi band → chọn zone có `low` nhỏ nhất vẫn **> close** (không nhảy kệ xa hơn).

**Entry region**

- **`inside_zone`** — `zone.low ≤ close`, và close nằm trong **0% → `inside_zone_max_pct`** của span (`0%` = low, `100%` = high). Default max = `1.00` (cả band).
- **`below_zone` / below-zone band** — `close < zone.low`, và close nằm **`below_zone_min_pct` → 100%** của khe `(next_lower.high → zone.low)`. Default min = `0.50`.

**Higher zone** — Zone gần nhất **hẳn** phía trên (`low > selected.high`). Chạm/overlap → coi như không có.

**Next-lower zone** — Zone gần nhất **hẳn** phía dưới (`high < selected.low`). Cần cho below-zone.

**Internal range / midpoint** — Từ `selected.high` lên `higher.low`. Mid = trung bình hai mép. Dip origin phải `close` **nghiêm** trên mid này.

**Dip origin** — Nến 1h đóng **gần nhất trước** trigger, `close > internal_range_midpoint`, trong lookback. Không dùng `high` nến, không lấy max cửa sổ, không đòi pivot 4h.

**`dip_lookback_hours`** — Mặc định 48. Sàn thực: `max(trigger − 48h, zone_source_time)`.

**Approach from above** — Nến đóng trước, `close` **ngoài** band, **gần nhất**: phải `close > zone.high`. Nếu last-outside dưới `zone.low` → không BUY (thủng từ dưới đi lên).

**Setup / `setup_id`** — `selected_zone_fingerprint` + `dip_origin_open_time`. Một setup BUY **một lần**. Hết 24h **không** reset setup. Setup mới = close sau đó lại trên internal mid → origin mới.

**Cooldown (`cooldown_hours`, mặc định 24)** — **Theo zone**, không global. Trong cửa sổ: cùng fingerprint không BUY lại, kể cả origin mới. Zone **sâu hơn** (fingerprint khác) vẫn được — DCA xuống kệ dưới.

**Prior-BUY** — Live/observe: bảng `decisions`. Backtest: list **in-memory** của đúng lần chạy đó.

**`reason_code`**

- `CLOSE_NOT_BELOW_OPEN` — nến không đỏ; chưa chọn zone
- `CLOSE_OUTSIDE_ENTRY_REGION` — không inside, không below hợp lệ (kể cả zero zones)
- `NO_HIGHER_ZONE` — không tạo được internal mid
- `NO_RECENT_CLOSE_ABOVE_INTERNAL_MID` — không có dip origin
- `NO_LOWER_ZONE` — dưới kệ nhưng không có kệ dưới để đo band
- `BELOW_ZONE_OUT_OF_BAND` — dưới kệ nhưng chưa đủ gần (default cần ≥ 50% khe)
- `ZONE_APPROACHED_FROM_BELOW`
- `RECENT_BUY_IN_24H` — ưu tiên hơn setup-already khi cả hai đúng
- `SETUP_ALREADY_BOUGHT`
- `BUY_GATES_PASSED` → **BUY**

Pause, cap USDT, gas, quote, allowance **không** đổi BUY thành HOLD. Đó là skip execution (plan: `trade_executions`).

---

## 6. Dữ liệu và CLI

**`candles`** — Cùng bảng, phân biệt `timeframe` `"1h"` / `"4h"`.

**`zones` / `zone_sets` / `decisions` / `bot_state`** — Snapshot live, manifest, mọi BUY/HOLD, key-value (watermark, tracks).

**`trade_executions`** — Trạng thái redacted của BUY sau decision: quote, simulate, nonce/hash, broadcast và receipt. Không lưu calldata, signed bytes hoặc verification token.

**Views `*_readable`** — Cột gốc + hiển thị UTC+7. Trading vẫn dùng ms UTC.

**`init-db`** — Tạo schema, drop `signals` cũ, view UTC+7.

**`trade-once --mode observe|dry_run|live`** — Một cycle hourly dùng chung signal; mode chỉ thay đổi bước sau BUY.

**`backfill`** — Kéo kline lịch sử (Binance tối đa 1000/lần). `--days` chọn cửa sổ (mặc định 365). Upsert an toàn.

**`zones` (CLI)** — In ladder support từ 4h đóng trong DB.

**Chart** — `serve_chart.py` (4h, zone mới nhất; UI lọc bớt band cho dễ đọc). `serve_backtest_chart.py` (1h + marker BUY + zone theo khoảng snapshot còn hiệu lực). Trục UI UTC+7; API ms UTC.

---

## 7. Ví, swap, rủi ro

**Polygon / chain 137** — Chain canary. PRANA nhận về ví bot;

**Keystore** — File mã hóa khóa. Dev: `trader-dev.json`. Prod Pi: file riêng, **không** copy sang máy dev. `KEYSTORE_PASSWORD` chỉ từ env / systemd credential, **không** YAML.

**`quote_base_url`** — Host `POST /api/swap/quote`. Dev: public `https://prana.triethocduongpho.net`. Prod: loopback `http://127.0.0.1:4173` (route server local, bot không tự start).

**USDT → PRANA, `amountIn="1"`** — Canary luôn 1 USDT. `slippageBps` 50 = 0.5%.

**Router allowlist** — Thường Uniswap SwapRouter02. Fail-closed nếu quote trỏ chỗ khác.

**`approve-trading` / `revoke-trading`** — Allowance USDT cho router **capped 10 USDT**, không unlimited. Revoke = 0.

**`trade-check`** — Chain, bytecode router, decimals, balance, allowance. Không trade.

**`deadline`** — Quote hết hạn ~3 phút. Không ký sau deadline.

**Quote verification** — Bot yêu cầu version 2, token không rỗng và `expiresAt` còn đủ thời gian, nhưng không persist token.

**`data/PAUSE_TRADING`** — Kill switch mặc định. Khi file tồn tại, BUY decision vẫn được giữ nhưng execution là `skipped`.

**LoadCredential** — Pi: systemd nhét password vào `/run/credentials/…`. Repo không cài unit.

**Idempotent cycle** — Chạy lại cùng giờ không nhân lệnh. Nonce/hash được commit trước broadcast; rerun reconcile đúng hash đó và không gửi replacement.

**Canary risk** — Mỗi lệnh đúng 1 USDT, tối đa 3 attempt/ngày UTC và 10 USDT cumulative. Các trạng thái từ `signed` trở đi được tính bảo thủ, kể cả pending/reverted.

**In-flight lock** — Một execution chưa terminal (`started` → `pending`) chặn execution mới. Trạng thái skip/fail nằm ở `trade_executions`, không đổi BUY thành HOLD.

**Structured audit log** — JSON Lines ra stdout và rotating file. `timestamp` là UTC+7 (`YYYY-MM-DD HH:MM:SS +07:00`). Event `decision_persisted` log low/high của selected, higher, và next-lower zone (không log fingerprint). Có correlation ID và trạng thái; password, key, RPC URL, calldata, signed bytes và verification token bị redacted.

---

## 8. Hằng số hay gặp

Giá trị **hardcode** trong `src/zones/types.py` (không phải YAML), trừ khi ghi chú:

- Band cấu trúc **$500**
- Slot: cạnh **$650**, midpoint **$1000**
- Macro-merge: khe **$300**, span source **$2000**
- Persistent: wick **2%** dưới body (không phải $500)
- Stair sớm: khe **$4000**, tối đa 6 insert
- Gap-fill cuối: **$1800**
- Local lookback: **150** bar 4h
- Split-rejection: wick ≥ **$1000**, retest trong **4** internal low

YAML `zones:` / `strategy:`: swing order, ATR, min touches, near-price fill, `dip_lookback_hours`, `cooldown_hours`, `inside_zone_max_pct`, `below_zone_min_pct`.

---

## Đọc tiếp

- Chạy bot, CLI, reason codes: `README.md`
- Pipeline detector từng bước: `docs/zones finder algo overview.md`
- Live swap, schema, rollout: `plans/live_dip_execution_a7cf3b95.plan.md`
