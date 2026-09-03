---
name: live dip execution
overview: "Add a fail-closed Polygon trading flow that extends the Binance 4H zone detector with persistent wick floors, fetches only Binance BTCUSDT 1h candles into the existing candles table, derives closed 4H bars from those 1h candles to rebuild zones, and buys exactly 1 USDT of PRANA through the in-house swap quote API (prod: POST /api/swap/quote on the local route server; dev: https://prana.triethocduongpho.net/api/swap/quote). Retire the Phase 1 paper scorer and signals table in favor of one support_close_v2 decision engine and one decisions schema shared by observe, dry_run, and live. Use one dip-to-support entry flow: the trigger closed 1h candle must be red (close < open); within the prior 48h, the nearest earlier closed 1h candle whose close is strictly above the internal-range midpoint marks the dip origin; buy when the current closed 1h candle reaches the nearest support either inside the zone anywhere from 0%–100% of the zone span (0% = zone.low, 100% = zone.high) or immediately below it in the 50%–100% portion of the gap to the next-lower zone, provided no BUY for that same selected zone fingerprint exists in the prior 24h (cooldown is per-zone, not global—so a deeper zone B can still BUY within 24h of a BUY at shallower zone A). Use separate dev/prod keystores; Pi prod runs 24/7 via systemd with LoadCredential for secrets."
todos:
  - id: data-signal
    content: Retire paper scorer/signals table; add one support_close_v2 dip-to-support flow (red trigger 1h close < open + nearest 48h close above internal midpoint + current close inside-zone 0–100% or below-zone 50–100% band + per-zone 24h no-buy) and decisions schema; fetch-only-1h; derive closed 4H; abort on overdue incomplete 4H; build/validate atomic bot_state zone watermark; resolve source_indexes→open_times/zone_source_time and create/persist deterministic zf1 fingerprints in zone_refresh before signal evaluation.
    status: completed
  - id: wallet-safety
    content: Add dev/prod keystore separation, LoadCredential secret loading on Pi, encrypted-keystore CLI flows, contract checks, direct capped in-house router approval, revocation, and live-mode guards.
    status: pending
  - id: quote-execute
    content: Add a thin in-house swap quote adapter (prod local route server; dev https://prana.triethocduongpho.net); validate quote/transaction/deadline/verification; approve router if needed; simulate, execute, and reconcile swaps.
    status: completed
  - id: audit-risk
    content: Wire decisions/executions persistence, structured redacted logs, duplicate prevention, pause switch, and canary risk limits.
    status: completed
  - id: tests
    content: Replace paper-signal tests with the unified dip-origin/support-entry gates, including nearest qualifying 48h close, inside/below-zone entry regions, and per-zone 24h no-buy (same zone blocked; deeper zone still allowed); cover 1h fetch, 1h-to-4h, zone watermark, risk, storage, in-house quote response validation, runner, and tx lifecycle.
    status: pending
  - id: backtest
    content: Offline support_close_v2 replay in src/trading/backtest.py with isolated in-memory prior-BUY state; CLI backtest --start/--end/--csv (BUY-only summary+CSV); shared build_fingerprinted_support_zones helper; serve_backtest_chart.py with GET /api/backtest (no HOLD).
    status: completed
  - id: docs-rollout
    content: Update README/study to drop paper-score docs; document zf1 creation/storage/cooldown/output, decisions schema, 1h backfill + backtest BUY CSV gate, visual backtest chart, dev/prod keystores and quote hosts (prod loopback vs https://prana.triethocduongpho.net), Pi systemd LoadCredential units, in-house USDT→PRANA swap flow, and observe/dry-run/capped-live rollout.
    status: pending
isProject: false
---

# Phase 2 Live Dip Execution

## Locked decisions and important constraints
- Keep `support_structure_v2` in [src/zones/](src/zones/) as the swing detector, plus a last-step **persistent wick-floor** overlay: a closed 4H local swing low whose wick hangs at least `2%` of the wick price below the body pins `low = wick`, `high = wick + 500` (`origin=persistent_wick_floor`, one touch, frozen source candle). Band height stays `$500`; the percent filter is independent of zone width. After overlay, collapse zones closer than a `$650` gap or `$1000` midpoint (persistent outranks swing/daily; older persistent wins). Later deeper lows, macro-merge, and daily overlay must not move or drop that shelf; overlapping persistent floors keep the oldest. Never feed an open/incomplete 4H bar into the detector (no look-ahead / fake pivots).
- Live price feed fetches **only** Binance `BTCUSDT` `1h` klines. Do **not** call Binance for `4h` during the hourly trading cycle.
- Reuse the existing `candles` table for those `1h` rows (`timeframe="1h"`). Do **not** add a separate hourly price observations table. The decision price is the latest closed 1h `close`.
- Extend historical backfill to support `1h` (Phase 1 [src/candles.py](src/candles.py) currently only allows `4h`). Multi-month `1h` history is required before offline backtest; derived closed 4H continues from 1h aggregation.
- Derive closed 4H bars from closed 1h candles aligned to Binance UTC buckets (`00/04/08/12/16/20`). A 4H bar is closed only when all 4 constituent 1h candles are closed. Upsert derived closed 4H bars into `candles` with `timeframe="4h"` so the detector keeps reading the familiar 4H store (historical backfill 4H remains valid; new bars continue from 1h aggregation).
- **Incomplete overdue 4H bucket is a runner abort, not a stale-zone continue:** while the current 4H bucket is still forming (clock still inside the bucket), keep using the last persisted zone set. Once the bucket's end time has passed but one or more of its four closed 1h candles are missing, abort the cycle with a runner/data error—do **not** create a `decisions` row and do **not** BUY against the pre-watermark zone set. That gap means the zone snapshot may be due for rebuild and continuing would trade on a stale support set.
- Rebuild zones automatically only when a newly completed closed 4H bar appears (compared against a watermark). Between 4H closes, keep using the last persisted zone set for the hourly signal. Detector keeps emitting `source_indexes` only; resolve them to `source_open_times` / `zone_source_time` in `zone_refresh` immediately after detection (4H frame vs daily frame for `source_timeframe="1d"`), persist those times, and never re-map indexes later in the signal engine.
- **Retire Phase 1 paper scoring.** Remove the score-based path in [src/signals.py](src/signals.py) (`signal_score`, `ALERT_ONLY` / `STRONG_BUY_SIGNAL`, distance-to-`zone.high` heuristics) and the `signals` table / `insert_signal` helpers. Paper history is disposable; DB may be recreated. Do not keep two signal engines.
- Replace them with **one** deterministic `support_close_v2` decision engine and **one** `decisions` schema. `observe`, `dry_run`, and `live` all call the same engine; modes only change what happens after a `BUY` decision (record only / quote+simulate / sign+broadcast).
- Execute a market swap shortly after the UTC hour through the **in-house swap quote API** on Polygon (chain ID 137): `POST …/api/swap/quote` with `tokenInSymbol=USDT`, `tokenOutSymbol=PRANA`, `amountIn="1"`. Same request/response contract for both environments; only the base URL differs by env:
  - **Prod (Pi):** `http://127.0.0.1:4173` — local route server on the same host. Routing, calldata, and router selection come from that local server. The bot does not start it; it must already be running.
  - **Dev/local:** `https://prana.triethocduongpho.net` — public quote host for observe/dry_run and local tests (no local route server required on the dev machine).
- Pin execution to the quote response: use `quote.transaction.to` / `data` / `value`, require `chainId == 137`, non-empty calldata, and a usable `deadline`. Prefer validating `routerAddress` against an allowlisted Uniswap SwapRouter02 (example from quote: `0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45`) when configured; fail closed if host, chain, token symbols, recipient, amount, or transaction target do not match configuration. Config must pin the expected quote host per environment so a prod run cannot silently hit the public URL (and vice versa unless explicitly overridden for testing).
- Operate 24/7 on **Pi Ubuntu prod** with a **systemd** timer/service that invokes one idempotent `trade-once` cycle each hour. Do not auto-install units; document example unit files only. Prod quote base URL stays loopback (`127.0.0.1:4173`).
- Use **separate keystores for dev and prod**. Never share one private key across machines. Dev uses a throwaway wallet for `observe` / `dry_run` and local tests; prod Pi holds the only live canary wallet. Config selects the keystore path per environment.
- Keep keystores gitignored and outside agent-exposed paths where practical. Never commit a private key, password, API key, raw signed transaction, or RPC URL containing the API key.
- **Prod Pi:** inject the keystore password (and optionally Polygon RPC URL) via systemd **`LoadCredential=`** (not a plain `EnvironmentFile` in the repo). **Dev/local:** secrets may come from env or prompt for manual runs only; do not copy prod keystore or prod credentials onto the dev machine.

## In-house swap quote API (USDT → PRANA)
- Path: `POST /api/swap/quote`. Base URL is env-specific (YAML `execution.quote_base_url`):
  - **Prod:** `http://127.0.0.1:4173` → `POST http://127.0.0.1:4173/api/swap/quote`
  - **Dev:** `https://prana.triethocduongpho.net` → `POST https://prana.triethocduongpho.net/api/swap/quote`
- Required header: `Content-Type: application/json`. **Do not send `Origin`.**
- Body max **2 KB**. JSON shape (same as UI; identical for prod and dev):

```json
{
  "tokenInSymbol": "USDT",
  "tokenOutSymbol": "PRANA",
  "amountIn": "1",
  "recipient": "0xBotAddressHere",
  "slippageBps": 50
}
```

- Field meanings:
  - `tokenInSymbol` / `tokenOutSymbol`: allowlist `PRANA`, `WBTC`, `POL`, `USDC`, `USDT`, `WETH`, `DAI`; must differ. Canary path always uses `USDT` → `PRANA`.
  - `amountIn`: human-readable **string** (not wei); server `parseUnits` by token decimals. Canary always `"1"`.
  - `recipient`: wallet that receives token out (bot signer address).
  - `slippageBps`: basis points; `50` = 0.5%. Clamp 1–500; missing/invalid → default `50`.
- Example curl (dev host; swap base URL for prod loopback):

```bash
curl -sS -X POST 'https://prana.triethocduongpho.net/api/swap/quote' \
  -H 'Content-Type: application/json' \
  -d '{
    "tokenInSymbol": "USDT",
    "tokenOutSymbol": "PRANA",
    "amountIn": "1",
    "recipient": "0x1234567890123456789012345678901234567890",
    "slippageBps": 50
  }'
```

- Success response fields the bot uses:

```json
{
  "request": {
    "tokenInSymbol": "USDT",
    "tokenOutSymbol": "PRANA",
    "amountIn": "1",
    "amountInRaw": "1000000",
    "recipient": "0x...",
    "slippageBps": 50,
    "chainId": 137
  },
  "amountOut": "...",
  "amountOutRaw": "...",
  "minimumAmountOut": "...",
  "routerAddress": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
  "transaction": {
    "to": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
    "data": "0x...",
    "value": "0"
  },
  "deadline": 1730000000,
  "verification": { "version": 2, "token": "...", "expiresAt": "..." }
}
```

- After a valid quote:
  1. If token in is ERC-20: `approve(router, amountInRaw)` when allowance is too low (router = `quote.routerAddress` / `quote.transaction.to`).
  2. Broadcast: `to` = `quote.transaction.to`, `data` = `quote.transaction.data`, `value` = `BigInt(quote.transaction.value)` (POL native may be > 0; USDT/PRANA usually `"0"`).
  3. Must complete before `deadline` (~3 minutes from quote time). Reject stale quotes; never sign after deadline.

## Single decision engine (`support_close_v2`)
- Output is gate-based, not scored. Every persisted decision has exactly one `decision` (`BUY` or `HOLD`) and exactly one `reason_code` from the closed set below. There is one **dip-to-support** entry flow. The trigger closed 1h candle must be **red** (`close < open`); a green or doji candle never selects a zone. After that, the current close may qualify in either of two adjacent entry regions:
  - **Inside support:** `zone.low <= close <= zone.high` (full zone span: `0%` = `zone.low`, `100%` = `zone.high`). Any close inside the band qualifies.
  - **Immediately below support:** `close < zone.low` and `0.50 <= below_zone_pct <= 1.0` in the gap `(next_lower_zone.high → zone.low)`; the separate `close < zone.low` condition excludes the 100% boundary in practice.
- Both regions share the same setup gates: the trigger candle is red (`close < open`), a nearest qualifying dip-origin candle exists in the 48h lookback with `dip_origin.close > internal_range_midpoint`, the nearest earlier close strictly outside the selected zone is above `zone.high` (approach from above), no `BUY` for the same `selected_zone_fingerprint` exists in the prior 24h (`RECENT_BUY_IN_24H`; a new dip origin does not unlock the zone inside that window), and this `setup_id = selected_zone_fingerprint + dip_origin_open_time` has not already `BUY`. After 24h the same zone may be bought again only when a later close above `internal_range_midpoint` creates a new dip origin. A BUY at shallower zone A does not block a later BUY at deeper zone B. The prior-BUY source is mode-specific: `observe`/`dry_run`/`live` read persisted `decisions`; backtest reads only that replay's isolated prior-BUY list.
- Reason codes (closed set):
  1. `CLOSE_NOT_BELOW_OPEN` → `HOLD`: the trigger 1h candle is not red (`close >= open`). Evaluated first, before zone selection.
  2. `CLOSE_OUTSIDE_ENTRY_REGION` → `HOLD`: the current close is neither inside the selected support (`zone.low <= close <= zone.high`) nor immediately below it in the 50%–100% band.
  3. `NO_HIGHER_ZONE` → `HOLD`: the selected support has no usable higher support above it (none exists, or the nearest above touches/overlaps), so a positive internal range and its midpoint cannot be formed.
  4. `NO_RECENT_CLOSE_ABOVE_INTERNAL_MID` → `HOLD`: no earlier closed 1h candle in the effective 48h lookback has `close > internal_range_midpoint`.
  5. `NO_LOWER_ZONE` → `HOLD`: the current close is below the selected support, but there is no usable next-lower support (`next_lower.high < zone.low`) from which to form the below-zone band.
  6. `BELOW_ZONE_OUT_OF_BAND` → `HOLD`: the current close is below `zone.low` but not inside the 50%–100% band of `(next_lower.high → zone.low)`.
  7. `ZONE_APPROACHED_FROM_BELOW` → `HOLD`: the nearest earlier closed 1h candle whose `close` is strictly outside the selected zone (`close > zone.high` or `close < zone.low`) closed below `zone.low`. Only an approach from above (`last_outside.close > zone.high`) may BUY.
  8. `RECENT_BUY_IN_24H` → `HOLD`: a persisted `BUY` already exists for the same `selected_zone_fingerprint` in the prior `cooldown_hours` (default 24). A later close above `internal_range_midpoint` does not unlock the zone inside that window. A prior BUY at a different zone does not trigger this reason. Evaluated before setup identity so it is the stored reason when both would apply.
  9. `SETUP_ALREADY_BOUGHT` → `HOLD`: a persisted `BUY` already exists for the same `setup_id` (`selected_zone_fingerprint` + `dip_origin_open_time`). Elapsing 24h does not reset the setup. A later close above `internal_range_midpoint` creates a new dip origin and therefore a new setup. A prior BUY at a different zone does not trigger this reason.
  10. `BUY_GATES_PASSED` → `BUY`: all unified dip-to-support gates passed.
- Decision trusts zones already produced by the zones finder and candles already stored in DB. It does not re-validate zone construction or candle integrity. An empty zone list simply yields `CLOSE_OUTSIDE_ENTRY_REGION`. Fetch/API failures, zone-build failures, and an overdue incomplete 4H bucket (bucket end passed but missing constituent closed 1h candles) are runner/zones-finder errors, not decision `reason_code`s—abort before evaluating the signal.
- These are the complete decision reason codes. Pause state, daily/cumulative limits, wallet balance/allowance, gas, quote freshness/`deadline`, router validation, and simulation do **not** rewrite a valid `BUY` as `HOLD`; they are downstream execution skip/failure reasons stored in `trade_executions`. This keeps the signal deterministic and identical across `observe`, `dry_run`, `live`, and backtest.
- Shared payload for every cycle: current closed 1h candle/time, `zone_set_as_of`, fingerprint version, selected zone fingerprint and bounds (including zone mid), selected `zone_source_time` / `source_open_times`, entry region (`inside_zone` / `below_zone_band`), adjacent higher-zone and optional next-lower-zone fingerprints and bounds, internal-range midpoint, below-zone band bounds + close position pct when below the zone, effective 48h lookback bounds (including any `zone_source_time` floor), selected dip-origin candle time/close, `setup_id`, last-outside candle time/close, setup-already-bought flag, recent-buy-in-24h flag, gate results, whether zones rebuilt this cycle, and strategy/config version.
- `observe` = same signal + persist decision (no DEX). This is the useful “paper” path for the live strategy.
- `dry_run` = same signal + quote/simulate + persist, no signing.
- `live` = same signal + risk + quote + sign/broadcast when all gates pass.

## Zone identity and fingerprint (`zf1`)
- **Yes, fingerprints are persisted in SQLite.** Identities are computed once for every zone immediately after zone finding/source-time resolution, before the zone set is persisted or used by `signal.py`. Loaded zone sets reuse the stored values; the hourly signal path must never silently recompute them.
- `zone_identity.py` owns two `zf1:` SHA-256 hashes from the same canonicalize/serialize path:
  - `zone_lineage_id`: stable shelf identity from `low`, `high`, `source_timeframe`, and `bounds_style` (plus exchange/symbol/detector). Stored as `fingerprint` for cooldown, setup id, and chart segment merge.
  - `revision_fingerprint`: same band plus canonical `source_open_times`, for audit/cache when evidence is added.
  - `zone_source_time = max(source_open_times)` is persisted next to both hashes but is not duplicated in either payload.
- Adding a later touch must **not** change `zone_lineage_id` / `fingerprint`. It must change `revision_fingerprint`. Changing canonical low/high, source timeframe, bounds style, or detector version creates a new lineage. Changing only `origin` / `touches` must not change lineage.
- Fail closed: empty/unresolvable/out-of-range source indexes, missing source candles, unsupported source timeframe, or missing fingerprint input abort the zone refresh. Do not persist a partial zone set, advance the watermark, or evaluate a decision.
- Setup lookup for live/`observe`/`dry_run` is an indexed query for a prior `BUY` with the exact `selected_zone_fingerprint` and `dip_origin_open_time`. The 24h same-zone cooldown is a separate indexed query on `selected_zone_fingerprint` + `candle_open_time` (no `dip_origin` join). Backtest stores `trigger_open_time` + fingerprint + dip origin in its isolated in-memory BUY list. No fallback to `low`/`high`, nearest-price matching, or raw `source_indexes`.

## `bot_state` and zone-rebuild watermark
- Reuse the generic `bot_state` table already declared in [src/db.py](src/db.py); `init_db` creates it idempotently:

```sql
CREATE TABLE IF NOT EXISTS bot_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
```

- Use one versioned/scoped key so another symbol, timeframe, or detector cannot reuse the watermark accidentally:

```text
zone_rebuild_watermark:binance:BTCUSDT:4h:support_structure_v2
```

  - `value` is the decimal-string form of the closed 4H `open_time` in Unix milliseconds (the same value used as `zone_set_as_of`), not JSON and not the candle close time.
  - `updated_at` is the bot write time in Unix seconds for audit only; it is not used to decide whether zones are stale.
- Add focused `src/trading/state_store.py` helpers instead of putting SQL in `runner.py`:
  - `get_zone_rebuild_watermark(conn, key) -> int | None`: read and strictly parse the TEXT value.
  - `set_zone_rebuild_watermark(conn, key, open_time_ms, updated_at_s)`: `INSERT ... ON CONFLICT(key) DO UPDATE`; accept an existing connection and never commit internally so zone rows + watermark remain one transaction.
  - Validate a loaded value is a non-negative integer, aligned to a Binance UTC 4H bucket (`open_time_ms % 14_400_000 == 0`), not newer than the latest completed closed 4H candle, and backed by a `zone_sets` manifest with matching scope/`zone_set_as_of` whose `zone_count` equals the persisted zone-row count. The manifest is required because a valid detector result may contain zero zones. Invalid/future/orphaned/incomplete state is a runner error; do not silently coerce or advance it.
- Bootstrap when the key is absent:
  1. Require at least one usable latest completed closed 4H candle and enough history for `support_structure_v2`; otherwise abort with no decision.
  2. Run the normal zone rebuild through `zone_refresh` using all eligible closed 4H history through that latest candle.
  3. Resolve sources, create all `zf1` fingerprints, persist the zone rows plus a `zone_sets` manifest (`zone_count` may be zero) with `zone_set_as_of = latest_closed_4h.open_time`, and upsert the watermark to the same value in one SQLite transaction.
  4. Only after commit may the runner evaluate the current 1h signal. A failure rolls back both zone rows and `bot_state`, so the next cycle retries bootstrap.
- Normal cycle behavior:
  - `latest_completed_4h.open_time == watermark` → validate the matching `zone_sets` manifest/count and load its zone rows (including a valid empty set); do not run the detector.
  - `latest_completed_4h.open_time > watermark` → rebuild once through the latest completed 4H candle, then persist fingerprinted zone rows + manifest and advance the watermark atomically.
  - `latest_completed_4h.open_time < watermark`, malformed state, or no matching complete manifest/snapshot → fail closed as a runner/state error; no decision or trade.
- Use `BEGIN IMMEDIATE` for zone rows + `zone_sets` manifest + watermark. Re-read the watermark inside that transaction; if another idempotent runner already advanced it to the target, do not write a duplicate snapshot—validate/load the committed target set. Never update `bot_state` before all zone rows validate and the matching manifest is ready to persist.
- Backtest must not read or mutate this live `bot_state` key. It keeps an in-memory replay watermark (and isolated BUY state), initialized fresh for each run, so repeated input produces identical rebuilds and BUY timestamps.

## Signal timing and algorithm
1. systemd runs one idempotent cycle around `HH:00:10 UTC`; retry Binance only for a short bounded window.
2. Fetch recent Binance `BTCUSDT` `1h` klines only and upsert them into `candles` with `timeframe="1h"`. Use the latest closed `1h` row from DB as the reference candle (`is_closed=1`); ignore any currently open candle. If the fetch fails or no closed candle exists yet, abort the cycle as a runner error—do not invent a decision.
3. From closed 1h candles, decide how to treat the Binance-aligned 4H bucket relative to the zone watermark:
   - while the current 4H bucket is still forming (clock still inside `00/04/08/12/16/20` + 4h), skip detector work and load the last persisted zones for evaluation—stale-until-close is intentional;
   - when the bucket end time has passed, require all 4 constituent closed 1h candles. If any are missing, abort the cycle as a runner/data error: do not persist a decision, do not rebuild, and do not trade on the pre-watermark zone set;
   - when all 4 are present, aggregate OHLCV for that bucket (`open`=first, `high`=max, `low`=min, `close`=last, `volume`=sum) and upsert the closed 4H bar into `candles` with `timeframe="4h"`;
   - initialize/read/validate the scoped zone-rebuild watermark from `bot_state` using the locked bootstrap and state rules above, then compare the completed 4H `open_time` with it;
   - if newer, run `support_structure_v2` on closed 4h candles only; then in `zone_refresh` (not inside the detector, not in the signal engine) resolve each zone's detector `source_indexes` into stable `source_open_times` **before** persistence and fingerprinting;
   - `source_indexes` are dataframe row positions created during zone finding (`pivots` / `factory`), not timestamps. They are only valid against the dataframe that produced them:
     - normal 4H zones → map against the closed 4H dataframe used for detection;
     - daily overlay zones (`source_timeframe="1d"`) → map against the derived daily dataframe aggregated from those same 4H candles (see [src/zones/daily.py](src/zones/daily.py)), never against the 4H frame;
   - for each zone: `source_open_times = sorted(set(int(source_df.iloc[i]["open_time"]) for i in source_indexes))`; `zone_source_time = max(source_open_times)`; create `zone_lineage_id` and `revision_fingerprint`; persist `source_open_times`, `zone_source_time`, lineage as `fingerprint`, and `zone_set_as_of` with the zone set;
   - `zone_set_as_of` is the newly completed 4H bucket `open_time` used for that detector rebuild/watermark. Every zone produced by one rebuild carries the same value;
   - persist the complete fingerprinted zone set and advance the watermark in one transaction only after every zone validates; if source-time resolution, fingerprint generation, or persistence fails for any zone, roll back and leave both the prior zone set and watermark unchanged;
   - do **not** re-resolve indexes later in `signal.py`: hourly cycles may load a previously persisted zone set whose indexes would be wrong against a longer/shorter candle frame;
   - if the latest completed 4H `open_time` is not newer than the watermark, skip detector work and load the last persisted zones (already carrying `source_open_times` / `zone_source_time` / fingerprint) for evaluation.
4. Use `zones = detector_result["support"]` (already sorted low→high in the detector; `active` is always empty). Do **not** filter by `price_state == "support"`: a zone currently above price is classified `resistance`/`active` by the detector, but Path B (immediately-below-zone entry) still needs that broken/overhead support in the candidate list. Before selecting a zone, require the trigger 1h candle to be **red** (`close < open`); if `close >= open`, return `CLOSE_NOT_BELOW_OPEN` and do not select a zone. Then select the nearest support reached by the current close, and validate the current entry region before evaluating dip history:
   - if the close is inside one or more zones (`zone.low <= close <= zone.high`):
     - `containing_zones = [z for z in zones if z.low <= close <= z.high]`;
     - `selected = max(containing_zones, key=lambda z: z.low)` (detector usually removes overlaps; this tie-break keeps live/observe/backtest replay independent of list order);
     - any close inside the selected band qualifies (`0%` = `selected.low`, `100%` = `selected.high`); set `entry_region = inside_zone`;
   - otherwise:
     - select the nearest zone above the close: `selected = min((z for z in zones if z.low > close), key=lambda z: z.low)`;
     - do not skip the nearest zone to evaluate a farther one;
     - `next_lower_zone = max((z for z in zones if z.high < selected.low), key=lambda z: z.high)`; if none exists, return `NO_LOWER_ZONE`;
     - define `below_gap_low = next_lower_zone.high` and `below_gap_high = selected.low`;
     - calculate `below_zone_pct = (close - below_gap_low) / (below_gap_high - below_gap_low)`;
     - require **50% ≤ `below_zone_pct` ≤ 100%** and `close < selected.low` (effectively `0.50 <= below_zone_pct < 1.0`); otherwise return `BELOW_ZONE_OUT_OF_BAND`;
   - if no zone contains the close and no zone exists above it, return `CLOSE_OUTSIDE_ENTRY_REGION`.
5. For the selected zone, lock adjacent higher support with the same deterministic formula used by live and offline replay:
     - `higher_zone = min((z for z in zones if z.low > selected.high), key=lambda z: z.low)`;
     - if none exists (no zone fully above, or every neighbor above touches/overlaps), return `NO_HIGHER_ZONE`;
     - `internal_range_low = selected.high`;
     - `internal_range_high = higher_zone.low`;
     - `internal_range_midpoint = (internal_range_low + internal_range_high) / 2`.
6. Find the dip origin in the **48h lookback** using closed 1h candles with `open_time` in `[trigger.open_time - 48h, trigger.open_time)`:
   - `zone_source_time` is the newest forming touch of the **selected** zone: `max(source_open_times)` already resolved and persisted at zone-refresh time. Effective lookback floor is `max(trigger.open_time - 48h, zone_source_time)` so a dip origin cannot come from before the selected zone existed;
   - also ignore candles older than `zone_source_time` when that is newer than the 48h floor;
   - scan backward from the candle immediately before the trigger and stop at the **first (nearest)** candle whose `close` is **strictly greater than** `internal_range_midpoint`;
   - store that candle as `dip_origin_candle`; do not use its OHLC `high`, do not calculate a maximum across the window, and do not continue to an older qualifying candle;
   - this candle may visually resemble an internal high of the down/dip leg, but the signal does not require a pivot label: the existing zone-finder internal pivots are 4H/wick-based, while this gate is explicitly the nearest qualifying 1h `close`;
   - because this is the nearest qualifying candle, every later closed 1h candle before the trigger has `close <= internal_range_midpoint`; do not additionally require those intermediate closes to descend monotonically;
   - if no qualifying candle exists, return `NO_RECENT_CLOSE_ABOVE_INTERNAL_MID`. Do not require perfect contiguity of every hourly slot.
7. Buy only when all unified entry conditions pass:
   - the trigger closed 1h candle is **red** (`close < open`);
   - the current closed 1h `close` is either **inside the selected support anywhere from 0%–100% of the zone span** (`zone.low <= close <= zone.high`, where `0%` = `zone.low` and `100%` = `zone.high`) or **immediately below the support in the 50%–100% band** (`close < zone.low` and `0.50 <= below_zone_pct <= 1.0`; the separate `close < zone.low` condition excludes the 100% boundary);
   - `dip_origin_candle` exists in the effective 48h lookback;
   - the nearest earlier closed 1h candle whose `close` is strictly outside the selected zone closed **above** `zone.high`. A last-outside close below `zone.low` returns `ZONE_APPROACHED_FROM_BELOW`;
   - there is **no prior `BUY` for the same zone fingerprint in the prior 24h**. A same-zone rebuy inside that window returns `RECENT_BUY_IN_24H` even when a later close above `internal_range_midpoint` created a new dip origin;
   - there is **no prior `BUY` for the same `setup_id`** (`selected_zone_fingerprint` + `dip_origin_open_time`). After 24h, a same-setup rebuy still returns `SETUP_ALREADY_BOUGHT`. The zone resets only when a later close above `internal_range_midpoint` creates a new dip origin. Live/`observe`/`dry_run` scan the `decisions` table only—no separate lock table. Backtest uses an isolated prior-BUY store for the current replay (see Offline backtest)—never the live `decisions` table. Intentional DCA: if price continues down to a deeper support, that deeper zone may still BUY.
8. Persist every cycle outcome into `decisions` (including `HOLD` with reason codes).
9. Once `BUY_GATES_PASSED` is selected, `observe` stops after persistence. `dry_run` / `live` first apply pause, unresolved-transaction, daily/cumulative cap, balance, allowance, and gas-reserve checks. If blocked, preserve the `BUY` decision and persist the downstream skip reason in `trade_executions`.
10. If execution checks pass, call the in-house quote API for a fresh `USDT → PRANA` quote for exactly 1 USDT (`amountIn="1"`), with the bot wallet as `recipient` and configured `slippageBps` (default 50). Continue only when the response echoes the expected symbols/amount/recipient/`chainId=137`, `transaction.to` equals `routerAddress` (and matches the configured router allowlist if set), `transaction.value` is `"0"` for this ERC-20 swap, calldata is non-empty, `deadline` is still in the future with enough margin to sign/broadcast, and `amountOut` / `minimumAmountOut` are present and parseable. Then run `eth_call` and `estimate_gas` before signing. Persist quote/validation/simulation failures as execution outcomes, not decision reason codes. **Do not** apply a Binance-BTC-to-DEX price-deviation check to PRANA quotes (no comparable BTCUSDT→PRANA spot on Binance); trust route + slippage + deadline gates instead.

## Trading package and configuration
- Extend [src/config.py](src/config.py) and [config.example.yaml](config.example.yaml) with typed sections for `price_feed`, `wallet`, `strategy`, `execution`, `risk`, and `logging`. Remove obsolete `SignalConfig` score thresholds (`near_support_pct_*`, `dip_*`) once the paper scorer is gone. Put paths and non-secret limits in YAML (`wallet.keystore_path`, optional credential paths, `execution.quote_base_url` with documented defaults **prod** `http://127.0.0.1:4173` / **dev** `https://prana.triethocduongpho.net`, pinned router allowlist, token symbols). Accept the Polygon RPC URL and keystore password only from env, systemd credentials, or manual prompt—never from committed YAML.
- Add `src/trading/` with focused modules:
  - `models.py`: decision, quote, swap transaction, intent, and receipt models using `Decimal`/integer token units rather than binary floats.
  - `constants.py`: chain ID 137 and an immutable allowlist for USDT, PRANA, and the expected SwapRouter02 address; runtime checks must verify chain ID, bytecode, token decimals, and wallet address.
  - `binance_hourly.py`: thin helper to fetch/validate/upsert closed Binance `BTCUSDT` `1h` candles into the existing `candles` table (reuse [src/binance_client.py](src/binance_client.py) and [src/db.py](src/db.py) helpers). No live `4h` Binance fetch in this path.
  - `aggregate_4h.py`: derive closed Binance-aligned 4H bars from closed 1h candles; reject incomplete buckets. Distinguish “bucket still forming” (ok to keep prior zones) from “bucket overdue and incomplete” (runner must abort).
  - `state_store.py`: typed `bot_state` watermark key construction, strict read/validation, and connection-scoped upsert helpers; no internal commits.
  - `zone_refresh.py`: bootstrap when the scoped watermark is absent; compare derived/latest closed 4h `open_time` against a validated watermark; rebuild zones only when newer; immediately resolve `source_indexes` → `source_open_times` / `zone_source_time` (4H frame vs daily frame for `source_timeframe="1d"`); atomically persist the fingerprinted zone snapshot + watermark; expose `detector_result["support"]` (full support list, not `active` / not `price_state`-filtered) for the signal. Pure rebuild helper `build_fingerprinted_support_zones` is shared with offline backtest so detector/source-time/`zf1` logic is not duplicated.
  - `zone_identity.py`: pure helpers to resolve source indexes against the correct dataframe, canonicalize source times/prices, compute `zone_source_time = max(source_open_times)`, build a stable `zone_lineage_id` (band + timeframe + bounds style) plus a source-aware `revision_fingerprint`, store the lineage as `fingerprint` for cooldown/setup/chart merge, and validate required inputs. Called from `zone_refresh` at rebuild time only—not from the detector and not from the hourly signal path.
  - `signal.py`: the sole unified dip-to-support decision engine (red trigger candle, then inside-zone 0–100% or below-zone 50–100% entry region). Reads already-persisted fingerprints / `zone_source_time`; keep nearest-support selection, the 48h backward scan for the nearest qualifying close, last-outside approach direction, midpoint calculation, below-zone pct helper, per-zone 24h cooldown lookup, and same-setup prior-BUY lookup (`fingerprint` + `dip_origin_open_time`) here as small pure helpers; replace [src/signals.py](src/signals.py) and do not leave a parallel scorer. Pure engine also accepts `mode="backtest"` (not a `decisions.mode` schema value).
  - `wallet.py`: encrypted-keystore creation/loading, password resolution (env → systemd credential file → optional prompt), address verification, permissions checks, and signing only after all other gates pass.
  - `prana_swap.py`: a thin HTTP adapter for `POST {quote_base_url}/api/swap/quote` plus strict response/transaction/`deadline` validation. Never send an `Origin` header. Do not add protocol-specific route selection; the configured quote host owns routing (prod local route server; dev public PRANA host).
  - `transaction.py`: shared simulation, gas estimation, signing, broadcast, receipt decoding, and pending-transaction reconciliation.
  - `risk.py`: amount, daily/total caps, balance, allowance, gas, response-age/`deadline`, pause-file, and in-flight checks.
  - `store.py`: SQLite reads/writes for `decisions` / `trade_executions` and idempotent state transitions.
  - `runner.py`: orchestration only—fetch 1h, derive/rebuild 4H zones when complete, abort on overdue incomplete 4H (no decision / no trade on stale pre-watermark zones), evaluate decision, then mode-gated risk/quote/simulate/sign/send/reconcile.
  - `backtest.py`: offline replay that walks historical closed 1h candles, rebuilds zones only on newly completed closed 4H bars via the shared fingerprinted-zone helper, and calls the same `support_close_v2` engine with `mode="backtest"`. Keep a **per-replay isolated prior-BUY list** (in-memory) for the 24h per-zone cooldown and same-setup identity (`fingerprint` + `dip_origin_open_time`)—do **not** read `decisions` from `observe`/`dry_run`/`live` or from a previous backtest run. Same input must yield the same BUY timestamps. CLI prints a BUY-only summary and writes the locked BUY CSV. Optional visual chart is served by [src/backtest_chart_server.py](src/backtest_chart_server.py) / [scripts/serve_backtest_chart.py](scripts/serve_backtest_chart.py) (`GET /api/backtest` returns meta/candles/buys/zone_segments only—never HOLD). No swap quote, wallet, or gas simulation in this phase.
- Add only the required `web3`/`eth-account` support in [requirements.txt](requirements.txt); reuse the project's HTTP facilities for the configured quote API (loopback or public HTTPS) and the standard library rotating logger rather than adding SDK/logging dependencies.
- Keep [src/cli.py](src/cli.py) thin. Add commands that delegate to the trading package: `wallet-create`, `wallet-status`, `approve-trading`, `revoke-trading`, `trade-check`, `trade-once`, and `backtest`. Extend or add a backfill path that can fetch multi-month closed `1h` candles (not only Phase 1 `4h`). **Remove** paper `run-once` (or repoint it to `trade-once --mode observe` if a thin alias is useful). Remove `assert_paper_mode_only` / paper-only guardrails that block live work once keystore flows exist; keep fail-closed live confirmation instead.

## Wallet, approval, and transaction safety
- **Dev vs prod keystores (required):**
  - **Dev/local:** `data/wallet/trader-dev.json` (or temp keystore in tests). Fund minimally or not at all. Default modes `observe` / `dry_run`. No prod keystore on dev machines.
  - **Prod Pi:** `data/wallet/trader-prod.json` (or `/var/lib/buy-the-dips-bot/wallet/trader-prod.json`). Only this wallet receives canary USDT/POL and live approvals. Operator creates it manually on the Pi; agents must not run prod `wallet-create` with real secrets.
  - Config example documents both paths; each machine uses the path matching its role. `wallet-status` prints address only.
- `wallet-create` generates the account locally, encrypts it with `eth-account`, writes to the configured keystore path with mode `0600`, and prints only the public address. Update [.gitignore](.gitignore) to exclude keystores and runtime logs.
- **Prod secret delivery (Pi Ubuntu):** document a systemd `service` + `timer` using `LoadCredential=keystore_password:/etc/buy-the-dips-bot/keystore.password` (and optionally `LoadCredential=polygon_rpc_url:...`). Bot reads the matching files under `/run/credentials/<unit>/` at runtime. Source files under `/etc/buy-the-dips-bot/` are `chmod 600`, owned by root; service runs as dedicated `botuser`. Do not store prod secrets in the git repo or Cursor workspace.
- **Dev secret delivery:** local manual runs may use `KEYSTORE_PASSWORD` in shell env for throwaway-wallet testing only. Never copy prod credential files to dev.
- Approval is never automatic on every trade beyond the per-quote allowance top-up when needed. `approve-trading` first verifies Polygon chain ID 137, allowlisted router code, token symbol/decimals, balances, and configured wallet. It then grants the router a direct USDT allowance capped to the 10 USDT canary total; never grant an unlimited allowance. Handle USDT's zero-reset requirement when changing a non-zero allowance. `revoke-trading` resets that allowance to zero. Run prod approvals only on the Pi against `trader-prod`. Live path may still call `approve(router, amountInRaw)` when allowance is below the quote's `amountInRaw`.
- Ask the in-house quote API for exactly 1 USDT → PRANA with the bot as `recipient` and configured `slippageBps`. Reject stale or mismatched responses (wrong symbols, amount, recipient, chain, router, nonzero value for ERC-20, empty calldata, expired/`deadline` too soon), then run `eth_call` plus `estimate_gas` before signing. Do not locally encode Uniswap paths or router calldata; use `quote.transaction` as-is.
- Persist the reserved nonce and locally derived transaction hash before broadcast. On timeout, reconcile that exact hash instead of creating another trade. Never auto-replace a pending transaction and never open a second trade while one is unresolved.
- Decode the PRANA `Transfer` event and receipt to store actual output, block, gas used, and final status. PRANA remains in the bot wallet; selling or stop-loss behavior is intentionally outside this phase.

## Persistence and logging
- Do **not** add an `hourly_price_observations` table. Persist 1h decision prices in the existing `candles` table with `timeframe="1h"`. Persist derived closed 4H bars in the same `candles` table with `timeframe="4h"`.
- **Drop the `signals` table** and `insert_signal` from [src/db.py](src/db.py). Paper rows are not migrated. Prefer recreating the local SQLite DB (or documenting a one-shot drop) rather than keeping a dual schema.
- Add a single `decisions` table for every hourly evaluation (`BUY`/`HOLD`) and a `trade_executions` table for on-chain work. Uniqueness around closed 1h decision time, strategy/zone fingerprint, and transaction hash so reruns cannot duplicate a buy.
- Add a `zone_sets` manifest table so completeness is explicit even when the detector returns zero zones:

```sql
CREATE TABLE zone_sets (
  exchange TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  zone_set_as_of INTEGER NOT NULL,
  zone_count INTEGER NOT NULL CHECK(zone_count >= 0),
  created_at INTEGER NOT NULL,
  PRIMARY KEY(exchange, symbol, timeframe, detector_version, zone_set_as_of)
);
```

- Persist zone rows in SQLite with explicit identity columns (recreate the disposable DB rather than retaining the old underspecified schema): `exchange TEXT NOT NULL`, `zone_set_as_of INTEGER NOT NULL`, `fingerprint_version TEXT NOT NULL`, `fingerprint TEXT NOT NULL`, `detector_version TEXT NOT NULL`, `source_timeframe TEXT NOT NULL`, `source_open_times_json TEXT NOT NULL`, and `zone_source_time INTEGER NOT NULL`, alongside existing symbol/timeframe/origin/bounds/`source_indexes`. Add `UNIQUE(exchange, symbol, timeframe, detector_version, zone_set_as_of, fingerprint)`. The manifest `zone_count` must equal the number of matching rows; indexes alone are not enough for later hourly cycles or cooldown matching.
- Decision identity columns are explicit: `zone_set_as_of`, `fingerprint_version`, nullable `selected_zone_fingerprint`, nullable `higher_zone_fingerprint`, and nullable `next_lower_zone_fingerprint`, plus selected `zone_source_time` / `source_open_times`. They are nullable only when the gate exits before that zone exists (for example, no selected zone or no next-lower zone). Other decision columns remain: candle open/close times and reference close, entry region and zone bounds, internal-range midpoint, optional below-zone band bounds + pct, effective 48h window bounds, dip-origin candle time/close, `decision`, closed-set `reason_code`, gate JSON, zones-rebuilt flag, mode, strategy/config version, and sanitized error.
- Add an index supporting setup lookup, e.g. `INDEX decisions_buy_setup ON decisions(decision, selected_zone_fingerprint, dip_origin_open_time)`. A BUY decision must have non-null `selected_zone_fingerprint`; fail persistence otherwise. `trade_executions` references the decision ID and therefore inherits its selected-zone identity; do not duplicate/recompute fingerprints in execution code.
- Execution columns: decision id, execution status/skip/failure reason, quote server/router version summary, sanitized route summary, quote/`minimumAmountOut`, nonce, transaction hash, receipt fields, actual PRANA output, status. Never persist API keys, raw calldata, signed transactions, or `verification.token`.
- Watermark persistence uses the existing generic `bot_state(key, value, updated_at)` table and the exact scoped key/value/validation/bootstrap rules above. The watermark equals the last closed 4H `open_time` for which matching zone rows + a count-verified `zone_sets` manifest committed. Zone rows, manifest, and watermark advance are one `BEGIN IMMEDIATE` transaction; never commit one without the others.
- Add structured JSON logging to stdout and a rotating ignored file. Include a per-cycle correlation ID, `zone_set_as_of`, `fingerprint_version`, selected/higher/next-lower fingerprints when available, and every no-trade/trade transition. Add redaction tests to ensure passwords, decrypted keys, RPC URLs, signed transaction bytes, and quote verification tokens never appear.

## Default canary controls
- Modes: `observe` records 1h candles and decisions only; `dry_run` also quotes and simulates; `live` may sign and broadcast. Default to `observe`, and require both live config and a separate wallet-specific confirmation value to enter `live`.
- Initial hard limits: exactly 1 USDT per trade, at most 3 trades per UTC day, and 10 USDT cumulative live spend. Increasing the cumulative cap requires an explicit config change after review.
- Default execution checks: one dip-to-support signal where the trigger 1h candle is red (`close < open`), the nearest qualifying closed 1h candle in the effective 48h lookback closed above `internal_range_midpoint`, the current close is inside support anywhere from 0%–100% of the zone span (`0%` = `zone.low`, `100%` = `zone.high`) or immediately below it in the 50%–100% band, the last outside close approached the selected zone from above, the same zone has no BUY in the prior 24h, and this `setup_id` has not already BUY (deeper zones remain eligible); plus 0.50% slippage (`slippageBps=50`), quote completion before `deadline` (~3 min), configurable maximum gas, minimum POL reserve, a short maximum quote-response age, and a `data/PAUSE_TRADING` kill switch. No Binance-to-DEX PRANA price-deviation gate.
- The dedicated wallet should hold only the small approved USDT canary amount plus enough POL for gas.

## Offline backtest (required before live) — implemented
- Goal: prove `support_close_v2` fires sensibly on history before spending canary capital—especially **where** each BUY landed. This is signal-only replay, not a full PnL/portfolio simulator.
- **1h historical backfill:** [src/candles.py](src/candles.py) supports `1h` and `4h`. Use `python3 -m src.cli backfill --timeframe 1h` (and optional 4h warm-up) or [scripts/backfill_1h_from_2026_06_01.py](scripts/backfill_1h_from_2026_06_01.py).
- Input: already stored closed Binance `BTCUSDT` `1h` candles; warm-up uses older closed `4h` rows before the 1h-covered region. Inside the replay window, completed 4H bars are always derived from four closed 1h candles (no future 4h).
- Method ([src/trading/backtest.py](src/trading/backtest.py)): walk each closed 1h candle in `[start, end)`; rebuild zones only when a newer closed 4H bar appears via shared `build_fingerprinted_support_zones`; call `evaluate_support_close_v2(..., mode="backtest")`; assume fill at the trigger candle `close` for summary only.
- Bounds: `start` inclusive / `end` exclusive; ISO-8601 with timezone on UTC hour boundaries; `end` defaults after the latest closed 1h; require continuous 1h from `start - 48h`.
- **Isolated setup state:** each backtest run keeps its own in-memory prior-BUY list (`trigger_open_time` + zone fingerprint + `dip_origin_open_time`) for both the 24h per-zone cooldown and same-setup identity. Do not mix with live `decisions`. Re-running the same window emits the same BUY timestamps.
- Output:
  - Compact CLI summary — range, evaluated candle count, zone rebuild count, `BUY` count, CSV path. HOLD is computed internally but not printed.
  - **Export every BUY row to CSV** with columns:
    - `trigger_time`
    - `trigger_close`
    - `entry_region`
    - `fingerprint_version`
    - `selected_zone_fingerprint`
    - `zone_low`
    - `zone_mid`
    - `zone_high`
    - `higher_zone_fingerprint`
    - `higher_zone_low`
    - `internal_range_midpoint`
    - `next_lower_zone_fingerprint`
    - `next_lower_zone_high`
    - `below_zone_pct`
    - `dip_origin_time`
    - `dip_origin_close`
    - `zone_set_as_of`
  - Visual chart: `python3 scripts/serve_backtest_chart.py --start <ISO> [--end <ISO>]` serves 1h candles, BUY markers, and `zone_segments` with `valid_from`/`valid_to` (merged consecutive identical bands). `GET /api/backtest` never includes HOLD. Keep [scripts/serve_chart.py](scripts/serve_chart.py) for the latest-zone 4h helper chart.
- Scope limits: no swap quotes, no slippage model, no gas, no sell/exit logic. Backtest validates entry timing and gate behavior only. Does not mutate `bot_state` / `zones` / `zone_sets` / `decisions`.
- Gate: operator reviews at least one multi-month backtest window (after 1h backfill) via CLI summary + BUY CSV (+ optional chart) and confirms BUY density/locations look acceptable before enabling `observe` on the Pi. Unit tests cover synthetic replay, CSV columns, CLI parser, and chart payload; the historical run is an operator CLI step, not CI.

## Verification and rollout
- Replace [tests/test_signals.py](tests/test_signals.py) and paper-score assertions with one direct test for every reason code plus precedence tests proving only the first failed gate is stored. Cover: green and doji trigger candles HOLD with `CLOSE_NOT_BELOW_OPEN` before zone selection; empty zones / close outside entry regions; close at zone low; close at zone mid; close at zone high; below-zone close inside/outside the 50%–100% band; no higher zone including touching/overlapping neighbor; no next-lower zone when below support; no qualifying close in the effective 48h window; qualifying close exactly at/just above `internal_range_midpoint`; nearest qualifying candle wins over older qualifying candles; candle `high` above midpoint but `close` at/below it does not qualify; trigger excluded; 48h and `zone_source_time` floors; per-zone 24h lookback with/without a prior same-zone `BUY` for either entry region; prior BUY at shallower zone A within 24h still allows BUY at deeper zone B; and nearest support selection without skipping to a farther zone. Also cover open candle ignored in favor of the latest closed DB row; `BUY_GATES_PASSED` for both entry regions; closed 1h upsert/read; 1h historical backfill support (not 4h-only); 1h→4h aggregation; incomplete in-progress bucket keeps prior zones; overdue incomplete 4H aborts with no decision and no trade on pre-watermark zones; rebuild-zones-only-on-new-4h watermark; zone identity resolving 4H vs daily (`source_timeframe="1d"`) indexes to open times and `zone_source_time = max(source_open_times)`; exact `zf1` canonical JSON/hash fixture; source-time order/dedup invariance; fixed-eight-decimal price canonicalization; fingerprint stability across longer candle frames; fingerprint unchanged when only `origin` / `bounds_style` / `touches` change; fingerprint change when canonical bounds, source touches, source timeframe, or detector version change; fail-closed invalid source resolution without watermark advance; zone/decision DB fingerprint round-trip, constraints, and cooldown index query; token units; minimum-output rounding; config/risk gates; SQLite decision idempotency; execution skip reasons preserving the original `BUY`; encrypted-keystore handling in a temporary directory; logging fingerprint fields and redaction; runner retries; pending/confirmed/reverted reconciliation; and a small synthetic backtest replay whose 24h cooldown uses only that run's in-memory prior BUYs (same input → same BUY timestamps; live `decisions` rows must not affect the replay) and whose BUY CSV includes fingerprint version plus selected/higher/next-lower fingerprints and the locked location columns.
- Add focused `bot_state` tests for: absent-key bootstrap; exact key/value units; equal watermark causing no rebuild; newer watermark causing exactly one rebuild; a valid empty-zone manifest (`zone_count=0`); manifest/row-count mismatch; malformed, unaligned, future, or orphaned watermark failing closed; rollback preserving the previous rows/manifest/watermark; `BEGIN IMMEDIATE` idempotency when another runner reaches the same target; and backtest never reading or writing the live watermark.
- Mock all network/signing boundaries in normal tests. Add quote-adapter contract tests for wrong router/`transaction.to`, nonzero value, empty calldata, stale/`deadline` expired response, API failure, wrong symbols/amount/recipient/chainId, and missing Origin-free header behavior. Add an opt-in Polygon read-only integration check that verifies allowlisted router bytecode, token decimals, and a live `USDT→PRANA` quote without signing (dev may use `https://prana.triethocduongpho.net`; prod check uses loopback `:4173`).
- Update [README.md](README.md): remove Phase 1 paper-score / `signals` table docs; describe the unified dip-to-support algorithm (red trigger 1h `close < open`, nearest qualifying 1h close above the internal midpoint within 48h, current close inside-zone 0%–100% or below-zone 50%–100%, and per-zone 24h no-buy so a deeper zone may still BUY within 24h); document the exact `zf1` canonical payload/hash, source-index resolution, SQLite columns/index, cooldown lookup, logs, and CSV fingerprint fields; describe the offline backtest gate (1h multi-month backfill + full BUY CSV columns + optional visual backtest chart), `decisions`/`trade_executions` schema, dual-timeframe flow (fetch 1h only; derive closed 4H; rebuild on watermark), in-house `POST /api/swap/quote` USDT→PRANA flow with **prod** `http://127.0.0.1:4173` vs **dev** `https://prana.triethocduongpho.net` (approve → broadcast before deadline), modes, dev/prod keystore separation, Pi systemd `LoadCredential` setup for keystore password, direct capped router approval/revocation, audit queries, pause/recovery, and buy-only scope. Note that recreating the local DB drops old paper signal rows.
- Also document the scoped `bot_state` watermark key/value, first-run bootstrap, strict validation, atomic zone-snapshot + watermark transaction, fail-closed state errors, and the fact that backtest uses in-memory state instead.
- Roll out in gates: run the full test suite; backfill multi-month `1h` history; run offline `backtest` and review CLI summary + full BUY CSV; run `trade-check`; collect at least 24 closed 1h decisions in `observe`; run `dry_run` until trigger/skip logs and idempotency are verified; fund only the capped wallet; explicitly approve the capped allowance; enable `live`; stop automatically at 10 USDT cumulative spend and review every receipt before raising any limit.
- Keep `trade-once` as the only scheduler target. Document example Pi Ubuntu systemd `service` + `timer` units with `LoadCredential=` for the keystore password that run at minute 2 of each UTC hour, 24/7. Do not install or enable units automatically. Prod rollout (wallet-create, fund, approve, enable live, ensure quote server on `:4173`) is operator-only on the Pi, not via Cursor agents.
