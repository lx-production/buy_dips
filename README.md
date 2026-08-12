# PRANA Buy the Dips Bot

Local Python bot for a fail-closed Polygon canary flow. It fetches Binance Spot `BTCUSDT` **1h** candles, derives closed **4h** bars, detects support zones with `support_structure_v1` (`src/zones/`), and evaluates one gate-based decision engine: `support_close_v1`. Every cycle writes a `BUY` or `HOLD` row to the `decisions` table.

The hourly CLI path today is **`trade-once --mode observe`**: fetch → zones → decision → persist. Wallet helpers (`wallet-create`, `trade-check`, `approve-trading`, `revoke-trading`) exist for prep. Quote/simulate/`live` broadcast and the offline `backtest` CLI from the Phase 2 plan are not wired into the runner yet.

## What It Does

- Fetches public Binance Spot `BTCUSDT` **1h** klines into SQLite (`candles`, `timeframe="1h"`).
- Derives closed Binance-aligned **4h** bars from those 1h candles and stores them as `timeframe="4h"`.
- Rebuilds support zones only when a newer completed 4h bar appears (scoped `bot_state` watermark).
- Persists zone fingerprints (`zf1:…`) and evaluates `support_close_v1` on the latest closed 1h close.
- Stores every decision (`BUY` / `HOLD` + `reason_code`) in `decisions`.
- Creates an encrypted local keystore, checks Polygon contracts/balances, and can grant/revoke a capped USDT router allowance.

## What It Does Not Do (yet)

- No automatic DEX swap in `trade-once` (no quote → sign → broadcast path in the runner).
- No `dry_run` / `live` CLI modes exposed yet (only `observe`).
- No offline `backtest` CLI / BUY CSV export yet.
- No systemd units installed by this repo (Pi rollout stays operator-owned).
- No sell / stop-loss logic.

## Install

Requires Python 3.10+ (3.11 recommended).

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Copy secrets into the environment only (never into YAML):

- `KEYSTORE_PASSWORD` — decrypts the local keystore
- `POLYGON_RPC_URL` — Polygon JSON-RPC endpoint for wallet/contract commands

Default config is **dev**: `wallet.keystore_path: data/wallet/trader-dev.json` and `execution.quote_base_url: https://prana.triethocduongpho.net`. Prod expects a separate keystore and loopback quote host `http://127.0.0.1:4173`.

## Initialize The Database

```bash
python3 -m src.cli init-db
```

Default database path:

```text
data/prana_buy_the_dips.sqlite
```

`init_db` creates `candles`, `zones`, `zone_sets`, `decisions`, and `bot_state`, and drops any leftover Phase 1 `signals` table. It also creates read-only UTC+7 views for convenient inspection. Existing databases only need one `init-db` or `trade-once` run to receive the views; do not delete the database or backfill existing rows.

### Read Database Times In UTC+7

Trading logic continues to use the original Unix timestamps in UTC. This keeps comparisons, candle arithmetic, fingerprints (`zf1`), zone watermarks, and persisted data unchanged. The following read-only views retain every canonical column and append display columns ending in `_utc7`, formatted as `YYYY-MM-DD HH:MM:SS +07:00`:

- `candles_readable`
- `zones_readable`
- `zone_sets_readable`
- `decisions_readable`
- `bot_state_readable`

The zone and decision views also provide `source_open_times_json_utc7` and `selected_source_open_times_json_utc7`; their original JSON millisecond arrays remain available unchanged. For example:

```sql
SELECT symbol, timeframe, open_time, open_time_utc7, close
FROM candles_readable
ORDER BY open_time DESC
LIMIT 10;

SELECT decision, reason_code, candle_open_time_utc7, selected_source_open_times_json_utc7
FROM decisions_readable
ORDER BY candle_open_time DESC
LIMIT 10;

SELECT key, value, value_utc7, updated_at_utc7
FROM bot_state_readable;
```

## Backfill Candles

Backfill roughly the last 12 months. Binance caps each request at 1000 klines; the client pages through the range. Rows are upserted, so re-runs are safe.

```bash
# ~12 months of 1h candles (needed for observe + future backtest)
python3 -m src.cli backfill --timeframe 1h

# ~12 months of 4h candles (optional; live cycles also derive 4h from 1h)
python3 -m src.cli backfill --timeframe 4h
```

For a shorter backtest-prep window starting **2026-06-01 UTC** (script begins **2026-05-30** so the first hours already have a 48h lookback):

```bash
python3 scripts/backfill_1h_from_2026_06_01.py
```

## Print Zones

```bash
python3 -m src.cli zones
```

Loads closed **4h** candles from SQLite, runs support-only zone detection, and prints support bands.

## Open Local 4H Chart

```bash
python3 scripts/serve_chart.py
```

Then open:

```text
http://127.0.0.1:8000
```

The page overlays `support_structure_v1` zones on closed `BTCUSDT` 4h candles. For readability it only draws the nearest 4 supports at/below price plus the nearest 2 above; the detector itself is unchanged.

## Run One Observe Cycle

```bash
python3 -m src.cli trade-once --mode observe
```

Or:

```bash
python3 scripts/run_once.py
```

One cycle:

1. Fetches recent closed `BTCUSDT` 1h klines into `candles`.
2. Derives any overdue completed 4h buckets from those 1h rows (aborts if a due 4h bucket is missing 1h constituents).
3. Rebuilds zones when the 4h watermark advances; otherwise loads the last fingerprinted zone set.
4. Evaluates `support_close_v1` on the latest closed 1h candle.
5. Persists the decision (`BUY` or `HOLD`) and prints id / decision / reason / zones-rebuilt.

No wallet credentials are required for `observe`.

## Wallet And Contract Helpers

Use a throwaway **dev** keystore locally. Keep prod keystores only on the Pi.

```bash
# Create encrypted keystore at wallet.keystore_path (prints address only)
python3 -m src.cli wallet-create

# Decrypt and print the configured address only
python3 -m src.cli wallet-status

# Verify chain, router bytecode, token decimals, balances, allowance
python3 -m src.cli trade-check

# Cap router USDT allowance to the 10 USDT canary total
python3 -m src.cli approve-trading

# Reset that router allowance to zero
python3 -m src.cli revoke-trading
```

## Decision Engine (`support_close_v1`)

One dip-to-support flow. Output is gate-based (not scored): exactly one `decision` (`BUY` / `HOLD`) and one `reason_code` per cycle.

Entry regions for the current closed 1h `close`:

- **Inside support:** `zone.low <= close < zone.mid`
- **Immediately below support:** `close < zone.low` and close sits in the **70%–100%** band of the gap from the next-lower zone high up to this zone’s low

Shared setup gates:

- In the prior **48h** (floored by the selected zone’s `zone_source_time`), there is a nearest earlier closed 1h candle whose `close` is **strictly above** the internal-range midpoint (midpoint between the selected zone high and the next higher zone low).
- No prior `BUY` for the **same selected zone fingerprint** in the prior **24h**. Cooldown is per-zone: a deeper zone may still `BUY` within 24h of a shallower-zone `BUY`.

Reason codes:

- `CLOSE_OUTSIDE_ENTRY_REGION`
- `CLOSE_NOT_BELOW_ZONE_MID`
- `NO_HIGHER_ZONE`
- `NO_RECENT_CLOSE_ABOVE_INTERNAL_MID`
- `NO_LOWER_ZONE`
- `BELOW_ZONE_OUT_OF_BAND`
- `RECENT_BUY_IN_24H`
- `BUY_GATES_PASSED` → `BUY`

Fetch failures, zone-build failures, and an overdue incomplete 4h bucket abort the runner **before** a decision row is written.

## Zone Detection (`support_structure_v1`)

Detector lives under `src/zones/` and stays support-oriented. Tune swing sensitivity under `zones:` in `config.yaml`.

High level:

- High/low/body ranges detect internal and external swing points on closed 4h OHLC.
- External swings are filtered into prominent pivots with ATR/percent thresholds.
- Support evidence includes swing lows, reclaimed resistance, wick-floor retests, and derived 1D body-support overlays.
- Candidates group into fixed-width (~$500) bands; zones need at least `min_touches` touches.
- Detector returns `support` / `resistance` / `active` / `all`; `resistance` and `active` stay empty. Hourly trading uses the full `support` list (including zones currently above price) so below-zone entries still work.

Default prominent-pivot filter: reversal of at least `max(4.0 * ATR, 2.5% of price)`. Set `external_min_swing_atr_mult: 0.0` and `external_min_swing_pct: 0.0` to inspect raw local extrema. Chart internal pivots stay hidden unless `show_internal_pivots: true`.

After each rebuild, `zone_refresh` resolves `source_indexes` → `source_open_times` / `zone_source_time`, computes deterministic `zf1` fingerprints, and persists them with a `zone_sets` manifest. The hourly signal path never recomputes fingerprints from raw indexes.

## Safety

- Default mode is observe-only decision logging.
- Live trading (when enabled later) requires `execution.live_enabled`, a pinned wallet address, and the prod loopback quote host.
- Keystores and `.env` are gitignored; never commit passwords, private keys, signed txs, or RPC URLs with API keys.
- Canary intent: **1 USDT** per trade, **10 USDT** cumulative cap, capped router approval (not unlimited).

## Tests

```bash
pytest
```
