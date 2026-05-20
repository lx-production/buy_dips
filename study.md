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
4. Filter those pivots into prominent structure.
5. Label pivots as `H`, `HH`, `LH`, `L`, `HL`, or `LL`.
6. Convert pivots into support candidates.
7. Cluster candidates into fixed-width zones.
8. Remove overlaps, fill large staircase gaps, and sort zones by distance to price.

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

- `_find_structure_pivots` scans each candle with a left/right window measured in `bars_each_side` (config feeds this via `internal_swing_order` / `external_swing_order`).
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

- Prominent swing lows: `structure_swing_low`
- Reclaimed swing highs: `flipped_resistance`
- Long-wick low floors: `structure_swing_low_wick`
- Retests near those wick floors: `structure_swing_low_body_floor`

Reclaimed highs matter because old resistance can become support. `_first_reclaim_index` looks forward after a high pivot and finds the first close above the pivot high plus an ATR-based threshold.

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

- Candidates only cluster when their prices fit inside the fixed zone width.
- The current fixed width is `STRUCTURE_ZONE_WIDTH = 500.0`.
- A zone needs at least `min_touches` unique source touches.
- Normal support zones anchor downward from the support base:
  - `high = support_base`
  - `low = high - zone_width`
- Support-floor zones anchor upward from the wick/body floor:
  - `low = support_floor`
  - `high = low + zone_width`
- Macro consolidation can combine nearby small zones when their source prices still form one broader structure area.

Study questions:

- Why does normal support use the upper edge as the anchor?
- Why does wick-floor support use the lower edge as the anchor?
- How does `min_touches` reduce weak zones?

## Block 5: Post-Processing

Read: `src/zones/postprocess.py`

Key functions:

- `_make_support_zones_distinct`
- `_fill_support_staircase_gaps`
- `_best_support_staircase_gap_fill`
- `_stair_step_support_candidates`
- `_classify_price_state`
- `_zone_distance_sort_key`

What to understand:

Post-processing makes the zone list usable:

- Overlapping zones are reduced to the stronger zone.
- Each zone gets a current `price_state`.
- Large gaps between support zones can be filled with dense reclaimed-high clusters.
- Final zones are sorted by distance to `current_price`, then score, then touches.

The staircase fill is designed for markets that moved upward through multiple resistance levels. Those reclaimed highs may form intermediate support even if they were not part of the prominent pivot set.

Study questions:

- Why does staircase filling use raw external pivots instead of only prominent pivots?
- What makes one overlapping support zone preferred over another?
- Why sort by distance to current price at the end?

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
- `internal_swing_order` and `zone_tolerance_pct` are accepted for compatibility but not used by this support-only v1 path.
- Empty or insufficient data returns empty zone lists.
- Resistance and active lists are intentionally empty in this phase.

Study question:

- Can you explain the full pipeline from candles to final `support` list without opening any helper function?

## Output Fields

Every support zone includes:

- `origin`: what kind of evidence formed the zone.
- `role`: currently always `"support"`.
- `low`, `high`, `mid`: zone boundaries.
- `width`, `width_pct`: zone size.
- `touches`: number of source touches.
- `source_closes`: source prices used to form the zone. These are often body edges, not literal candle closes.
- `source_indexes`: candle indexes for the source touches.
- `score`: simple strength score.
- `structure_role`: pivot role such as `HL`, `LL`, `HH`, or `mixed`.
- `structure_bias`: currently `"support"`.
- `price_state`: current position of price relative to the zone.
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
- `test_structure_v1_clusters_external_swing_lows_with_fixed_500_dollar_width`
- `test_structure_v1_returns_reclaimed_highs_as_support_only`
- `test_structure_v1_fills_large_support_gap_with_reclaimed_high_clusters`

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
