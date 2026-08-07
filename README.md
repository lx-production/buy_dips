# PRANA Buy the Dips Bot

Phase 1 is a local, Python-based foundation for a paper-only Buy the Dips system. It collects Binance Spot `BTCUSDT` 4H candles, stores raw candle data in SQLite, detects support zones with the `support_structure_v1` detector (`src/zones.py`), generates paper signals, and logs every decision including `HOLD`.

## What Phase 1 Does

- Fetches public Binance Spot `BTCUSDT` 4H klines.
- Stores raw candle data permanently in local SQLite.
- Uses only closed 4H candles for signal generation.
- Detects support zones from closed 4H OHLC using swing lows, reclaimed resistance, wick-floor retests, fixed-width bands, and derived 1D body-support overlays.
- Stores detected zones with `origin` and `role`; support-only zones use `role="support"`.
- Generates and stores paper signals.
- Logs `HOLD`, `ALERT_ONLY`, `PREPARE_MANUAL_REVIEW`, and `STRONG_BUY_SIGNAL`.

## What Phase 1 Does Not Do

- No real trades.
- No wallet execution.
- No smart contract execution.
- No private key handling.
- No blockchain transactions.
- No hardcoded secrets.
- No Web3 or Hardhat dependency.
- No hidden background jobs.

Even `STRONG_BUY_SIGNAL` is only a logged paper signal in Phase 1.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

## Initialize The Database

```bash
python3 -m src.cli init-db
```

Default database path:

```text
data/prana_buy_the_dips.sqlite
```

## Backfill 12 Months Of BTCUSDT 4H Candles

```bash
python3 -m src.cli backfill
```

Fetches roughly the **last 12 months** of public `BTCUSDT` 4H klines into SQLite. Binance allows at most **1000 klines per request**, so the client **pages** through the range (moving `startTime` forward after each batch)—that is a page size, not “only 1000 candles total.” Rows are **upserted**; re-runs are safe. On success the CLI prints insert/update counts, first and last candle (with ISO times), and the database path.

## Print Zones

```bash
python3 -m src.cli zones
```

This loads closed candles from SQLite, runs support-only zone detection, stores the zone snapshot, and prints support zones.

## Open Local 4H Chart

```bash
python3 scripts/serve_chart.py
```

Then open:

```text
http://127.0.0.1:8000
```

The page is a local fullscreen canvas chart. It reads closed `BTCUSDT` 4H candles from SQLite and overlays support zones from `support_structure_v1` (`src/zones.py`). For readability the chart only draws the nearest 4 supports at/below price plus the nearest 2 above; the detector itself is unchanged.

## Run One Paper Signal Cycle

```bash
python3 -m src.cli run-once
```

This fetches the latest candles, stores them, excludes any currently open 4H candle from signal calculations, detects zones, generates one paper signal, and stores it in the `signals` table. A `HOLD` decision is stored just like any other decision.

## Zone Detection (`support_structure_v1`)

Phase 1 uses **support-only structure detection** for zones (implemented in `detect_support_resistance_zones` -> `detect_support_resistance_zones_structure_v1`). Tune swing sensitivity and break thresholds under `zones:` in `config.yaml`.

`support_structure_v1` treats candles as a time-price path:

- high/low/body ranges detect raw internal and external swing points
- external swing points are filtered into prominent 4H pivots using the configured ATR/percent reversal thresholds
- support evidence can come from prominent swing lows, reclaimed swing highs (`flipped_resistance`), retested wick floors, dense reclaimed internal-high clusters inside large support gaps, and higher-timeframe 1D low-pivot body anchors
- support candidates are grouped when their source prices fit within the fixed 500 USD zone width
- support bands are anchored to the relevant support base for that evidence type
- a deep external swing-low rejection followed by a quick higher-low retest is split into a variable-width `wick_retest_support` floor and a fixed-width `body_rejection_support` shelf; these replace an ambiguous `mixed_structure` band trapped between them
- complete 1D candles are derived from six closed 4H candles; a prominent 1D low pivot can add `daily_body_support`, anchored from the daily body low with the same fixed `$500` width
- when a 1D body-support zone overlaps a 4H `mixed_structure` bridge or sits immediately below a nearby 4H `flipped_resistance` body band, the 1D zone replaces the 4H band
- zones require at least `min_touches` unique source touches
- support-biased zones stay in the support list even if they are currently above, below, or touching price

The default prominent-pivot filter requires an external swing reversal of at least `max(4.0 * ATR, 2.5% of price)`. Set `external_min_swing_atr_mult: 0.0` and `external_min_swing_pct: 0.0` to inspect the raw local-extrema behavior. The chart hides internal pivot labels unless `show_internal_pivots: true` is set.

The output remains compatible with the paper signal logic: the detector still returns `support`, `resistance`, `active`, and `all` keys, but `resistance` and `active` are always empty. Every support zone includes `low`, `high`, `mid`, `width`, `width_pct`, `touches`, `origin`, `role`, `source_closes`, and `source_indexes`. Additional metadata such as `score`, `structure_role`, `last_touch_index`, and `zone_width` is included for inspection. Daily overlay zones also include `source_timeframe="1d"`.

**`source_closes`** - one price per touch that formed the zone (same length and order as `source_indexes`). Despite the name, these are **not always** the candle `close` from OHLC; most values are body edges such as `min(open, close)` for swing lows or `max(open, close)` for reclaimed highs. `touches` is `len(source_closes)`.

## Safety Warning

Phase 1 is paper signal mode only. It has no private key support, no wallet
logic, no smart contract calls, and no real trade execution path.

## Tests

```bash
pytest
```
