# Zone Algorithm Study Guide

This guide follows the new `src/zones/` package in the order the algorithm runs. Read one block at a time, then jump into the matching file.

## Big Picture

The detector is a support-only market-structure algorithm for closed OHLC candles. It returns the legacy shape:

```python
{
    "support": [...],
    "resistance": [],
    "active": [],
    "all": [...],
}
```

Even when a support-biased zone is above or touching the current price, it stays in `support`. The `price_state` field records whether price currently sees that zone as `support`, `active`, or `resistance`.

The main pipeline lives in `src/zones/detector.py`:

1. Clean OHLC data.
2. Compute ATR.
3. Find raw external swing pivots.
4. Filter those pivots into prominent structure. Return empty zones if none survive.
5. Label both raw and prominent pivots as `H`, `HH`, `LH`, `L`, `HL`, or `LL`.
6. Convert pivots into support candidates (prominent lows/reclaimed highs plus wick-floor evidence from raw lows).
7. Cluster candidates into fixed-width zones.
8. Macro-consolidate nearby zones, collapse adjacent duplicate bands, suppress remaining close zones by midpoint, remove overlaps, fill large staircase gaps, and sort zones by price from low to high.

Fixed constants live in `src/zones/types.py`. Runtime tuning comes from `ZoneConfig` in `config.yaml` (`external_swing_order`, `min_touches`, `role_buffer_pct`, and the ATR/swing thresholds).

## Block 1: OHLC And ATR

Read: `src/zones/ohlc.py`

Key functions:

- `_coerce_ohlc`
- `_average_true_range`

What to understand:

- `_coerce_ohlc` accepts only `open`, `high`, `low`, and `close`.
- It converts those columns to numeric values, drops bad rows, and resets the index.
- `_average_true_range` calculates rolling ATR from high/low/close movement.
- ATR is later used to decide whether a swing is prominent enough and whether a high has been meaningfully reclaimed.

Study question:

- If a candle has a non-numeric close, what happens to that row before pivot detection?

## Block 2: Structure Pivots

Read: `src/zones/pivots.py`

Key functions:

- `_find_structure_pivots`
- `_filter_prominent_structure_pivots`
- `_label_structure_pivots`

What to understand:

- `_find_structure_pivots` scans each candle with a left/right window measured in `bars_each_side`.
- The detector uses `external_swing_order` from config. `internal_swing_order` is chart-only and only runs when `show_internal_pivots` is enabled.
- A pivot high must be the unique highest high in its window.
- A pivot low must be the unique lowest low in its window.
- Each pivot stores both wick price and body edge price:
  - High pivot body price: `max(open, close)`
  - Low pivot body price: `min(open, close)`
- `_filter_prominent_structure_pivots` reduces noisy raw pivots into larger structure swings.
- A move is prominent when it clears the configured ATR or percent threshold.
- `_label_structure_pivots` labels structure:
  - Highs: first `H`, then `HH` or `LH`
  - Lows: first `L`, then `HL` or `LL`

Study questions:

- Why does the algorithm require pivot highs/lows to be unique inside the window?
- What changes when `external_swing_order` increases?
- Why might body price be more useful than wick price for zone construction?

## Block 3: Support Candidates

Read: `src/zones/candidates.py`

Key functions:

- `_support_candidates`
- `_candidate_from_pivot`
- `_support_floor_candidates`
- `_high_is_confirmed_reclaimed`
- `_first_reclaim_index`

What to understand:

Support candidates are raw evidence points. They are not zones yet.

The algorithm creates support candidates from:

- Prominent swing lows: `structure_swing_low` (uses body price)
- Reclaimed swing highs: `flipped_resistance` (uses body price)
- Long-wick low floors: `structure_swing_low_wick` (uses wick price, `bounds_style = support_floor`)
- Retests near those wick floors: `structure_swing_low_body_floor` (uses body price, `bounds_style = support_floor`)

A prominent low qualifies for wick-floor evidence when `body_price - wick_price >= zone_width` (currently `$500`). Retests must land within `20%` of zone width (`STRUCTURE_SUPPORT_FLOOR_RETEST_WIDTH_MULT = 0.2`, so `$100` today).

Reclaimed highs matter because old resistance can become support. `_first_reclaim_index` looks forward after a high pivot and finds the first close above the pivot wick high plus `break_atr_mult * ATR` (default `0.2 * ATR`).

Study questions:

- Why does a high pivot need a future close above it before it becomes support evidence?
- What is the difference between a `structure_swing_low` and a `structure_swing_low_wick` candidate?

## Block 4: Building Zones

Read: `src/zones/build.py`

Key functions:

- `_build_support_zones`
- `_zone_from_support_cluster`
- `_fixed_support_zone_bounds`
- `_fixed_support_floor_zone_bounds`
- `_consolidate_support_zones`

What to understand:

Candidates become zones by clustering nearby source prices.

Important rules:

- Candidates only cluster when their prices fit inside the fixed zone width and share the same `bounds_style` (`body` vs `support_floor` never mix in one cluster). Different candidate types use different prices and different ways to draw the zone. They can be combined later (macro consolidation, body–floor bridge), but not in the initial clustering step.
- The current fixed width is `STRUCTURE_ZONE_WIDTH = 500.0`.
- A zone needs at least `min_touches` unique source touches, counted as distinct `(index, origin)` pairs in the cluster.
- Normal body zones anchor downward from the support upper anchor:
  - `high = support_upper_anchor`
  - `low = high - zone_width`
  - For clusters with more than 10 source prices, `support_upper_anchor` is the 10th percentile of sorted prices (not simply the max). Smaller clusters use the max price.
- Support-floor zones anchor upward from the wick/body floor:
  - `low = support_floor`
  - `high = low + zone_width`
  - Large clusters use the 10th percentile of sorted prices as the floor anchor; smaller clusters use the min price.
- Macro consolidation can combine nearby small zones when:
  - the gap between zones is `<= STRUCTURE_MACRO_GAP` (`300.0`)
  - all source prices in the group span `<= STRUCTURE_MACRO_MAX_SOURCE_SPAN` (`2000.0`)
  - `bounds_style` rules allow grouping (body zones can absorb a trailing support-floor shelf)
- After macro consolidation, a body swing-low zone can bridge to the next support-floor zone when:
  - the lower zone is `structure_swing_low`
  - the upper zone is `structure_support_floor`
  - the edge gap between them is `<= STRUCTURE_BODY_FLOOR_BRIDGE_MAX_GAP` (`1000.0`)
  - the upper floor zone confirms the bridge, but does not stretch the output width
  - the bridge zone uses the lower zone high as `low` and `low + STRUCTURE_ZONE_WIDTH` as `high`
  - this consumes the two bracket shelves plus any lower support-floor companion built from the same swing-low indexes
  - the result is one fixed-width mixed-structure band over the manual support area
- After macro consolidation, the builder suppresses duplicate nearby zones in two passes:
  - Adjacent bands with the same `bounds_style` and edge gap `< STRUCTURE_ADJACENT_ZONE_MIN_GAP` (`650.0`) collapse to the upper zone unless the lower band has at least `STRUCTURE_ADJACENT_STRONGER_TOUCH_MARGIN` (`3`) more touches. That keeps a dense `65.9k` shelf with `10` touches while still dropping a weaker `63.0k` band below `64.0k` (`8` vs `6` touches).
  - Remaining zones use `STRUCTURE_IMPORTANT_ZONE_SPACING = 1000.0` midpoint spacing, keeping the stronger zone by score, then touches, then narrower width.
- That spacing is intentionally narrower than the old `$1600` midpoint rule so adjacent BTC 4H levels can survive when they represent separate structure, such as a `73.3k` support band below a stronger `74.5k` band (`721` edge gap).
- Zone score starts as `touches * 2 + flipped_resistance_count`.

Study questions:

- Why does normal support use the upper edge as the anchor?
- Why does wick-floor support use the lower edge as the anchor?
- How does `min_touches` reduce weak zones?
- What is the tradeoff when nearby-zone suppression is too wide?

## Block 5: Post-Processing

Read: `src/zones/postprocess.py`

Key functions:

- `_make_support_zones_distinct`
- `_fill_support_staircase_gaps`
- `_best_support_staircase_gap_fill`
- `_stair_step_support_candidates`
- `_classify_price_state`

What to understand:

Post-processing makes the zone list usable:

- Overlapping zones are reduced to the stronger zone (score, then touches, then narrower width).
- Each zone gets a current `price_state` using `role_buffer_pct` (passed as `buffer_pct`, default `0.15%`):
  - `support`: zone high is below price minus the buffer
  - `active`: price is inside or near the zone
  - `resistance`: zone low is above price plus the buffer
- Large gaps between support zones can be filled with dense reclaimed-high clusters.
- A gap must exceed `STRUCTURE_STAIR_STEP_MAX_SUPPORT_GAP` (`4000.0`) to qualify.
- Staircase filling uses all current zones as upper/lower boundaries, but only fills upward from a lower zone that is currently classified as `support`.
- This matters when the next structural zone is `active` or just above current price: it can still act as the upper boundary for missing reclaimed-high support levels below it.
- Staircase candidates come from raw external high pivots with origin `stair_step_flipped_resistance`. They must be reclaimed, sit strictly between the two boundary zones, and stay below `current_price * (1 - buffer_pct)`.
- The filler runs up to `STRUCTURE_STAIR_STEP_MAX_INSERTIONS` (`6`) times. After each insertion it re-runs overlap cleanup.
- Final zones are sorted by price from low to high.

The staircase fill is designed for markets that moved upward through multiple resistance levels. Those reclaimed highs may form intermediate support even if they were not part of the prominent pivot set.

Example from the BTCUSDT 4H study:

- The `73.3k` band came from existing body/reclaimed-high evidence, but used to be suppressed because it was too close to the stronger `74.5k` zone.
- The `63.0k` flipped-resistance band used to survive below `64.0k` because midpoint spacing was `$1081` even though the edge gap was only `$581`. Adjacent body-band collapse now drops the lower shelf when it is not much stronger (`8` vs `6` touches).
- The `65.9k` band with `10` touches used to disappear when weaker `66.7k` and `67.7k` neighbors chained on top of it during collapse. The stronger-touch margin now keeps that shelf.
- The `67.6k` and `70.4k` bands came from dense reclaimed-high clusters, but used to be skipped when the next boundary was not already classified as support.

Study questions:

- Why does staircase filling use raw external pivots instead of only prominent pivots?
- What makes one overlapping support zone preferred over another?
- Why should an active structural zone still be allowed to bound a gap-fill search?
- Why sort final support zones by price from low to high?

## Block 6: Detector Orchestration

Read: `src/zones/detector.py`

Key functions:

- `detect_support_resistance_zones`
- `detect_support_resistance_zones_structure_v1`

What to understand:

This file should read like the table of contents for the algorithm. It does not own the details; it wires the blocks together.

Important details:

- `detect_support_resistance_zones` is the stable public wrapper.
- `detect_support_resistance_zones_structure_v1` is the current implementation.
- `internal_swing_order` lives on `ZoneConfig` but is only used by the chart server to draw internal pivots; the detector itself only uses `external_swing_order`.
- Empty OHLC, insufficient bars for the swing window, or zero prominent external pivots all return empty zone lists.
- `_support_candidates` receives both `raw_external_pivots` (all swings) and `external_pivots` (prominent swings).
- Resistance and active top-level lists are intentionally empty in this phase; use each zone's `price_state` instead.

Study question:

- Can you explain the full pipeline from candles to final `support` list without opening any helper function?

## Output Fields

Every support zone includes:

- `origin`: what kind of evidence formed the zone (`structure_swing_low`, `flipped_resistance`, `structure_support_floor`, `stair_step_flipped_resistance`, `mixed_structure`, etc.).
- `role`: currently always `"support"`.
- `bounds_style`: `"body"` or `"support_floor"`.
- `low`, `high`, `mid`: zone boundaries.
- `width`, `width_pct`: zone size.
- `touches`: number of source touches in the cluster.
- `source_closes`: source prices used to form the zone. These are often body edges, not literal candle closes.
- `source_indexes`: candle indexes for the source touches.
- `score`: `touches * 2 + flipped_resistance_count`.
- `structure_role`: pivot role such as `HL`, `LL`, `HH`, or `mixed`.
- `structure_bias`: currently `"support"`.
- `price_state`: current position of price relative to the zone (`support`, `active`, or `resistance`).
- `last_touch_index`: latest source candle index.
- `broken_index`: reclaim candle index for flipped resistance evidence, when available.
- `zone_width`: fixed width used to build the zone.

## Suggested Study Path

1. Run `python3 -m pytest tests/test_zones.py`.
2. Open `tests/test_zones.py`.
3. For each test, identify which block it exercises.
4. Read the matching file in `src/zones/`.
5. Change only one config value in your head, such as `external_swing_order` or `min_touches`, and predict which tests would be affected.

Useful tests to start with:

- `test_internal_and_external_structure_pivots_have_different_granularity`
- `test_prominent_structure_pivots_ignore_small_reversals_and_keep_extremes`
- `test_prominent_structure_pivots_can_require_percent_move`
- `test_structure_v1_clusters_external_swing_lows_with_fixed_500_dollar_width`
- `test_structure_v1_returns_reclaimed_highs_as_support_only`
- `test_structure_v1_adds_retested_long_wick_support_floor`
- `test_support_bands_anchor_to_support_upper_anchor`
- `test_support_floor_shelf_is_not_swallowed_by_body_macro_group`
- `test_structure_v1_fills_large_support_gap_with_reclaimed_high_clusters`
- `test_structure_v1_fills_staircase_gap_to_next_active_boundary`
- `test_nearby_reclaimed_high_zone_survives_next_major_level`
- `test_adjacent_close_support_zone_collapses_to_upper_band`
- `test_stronger_adjacent_lower_support_zone_survives_weak_upper_neighbors`
- `test_structure_v1_output_is_signal_compatible`

## Mental Model

Think of the algorithm as evidence collection plus cleanup:

```text
candles
  -> pivots
  -> prominent structure
  -> support evidence
  -> clustered zones
  -> distinct ranked support list
```

The detector is not trying to draw every possible support and resistance level. It is trying to produce a small, support-biased list of meaningful dip-buying areas from closed 4H structure.
