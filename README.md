# PRANA Buy the Dips Bot

Phase 1 is a local, Python-based foundation for a paper-only Buy the Dips system. It collects Binance Spot `BTCUSDT` 4H candles, stores raw candle data in SQLite, detects support and resistance zones with the `structure_v1` detector (`src/zones.py`), generates paper signals, and logs every decision including `HOLD`.

## What Phase 1 Does

- Fetches public Binance Spot `BTCUSDT` 4H klines.
- Stores raw candle data permanently in local SQLite.
- Uses only closed 4H candles for signal generation.
- Detects support, active, and resistance zones from closed 4H OHLC using `structure_v1` (swing structure, fixed-width bands, BOS/CHOCH-style breaks).
- Stores detected zones with separate `origin` and current `role`.
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

This loads closed candles from SQLite, runs `structure_v1` zone detection, stores the zone snapshot, and prints support, active, and resistance zones.

## Open Local 4H Chart

```bash
python3 scripts/serve_chart.py
```

Then open:

```text
http://127.0.0.1:8000
```

The page is a local fullscreen canvas chart. It reads closed `BTCUSDT` 4H candles from SQLite and overlays support zones from `structure_v1` (`src/zones.py`).

## Run One Paper Signal Cycle

```bash
python3 -m src.cli run-once
```

This fetches the latest candles, stores them, excludes any currently open 4H candle from signal calculations, detects zones, generates one paper signal, and stores it in the `signals` table. A `HOLD` decision is stored just like any other decision.

## Zone detection (`structure_v1`)

Phase 1 uses **`structure_v1` only** for zones (implemented in `detect_support_resistance_zones` → `detect_support_resistance_zones_structure_v1`). Tune swing sensitivity and break thresholds under `zones:` in `config.yaml`.

`structure_v1` treats candles as a time-price path:

- high/low/body ranges detect raw internal and external swing points
- external swing points are filtered into prominent 4H pivots using the configured ATR/percent reversal thresholds
- prominent external pivots form legs with ATR-normalized slope metadata
- zones are built from prominent external 4H swing points, not minor internal swings
- every structure zone is a fixed 500 USD band from `low` to `high`
- nearby fixed bands are consolidated so only the strongest macro zones remain
- support bands are anchored to the lower base of their external swing-low group, not the group midpoint
- long-wick prominent swing lows can form `structure_support_floor` zones when the wick floor is retested by another external swing-low body
- oversized support gaps can be filled with one `stair_step_flipped_resistance` zone from broken raw external swing highs
- candle closes confirm BOS/CHOCH-style structure breaks
- flipped structure levels can become support or resistance

The default prominent-pivot filter requires an external swing reversal of at least `max(4.0 * ATR, 2.5% of price)`. Set `external_min_swing_atr_mult: 0.0` and `external_min_swing_pct: 0.0` to inspect the raw local-extrema behavior. The chart hides internal pivot labels unless `show_internal_pivots: true` is set.

The output remains compatible with the paper signal logic: every zone still includes `low`, `high`, `mid`, `width`, `width_pct`, `touches`, `origin`, `role`, `source_closes`, and `source_indexes`. Additional metadata such as `score`, `structure_role`, `broken_index`, `zone_width`, and `leg_ids` is included for inspection and later signal scoring.

**`source_closes`** — one price per pivot touch that formed the zone (same length and order as `source_indexes`). Despite the name, these are **not** always the candle `close` from OHLC. Most values are the relevant **body edge** from that pivot bar: for swing lows, `min(open, close)`; for swing highs, `max(open, close)`. `structure_support_floor` zones can also include a prominent swing-low wick floor when that floor is retested by another external swing-low body. `touches` is `len(source_closes)`.

## Safety Warning

Phase 1 is paper signal mode only. It has no private key support, no wallet
logic, no smart contract calls, and no real trade execution path.

## Tests

```bash
pytest
```
