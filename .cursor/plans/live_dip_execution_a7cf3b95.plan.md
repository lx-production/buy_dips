---
name: live dip execution
overview: "Add a fail-closed Polygon trading flow that keeps the existing Binance 4H zone detector unchanged, fetches only Binance BTCUSDT 1h candles into the existing candles table, derives closed 4H bars from those 1h candles to rebuild zones, and buys exactly 1 USDT of PRANA through the in-house swap quote API (prod: POST /api/swap/quote on the local route server; dev: https://prana.triethocduongpho.net/api/swap/quote). Retire the Phase 1 paper scorer and signals table in favor of one support_close_v1 decision engine and one decisions schema shared by observe, dry_run, and live. Use one dip-to-support entry flow: within the prior 48h, the nearest earlier closed 1h candle whose close is strictly above the internal-range midpoint marks the dip origin; buy when the current closed 1h candle reaches the nearest support either inside the zone strictly below its midpoint or immediately below it in the 70%–100% portion of the gap to the next-lower zone, provided no BUY exists in the prior 24h. Use separate dev/prod keystores; Pi prod runs 24/7 via systemd with LoadCredential for secrets."
todos:
  - id: data-signal
    content: Retire paper scorer/signals table; add one support_close_v1 dip-to-support flow (nearest 48h close above internal midpoint + current close inside-below-mid or below-zone 70–100% band + 24h no-buy) and decisions schema; fetch-only-1h; derive closed 4H; rebuild zones on watermark.
    status: pending
  - id: wallet-safety
    content: Add dev/prod keystore separation, LoadCredential secret loading on Pi, encrypted-keystore CLI flows, contract checks, direct capped in-house router approval, revocation, and live-mode guards.
    status: pending
  - id: quote-execute
    content: Add a thin in-house swap quote adapter (prod local route server; dev https://prana.triethocduongpho.net); validate quote/transaction/deadline/verification; approve router if needed; simulate, execute, and reconcile swaps.
    status: pending
  - id: audit-risk
    content: Wire decisions/executions persistence, structured redacted logs, duplicate prevention, pause switch, and canary risk limits.
    status: pending
  - id: tests
    content: Replace paper-signal tests with the unified dip-origin/support-entry gates, including nearest qualifying 48h close, inside/below-zone entry regions, and 24h no-buy; cover 1h fetch, 1h-to-4h, zone watermark, risk, storage, in-house quote response validation, runner, and tx lifecycle.
    status: pending
  - id: backtest
    content: Add a minimal offline support_close_v1 replay over historical closed 1h/4h candles; print BUY/HOLD summary before observe/dry_run/live.
    status: pending
  - id: docs-rollout
    content: Update README/study to drop paper-score docs; document decisions schema, backtest gate, dev/prod keystores and quote hosts (prod loopback vs https://prana.triethocduongpho.net), Pi systemd LoadCredential units, in-house USDT→PRANA swap flow, and observe/dry-run/capped-live rollout.
    status: pending
isProject: false
---

# Phase 2 Live Dip Execution

## Locked decisions and important constraints
- Keep the existing `support_structure_v1` detector logic in [src/zones/](src/zones/) unchanged. Never feed an open/incomplete 4H bar into the detector (no look-ahead / fake pivots).
- Live price feed fetches **only** Binance `BTCUSDT` `1h` klines. Do **not** call Binance for `4h` during the hourly trading cycle.
- Reuse the existing `candles` table for those `1h` rows (`timeframe="1h"`). Do **not** add a separate hourly price observations table. The decision price is the latest closed 1h `close`.
- Derive closed 4H bars from closed 1h candles aligned to Binance UTC buckets (`00/04/08/12/16/20`). A 4H bar is closed only when all 4 constituent 1h candles are closed. Upsert derived closed 4H bars into `candles` with `timeframe="4h"` so the detector keeps reading the familiar 4H store (historical backfill 4H remains valid; new bars continue from 1h aggregation).
- Rebuild zones automatically only when a newly completed closed 4H bar appears (compared against a watermark). Between 4H closes, keep using the last persisted zone set for the hourly signal.
- **Retire Phase 1 paper scoring.** Remove the score-based path in [src/signals.py](src/signals.py) (`signal_score`, `ALERT_ONLY` / `STRONG_BUY_SIGNAL`, distance-to-`zone.high` heuristics) and the `signals` table / `insert_signal` helpers. Paper history is disposable; DB may be recreated. Do not keep two signal engines.
- Replace them with **one** deterministic `support_close_v1` decision engine and **one** `decisions` schema. `observe`, `dry_run`, and `live` all call the same engine; modes only change what happens after a `BUY` decision (record only / quote+simulate / sign+broadcast).
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

## Single decision engine (`support_close_v1`)
- Output is gate-based, not scored. Every persisted decision has exactly one `decision` (`BUY` or `HOLD`) and exactly one `reason_code` from the closed set below. There is one **dip-to-support** entry flow; the current close may qualify in either of two adjacent entry regions:
  - **Inside support:** `zone.low <= close < zone.mid`.
  - **Immediately below support:** `close < zone.low` and `0.70 <= below_zone_pct <= 1.0` in the gap `(next_lower_zone.high → zone.low)`; the separate `close < zone.low` condition excludes the 100% boundary in practice.
- Both regions share the same setup gates: a nearest qualifying dip-origin candle exists in the 48h lookback with `dip_origin.close > internal_range_midpoint`, and no persisted `BUY` exists in the prior 24h.
- Reason codes (closed set):
  1. `CLOSE_OUTSIDE_ENTRY_REGION` → `HOLD`: the current close is neither inside the selected support strictly below its midpoint nor immediately below it in the 70%–100% band.
  2. `CLOSE_NOT_BELOW_ZONE_MID` → `HOLD`: the close is inside the selected support but at or above its midpoint; the inside-zone entry region requires `zone.low <= close < zone.mid`.
  3. `NO_HIGHER_ZONE` → `HOLD`: the selected support has no usable higher support above it (none exists, or the nearest above touches/overlaps), so a positive internal range and its midpoint cannot be formed.
  4. `NO_RECENT_CLOSE_ABOVE_INTERNAL_MID` → `HOLD`: no earlier closed 1h candle in the effective 48h lookback has `close > internal_range_midpoint`.
  5. `NO_LOWER_ZONE` → `HOLD`: the current close is below the selected support, but there is no usable next-lower support (`next_lower.high < zone.low`) from which to form the below-zone band.
  6. `BELOW_ZONE_OUT_OF_BAND` → `HOLD`: the current close is below `zone.low` but not inside the 70%–100% band of `(next_lower.high → zone.low)`.
  7. `RECENT_BUY_IN_24H` → `HOLD`: a persisted `BUY` decision already exists with trigger `open_time` in `[trigger.open_time - 24h, trigger.open_time)`.
  8. `BUY_GATES_PASSED` → `BUY`: all unified dip-to-support gates passed.
- Decision trusts zones already produced by the zones finder and candles already stored in DB. It does not re-validate zone construction or candle integrity. An empty zone list simply yields `CLOSE_OUTSIDE_ENTRY_REGION`. Fetch/API failures and zone-build failures are runner/zones-finder errors, not decision `reason_code`s.
- These are the complete decision reason codes. Pause state, daily/cumulative limits, wallet balance/allowance, gas, quote freshness/`deadline`, router validation, and simulation do **not** rewrite a valid `BUY` as `HOLD`; they are downstream execution skip/failure reasons stored in `trade_executions`. This keeps the signal deterministic and identical across `observe`, `dry_run`, `live`, and backtest.
- Shared payload for every cycle: current closed 1h candle/time, selected zone fingerprint and bounds (including zone mid), entry region (`inside_below_mid` / `below_zone_band`), adjacent higher-zone and optional next-lower-zone fingerprints and bounds, internal-range midpoint, below-zone band bounds + close position pct when below the zone, effective 48h lookback bounds, selected dip-origin candle time/close, 24h prior-BUY flag, gate results, whether zones rebuilt this cycle, and strategy/config version.
- `observe` = same signal + persist decision (no DEX). This is the useful “paper” path for the live strategy.
- `dry_run` = same signal + quote/simulate + persist, no signing.
- `live` = same signal + risk + quote + sign/broadcast when all gates pass.

## Signal timing and algorithm
1. systemd runs one idempotent cycle around `HH:00:10 UTC`; retry Binance only for a short bounded window.
2. Fetch recent Binance `BTCUSDT` `1h` klines only and upsert them into `candles` with `timeframe="1h"`. Use the latest closed `1h` row from DB as the reference candle (`is_closed=1`); ignore any currently open candle. If the fetch fails or no closed candle exists yet, abort the cycle as a runner error—do not invent a decision.
3. From closed 1h candles, detect whether a Binance-aligned 4H bucket just completed:
   - aggregate OHLCV for that bucket (`open`=first, `high`=max, `low`=min, `close`=last, `volume`=sum) and upsert the closed 4H bar into `candles` with `timeframe="4h"`;
   - compare that 4H `open_time` with the last zone-rebuild watermark in `bot_state` (the last 4H `open_time` already used to rebuild zones);
   - if newer, run `support_structure_v1` on closed 4h candles only, persist the new zone set, convert each detector `source_index` to source candle `open_time` for a stable zone fingerprint, and advance the watermark;
   - otherwise skip detector work and load the last persisted zones for evaluation.
4. Use `zones = detector_result["support"]` (already sorted low→high in the detector; `active` is always empty). Do **not** filter by `price_state == "support"`: a zone currently above price is classified `resistance`/`active` by the detector, but Path B (immediately-below-zone entry) still needs that broken/overhead support in the candidate list. Select the nearest support reached by the current close, and validate the current entry region before evaluating dip history:
   - if the close is inside one or more zones (`zone.low <= close <= zone.high`):
     - `containing_zones = [z for z in zones if z.low <= close <= z.high]`;
     - `selected = max(containing_zones, key=lambda z: z.low)` (detector usually removes overlaps; this tie-break keeps live/observe/backtest replay independent of list order);
     - require `selected.low <= close < selected.mid`;
     - if `selected.mid <= close <= selected.high`, return `CLOSE_NOT_BELOW_ZONE_MID`;
   - otherwise:
     - select the nearest zone above the close: `selected = min((z for z in zones if z.low > close), key=lambda z: z.low)`;
     - do not skip the nearest zone to evaluate a farther one;
     - `next_lower_zone = max((z for z in zones if z.high < selected.low), key=lambda z: z.high)`; if none exists, return `NO_LOWER_ZONE`;
     - define `below_gap_low = next_lower_zone.high` and `below_gap_high = selected.low`;
     - calculate `below_zone_pct = (close - below_gap_low) / (below_gap_high - below_gap_low)`;
     - require **70% ≤ `below_zone_pct` ≤ 100%** and `close < selected.low` (effectively `0.70 <= below_zone_pct < 1.0`); otherwise return `BELOW_ZONE_OUT_OF_BAND`;
   - if no zone contains the close and no zone exists above it, return `CLOSE_OUTSIDE_ENTRY_REGION`.
5. For the selected zone, lock adjacent higher support with the same deterministic formula used by live and offline replay:
     - `higher_zone = min((z for z in zones if z.low > selected.high), key=lambda z: z.low)`;
     - if none exists (no zone fully above, or every neighbor above touches/overlaps), return `NO_HIGHER_ZONE`;
     - `internal_range_low = selected.high`;
     - `internal_range_high = higher_zone.low`;
     - `internal_range_midpoint = (internal_range_low + internal_range_high) / 2`.
6. Find the dip origin in the **48h lookback** using closed 1h candles with `open_time` in `[trigger.open_time - 48h, trigger.open_time)`:
   - also ignore candles older than `zone_source_time` when that is newer than the 48h floor;
   - scan backward from the candle immediately before the trigger and stop at the **first (nearest)** candle whose `close` is **strictly greater than** `internal_range_midpoint`;
   - store that candle as `dip_origin_candle`; do not use its OHLC `high`, do not calculate a maximum across the window, and do not continue to an older qualifying candle;
   - this candle may visually resemble an internal high of the down/dip leg, but the signal does not require a pivot label: the existing zone-finder internal pivots are 4H/wick-based, while this gate is explicitly the nearest qualifying 1h `close`;
   - because this is the nearest qualifying candle, every later closed 1h candle before the trigger has `close <= internal_range_midpoint`; do not additionally require those intermediate closes to descend monotonically;
   - if no qualifying candle exists, return `NO_RECENT_CLOSE_ABOVE_INTERNAL_MID`. Do not require perfect contiguity of every hourly slot.
7. Buy only when all unified entry conditions pass:
   - the current closed 1h `close` is either **inside the selected support and strictly below its midpoint** (`zone.low <= close < zone.mid`) or **immediately below the support in the 70%–100% band** (`close < zone.low` and `0.70 <= below_zone_pct <= 1.0`; the separate `close < zone.low` condition excludes the 100% boundary);
   - `dip_origin_candle` exists in the effective 48h lookback;
   - there is **no persisted `BUY`** whose trigger `open_time` falls in `[trigger.open_time - 24h, trigger.open_time)`. A recent buy returns `RECENT_BUY_IN_24H` (scan `decisions` only—no separate lock table).
8. Persist every cycle outcome into `decisions` (including `HOLD` with reason codes).
9. Once `BUY_GATES_PASSED` is selected, `observe` stops after persistence. `dry_run` / `live` first apply pause, unresolved-transaction, daily/cumulative cap, balance, allowance, and gas-reserve checks. If blocked, preserve the `BUY` decision and persist the downstream skip reason in `trade_executions`.
10. If execution checks pass, call the in-house quote API for a fresh `USDT → PRANA` quote for exactly 1 USDT (`amountIn="1"`), with the bot wallet as `recipient` and configured `slippageBps` (default 50). Continue only when the response echoes the expected symbols/amount/recipient/`chainId=137`, `transaction.to` equals `routerAddress` (and matches the configured router allowlist if set), `transaction.value` is `"0"` for this ERC-20 swap, calldata is non-empty, `deadline` is still in the future with enough margin to sign/broadcast, and `amountOut` / `minimumAmountOut` are present and parseable. Then run `eth_call` and `estimate_gas` before signing. Persist quote/validation/simulation failures as execution outcomes, not decision reason codes. **Do not** apply a Binance-BTC-to-DEX price-deviation check to PRANA quotes (no comparable BTCUSDT→PRANA spot on Binance); trust route + slippage + deadline gates instead.

## Trading package and configuration
- Extend [src/config.py](src/config.py) and [config.example.yaml](config.example.yaml) with typed sections for `price_feed`, `wallet`, `strategy`, `execution`, `risk`, and `logging`. Remove obsolete `SignalConfig` score thresholds (`near_support_pct_*`, `dip_*`) once the paper scorer is gone. Put paths and non-secret limits in YAML (`wallet.keystore_path`, optional credential paths, `execution.quote_base_url` with documented defaults **prod** `http://127.0.0.1:4173` / **dev** `https://prana.triethocduongpho.net`, pinned router allowlist, token symbols). Accept the Polygon RPC URL and keystore password only from env, systemd credentials, or manual prompt—never from committed YAML.
- Add `src/trading/` with focused modules:
  - `models.py`: decision, quote, swap transaction, intent, and receipt models using `Decimal`/integer token units rather than binary floats.
  - `constants.py`: chain ID 137 and an immutable allowlist for USDT, PRANA, and the expected SwapRouter02 address; runtime checks must verify chain ID, bytecode, token decimals, and wallet address.
  - `binance_hourly.py`: thin helper to fetch/validate/upsert closed Binance `BTCUSDT` `1h` candles into the existing `candles` table (reuse [src/binance_client.py](src/binance_client.py) and [src/db.py](src/db.py) helpers). No live `4h` Binance fetch in this path.
  - `aggregate_4h.py`: derive closed Binance-aligned 4H bars from closed 1h candles; reject incomplete buckets.
  - `zone_refresh.py`: compare derived/latest closed 4h `open_time` against the rebuild watermark, rebuild zones only when newer, persist them, and expose `detector_result["support"]` (full support list, not `active` / not `price_state`-filtered) for the signal.
  - `signal.py` and `zone_identity.py`: the sole unified dip-to-support decision engine (inside-below-mid or below-zone 70–100% entry region) and stable source-time fingerprint. Keep nearest-support selection, the 48h backward scan for the nearest qualifying close, midpoint calculation, below-zone pct helper, and 24h prior-BUY lookup here as small pure helpers; replace [src/signals.py](src/signals.py) and do not leave a parallel scorer.
  - `wallet.py`: encrypted-keystore creation/loading, password resolution (env → systemd credential file → optional prompt), address verification, permissions checks, and signing only after all other gates pass.
  - `prana_swap.py`: a thin HTTP adapter for `POST {quote_base_url}/api/swap/quote` plus strict response/transaction/`deadline` validation. Never send an `Origin` header. Do not add protocol-specific route selection; the configured quote host owns routing (prod local route server; dev public PRANA host).
  - `transaction.py`: shared simulation, gas estimation, signing, broadcast, receipt decoding, and pending-transaction reconciliation.
  - `risk.py`: amount, daily/total caps, balance, allowance, gas, response-age/`deadline`, pause-file, and in-flight checks.
  - `store.py`: SQLite reads/writes for `decisions` / `trade_executions` and idempotent state transitions.
  - `runner.py`: orchestration only—fetch 1h, maybe derive/rebuild 4H zones, evaluate decision, then mode-gated risk/quote/simulate/sign/send/reconcile.
  - `backtest.py`: offline replay that walks historical closed 1h candles, rebuilds zones only on newly completed closed 4H bars, and calls the same `support_close_v1` engine. No swap quote, wallet, or gas simulation in this phase.
- Add only the required `web3`/`eth-account` support in [requirements.txt](requirements.txt); reuse the project's HTTP facilities for the configured quote API (loopback or public HTTPS) and the standard library rotating logger rather than adding SDK/logging dependencies.
- Keep [src/cli.py](src/cli.py) thin. Add commands that delegate to the trading package: `wallet-create`, `wallet-status`, `approve-trading`, `revoke-trading`, `trade-check`, `trade-once`, and `backtest`. **Remove** paper `run-once` (or repoint it to `trade-once --mode observe` if a thin alias is useful). Remove `assert_paper_mode_only` / paper-only guardrails that block live work once keystore flows exist; keep fail-closed live confirmation instead.

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
- Decision columns (conceptual): candle open/close times and reference close, entry region, selected/higher/optional next-lower zone fingerprints and bounds, internal-range midpoint, optional below-zone band bounds + pct, effective 48h window bounds, dip-origin candle time/close, 24h prior-BUY flag, `decision`, the closed-set `reason_code`, gate JSON, zones-rebuilt flag, mode, strategy/config version, sanitized error. Execution columns: decision id, execution status/skip/failure reason, quote server/router version summary, sanitized route summary, quote/`minimumAmountOut`, nonce, transaction hash, receipt fields, actual PRANA output, status. Never persist API keys, raw calldata, signed transactions, or `verification.token`.
- Watermark = the last closed 4H `open_time` for which zones were already rebuilt. Store it in `bot_state` so each hourly cycle can cheaply answer “did a newer closed 4H appear since last rebuild?” without re-running the detector every hour.
- Add structured JSON logging to stdout and a rotating ignored file. Include a per-cycle correlation ID and every no-trade/trade transition. Add redaction tests to ensure passwords, decrypted keys, RPC URLs, signed transaction bytes, and quote verification tokens never appear.

## Default canary controls
- Modes: `observe` records 1h candles and decisions only; `dry_run` also quotes and simulates; `live` may sign and broadcast. Default to `observe`, and require both live config and a separate wallet-specific confirmation value to enter `live`.
- Initial hard limits: exactly 1 USDT per trade, at most 3 trades per UTC day, and 10 USDT cumulative live spend. Increasing the cumulative cap requires an explicit config change after review.
- Default execution checks: one dip-to-support signal where the nearest qualifying closed 1h candle in the effective 48h lookback closed above `internal_range_midpoint`, the current close is inside support strictly below zone mid or immediately below it in the 70%–100% band, and no BUY exists in the prior 24h; plus 0.50% slippage (`slippageBps=50`), quote completion before `deadline` (~3 min), configurable maximum gas, minimum POL reserve, a short maximum quote-response age, and a `data/PAUSE_TRADING` kill switch. No Binance-to-DEX PRANA price-deviation gate.
- The dedicated wallet should hold only the small approved USDT canary amount plus enough POL for gas.

## Offline backtest (required before live)
- Goal: prove `support_close_v1` fires sensibly on history before spending canary capital. This is signal-only replay, not a full PnL/portfolio simulator.
- Input: already stored closed Binance `BTCUSDT` `1h` candles (and derived/closed `4h` bars). Reuse existing backfill + 1h→4h aggregation; do not invent a second candle store.
- Method: walk each closed 1h candle in time order; rebuild zones only when a newer closed 4H bar appears; call the same decision engine used by `observe`/`dry_run`/`live`; assume fill at the trigger candle `close` for summary only.
- Output: compact CLI summary — candle count, zone rebuild count, `BUY` count, `HOLD` reason-code tallies, and a short list of BUY timestamps/prices/zone bounds. Optional CSV export is fine later; do not build charts or a research framework in this phase.
- Scope limits: no swap quotes, no slippage model, no gas, no sell/exit logic. Backtest validates entry timing and gate behavior only.
- Gate: operator reviews at least one multi-month backtest window and confirms BUY density/reason codes look acceptable before enabling `observe` on the Pi. Unit tests cover a tiny synthetic replay; the historical run is an operator CLI step, not CI.

## Verification and rollout
- Replace [tests/test_signals.py](tests/test_signals.py) and paper-score assertions with one direct test for every reason code plus precedence tests proving only the first failed gate is stored. Cover: empty zones / close outside entry regions; close at zone low; close just below/at/above zone mid; below-zone close inside/outside the 70%–100% band; no higher zone including touching/overlapping neighbor; no next-lower zone when below support; no qualifying close in the effective 48h window; qualifying close exactly at/just above `internal_range_midpoint`; nearest qualifying candle wins over older qualifying candles; candle `high` above midpoint but `close` at/below it does not qualify; trigger excluded; 48h and `zone_source_time` floors; 24h lookback with/without a prior `BUY` for either entry region; and nearest support selection without skipping to a farther zone. Also cover open candle ignored in favor of the latest closed DB row; `BUY_GATES_PASSED` for both entry regions; closed 1h upsert/read; 1h→4h aggregation and incomplete-bucket rejection; rebuild-zones-only-on-new-4h watermark; zone identity; token units; minimum-output rounding; config/risk gates; SQLite decision idempotency; execution skip reasons preserving the original `BUY`; encrypted-keystore handling in a temporary directory; redaction; runner retries; pending/confirmed/reverted reconciliation; and a small synthetic backtest replay.
- Mock all network/signing boundaries in normal tests. Add quote-adapter contract tests for wrong router/`transaction.to`, nonzero value, empty calldata, stale/`deadline` expired response, API failure, wrong symbols/amount/recipient/chainId, and missing Origin-free header behavior. Add an opt-in Polygon read-only integration check that verifies allowlisted router bytecode, token decimals, and a live `USDT→PRANA` quote without signing (dev may use `https://prana.triethocduongpho.net`; prod check uses loopback `:4173`).
- Update [README.md](README.md) and [study.md](study.md): remove Phase 1 paper-score / `signals` table docs; describe the unified dip-to-support algorithm (nearest qualifying 1h close above the internal midpoint within 48h, current close inside-below-mid or below-zone 70%–100%, and 24h no-buy), offline backtest gate, `decisions`/`trade_executions` schema, dual-timeframe flow (fetch 1h only; derive closed 4H; rebuild on watermark), in-house `POST /api/swap/quote` USDT→PRANA flow with **prod** `http://127.0.0.1:4173` vs **dev** `https://prana.triethocduongpho.net` (approve → broadcast before deadline), modes, dev/prod keystore separation, Pi systemd `LoadCredential` setup for keystore password, direct capped router approval/revocation, audit queries, pause/recovery, and buy-only scope. Note that recreating the local DB drops old paper signal rows.
- Roll out in gates: run the full test suite; run offline `backtest` on a multi-month window and review BUY/HOLD summary; run `trade-check`; collect at least 24 closed 1h decisions in `observe`; run `dry_run` until trigger/skip logs and idempotency are verified; fund only the capped wallet; explicitly approve the capped allowance; enable `live`; stop automatically at 10 USDT cumulative spend and review every receipt before raising any limit.
- Keep `trade-once` as the only scheduler target. Document example Pi Ubuntu systemd `service` + `timer` units with `LoadCredential=` for the keystore password that run at minute 2 of each UTC hour, 24/7. Do not install or enable units automatically. Prod rollout (wallet-create, fund, approve, enable live, ensure quote server on `:4173`) is operator-only on the Pi, not via Cursor agents.
