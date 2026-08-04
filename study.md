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
3. Find raw external swing pivots and one-bar internal pivots.
4. Filter the external pivots into prominent structure. Return empty zones if none survive.
5. Label raw, prominent, and internal pivots as `H`, `HH`, `LH`, `L`, `HL`, or `LL`.
6. Convert external pivots into support candidates (prominent lows/reclaimed highs plus wick-floor evidence from raw lows).
7. Build fixed-width zones through four ordered stages: cluster candidates, merge macro groups, bridge confirmed body/floor pairs, then suppress nearby duplicates.
8. Build recent variable-width reaction zones from repeated internal lows and reclaimed internal highs, add retested-flip zones when greedy clusters split the evidence, then select a clean local ladder before overlaying it with macro zones.
9. Remove overlaps and fill large staircase gaps.
10. Derive complete 1D candles from the 4H data, build fixed-width daily body-support zones from prominent daily low pivots, and let them replace overlapping 4H `mixed_structure` bridge zones.
11. Sort zones by price from low to high.

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
- The detector uses `external_swing_order` from config for major structure. It also uses a fixed one-bar internal window for recent reaction zones. `internal_swing_order` remains a chart-debug setting and only controls the optional internal pivots drawn by the chart server.
- A pivot high must be the unique highest high in its window.
- A pivot low must be the unique lowest low in its window.
- Each `StructurePivot` stores both `wick_price` and `body_price`:
  - High pivot `wick_price`: candle high
  - Low pivot `wick_price`: candle low
  - High pivot `body_price`: `max(open, close)`
  - Low pivot `body_price`: `min(open, close)`
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

Read: `src/zones/candidates.py`, `src/zones/reactions.py`, then `src/zones/daily.py`

Key functions:

- `_support_candidates`
- `_candidate_from_pivot`
- `_support_floor_candidates`
- `_high_is_confirmed_reclaimed`
- `_first_reclaim_index`
- `_build_local_reaction_zones`
- `_zone_from_local_reaction_cluster`
- `_build_retested_flip_zones`
- `_select_local_reaction_zones`
- `_build_daily_body_support_zones`
- `_overlay_daily_support_zones`

What to understand:

Support candidates are raw evidence points. They are not zones yet.

The algorithm creates support candidates from:

- Prominent swing lows: `structure_swing_low` (uses body price)
- Reclaimed swing highs: `flipped_resistance` (uses body price)
- Long-wick low floors: `structure_swing_low_wick` (uses wick price, `bounds_style = support_floor`)
- Retests near those wick floors: `structure_swing_low_body_floor` (uses body price, `bounds_style = support_floor`)

A prominent low qualifies for wick-floor evidence when `body_price - wick_price >= zone_width` (currently `$500`). Retests must land within `20%` of zone width (`STRUCTURE_SUPPORT_FLOOR_RETEST_WIDTH_MULT = 0.2`, so `$100` today).

Reclaimed highs matter because old resistance can become support. `_first_reclaim_index` looks forward after a high pivot and finds the first close above the pivot wick high plus `break_atr_mult * ATR` (default `0.2 * ATR`).

The recent-reaction path complements that macro evidence. It examines only the latest `150` candles (25 days on 4H), clusters one-bar internal pivot body prices inside the same `$500` maximum span, and requires both:

- at least `max(2, min_touches)` distinct internal lows
- at least one reclaimed internal high in the cluster

Unlike macro zones, these bands use observed structure for variable-width bounds. The upper edge is the highest low-body anchor in the cluster. If reclaimed resistance existed before that low, the strongest such body sets the lower edge; otherwise the anchor candle's wick sets the lower edge. A recent reaction band must be no wider than `$500`; it may sit above current price, and `price_state` records whether it is currently support, active, or resistance.

Greedy price clustering can split useful local evidence. For example, a reclaimed high can land in one cluster while later low-body retests land just above it in the next cluster. `_build_retested_flip_zones` handles that case by pairing a reclaimed internal high with the first later low body above it, then collecting later held lows up to that first retest price. This creates a body-to-body local zone without broadening the original cluster span rule.

After normal local clusters and retested-flip zones are built, `_select_local_reaction_zones` keeps a clean recent ladder. It drops very thin local bands, groups nearby local candidates into the same ladder slot, prefers retested-flip zones inside a slot, then prefers the lower band. This prevents dense recent noise from swallowing the first meaningful reaction levels.

This produces the June 2026 BTC examples without broadening every historical zone:

- `61,056.47-61,328.00`: recent reclaimed resistance plus repeated low bodies; it is produced by the retested-flip path after greedy clustering splits the high and held lows.
- `62,205.00-62,545.99`: a local low wick-to-body reaction validated by later internal lows; the larger 150-candle lookback includes the source candle.

Study questions:

- Why does a high pivot need a future close above it before it becomes support evidence?
- What is the difference between a `structure_swing_low` and a `structure_swing_low_wick` candidate?
- Why are recent reaction zones variable-width while macro structure zones remain fixed-width?

The daily overlay is separate from the 4H candidate path. `aggregate_ohlc_to_daily` derives only complete 1D candles for zone detection, requiring six closed 4H candles per day. `_build_daily_body_support_zones` then finds prominent daily low pivots and anchors a fixed-width support band from the daily body low:

- `high = daily_low_pivot_body_low`
- `low = high - STRUCTURE_ZONE_WIDTH`

For example, the May 1, 2024 daily red candle has a body low/close at `58,364.97`, so its daily body-support zone is `57,864.97-58,364.97`. During overlay, this higher-timeframe zone can replace an overlapping 4H `mixed_structure` bridge zone such as `57,500.00-58,000.00`.

A deep external swing-low rejection can also be split when its lower wick spans at least two zone widths and a higher-low retest arrives within four candles. The rejection wick and retest wick form `wick_retest_support`; the rejection candle's body top anchors the fixed-width `body_rejection_support` shelf. The pair replaces an ambiguous `mixed_structure` band contained between those levels.

## Block 4: Building Zones

Read: `src/zones/build.py`, then `src/zones/factory.py`

Key functions:

- `_build_support_zones`
- `_cluster_support_candidates`
- `_merge_support_macro_groups`
- `_bridge_body_floor_support_gaps`
- `_suppress_nearby_support_zones`
- `factory._zone_from_support_cluster`
- `factory._make_support_zone`
- `factory._fixed_support_zone_bounds`
- `factory._fixed_support_floor_zone_bounds`

What to understand:

`_build_support_zones` is the table of contents for this block. It deliberately runs four transformations in order:

1. Cluster compatible candidates and discard clusters below `min_touches`.
2. Merge compatible clusters into macro zones.
3. Replace confirmed body/floor pairs with bridge zones.
4. Collapse adjacent duplicates and enforce midpoint spacing.

The order is part of the algorithm. These rules use different thresholds and precedence, so combining them into one generic merge pass would change behavior.

`build.py` owns those policies. `factory.py` owns zone representation: calculating bounds, aggregating candidate metadata, and producing the standard zone dictionary. All initial, macro-merged, and bridged zones go through `_make_support_zone`, which prevents their output fields from drifting apart.

Important rules:

- Candidates only cluster when their prices fit inside the fixed zone width and share the same `bounds_style` (`body` vs `support_floor` never mix in one cluster). Different candidate types use different prices and different ways to draw the zone. They can be combined later (macro consolidation, body–floor bridge), but not in the initial clustering step.
- The current width constant is `STRUCTURE_ZONE_WIDTH = 500.0`, but `$500` is not a minimum width and no longer means that every output band must be `$500` wide. It has three related jobs:
  - Maximum candidate span: evidence points more than `$500` apart cannot enter the same initial cluster.
  - Fixed-width fallback: macro body, support-floor, macro-merged, and bridged zones remain exactly `$500` wide because their candidates generally provide anchor prices, not complete lower/upper boundaries.
  - Maximum local width: a recent `local_reaction` zone may be narrower when candle structure supplies both edges, but it is rejected if those edges are more than `$500` apart.
- This distinction is based on the quality of the evidence, not on a preference for narrow zones:
  - A macro candidate such as a swing-low body says where support is anchored, but it does not identify the other edge of the band. Drawing `$500` around that anchor is an explicit fallback assumption.
  - A local reaction can provide a proximal edge and a distal edge: for example, reclaimed resistance can define one edge while a later low body defines the other, or a rejection candle's wick and body can define the pair directly. In that case, expanding the observed band to `$500` would discard useful precision.
  - The raw spread of macro `source_closes` is not a safe replacement for fixed width. Identical or tightly grouped point anchors could otherwise create a zero-width or unrealistically thin band even though no candle evidence established those boundaries.
- Therefore, widths below `$500` are allowed only for evidence-bounded local reactions. Current examples are:
  - Purple: `61,056.47–61,328.00`, width `$271.53`.
  - Blue: `62,205.00–62,545.99`, width `$340.99`.
- If variable widths are later extended to macro zones, their candidate model should first store meaningful lower and upper edges. The resulting width should still be validated by repeated evidence and constrained by a sensible minimum-noise rule and a maximum cap; simply shrinking every macro zone to `min(source_closes)..max(source_closes)` would be structurally unjustified.
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
- Recent reaction zones use `bounds_style = local_reaction`. They are selected into a local ladder inside `reactions.py` first, then join the fixed-width zones. The final global spacing still decides whether a recent band replaces a nearby macro band.

Implementation boundaries:

- `build.py`: clustering, macro grouping, body/floor bridging, and nearby-zone selection policy.
- `reactions.py`: recent internal evidence, variable reaction bounds, retested-flip recovery, local ladder selection, and the 150-candle freshness window.
- `daily.py`: complete 4H-to-1D aggregation, daily low-pivot body support zones, and higher-timeframe replacement of overlapping 4H mixed-structure bridge zones.
- `factory.py`: bounds, anchors, origin/role aggregation, and construction of complete zone dictionaries.
- `state.py`: shared `support`/`active`/`resistance` price-state classification, used by both building and post-processing without a circular import.

Study questions:

- In which cases is `$500` an output width, and in which cases is it only a maximum?
- Why is `min(source_closes)..max(source_closes)` insufficient to define macro-zone boundaries?
- Why does normal support use the upper edge as the anchor?
- Why does wick-floor support use the lower edge as the anchor?
- How does `min_touches` reduce weak zones?
- What is the tradeoff when nearby-zone suppression is too wide?

## Block 5: Post-Processing

Read: `src/zones/postprocess.py` and `src/zones/state.py`

Key functions:

- `_make_support_zones_distinct`
- `_fill_support_staircase_gaps`
- `_best_support_staircase_gap_fill`
- `_stair_step_support_candidates`
- `state._classify_price_state`

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
- `internal_swing_order` lives on `ZoneConfig` but only controls chart-debug pivots. Detection uses `external_swing_order` for macro structure and a fixed one-bar internal window for recent reactions.
- Empty OHLC, insufficient bars for the swing window, or zero prominent external pivots all return empty zone lists.
- `_support_candidates` receives both `raw_external_pivots` (all swings) and `external_pivots` (prominent swings).
- Daily overlays use the same external swing settings, but on derived complete 1D candles. They run after 4H macro/local zones and staircase fills.
- Resistance and active top-level lists are intentionally empty in this phase; use each zone's `price_state` instead.

Study question:

- Can you explain the full pipeline from candles to final `support` list without opening any helper function?

## Output Fields

Every support zone includes:

- `origin`: what kind of evidence formed the zone (`structure_swing_low`, `flipped_resistance`, `structure_support_floor`, `local_reaction_support`, `local_retested_flip_support`, `stair_step_flipped_resistance`, `mixed_structure`, etc.).
- `role`: currently always `"support"`.
- `bounds_style`: `"body"`, `"support_floor"`, or `"local_reaction"`.
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
- `zone_width`: fixed macro width, also used as the maximum allowed width for a local reaction zone.
- `source_timeframe`: present on daily overlay zones as `"1d"`; absent on normal 4H zones.

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
- `test_local_reaction_zones_use_recent_base_and_retested_rejection_bounds`
- `test_local_retested_flip_zone_survives_split_greedy_clusters`
- `test_local_reaction_zone_can_use_local_low_wick_to_body_bounds`
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
  -> macro support evidence -> fixed-width zones
  -> recent internal evidence -> variable-width reaction zones
  -> ranked zone overlay
  -> distinct ranked support list
```

The detector is not trying to draw every possible support and resistance level. It is trying to produce a small, support-biased list of meaningful dip-buying areas from closed 4H structure.
