---
name: live dip execution
overview: Add a fail-closed Polygon trading path that keeps the existing Binance 4H zone detector unchanged, fetches only Binance BTCUSDT 1h candles into the existing candles table, derives closed 4H bars from those 1h candles to rebuild zones, and buys exactly 1 USDT of WBTC through 1inch Aggregation Router V6 using the Classic Swap API / Pathfinder v6.1. Retire the Phase 1 paper scorer and signals table in favor of one support_close_v1 decision engine and one decisions schema shared by observe, dry_run, and live. Buy when a closed 1h candle closes inside a support zone below the zone midpoint, and the completed 1h high in a 24h pre-entry lookback is strictly above the midpoint of the internal range between that zone and the next higher zone. Use separate dev/prod keystores; Pi prod runs 24/7 via systemd with LoadCredential for secrets.
todos:
  - id: data-signal
    content: Retire paper scorer/signals table; add support_close_v1 engine + decisions schema; fetch-only-1h; derive closed 4H; rebuild zones on watermark; fail-closed timestamps.
    status: pending
  - id: wallet-safety
    content: Add dev/prod keystore separation, LoadCredential secret loading on Pi, encrypted-keystore CLI flows, contract checks, direct capped 1inch router approval, revocation, and live-mode guards.
    status: pending
  - id: quote-execute
    content: Add a thin 1inch Classic Swap API v6.1 adapter; validate Aggregation Router V6 quote/swap responses; simulate, execute, and reconcile swaps.
    status: pending
  - id: audit-risk
    content: Wire decisions/executions persistence, structured redacted logs, duplicate prevention, pause switch, and canary risk limits.
    status: pending
  - id: tests
    content: Replace paper-signal tests with support-close/internal-range gates; cover 1h fetch, 1h-to-4h, zone watermark, risk, storage, 1inch response validation, runner, and tx lifecycle.
    status: pending
  - id: backtest
    content: Add a minimal offline support_close_v1 replay over historical closed 1h/4h candles; print BUY/HOLD summary before observe/dry_run/live.
    status: pending
  - id: docs-rollout
    content: Update README/study to drop paper-score docs; document decisions schema, backtest gate, dev/prod keystores, Pi systemd LoadCredential units, and observe/dry-run/capped-live rollout.
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
- Execute a market swap shortly after the UTC hour through the official 1inch Classic Swap API v6.1 on Polygon (chain ID 137). Use its Pathfinder aggregation across supported liquidity sources instead of maintaining protocol-specific Uniswap routes.
- Pin the on-chain destination to the canonical 1inch Aggregation Router V6 (`0x111111125421ca6dc452d289314280a0f8842a65`). Treat Pathfinder v6.1 as the API/routing version, not a different router contract. Fail closed if the API host/version, chain, spender, router bytecode, or returned transaction target does not match configuration.
- Operate 24/7 on **Pi Ubuntu prod** with a **systemd** timer/service that invokes one idempotent `trade-once` cycle each hour. Do not auto-install units; document example unit files only.
- Use **separate keystores for dev and prod**. Never share one private key across machines. Dev uses a throwaway wallet for `observe` / `dry_run` and local tests; prod Pi holds the only live canary wallet. Config selects the keystore path per environment.
- Keep keystores gitignored and outside agent-exposed paths where practical. Never commit a private key, password, API key, raw signed transaction, or RPC URL containing the API key.
- **Prod Pi:** inject the keystore password and 1inch API key via systemd **`LoadCredential=`** (not a plain `EnvironmentFile` in the repo). **Dev/local:** secrets may come from env or prompt for manual runs only; do not copy prod keystore or prod credentials onto the dev machine.

## Single decision engine (`support_close_v1`)
- Output is gate-based, not scored: `decision` is `BUY` or `HOLD`, plus one clear `reason_code` such as `NO_ZONE_CLOSE`, `CLOSE_AT_OR_ABOVE_ZONE_MID`, `NO_HIGHER_ZONE`, `INVALID_INTERNAL_RANGE`, `HIGH_NOT_ABOVE_MIDPOINT`, `ALREADY_BOUGHT`, `STALE_PRICE`, or a risk code.
- Shared payload for every cycle: current closed 1h candle/time, selected lower-zone fingerprint and bounds (including zone mid), adjacent higher-zone fingerprint and bounds, internal-range midpoint, 24h lookback window, pre-entry closed-candle high, gate results, whether zones rebuilt this cycle, and strategy/config version.
- `observe` = same signal + persist decision (no DEX). This is the useful “paper” path for the live strategy.
- `dry_run` = same signal + quote/simulate + persist, no signing.
- `live` = same signal + risk + quote + sign/broadcast when all gates pass.

## Signal timing and algorithm
1. systemd runs one idempotent cycle around `HH:00:10 UTC`; retry Binance only for a short bounded window.
2. Fetch recent Binance `BTCUSDT` `1h` klines only and upsert them into `candles` with `timeframe="1h"`. Require a fully closed `1h` candle for the decision (reject open, missing, stale, duplicated, or future closes). Use its `close` as the hourly reference price; do not evaluate an open candle.
3. From closed 1h candles, detect whether a Binance-aligned 4H bucket just completed:
   - aggregate OHLCV for that bucket (`open`=first, `high`=max, `low`=min, `close`=last, `volume`=sum) and upsert the closed 4H bar into `candles` with `timeframe="4h"`;
   - compare that 4H `open_time` with the last zone-rebuild watermark in `bot_state` (the last 4H `open_time` already used to rebuild zones);
   - if newer, run `support_structure_v1` on closed 4h candles only, persist the new zone set, convert each detector `source_index` to source candle `open_time` for a stable zone fingerprint, and advance the watermark;
   - otherwise skip detector work and load the last persisted zones for evaluation.
4. Sort active support zones from low to high. For each candidate `lower_zone`, pair it only with the nearest distinct `higher_zone` above it and define:
   - `internal_range_low = lower_zone.high`;
   - `internal_range_high = higher_zone.low`;
   - `internal_range_midpoint = (internal_range_low + internal_range_high) / 2`.
   Reject the pair when the zones overlap/touch (`internal_range_high <= internal_range_low`). The highest zone has no higher neighbor and is not eligible.
5. Define the completed pre-entry leg without adding new state, capped to a **24h lookback** (at most the 24 closed 1h candles immediately before the trigger candle):
   - only scan that 24h window; ignore older candles even if `zone source time` is older;
   - find the most recent earlier candle in the window whose range intersects the candidate lower zone (the previous support touch/bounce); evaluate only candles strictly after that candle inside the window;
   - if no earlier touch exists in the window, evaluate closed candles from `max(zone source time, lookback window start)` through the candle before the trigger;
   - `pre_entry_closed_high` is the maximum `high` from those completed candles, excluding both the earlier touch candle and the current trigger candle. An empty leg is ineligible.
6. Select a zone only when:
   - the current closed 1h candle `close` is inside the support zone and **strictly below** the zone midpoint (`lower_zone.low <= close < lower_zone.mid`); closes at/above `zone.mid` up through `zone.high` are `HOLD`;
   - `pre_entry_closed_high` is **strictly greater than** `internal_range_midpoint` (exactly 50% is `HOLD`);
   - this zone or a materially overlapping version has not already produced a submitted or confirmed buy;
   - no transaction is unresolved and all daily/canary limits remain available.
   If more than one zone contains the close, choose the one with the highest `low`.
7. Persist every cycle outcome into `decisions` (including `HOLD` with reason codes).
8. Once `BUY` is selected, call the 1inch Classic Swap API v6.1 for a fresh `USDT0 -> WBTC` quote and `/swap` transaction for exactly 1 USDT0, with the bot wallet explicitly set as sender and receiver and partial fill disabled. Continue only when the quoted effective WBTC/USDT price differs from the closed Binance reference by no more than the configured quote-deviation limit. Require the response to be fresh, the spender/`tx.to` to equal the pinned Router V6 address, `tx.value` to be zero for this ERC-20 swap, and calldata to be non-empty; then run `eth_call` and `estimate_gas` before signing. Skip all 1inch calls in `observe`.

## Trading package and configuration
- Extend [src/config.py](src/config.py) and [config.example.yaml](config.example.yaml) with typed sections for `price_feed`, `wallet`, `strategy`, `execution`, `risk`, and `logging`. Remove obsolete `SignalConfig` score thresholds (`near_support_pct_*`, `dip_*`) once the paper scorer is gone. Put paths and non-secret limits in YAML (`wallet.keystore_path`, optional credential paths, pinned 1inch API base/version, and Router V6 address); accept the Polygon RPC URL, 1inch API key, and keystore password only from env, systemd credentials, or manual prompt—never from committed YAML.
- Add `src/trading/` with focused modules:
  - `models.py`: decision, quote, swap transaction, intent, and receipt models using `Decimal`/integer token units rather than binary floats.
  - `constants.py`: chain ID 137 and an immutable allowlist for WBTC, USDT0, and 1inch Aggregation Router V6; runtime checks must verify chain ID, bytecode, token decimals, and wallet address.
  - `binance_hourly.py`: thin helper to fetch/validate/upsert closed Binance `BTCUSDT` `1h` candles into the existing `candles` table (reuse [src/binance_client.py](src/binance_client.py) and [src/db.py](src/db.py) helpers). No live `4h` Binance fetch in this path.
  - `aggregate_4h.py`: derive closed Binance-aligned 4H bars from closed 1h candles; reject incomplete buckets.
  - `zone_refresh.py`: compare derived/latest closed 4h `open_time` against the rebuild watermark, rebuild zones only when newer, persist them, and expose the active zone set for the signal.
  - `signal.py` and `zone_identity.py`: the sole support-close/internal-range decision engine, stable source-time fingerprint, and overlapping-zone duplicate guard. Keep the backward scan and midpoint calculation here as small pure helpers; replace [src/signals.py](src/signals.py) and do not leave a parallel scorer.
  - `wallet.py`: encrypted-keystore creation/loading, password resolution (env → systemd credential file → optional prompt), address verification, permissions checks, and signing only after all other gates pass.
  - `oneinch.py`: a thin HTTP adapter for official Classic Swap API v6.1 quote, spender, and swap calls plus strict response/transaction validation. Do not add protocol-specific route selection or a 1inch SDK unless the thin adapter proves insufficient.
  - `transaction.py`: shared simulation, gas estimation, signing, broadcast, receipt decoding, and pending-transaction reconciliation.
  - `risk.py`: amount, daily/total caps, balance, allowance, gas, deviation, response-age, pause-file, and in-flight checks.
  - `store.py`: SQLite reads/writes for `decisions` / `trade_executions` and idempotent state transitions.
  - `runner.py`: orchestration only—fetch 1h, maybe derive/rebuild 4H zones, evaluate decision, then mode-gated risk/quote/simulate/sign/send/reconcile.
  - `backtest.py`: offline replay that walks historical closed 1h candles, rebuilds zones only on newly completed closed 4H bars, and calls the same `support_close_v1` engine. No 1inch, wallet, or gas simulation in this phase.
- Add only the required `web3`/`eth-account` support in [requirements.txt](requirements.txt); reuse the project's HTTP facilities for 1inch and the standard library rotating logger rather than adding SDK/logging dependencies.
- Keep [src/cli.py](src/cli.py) thin. Add commands that delegate to the trading package: `wallet-create`, `wallet-status`, `approve-trading`, `revoke-trading`, `trade-check`, `trade-once`, and `backtest`. **Remove** paper `run-once` (or repoint it to `trade-once --mode observe` if a thin alias is useful). Remove `assert_paper_mode_only` / paper-only guardrails that block live work once keystore flows exist; keep fail-closed live confirmation instead.

## Wallet, approval, and transaction safety
- **Dev vs prod keystores (required):**
  - **Dev/local:** `data/wallet/trader-dev.json` (or temp keystore in tests). Fund minimally or not at all. Default modes `observe` / `dry_run`. No prod keystore on dev machines.
  - **Prod Pi:** `data/wallet/trader-prod.json` (or `/var/lib/buy-the-dips-bot/wallet/trader-prod.json`). Only this wallet receives canary USDT0/POL and live approvals. Operator creates it manually on the Pi; agents must not run prod `wallet-create` with real secrets.
  - Config example documents both paths; each machine uses the path matching its role. `wallet-status` prints address only.
- `wallet-create` generates the account locally, encrypts it with `eth-account`, writes to the configured keystore path with mode `0600`, and prints only the public address. Update [.gitignore](.gitignore) to exclude keystores and runtime logs.
- **Prod secret delivery (Pi Ubuntu):** document a systemd `service` + `timer` using `LoadCredential=keystore_password:/etc/buy-the-dips-bot/keystore.password` and `LoadCredential=oneinch_api_key:/etc/buy-the-dips-bot/oneinch.api-key` (and optionally `LoadCredential=polygon_rpc_url:...`). Bot reads the matching files under `/run/credentials/<unit>/` at runtime. Source files under `/etc/buy-the-dips-bot/` are `chmod 600`, owned by root; service runs as dedicated `botuser`. Do not store prod secrets in the git repo or Cursor workspace.
- **Dev secret delivery:** local manual runs may use `KEYSTORE_PASSWORD` and `ONEINCH_API_KEY` in shell env for throwaway-wallet testing only. Never copy prod credential files to dev.
- Approval is never automatic. `approve-trading` first verifies Polygon chain ID 137, canonical Router V6 code, token symbol/decimals, balances, configured wallet, and that 1inch `/approve/spender` returns the same pinned router. It then grants the router a direct USDT0 allowance capped to the 10 USDT0 canary total; never grant an unlimited allowance. Handle USDT's zero-reset requirement when changing a non-zero allowance. `revoke-trading` resets that allowance to zero. Run prod approvals only on the Pi against `trader-prod`.
- Ask 1inch `/swap` to build the transaction for exactly `1_000_000` USDT0 units, the configured slippage, `allowPartialFill=false`, and the bot wallet as sender/receiver. Reject stale or mismatched responses, then run `eth_call` plus `estimate_gas` before signing. Do not locally encode Uniswap paths or 1inch router calldata.
- Persist the reserved nonce and locally derived transaction hash before broadcast. On timeout, reconcile that exact hash instead of creating another trade. Never auto-replace a pending transaction and never open a second trade while one is unresolved.
- Decode the WBTC `Transfer` event and receipt to store actual output, block, gas used, and final status. WBTC remains in the bot wallet; selling or stop-loss behavior is intentionally outside this phase.

## Persistence and logging
- Do **not** add an `hourly_price_observations` table. Persist 1h decision prices in the existing `candles` table with `timeframe="1h"`. Persist derived closed 4H bars in the same `candles` table with `timeframe="4h"`.
- **Drop the `signals` table** and `insert_signal` from [src/db.py](src/db.py). Paper rows are not migrated. Prefer recreating the local SQLite DB (or documenting a one-shot drop) rather than keeping a dual schema.
- Add a single `decisions` table for every hourly evaluation (`BUY`/`HOLD`) and a `trade_executions` table for on-chain work. Uniqueness around closed 1h decision time, strategy/zone fingerprint, and transaction hash so reruns cannot duplicate a buy.
- Decision columns (conceptual): candle open/close times and reference close, lower/higher zone fingerprints and bounds, internal-range midpoint, pre-entry closed high, `decision`, `reason_code`, gate JSON, zones-rebuilt flag, mode, strategy/config version, sanitized error. Execution columns: decision id, 1inch API/router version, sanitized route summary, quote/minimum output, nonce, transaction hash, receipt fields, actual WBTC output, status. Never persist API keys, raw calldata, or signed transactions.
- Watermark = the last closed 4H `open_time` for which zones were already rebuilt. Store it in `bot_state` so each hourly cycle can cheaply answer “did a newer closed 4H appear since last rebuild?” without re-running the detector every hour.
- Add structured JSON logging to stdout and a rotating ignored file. Include a per-cycle correlation ID and every no-trade/trade transition. Add redaction tests to ensure API keys, passwords, decrypted keys, RPC URLs, and signed transaction bytes never appear.

## Default canary controls
- Modes: `observe` records 1h candles and decisions only; `dry_run` also quotes and simulates; `live` may sign and broadcast. Default to `observe`, and require both live config and a separate wallet-specific confirmation value to enter `live`.
- Initial hard limits: exactly 1 USDT0 per trade, one submitted trade per zone, at most 3 trades per UTC day, and 10 USDT0 cumulative live spend. Increasing the cumulative cap requires an explicit config change after review.
- Default execution checks: 1h close inside support and strictly below zone mid, 24h pre-entry lookback, strict internal-range `> 50%` pre-entry-high gate, 0.50% maximum slippage, 0.50% maximum Binance-to-DEX quote deviation, configurable maximum gas, minimum POL reserve, a short maximum quote/swap-response age, and a `data/PAUSE_TRADING` kill switch.
- The dedicated wallet should hold only the small approved USDT0 canary amount plus enough POL for gas.

## Offline backtest (required before live)
- Goal: prove `support_close_v1` fires sensibly on history before spending canary capital. This is signal-only replay, not a full PnL/portfolio simulator.
- Input: already stored closed Binance `BTCUSDT` `1h` candles (and derived/closed `4h` bars). Reuse existing backfill + 1h→4h aggregation; do not invent a second candle store.
- Method: walk each closed 1h candle in time order; rebuild zones only when a newer closed 4H bar appears; call the same decision engine used by `observe`/`dry_run`/`live`; apply the one-buy-per-zone guard; assume fill at the trigger candle `close` for summary only.
- Output: compact CLI summary — candle count, zone rebuild count, `BUY` count, `HOLD` reason-code tallies, and a short list of BUY timestamps/prices/zone bounds. Optional CSV export is fine later; do not build charts or a research framework in this phase.
- Scope limits: no 1inch quotes, no slippage model, no gas, no sell/exit logic. Backtest validates entry timing and gate behavior only.
- Gate: operator reviews at least one multi-month backtest window and confirms BUY density/reason codes look acceptable before enabling `observe` on the Pi. Unit tests cover a tiny synthetic replay; the historical run is an operator CLI step, not CI.

## Verification and rollout
- Replace [tests/test_signals.py](tests/test_signals.py) and any paper-score assertions with support-close/internal-range coverage: close at zone low; close just below zone mid (`BUY` eligible); close at/above zone mid (`HOLD`); close at zone high (`HOLD`); 24h lookback cap ignores older highs/touches; no higher zone; overlapping pair; first approach; prior bounce inside window; high below/equal/above internal-range midpoint; trigger-candle high excluded; already bought; and stale price. Also cover: closed 1h upsert/read; 1h→4h aggregation and incomplete-bucket rejection; rebuild-zones-only-on-new-4h watermark; zone identity; token units; minimum-output rounding; config/risk gates; SQLite decision idempotency; encrypted-keystore handling in a temporary directory; redaction; runner retries; pending/confirmed/reverted reconciliation; a small synthetic backtest replay.
- Mock all network/signing boundaries in normal tests. Add 1inch contract tests for wrong spender/router, nonzero value, empty calldata, stale response, API failure, and quote deviation. Add an opt-in Polygon read-only integration check that verifies canonical Router V6 bytecode, token decimals, `/approve/spender`, and a live 1inch v6.1 quote without signing.
- Update [README.md](README.md) and [study.md](study.md): remove Phase 1 paper-score / `signals` table docs; describe the support-close/internal-range algorithm, offline backtest gate, `decisions`/`trade_executions` schema, dual-timeframe flow (fetch 1h only; derive closed 4H; rebuild on watermark), 1inch Classic Swap API v6.1 and Router V6, modes, dev/prod keystore separation, Pi systemd `LoadCredential` setup for both keystore password and 1inch API key, direct capped router approval/revocation, audit queries, pause/recovery, and buy-only scope. Note that recreating the local DB drops old paper signal rows.
- Roll out in gates: run the full test suite; run offline `backtest` on a multi-month window and review BUY/HOLD summary; run `trade-check`; collect at least 24 closed 1h decisions in `observe`; run `dry_run` until trigger/skip logs and idempotency are verified; fund only the capped wallet; explicitly approve the capped allowance; enable `live`; stop automatically at 10 USDT0 cumulative spend and review every receipt before raising any limit.
- Keep `trade-once` as the only scheduler target. Document example Pi Ubuntu systemd `service` + `timer` units with `LoadCredential=` for the keystore password and 1inch API key that run at minute 2 of each UTC hour, 24/7. Do not install or enable units automatically. Prod rollout (wallet-create, fund, approve, enable live) is operator-only on the Pi, not via Cursor agents.
