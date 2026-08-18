# Incremental Zone Detector cho Cold Backtest

## Mục tiêu

Giảm thời gian chạy backtest lần đầu bằng cách chỉ ingest mỗi nến 4h một lần và giữ detector state giữa các watermark, nhưng vẫn tạo đúng cùng zone snapshot như detector stateless hiện tại tại mọi thời điểm.

Không cập nhật trực tiếp từ danh sách final zones. Final zones đã mất các pivot/candidate bị suppress và không đủ dữ liệu để xử lý pivot confirmation, historical reclaim, daily overlay, local lookback expiry, split rejection hoặc spacing conflict.

## Baseline đã đo

Benchmark được chạy trên bản sao SQLite tạm, không thay đổi database production, với config mặc định và range `2026-06-01T00:00:00Z` đến `2026-08-13T06:00:00Z`:

- Dữ liệu: 5.899 nến 4h, 1.758 nến 1h được evaluate, 440 zone snapshots.
- Cold cache: 440 detector builds, `56,4s`.
- Warm cache: 440 cache hits, `2,23s`.
- Current cold path xử lý xấp xỉ 2,5 triệu 4h bar-prefix; stateful ingestion chỉ cần ingest khoảng 5.900 bar một lần.

Profile cold run xác định các hotspot chính:

1. Tìm external/internal/daily pivots trên toàn prefix tại mỗi snapshot.
2. Quét lại split-rejection pairs trên toàn bộ pivot history.
3. Quét reclaim candidates cho gap-fill và local/structural zones.
4. Aggregate và detect daily zones lại từ đầu.
5. Cluster/merge/spacing và fingerprint final zones.

## Target hiệu năng

### Milestone 1 — incremental evidence, full materialization

- Ingest ATR, pivot, reclaim và daily state một lần theo thời gian.
- Vẫn chạy full cluster/merge/overlay/spacing trên evidence state ở mỗi watermark để giảm rủi ro thay đổi semantics.
- Target cold run cho benchmark trên: `12–20s` (`2,8–4,7x` nhanh hơn).
- Warm cache không được chậm quá `10%` so với baseline `2,23s`.

### Milestone 2 — dirty materialization, chỉ làm nếu cần

- Chỉ recompute price components bị ảnh hưởng bởi evidence mới, cộng các zone lân cận cần merge/spacing.
- Target cold run: `5–10s` (`5,6–11x` nhanh hơn).
- Không đặt mục tiêu bằng warm cache vì cold run vẫn phải tạo/fingerprint/persist 440 snapshots và replay 1h signals.

Các target là acceptance range, không phải cam kết trước implementation. Kết quả cuối phải được báo bằng benchmark thực tế trên cùng fixture/range.

## Nguyên tắc correctness

- Không lookahead: một pivot chỉ được đưa vào state sau khi đủ số right-side bars theo `external_swing_order` hoặc internal order.
- ATR tại index chỉ dùng candle ở index đó và quá khứ.
- Reclaim có thể xảy ra sau pivot, hoặc đã xảy ra trong các right-side bars trước lúc pivot được xác nhận; phải giữ đúng first reclaim index.
- Daily zone chỉ xuất hiện sau khi đủ sáu nến 4h của ngày UTC.
- Local reaction state phải expire evidence đúng tại cửa sổ 150 bars.
- Mọi output field phải parity, gồm bounds, origin, touches, source indexes/times, lineage, revision fingerprint và `zone_set_as_of`.
- Public stateless detector vẫn được giữ làm reference implementation và dùng cho live path cho đến khi incremental parity hoàn tất.
- Cache identity tiếp tục phụ thuộc detector source, zone config và exact 4h prefix hash.

## Plan implementation

### Bước 1 — khóa golden behavior và benchmark harness

Files dự kiến:

- `tests/test_incremental_zone_detector.py`
- `scripts/benchmark_backtest.py`
- `README.md`

Thực hiện:

1. Tạo helper canonicalize zone snapshots để so sánh deep equality theo thứ tự ổn định.
2. Chạy stateless detector cho mọi 4h prefix và lưu kết quả trong memory làm oracle; không commit snapshot data lớn vào repo.
3. Thêm fixture bao phủ riêng các transition:
   - external pivot được xác nhận sau N right bars;
   - internal pivot và local 150-bar expiry;
   - high pivot được reclaim bởi nến mới;
   - reclaim xảy ra trước lúc pivot đủ right bars;
   - ngày UTC đủ sáu nến 4h;
   - prominent pivot cùng loại bị thay bởi extreme mới;
   - persistent wick floor collision;
   - split rejection retest;
   - daily overlay và final spacing/gap-fill winner thay đổi.
4. Thêm benchmark script chỉ đọc source DB và chạy trên một temporary copy/cache để không làm bẩn production cache.
5. Ghi baseline elapsed time, detector builds/cache hits và số snapshots dưới dạng JSON/text ổn định.

Acceptance:

- Golden tests chạy được với detector hiện tại trước khi có incremental code.
- Benchmark có thể lặp lại cùng range mà không đọc `.env`, wallet hoặc logs.

### Bước 2 — tách feature extraction khỏi zone materialization

Files dự kiến:

- `src/zones/detector.py`
- `src/zones/types.py`
- `tests/test_zones.py`

Thực hiện:

1. Tạo một dataclass nội bộ chứa detector evidence:
   - canonical OHLC view;
   - closes/current price;
   - raw external pivots;
   - prominent external pivots;
   - internal pivots;
   - daily pivots/zones;
   - first reclaim indexes.
2. Tách pipeline hiện tại thành hai phần:
   - stateless extraction từ full DataFrame;
   - pure materialization từ evidence qua candidates, clustering, rejection, daily/persistent overlay, gap-fill và spacing.
3. Cho public `detect_support_resistance_zones` tiếp tục gọi hai phần trên theo stateless path.
4. Không đổi public signature hoặc output.

Acceptance:

- Toàn bộ `tests/test_zones.py` và `tests/test_backtest.py` vẫn pass.
- Stateless before/after refactor deep-equal tại mọi golden prefix.
- Chưa tích hợp incremental vào backtest ở bước này.

### Bước 3 — implement `IncrementalZoneDetectorState`

File mới dự kiến:

- `src/zones/incremental.py`

State tối thiểu:

- Ordered 4h open times và OHLC arrays.
- Rolling true ranges/ATR window.
- Unconfirmed external/internal pivot centers ở right edge.
- Confirmed raw external/internal pivots.
- Prominent pivot reducer state và structure-role history.
- Pending high pivots theo reclaim threshold và first reclaim index.
- Current UTC daily bucket, completed daily OHLC và daily pivot state.
- Recent internal pivot deque cho local 150-bar lookback.
- Indexes cần cho split rejection và support-floor retests.

API dự kiến:

```python
state = IncrementalZoneDetectorState(zone_config)
state.advance(four_hour_candle)
evidence = state.snapshot_evidence(zone_set_as_of)
zones = materialize_support_zones(evidence, zone_config)
```

Thực hiện cho mỗi candle:

1. Validate 4h alignment, closed status, strict chronological order và không trùng open time.
2. Append OHLC và cập nhật true range/rolling ATR.
3. Kiểm tra đúng một external pivot center vừa đủ right bars và một internal pivot center vừa đủ một right bar.
4. Khi pivot mới được xác nhận, kiểm tra các bars từ pivot đến current watermark để không bỏ reclaim xảy ra trước confirmation.
5. Với close mới, resolve pending reclaim thresholds và đóng băng first reclaim index.
6. Cập nhật prominent reducer; xử lý replace-last khi có pivot cùng loại nhưng extreme hơn.
7. Cập nhật/expire local evidence.
8. Khi kết thúc ngày UTC thứ sáu, finalize daily candle và cập nhật daily pivots.

Acceptance:

- Sau mỗi `advance`, evidence materialization deep-equal stateless detector tại cùng prefix.
- Test fail-closed cho out-of-order, duplicate, gap và unclosed candle.
- Không dùng candle sau watermark trong bất kỳ field nào.

### Bước 4 — tích hợp incremental state vào cold backtest

Files dự kiến:

- `src/trading/backtest.py`
- `src/trading/zone_refresh.py`
- `src/trading/backtest_zone_cache.py`
- `tests/test_backtest.py`

Flow mới:

1. Vẫn aggregate mỗi 4h bucket đúng một lần và build exact prefix hashes như hiện tại.
2. Bulk-check cache rows cho toàn bộ requested watermarks bằng một SQLite connection.
3. Nếu mọi snapshot đều cache hit, không khởi tạo incremental state; giữ warm path nhanh.
4. Tại cache miss đầu tiên, ingest history đến watermark đó đúng một lần để tạo state.
5. Với các watermark sau, `advance` state qua những candle chưa xử lý; cache hit vẫn có thể bỏ materialization nhưng state phải tiến đến đúng watermark trước cache miss kế tiếp.
6. Cache miss gọi `snapshot_evidence` + full materialization, fingerprint và store snapshot như hiện tại.
7. Custom detector injection trong tests tiếp tục dùng stateless/full-frame behavior để không thay contract test hiện có.
8. Giữ nguyên `zone_snapshot_count`, `zone_rebuild_count`, `zone_cache_hit_count` và bổ sung metric nội bộ/CLI nếu hữu ích:
   - `zone_state_ingested_candles`;
   - `zone_full_history_scans` (target tối đa 1 ở cold run).

Acceptance:

- Cold, warm, mixed-hit, extended-range, corrupt-cache và changed-candle/config tests pass.
- BUY rows, zone segments và all snapshots deep-equal baseline.
- Backtest vẫn không mutate `zones`, `zone_sets`, `bot_state` hoặc `decisions` live tables.
- Interrupted cold run vẫn để lại các completed cache snapshots có thể reuse.

### Bước 5 — benchmark Milestone 1 và quyết định có cần Milestone 2

Chạy ít nhất:

1. Unit/golden parity suite.
2. Full `pytest`.
3. Cold benchmark 440 snapshots trên cùng temporary DB copy.
4. Warm benchmark cùng range.
5. Extended range chỉ thêm watermark mới.
6. Profile incremental cold run để xác định hotspot còn lại.

Decision gate:

- Nếu cold `<=20s`, parity pass và warm regression `<=10%`: ship Milestone 1.
- Nếu cold `>20s`: tối ưu hotspot đo được trước khi cân nhắc dirty materialization.
- Chỉ triển khai Milestone 2 nếu materialization thật sự là bottleneck sau profile; không đoán trước.

#### Kết quả đo (2026-08-18)

Machine: macOS 26.5.2 arm64, Python 3.11.14. Source DB copy only; production `backtest_zone_cache` không bị ghi. Range và fixture trùng baseline: `2026-06-01T00:00:00Z` → `2026-08-13T06:00:00Z`, config mặc định.

Correctness:

- Golden / incremental parity: 19 passed (`tests/test_incremental_zone_detector.py` + backtest cache/parity tests).
- Full `pytest`: 178 passed.

Official cold + warm (một temp copy, cache cleared trước cold):

- 1.758 nến 1h, 440 snapshots, 32 BUY.
- Cold: `31,866s`, 440 rebuilds, 0 hits, 5.899 candles ingested, 1 full-history scan. Baseline cold `56,4s` → khoảng `1,77x`. Target `<=20s` **không đạt**.
- Warm: `2,33s`, 0 rebuilds, 440 hits, 0 ingested, 0 scans. Baseline warm `2,23s` → chậm `4,5%`, trong hạn `10%`.

Extended range (cùng copy, cold đến `2026-08-01T00:00:00Z` rồi mở đến `2026-08-13T06:00:00Z`):

- Partial: 367 snapshots, 367 rebuilds, `27,434s`, 1 scan.
- Extended: 440 snapshots, **73 rebuilds + 367 hits**, `10,84s`, 1 scan, ingested 5.899. Đúng “chỉ build watermark mới”.

Profile cold (cProfile, wall ~76s vì overhead; dùng cumtime/tottime chứ không dùng elapsed này cho gate):

- `run_backtest` ~76s; `build_fingerprinted_support_zones_from_evidence` ~56s; `materialize_support_zones` ~45s.
- Incremental ingest rẻ: `advance_to` ~4,9s cho 5.899 nến.
- Hotspot materialization: split-rejection retest (`_first_split_rejection_retest` ~20s tottime, ~59 triệu lambda), persistent-wick stair/gap-fill (`_fill_persistent_wick_floor_gaps` / `_stair_step_support_candidates` ~11s / ~9s), cluster (`_candidate_matches_cluster` / `_cluster_support_candidates`).
- Chi phí phụ: `snapshot_evidence` copy pivot (`dataclasses.replace` ~1,5 triệu lần, ~8s), fingerprint ~9s.

Quyết định:

- **Không ship Milestone 1** theo gate `cold <=20s`.
- **Chưa làm Milestone 2.** Materialization đúng là bottleneck, nhưng gate yêu cầu tối ưu hotspot đo được trên full materialization trước (split-rejection retest, stair-step gap-fill, snapshot copy), rồi mới cân nhắc dirty materialization.

### Bước 6 — Milestone 2: dirty price-component materialization (optional)

Chỉ bắt đầu sau decision gate ở Bước 5.

Thiết kế:

1. Index candidates theo `(bounds_style, price bucket)` và origin family.
2. Một evidence event đánh dấu dirty interval theo anchor price cộng các constants merge/spacing liên quan.
3. Rebuild dirty connected component cùng một neighbor phía dưới/trên để xử lý cluster, macro merge và ladder-slot conflict.
4. Các event có ảnh hưởng global dùng safe fallback full materialization, ví dụ:
   - prominent reducer replace làm mất một pivot cũ;
   - daily/persistent overlay thay winner;
   - current-price crossing làm thay gap-fill eligibility trên nhiều khoảng;
   - local lookback expiry làm mất candidate thắng.
5. Ghép component mới với các component sạch, rồi chạy invariant validator; validator fail thì fallback full materialization cho snapshot đó.

Acceptance:

- Vẫn deep-equal stateless oracle tại mọi prefix.
- Có test chứng minh fallback được gọi cho từng global event.
- Cold target `5–10s` trên benchmark baseline.

### Bước 7 — docs và rollout

Files dự kiến:

- `README.md`
- `plans/backtest.md`

Thực hiện:

1. Mô tả cold incremental state, warm snapshot cache và invalidation behavior.
2. Ghi benchmark before/after thực tế, machine/runtime context và range.
3. Rollout incremental chỉ cho offline backtest trước.
4. Live `refresh_zones` tiếp tục stateless rebuild cho đến khi backtest parity đã ổn định; migrate live là task riêng.
5. Giữ stateless implementation làm oracle/fallback thay vì xóa ngay.

## Definition of done

- Mọi incremental snapshot deep-equal stateless snapshot trên golden và production-derived temporary fixture.
- BUY output và chart segments không đổi.
- Full test suite pass.
- Cold benchmark đạt ít nhất `<=20s`; warm regression không quá `10%`.
- Cache invalidation và partial-run recovery vẫn đúng.
- README và backtest plan phản ánh behavior mới.

