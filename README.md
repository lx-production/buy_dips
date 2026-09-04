# PRANA Buy the Dips Bot

Local Python bot for a fail-closed Polygon canary flow. It fetches Binance Spot `BTCUSDT` **1h** candles, derives closed **4h** bars, detects support zones with `support_structure_v2` (`src/zones/` plus sticky `ZoneTrackState`), and evaluates one gate-based decision engine: `support_close_v2`. Every live cycle writes a `BUY` or `HOLD` row to the `decisions` table.

The hourly CLI path is **`trade-once`**. `observe` stops after decision persistence, `dry_run` requests and simulates a quote without signing, and `live` can approve/sign/broadcast only behind the production wallet guard. Offline **`backtest`** replays the same engine on stored candles and exports BUY CSV / a visual chart.

## What It Does

- Fetches public Binance Spot `BTCUSDT` **1h** klines into SQLite (`candles`, `timeframe="1h"`).
- Derives closed Binance-aligned **4h** bars from those 1h candles and stores them as `timeframe="4h"`.
- Rebuilds support zones only when a newer completed 4h bar appears (scoped `bot_state` watermark).
- Persists zone fingerprints (`zf1:…`, now the sticky `zone_track_id`) and evaluates `support_close_v2` on the latest closed 1h candle. BUY requires a **red** trigger candle (`close < open`).
- Stores every decision (`BUY` / `HOLD` + `reason_code`) in `decisions`.
- On a BUY, can request the pinned in-house USDT→PRANA quote, validate it, simulate the exact calldata, and persist the redacted lifecycle in `trade_executions`.
- In guarded `live` mode, tops up only the required USDT allowance, reserves nonce/hash before broadcast, and reconciles the same hash on reruns.
- Offline backtest replays history in memory (no live table writes), prints a BUY summary, writes a BUY CSV, and can serve a 1h chart with time-bounded zones.
- Creates an encrypted local keystore, checks Polygon contracts/balances, and can grant/revoke a capped USDT router allowance.

## What It Does Not Do (yet)

- No systemd units installed by this repo (Pi rollout stays operator-owned).
- No sell / stop-loss logic.
- Backtest is signal-only: no PnL, sell, quote, slippage, gas, or wallet simulation.

For an operator-owned Pi deployment, follow the Vietnamese [Pi rollout runbook](docs/pi-rollout-runbook.md). It covers the dedicated service user, permissions, systemd credentials, hourly service/timer templates, the observe-to-dry-run dev-canary rollout, and installation of the guarded one-command canary updater.

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
- `LIVE_TRADING_CONFIRMATION` — live-only value `polygon:137:<checksum wallet address>`

The bot does not auto-load `.env` files. Export dev values into the current process, or use systemd credentials in production.

Default config is **dev**: `wallet.keystore_path: data/wallet/trader-dev.json` and `execution.quote_base_url: https://prana.triethocduongpho.net`. Prod expects a separate keystore and loopback quote host `http://127.0.0.1:4173`.

## Initialize The Database

```bash
python3 -m src.cli init-db
```

Default database path:

```text
data/prana_buy_the_dips.sqlite
```

`init_db` creates `candles`, `zones`, `zone_sets`, `backtest_zone_cache`, `decisions`, `trade_executions`, and `bot_state`, and drops any leftover Phase 1 `signals` table. It also creates read-only UTC+7 views for convenient inspection. `decisions` has one idempotent row per mode/strategy/closed hour; each BUY decision can own only one `trade_executions` row, and transaction hashes are unique. Existing databases are upgraded automatically on the next command that initializes or reads the database; do not delete the database or backfill existing rows.

### Read Database Times In UTC+7

Trading logic continues to use the original Unix timestamps in UTC. This keeps comparisons, candle arithmetic, fingerprints (`zf1`), zone watermarks, and persisted data unchanged. The following read-only views retain every canonical column and append display columns ending in `_utc7`, formatted as `YYYY-MM-DD HH:MM:SS +07:00`:

- `candles_readable`
- `zones_readable`
- `zone_sets_readable`
- `decisions_readable`
- `trade_executions_readable`
- `bot_state_readable`

The zone and decision views also provide `source_open_times_json_utc7` and `selected_source_open_times_json_utc7`; their original JSON millisecond arrays remain available unchanged. For example:

```sql
SELECT symbol, timeframe, open_time, open_time_utc7, close
FROM candles_readable
ORDER BY open_time DESC
LIMIT 10;

SELECT
  id,
  decision,
  reason_code,
  candle_open_time_utc7,
  reference_close,
  selected_zone_low,
  selected_zone_high,
  entry_region,
  dip_origin_open_time_utc7,
  zones_rebuilt,
  mode
FROM decisions_readable
WHERE mode = 'observe'
ORDER BY candle_open_time DESC
LIMIT 24;

sqlite3 data/prana_buy_the_dips.sqlite \
  "SELECT decision, reason_code, candle_open_time_utc7 FROM decisions_readable WHERE mode='observe' ORDER BY candle_open_time DESC LIMIT 24;"

SELECT key, value, value_utc7, updated_at_utc7
FROM bot_state_readable;
```

## Backfill Candles

Backfill roughly the last 12 months. Binance caps each request at 1000 klines; the client pages through the range. Rows are upserted, so re-runs are safe.

```bash
# ~12 months of 1h candles (needed for observe + backtest)
python3 -m src.cli backfill --timeframe 1h

# ~12 months of 4h candles (optional warm-up; live cycles also derive 4h from 1h)
python3 -m src.cli backfill --timeframe 4h
```

For a shorter backtest-prep window starting **2026-06-01 UTC** (script begins **2026-05-30** so the first hours already have a 48h lookback):

```bash
python3 scripts/backfill_1h_from_2026_06_01.py
```

Backtest also needs enough older **4h** history in SQLite for detector warm-up before the 1h window. If zones fail to build, run a 4h backfill as well.

## Offline Backtest

Replay `support_close_v2` on stored closed 1h candles. The engine is the same as observe; already-bought setups and the per-zone 24h cooldown use an in-memory prior-BUY list for that run only (never reads/writes `decisions`, `zones`, `zone_sets`, or `bot_state`).

- `--start` is inclusive, `--end` is exclusive. Both must be ISO-8601 with timezone on any UTC hour boundary; 4h alignment is not required.
- `--end` defaults to after the latest closed 1h candle.
- Requires continuous 1h data from `start - dip_lookback_hours` (default 48h). Incomplete overdue 4h buckets abort the run.
- Zone snapshots are cached in the separate `backtest_zone_cache` table. The first run builds them; later runs reuse matching snapshots and build only new or stale 4h watermarks.
- Cold rebuilds ingest each closed 4h candle once into `IncrementalZoneDetectorState`, then materialize/fingerprint at each cache miss. A fully warm run never creates that state.
- Cache validity includes zone config, detector source code, and a cumulative hash of the exact 4h candle input. Config/code edits and historical candle changes invalidate affected snapshots automatically.
- Output is BUY-only: CLI summary + CSV. HOLD is computed for correct replay but not printed or exported.

```bash
python3 -m src.cli backtest \
  --start 2026-06-01T00:00:00+00:00 \
  --end 2026-07-01T00:00:00+00:00 \
  --csv data/backtest_buys.csv
```

CLI prints the range, evaluated candle count, zone snapshot count, cache hits, detector builds, incremental ingest/scan counts, BUY count, and CSV path.

BUY CSV columns:

- `trigger_time`, `trigger_close`, `entry_region`, `fingerprint_version`
- `selected_zone_fingerprint`, `zone_low`, `zone_mid`, `zone_high`
- `higher_zone_fingerprint`, `higher_zone_low`, `internal_range_midpoint`
- `next_lower_zone_fingerprint`, `next_lower_zone_high`, `below_zone_pct`
- `dip_origin_time`, `dip_origin_close`, `zone_set_as_of`

Zero BUYs still writes a CSV with only the header row. Same inputs/config must produce identical BUY timestamps and CSV rows.

Benchmark cold vs warm zone-snapshot rebuilds on a **temporary copy** of the source SQLite file. The script never reads `.env`, wallet files, or logs, and it never writes `backtest_zone_cache` on the source database. Default range matches the incremental-detector baseline (`2026-06-01T00:00:00Z` → `2026-08-13T06:00:00Z`):

```bash
python3 scripts/benchmark_backtest.py \
  --database data/prana_buy_the_dips.sqlite \
  --json data/backtest_zone_benchmark.json
```

Stdout prints elapsed time, snapshot count, detector builds, cache hits, incremental ingest/scan counts, as text plus JSON. `--config` is optional YAML for zone/strategy settings only.

Golden tests in `tests/test_incremental_zone_detector.py` lock current stateless detector snapshots at every 4h prefix (in memory, not committed). `IncrementalZoneDetectorState` must deep-equal that oracle after each `advance`. Extract-then-materialize stays the live-path reference.

## Print Zones

```bash
python3 -m src.cli zones
```

Loads closed **4h** candles from SQLite, runs support-only zone detection, and prints support bands.

## Open Local Charts

Latest zones on closed 4h candles (unchanged helper chart):

```bash
python3 scripts/serve_chart.py
```

Then open `http://127.0.0.1:8000`. The default 4H view plots closed candles from **2026-06-01 00:00 UTC** through the latest bar (zones still use full 4h history). The page is `src/chart.html` and uses TradingView Lightweight Charts (same library as the backtest chart): scroll to zoom time, drag the plot to pan, and use the price axis to scale price. Reset viewport fits the full window. Hover a candle for its UTC+7 time and OHLC; hover a support band or internal pivot to append those details. Axis labels, HUD, and hover times are shown in UTC+7; API times stay on UTC milliseconds. For readability it only draws support zones with `low > 57000`, plus at most the nearest 2 supports above current price; the detector itself is unchanged. The chart script is loaded from a CDN, so the browser needs network the first time the page opens.

Backtest chart (1h candles, BUY markers, zones only while each snapshot was valid):

```bash
python3 scripts/serve_backtest_chart.py \
  --start 2026-06-01T00:00:00+00:00 \
  --end 2026-08-18T00:00:00+00:00
```

Then open `http://127.0.0.1:8001`. The server prints the replay range, runs the offline replay once, and only then binds the port; bad/missing data prevents startup. Under `Running backtest …` an `Elapsed: Ns` line updates in place every second until replay finishes (whole seconds from launching the script). The page uses TradingView Lightweight Charts: scroll to zoom time, drag the plot to pan, and use the price axis to scale price. Reset viewport fits the full window. Hover a candle for its UTC+7 time and OHLC; hover a BUY marker or a support band to append those details under the candle values. Axis labels, HUD, and hover times are shown in UTC+7; replay math and the API stay on UTC milliseconds. There is no HOLD marker or HOLD table. For readability the chart only draws support zones with `low > 57000` and `high < 75000`; the replay and API still include every zone. The chart script is loaded from a CDN, so the browser needs network the first time the page opens.

During replay, each completed 4h bucket is aggregated once and reused by later 1h trigger candles. Cached zone snapshots are validated before use; malformed cache JSON is rebuilt from canonical candles. Backtest still never reads or writes the live `zones`, `zone_sets`, `decisions`, or `bot_state` rows.

## Xóa cache zone của backtest

python3 -c "
import sqlite3
conn = sqlite3.connect('data/prana_buy_the_dips.sqlite')
n = conn.execute('SELECT COUNT(*) FROM backtest_zone_cache').fetchone()[0]
conn.execute('DELETE FROM backtest_zone_cache')
conn.commit()
conn.close()
print(f'deleted {n} rows')
"

## Run One Trading Cycle

```bash
# Decision only; no wallet or Polygon access
python3 -m src.cli trade-once --mode observe

# On BUY: validate wallet/contracts, request quote, eth_call, and estimate gas; never sign
python3 -m src.cli trade-once --mode dry_run
```

Or:

```bash
python3 scripts/run_once.py
```

One cycle:

1. Fetches recent closed `BTCUSDT` 1h klines into `candles`.
2. Derives any overdue completed 4h buckets from those 1h rows (aborts if a due 4h bucket is missing 1h constituents).
3. Rebuilds zones when the 4h watermark advances; otherwise loads the last fingerprinted zone set.
4. Evaluates `support_close_v2` on the latest closed 1h candle.
5. Persists the decision (`BUY` or `HOLD`) and prints id / decision / reason / zones-rebuilt.
6. For a BUY in `dry_run` or `live`, creates one idempotent `trade_executions` row and checks the pause file plus any unresolved execution.
7. For `live`, also enforces at most 3 attempts per UTC day and at most 10 USDT cumulative reserved/attempted spend. Signed, broadcast, pending, confirmed, and reverted attempts count conservatively.
8. Checks USDT/POL balances and a conservative gas reserve before approval or signing, then validates a fresh `POST /api/swap/quote` response.

No wallet credentials are required for `observe`.

The quote must echo `USDT`→`PRANA`, `amountIn="1"`, the signer recipient, configured slippage, and chain ID 137. The router and `transaction.to` must match the allowlist, calldata must be non-empty, ERC-20 `value` must be zero, and both deadline and verification expiry must have enough time remaining. The adapter sends only `Content-Type: application/json`; it does not send `Origin`.

`dry_run` performs `eth_call` and `estimate_gas`, then stores `simulated` without approval, signing, or broadcast. `live` additionally requires `environment: prod`, the loopback quote host, `live_enabled: true`, the pinned wallet, and matching `LIVE_TRADING_CONFIRMATION`. It tops up only the quote amount when allowance is low, commits nonce/hash before broadcasting once, decodes received PRANA from the receipt, and reconciles that same hash on rerun.

Keep live disabled until the backtest, observe, dry-run, wallet funding, capped approval, and operator review rollout gates are complete.

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

## Decision Engine (`support_close_v2`)

One dip-to-support flow. Output is gate-based (not scored): exactly one `decision` (`BUY` / `HOLD`) and one `reason_code` per cycle.

The trigger 1h candle must be **red** (`close < open`). A green or doji candle is `HOLD` immediately (`CLOSE_NOT_BELOW_OPEN`) and never selects a zone.

Entry regions for the current closed 1h `close` (thresholds come from `strategy:` in YAML):

- **Inside support:** `zone.low <= close` and close sits in the **0%–`inside_zone_max_pct`** portion of the zone span (`0%` = `zone.low`, `100%` = `zone.high`). Default `inside_zone_max_pct` is `1.00` (the full band).
- **Immediately below support:** `close < zone.low` and close sits in the **`below_zone_min_pct`–100%** band of the gap from the next-lower zone high up to this zone’s low. Default `below_zone_min_pct` is `0.50`.

Shared setup gates:

- The trigger 1h candle is **red**: `close < open`.
- In the prior **`dip_lookback_hours`** (default 48; floored by the selected zone’s `zone_source_time`), there is a nearest earlier closed 1h candle whose `close` is **strictly above** the internal-range midpoint (midpoint between the selected zone high and the next higher zone low).
- The nearest earlier closed 1h candle whose `close` is strictly outside the selected zone must have closed **above** `zone.high` (approach from above). A last-outside close below `zone.low` is `ZONE_APPROACHED_FROM_BELOW`.
- Each dip setup (`selected_zone_fingerprint` + `dip_origin_open_time`) may `BUY` only once. After `cooldown_hours` (default 24), the same zone can be bought again only when a later close above the internal-range midpoint creates a new dip origin. Inside that window a new dip origin does **not** unlock the same zone (`RECENT_BUY_IN_24H`). A deeper zone may still `BUY` (different fingerprint).

Reason codes:

- `CLOSE_NOT_BELOW_OPEN`
- `CLOSE_OUTSIDE_ENTRY_REGION`
- `NO_HIGHER_ZONE`
- `NO_RECENT_CLOSE_ABOVE_INTERNAL_MID`
- `NO_LOWER_ZONE`
- `BELOW_ZONE_OUT_OF_BAND`
- `ZONE_APPROACHED_FROM_BELOW`
- `RECENT_BUY_IN_24H`
- `SETUP_ALREADY_BOUGHT`
- `BUY_GATES_PASSED` → `BUY`

Fetch failures, zone-build failures, and an overdue incomplete 4h bucket abort the runner **before** a decision row is written.

## Safety

- Default mode is observe-only decision logging; `dry_run` never signs.
- Live trading requires `execution.live_enabled`, a pinned wallet address, the prod loopback quote host, and wallet-specific confirmation.
- Quote verification tokens, calldata, signed transaction bytes, passwords, and RPC URLs are never stored in `trade_executions`.
- Keystores and `.env` are gitignored; never commit passwords, private keys, signed txs, or RPC URLs with API keys.
- Canary intent: exactly **1 USDT** per trade, at most **3 attempts per UTC day**, **10 USDT** cumulative cap, and capped router approval (not unlimited).
- `risk.min_pol_reserve` remains untouched after a conservative approval/swap gas budget.
- A non-terminal execution blocks every later execution. This prevents a second quote/sign/broadcast path while an earlier lifecycle is unresolved.

## Audit, Pause, And Recovery

Every trading cycle writes structured JSON events to stdout and the size-rotating `logging.file_path` (default `data/logs/trading.jsonl`). Events include a per-cycle correlation ID, decision/execution IDs, zone watermark (`zone_set_as_of` Unix ms plus `zone_set_as_of_utc7` as `YYYY-MM-DD HH:MM:SS +07:00`), fingerprint version, available selected/adjacent zone fingerprints, and each no-trade, skip, quote, simulation, signing, broadcast, and receipt transition.

The logger recursively redacts passwords, decrypted/private keys, RPC URLs, raw/signed transaction bytes, calldata, API keys, and quote verification tokens. SQLite stores only safe summaries and stable reason codes; it never stores raw calldata, signed transaction payloads, or verification tokens.

To stop `dry_run` and `live` execution while continuing candle/decision collection, create the configured pause file:

```bash
touch data/PAUSE_TRADING
```

Remove it only after reviewing the reason for the pause:

```bash
rm data/PAUSE_TRADING
```

A BUY remains a BUY when execution is blocked. Inspect the downstream result separately:

```sql
SELECT id, decision_id, mode, status, reason, transaction_hash, updated_at_utc7
FROM trade_executions_readable
ORDER BY id DESC
LIMIT 20;

SELECT mode, status, COUNT(*) AS attempts, SUM(COALESCE(amount_in_raw, 0)) AS amount_in_raw
FROM trade_executions
GROUP BY mode, status
ORDER BY mode, status;
```

Do not manually clear a `signed`, `broadcast`, or `pending` row. Re-run the same cycle to reconcile its stored transaction hash. A pre-broadcast row left in `started`, `risk_checked`, `quoted`, or `allowance_ready` after a crash intentionally blocks later execution; inspect it and mark it failed only after confirming that no transaction or approval remains unresolved.

## Tests

```bash
pytest
```

`tests/test_incremental_zone_detector.py` builds an in-memory prefix oracle from the current stateless detector (rebuilt twice only in the determinism test) and covers the zone-transition fixtures. Extract-then-materialize and `IncrementalZoneDetectorState.advance` must deep-equal that oracle at every golden prefix, including `internal_swing_order=2`. Fail-closed tests cover out-of-order, duplicate, gapped, and unclosed 4h candles. `tests/test_zone_tracks.py` locks 2-confirm / 3-miss / bound-hysteresis / challenger replacement. Offline backtest uses incremental state on cache misses, then applies tracks in watermark order. `scripts/benchmark_backtest.py` measures cold/warm snapshot rebuilds on a temporary database copy.
