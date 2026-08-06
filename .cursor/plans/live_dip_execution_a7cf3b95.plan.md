---
name: live dip execution
overview: Add a fail-closed Polygon trading path that keeps the existing Binance 4H zone detector unchanged, fetches only Binance BTCUSDT 1h candles into the existing candles table, derives closed 4H bars from those 1h candles to rebuild zones, and buys exactly 1 USDT of WBTC through an explicitly validated Uniswap Universal Router V3 route. Retire the Phase 1 paper scorer and signals table in favor of one lower_bound_v1 decision engine and one decisions schema shared by observe, dry_run, and live. Use separate dev/prod keystores; Pi prod runs 24/7 via systemd with LoadCredential for the keystore password.
todos:
  - id: data-signal
    content: Retire paper scorer/signals table; add lower_bound_v1 engine + decisions schema; fetch-only-1h; derive closed 4H; rebuild zones on watermark; fail-closed timestamps.
    status: pending
  - id: wallet-safety
    content: Add dev/prod keystore separation, LoadCredential password loading on Pi, encrypted-keystore CLI flows, contract checks, capped Permit2 approvals, revocation, and live-mode guards.
    status: pending
  - id: quote-execute
    content: Implement allowlisted V3 QuoterV2 route comparison and validated Universal Router exact-input execution with simulation and receipt reconciliation.
    status: pending
  - id: audit-risk
    content: Wire decisions/executions persistence, structured redacted logs, duplicate prevention, pause switch, and canary risk limits.
    status: pending
  - id: tests
    content: Replace paper-signal tests with lower_bound gates; cover 1h fetch, 1h-to-4h, zone watermark, risk, storage, runner, and tx lifecycle.
    status: pending
  - id: docs-rollout
    content: Update README/study to drop paper-score docs; document decisions schema, dev/prod keystores, Pi systemd LoadCredential units, and observe/dry-run/capped-live rollout.
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
- Replace them with **one** deterministic `lower_bound_v1` decision engine and **one** `decisions` schema. `observe`, `dry_run`, and `live` all call the same engine; modes only change what happens after a `BUY` decision (record only / quote+simulate / sign+broadcast).
- Execute a market swap shortly after the UTC hour. Universal Router cannot guarantee a fill exactly at the zone low, so a fresh on-chain quote must still be inside the entry band before signing.
- Operate 24/7 on **Pi Ubuntu prod** with a **systemd** timer/service that invokes one idempotent `trade-once` cycle each hour. Do not auto-install units; document example unit files only.
- Use **separate keystores for dev and prod**. Never share one private key across machines. Dev uses a throwaway wallet for `observe` / `dry_run` and local tests; prod Pi holds the only live canary wallet. Config selects the keystore path per environment.
- Keep keystores gitignored and outside agent-exposed paths where practical. Never commit a private key, password, API key, raw signed transaction, or RPC URL containing the API key.
- **Prod Pi:** inject the keystore password via systemd **`LoadCredential=`** (not a plain `EnvironmentFile` in the repo). **Dev/local:** password may come from env or prompt for manual runs only; do not copy prod keystore or prod credentials onto the dev machine.

## Single decision engine (`lower_bound_v1`)
- Output is gate-based, not scored: `decision` is `BUY` or `HOLD`, plus a `reason_code` such as `NO_BASELINE`, `NO_ZONE_CROSS`, `BELOW_ZONE`, `ALREADY_BOUGHT`, `STALE_PRICE`, or risk codes.
- Shared payload for every cycle: previous/current 1h closes and candle times, selected zone fingerprint and bounds, `entry_ceiling`, gate results, whether zones rebuilt this cycle, strategy/config version.
- `observe` = same signal + persist decision (no DEX). This is the useful “paper” path for the live strategy.
- `dry_run` = same signal + quote/simulate + persist, no signing.
- `live` = same signal + risk + quote + sign/broadcast when all gates pass.

## Signal timing and algorithm
1. systemd runs one idempotent cycle around `HH:02 UTC`; retry Binance only for a short bounded window.
2. Fetch recent Binance `BTCUSDT` `1h` klines only and upsert them into `candles` with `timeframe="1h"`. Require a fully closed `1h` candle for the decision (reject open, missing, stale, duplicated, or future closes). Use the two latest closed 1h closes as previous/current `hourly_reference_close`.
3. From closed 1h candles, detect whether a Binance-aligned 4H bucket just completed:
   - aggregate OHLCV for that bucket (`open`=first, `high`=max, `low`=min, `close`=last, `volume`=sum) and upsert the closed 4H bar into `candles` with `timeframe="4h"`;
   - compare that 4H `open_time` with the last zone-rebuild watermark in `bot_state` (the last 4H `open_time` already used to rebuild zones);
   - if newer, run `support_structure_v1` on closed 4h candles only, persist the new zone set, convert each detector `source_index` to source candle `open_time` for a stable zone fingerprint, and advance the watermark;
   - otherwise skip detector work and load the last persisted zones for evaluation.
4. For every support zone, calculate `entry_ceiling = min(zone.high, zone.low × 1.0025)`. The default trigger band is therefore the zone low through at most 0.25% above it.
5. Select the nearest eligible zone, preferring the highest `low`, only when:
   - the previous hourly reference close was above `entry_ceiling`;
   - the current hourly reference close is between `zone.low` and `entry_ceiling`, inclusive;
   - the price did not gap below `zone.low`;
   - this zone or a materially overlapping version has not already produced a submitted or confirmed buy;
   - no transaction is unresolved and all daily/canary limits remain available.
6. Persist every cycle outcome into `decisions` (including `HOLD` with reason codes). The first stored closed 1h close establishes a baseline and cannot trade.
7. Once `BUY` is selected, request fresh QuoterV2 exact-input quotes for the allowlisted direct V3 paths `USDT0 -> WBTC` at fee tiers 500 and 3000. Pick the highest WBTC output, but continue only when its effective WBTC/USDT price remains inside the trigger band and differs from the Binance reference by no more than the configured quote-deviation limit. Skip quote/sign steps entirely in `observe`.

## Trading package and configuration
- Extend [src/config.py](src/config.py) and [config.example.yaml](config.example.yaml) with typed sections for `price_feed`, `wallet`, `strategy`, `execution`, `risk`, and `logging`. Remove obsolete `SignalConfig` score thresholds (`near_support_pct_*`, `dip_*`) once the paper scorer is gone. Put paths and non-secret limits in YAML (`wallet.keystore_path`, optional `wallet.password_credential_path`); accept the Polygon RPC URL/API key and keystore password only from env, systemd credentials, or manual prompt—never from committed YAML.
- Add `src/trading/` with focused modules:
  - `models.py`: decision, quote, route, intent, and receipt models using `Decimal`/integer token units rather than binary floats.
  - `constants.py`: chain ID 137 and an immutable allowlist for WBTC, USDT0, Permit2, QuoterV2, Uniswap V3 Factory, and Universal Router V2; runtime checks must verify chain ID, bytecode, token decimals, and wallet address.
  - `binance_hourly.py`: thin helper to fetch/validate/upsert closed Binance `BTCUSDT` `1h` candles into the existing `candles` table (reuse [src/binance_client.py](src/binance_client.py) and [src/db.py](src/db.py) helpers). No live `4h` Binance fetch in this path.
  - `aggregate_4h.py`: derive closed Binance-aligned 4H bars from closed 1h candles; reject incomplete buckets.
  - `zone_refresh.py`: compare derived/latest closed 4h `open_time` against the rebuild watermark, rebuild zones only when newer, persist them, and expose the active zone set for the signal.
  - `signal.py` and `zone_identity.py`: the sole lower-bound decision engine, stable source-time fingerprint, and overlapping-zone duplicate guard. Replace [src/signals.py](src/signals.py); do not leave a parallel scorer.
  - `wallet.py`: encrypted-keystore creation/loading, password resolution (env → systemd credential file → optional prompt), address verification, permissions checks, and signing only after all other gates pass.
  - `uniswap_v3.py`: factory/pool validation, QuoterV2 calls, V3 path encoding, slippage calculation, Universal Router command encoding, simulation, gas estimation, signing, broadcast, receipt decoding, and pending-transaction reconciliation.
  - `risk.py`: amount, daily/total caps, balance, allowance, gas, deviation, deadline, pause-file, and in-flight checks.
  - `store.py`: SQLite reads/writes for `decisions` / `trade_executions` and idempotent state transitions.
  - `runner.py`: orchestration only—fetch 1h, maybe derive/rebuild 4H zones, evaluate decision, then mode-gated risk/quote/simulate/sign/send/reconcile.
- Add `web3`/`eth-account` support in [requirements.txt](requirements.txt); use the standard library rotating logger rather than adding a logging dependency.
- Keep [src/cli.py](src/cli.py) thin. Add commands that delegate to the trading package: `wallet-create`, `wallet-status`, `approve-trading`, `revoke-trading`, `trade-check`, and `trade-once`. **Remove** paper `run-once` (or repoint it to `trade-once --mode observe` if a thin alias is useful). Remove `assert_paper_mode_only` / paper-only guardrails that block live work once keystore flows exist; keep fail-closed live confirmation instead.

## Wallet, approval, and transaction safety
- **Dev vs prod keystores (required):**
  - **Dev/local:** `data/wallet/trader-dev.json` (or temp keystore in tests). Fund minimally or not at all. Default modes `observe` / `dry_run`. No prod keystore on dev machines.
  - **Prod Pi:** `data/wallet/trader-prod.json` (or `/var/lib/buy-the-dips-bot/wallet/trader-prod.json`). Only this wallet receives canary USDT0/POL and live approvals. Operator creates it manually on the Pi; agents must not run prod `wallet-create` with real secrets.
  - Config example documents both paths; each machine uses the path matching its role. `wallet-status` prints address only.
- `wallet-create` generates the account locally, encrypts it with `eth-account`, writes to the configured keystore path with mode `0600`, and prints only the public address. Update [.gitignore](.gitignore) to exclude keystores and runtime logs.
- **Prod secret delivery (Pi Ubuntu):** document a systemd `service` + `timer` using `LoadCredential=keystore_password:/etc/buy-the-dips-bot/keystore.password` (and optionally `LoadCredential=polygon_rpc_url:...`). Bot reads `/run/credentials/<unit>/keystore_password` at runtime. Source files under `/etc/buy-the-dips-bot/` are `chmod 600`, owned by root; service runs as dedicated `botuser`. Do not store prod passwords in the git repo or Cursor workspace.
- **Dev secret delivery:** local manual runs may use `KEYSTORE_PASSWORD` in shell env for throwaway wallets only. Never copy prod credential files to dev.
- Approval is never automatic. `approve-trading` first verifies Polygon chain ID 137, canonical contract code, token symbol/decimals, balances, and the configured wallet. It then grants USDT0 allowance to Permit2 and Permit2 allowance to the router, capped to the canary total and with a short expiry; handle USDT's zero-reset requirement when changing a non-zero allowance. `revoke-trading` resets both allowances. Run prod approvals only on the Pi against `trader-prod`.
- Build `V3_SWAP_EXACT_IN` (`0x00`) for exactly `1_000_000` USDT0 units with the wallet as recipient and `payerIsUser=true`. Calculate `amountOutMin` from the selected quote and configured slippage, use a short transaction deadline, and run `eth_call` plus `estimate_gas` before signing.
- Persist the reserved nonce and locally derived transaction hash before broadcast. On timeout, reconcile that exact hash instead of creating another trade. Never auto-replace a pending transaction and never open a second trade while one is unresolved.
- Decode the WBTC `Transfer` event and receipt to store actual output, block, gas used, and final status. WBTC remains in the bot wallet; selling or stop-loss behavior is intentionally outside this phase.

## Persistence and logging
- Do **not** add an `hourly_price_observations` table. Persist 1h decision prices in the existing `candles` table with `timeframe="1h"`. Persist derived closed 4H bars in the same `candles` table with `timeframe="4h"`.
- **Drop the `signals` table** and `insert_signal` from [src/db.py](src/db.py). Paper rows are not migrated. Prefer recreating the local SQLite DB (or documenting a one-shot drop) rather than keeping a dual schema.
- Add a single `decisions` table for every hourly evaluation (`BUY`/`HOLD`) and a `trade_executions` table for on-chain work. Uniqueness around closed 1h decision time, strategy/zone fingerprint, and transaction hash so reruns cannot duplicate a buy.
- Decision columns (conceptual): candle open/close times, previous/current reference closes, zone fingerprint and bounds, `entry_ceiling`, `decision`, `reason_code`, gate JSON, zones-rebuilt flag, mode, strategy/config version, sanitized error. Execution columns: decision id, route, quote/minimum output, nonce, transaction hash, receipt fields, actual WBTC output, status.
- Watermark = the last closed 4H `open_time` for which zones were already rebuilt. Store it in `bot_state` so each hourly cycle can cheaply answer “did a newer closed 4H appear since last rebuild?” without re-running the detector every hour.
- Add structured JSON logging to stdout and a rotating ignored file. Include a per-cycle correlation ID and every no-trade/trade transition. Add redaction tests to ensure API keys, passwords, decrypted keys, RPC URLs, and signed transaction bytes never appear.

## Default canary controls
- Modes: `observe` records 1h candles and decisions only; `dry_run` also quotes and simulates; `live` may sign and broadcast. Default to `observe`, and require both live config and a separate wallet-specific confirmation value to enter `live`.
- Initial hard limits: exactly 1 USDT0 per trade, one submitted trade per zone, at most 3 trades per UTC day, and 10 USDT0 cumulative live spend. Increasing the cumulative cap requires an explicit config change after review.
- Default execution checks: 0.25% entry band, 0.50% maximum slippage, 0.50% maximum Binance-to-DEX quote deviation, configurable maximum gas, minimum POL reserve, 2-minute transaction deadline, and a `data/PAUSE_TRADING` kill switch.
- The dedicated wallet should hold only the small approved USDT0 canary amount plus enough POL for gas.

## Verification and rollout
- Replace [tests/test_signals.py](tests/test_signals.py) and any paper-score assertions with lower-bound gate coverage (baseline, cross into band, below zone, already bought, stale price). Also cover: closed 1h upsert/read; 1h→4h aggregation and incomplete-bucket rejection; rebuild-zones-only-on-new-4h watermark; zone identity; token units; fee-path encoding; minimum-output rounding; config/risk gates; SQLite decision idempotency; encrypted-keystore handling in a temporary directory; redaction; runner retries; pending/confirmed/reverted reconciliation.
- Mock all network/signing boundaries in normal tests. Add an opt-in Polygon read-only integration check that verifies canonical bytecode, decimals, pools, and live QuoterV2 output without signing.
- Update [README.md](README.md) and [study.md](study.md): remove Phase 1 paper-score / `signals` table docs; describe the single decision engine, `decisions`/`trade_executions` schema, dual-timeframe flow (fetch 1h only; derive closed 4H; rebuild on watermark), modes, dev/prod keystore separation, Pi systemd `LoadCredential` setup, wallet/approval/revocation, audit queries, pause/recovery, and buy-only scope. Note that recreating the local DB drops old paper signal rows.
- Roll out in gates: run the full test suite; run `trade-check`; collect at least 24 closed 1h decisions in `observe`; run `dry_run` until trigger/skip logs and idempotency are verified; fund only the capped wallet; explicitly approve the capped allowance; enable `live`; stop automatically at 10 USDT0 cumulative spend and review every receipt before raising any limit.
- Keep `trade-once` as the only scheduler target. Document example Pi Ubuntu systemd `service` + `timer` units with `LoadCredential=` that run at minute 2 of each UTC hour, 24/7. Do not install or enable units automatically. Prod rollout (wallet-create, fund, approve, enable live) is operator-only on the Pi, not via Cursor agents.
