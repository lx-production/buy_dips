---
name: live dip execution
overview: Add a fail-closed Polygon trading path that keeps the existing Binance 4H zone detector unchanged, treats Alchemy's address-based hourly WBTC/USDT price point as the decision close, and buys exactly 1 USDT of WBTC through an explicitly validated Uniswap Universal Router V3 route. Roll out through observe, dry-run, and capped live modes with an encrypted gitignored wallet, durable audit records, structured logs, and strict duplicate/risk controls.
todos:
  - id: data-signal
    content: Add typed trading config, Alchemy hourly reference storage, stable zone identity, and the lower-bound_v1 signal with fail-closed timestamp handling.
    status: pending
  - id: wallet-safety
    content: Add encrypted-keystore CLI flows, canonical Polygon contract checks, capped Permit2 approvals, revocation, and live-mode guards.
    status: pending
  - id: quote-execute
    content: Implement allowlisted V3 QuoterV2 route comparison and validated Universal Router exact-input execution with simulation and receipt reconciliation.
    status: pending
  - id: audit-risk
    content: Add idempotent trading tables, structured redacted logs, duplicate prevention, pause switch, and canary risk limits.
    status: pending
  - id: tests
    content: Add unit/mocked integration coverage for signal, pricing, wallet, encoding, risk, storage, runner, and transaction lifecycle.
    status: pending
  - id: docs-rollout
    content: Update README, study notes, example config, ignore rules, and staged observe/dry-run/capped-live operating instructions.
    status: pending
isProject: false
---

# Phase 2 Live Dip Execution

## Locked decisions and important constraints
- Keep the existing `support_structure_v1` detector and Binance `BTCUSDT` 4H history in [src/zones/](src/zones/) unchanged. Recompute zones only from 4H candles whose `close_time` is no later than the hourly decision point.
- Add a separate `lower_bound_v1` execution signal instead of using the existing scorer in [src/signals.py](src/signals.py), because that scorer measures distance to `zone.high` and was designed for paper alerts, not lower-bound entries.
- Fetch address-based `1h` historical points for Polygon WBTC and USDT0 from Alchemy, then calculate `WBTC/USDT = WBTC_USD / USDT_USD`. Persist and describe this as an `hourly_reference_close`: Alchemy supplies one volume-weighted price point, not OHLCV.
- Execute a market swap shortly after the UTC hour. Universal Router cannot guarantee a fill exactly at the zone low, so a fresh on-chain quote must still be inside the entry band before signing.
- Keep the encrypted keystore physically under the workspace, but gitignore it. Never commit a private key, password, API key, raw signed transaction, or RPC URL containing the API key.

## Signal timing and algorithm
1. Run one idempotent cycle around `HH:02 UTC`; retry Alchemy only for a short bounded window. Fetch WBTC and USDT0 points with matching timestamps and reject missing, stale, duplicated, future, or mismatched points.
2. During first integration with the real Alchemy key, inspect at least 48 hours of returned timestamps to establish whether Alchemy labels a point at the beginning or end of its interval. Encode that mapping in the client and fail closed if cadence changes; do not infer candle semantics silently.
3. Refresh closed Binance 4H candles, run the current detector, and convert each detector `source_index` to the source candle `open_time` for a stable zone fingerprint.
4. For every support zone, calculate `entry_ceiling = min(zone.high, zone.low × 1.0025)`. The default trigger band is therefore the zone low through at most 0.25% above it.
5. Select the nearest eligible zone, preferring the highest `low`, only when:
   - the previous hourly reference close was above `entry_ceiling`;
   - the current hourly reference close is between `zone.low` and `entry_ceiling`, inclusive;
   - the price did not gap below `zone.low`;
   - this zone or a materially overlapping version has not already produced a submitted or confirmed buy;
   - no transaction is unresolved and all daily/canary limits remain available.
6. Log all non-buy outcomes, including `NO_BASELINE`, `NO_ZONE_CROSS`, `BELOW_ZONE`, `ALREADY_BOUGHT`, `STALE_PRICE`, and each risk rejection. The first stored hourly point establishes a baseline and cannot trade.
7. Once triggered, request fresh QuoterV2 exact-input quotes for the allowlisted direct V3 paths `USDT0 -> WBTC` at fee tiers 500 and 3000. Pick the highest WBTC output, but continue only when its effective WBTC/USDT price remains inside the trigger band and differs from the Alchemy reference by no more than the configured quote-deviation limit.

## Trading package and configuration
- Extend [src/config.py](src/config.py) and [config.example.yaml](config.example.yaml) with typed sections for `price_feed`, `wallet`, `strategy`, `execution`, `risk`, and `logging`. Put paths and non-secret limits in YAML; accept the Alchemy API key and keystore password only from the process environment or an OS secret provider.
- Add `src/trading/` with focused modules:
  - `models.py`: decision, quote, route, intent, and receipt models using `Decimal`/integer token units rather than binary floats.
  - `constants.py`: chain ID 137 and an immutable allowlist for WBTC, USDT0, Permit2, QuoterV2, Uniswap V3 Factory, and Universal Router V2; runtime checks must verify chain ID, bytecode, token decimals, and wallet address.
  - `alchemy_prices.py`: bounded HTTP retries, response/timestamp validation, WBTC/USDT ratio calculation, and API-key-safe errors.
  - `signal.py` and `zone_identity.py`: the lower-bound state transition, stable source-time fingerprint, and overlapping-zone duplicate guard.
  - `wallet.py`: encrypted-keystore creation/loading, address verification, permissions checks, and signing only after all other gates pass.
  - `uniswap_v3.py`: factory/pool validation, QuoterV2 calls, V3 path encoding, slippage calculation, Universal Router command encoding, simulation, gas estimation, signing, broadcast, receipt decoding, and pending-transaction reconciliation.
  - `risk.py`: amount, daily/total caps, balance, allowance, gas, deviation, deadline, pause-file, and in-flight checks.
  - `store.py`: trading-specific SQLite reads/writes and idempotent state transitions.
  - `runner.py`: orchestration only—refresh data, evaluate, risk-check, quote, simulate, persist, sign, send, and reconcile.
- Add `web3`/`eth-account` support in [requirements.txt](requirements.txt); use the standard library rotating logger rather than adding a logging dependency.
- Keep [src/cli.py](src/cli.py) thin. Add commands that delegate to the trading package: `wallet-create`, `wallet-status`, `approve-trading`, `revoke-trading`, `trade-check`, and `trade-once`. Preserve the current paper `run-once` behavior.

## Wallet, approval, and transaction safety
- `wallet-create` generates the account locally, encrypts it with `eth-account`, writes it to a path such as `data/wallet/trader.json` with mode `0600`, and prints only the public address. Update [.gitignore](.gitignore) to exclude keystores and runtime logs.
- Approval is never automatic. `approve-trading` first verifies Polygon chain ID 137, canonical contract code, token symbol/decimals, balances, and the configured wallet. It then grants USDT0 allowance to Permit2 and Permit2 allowance to the router, capped to the canary total and with a short expiry; handle USDT's zero-reset requirement when changing a non-zero allowance. `revoke-trading` resets both allowances.
- Build `V3_SWAP_EXACT_IN` (`0x00`) for exactly `1_000_000` USDT0 units with the wallet as recipient and `payerIsUser=true`. Calculate `amountOutMin` from the selected quote and configured slippage, use a short transaction deadline, and run `eth_call` plus `estimate_gas` before signing.
- Persist the reserved nonce and locally derived transaction hash before broadcast. On timeout, reconcile that exact hash instead of creating another trade. Never auto-replace a pending transaction and never open a second trade while one is unresolved.
- Decode the WBTC `Transfer` event and receipt to store actual output, block, gas used, and final status. WBTC remains in the bot wallet; selling or stop-loss behavior is intentionally outside this phase.

## Persistence and logging
- Extend [src/db.py](src/db.py) with additive tables for hourly price observations, lower-bound decisions, and trade executions. Add uniqueness around the hourly point, strategy/zone decision, and transaction hash so reruns cannot duplicate a buy.
- Store the zone fingerprint and bounds, previous/current reference closes, all decision gates, config/strategy version, route, quote/minimum output, nonce, transaction hash, receipt, actual WBTC output, and sanitized error details.
- Add structured JSON logging to stdout and a rotating ignored file. Include a per-cycle correlation ID and every no-trade/trade transition. Add redaction tests to ensure API keys, passwords, decrypted keys, RPC URLs, and signed transaction bytes never appear.

## Default canary controls
- Modes: `observe` records price and signal only; `dry_run` also quotes and simulates; `live` may sign and broadcast. Default to `observe`, and require both live config and a separate wallet-specific confirmation value to enter `live`.
- Initial hard limits: exactly 1 USDT0 per trade, one submitted trade per zone, at most 3 trades per UTC day, and 10 USDT0 cumulative live spend. Increasing the cumulative cap requires an explicit config change after review.
- Default execution checks: 0.25% entry band, 0.50% maximum slippage, 0.50% maximum Alchemy-to-DEX quote deviation, configurable maximum gas, minimum POL reserve, 2-minute transaction deadline, and a `data/PAUSE_TRADING` kill switch.
- The dedicated wallet should hold only the small approved USDT0 canary amount plus enough POL for gas.

## Verification and rollout
- Add focused tests beside the existing suite: Alchemy parsing/timestamp alignment; all lower-bound boundary and gap cases; stable and overlapping zone identity; raw token unit conversion; fee-path encoding; minimum-output rounding; config/risk gates; SQLite idempotency; encrypted-keystore handling in a temporary directory; redaction; runner retries; and pending/confirmed/reverted receipt reconciliation.
- Mock all network/signing boundaries in normal tests. Add an opt-in Polygon read-only integration check that verifies canonical bytecode, decimals, pools, and live QuoterV2 output without signing.
- Update [README.md](README.md) and [study.md](study.md) with the dual-timeframe data flow, Alchemy point caveat, exact trigger formula, modes, wallet/approval/revocation steps, contract-address verification, scheduler instructions, audit queries, pause/recovery procedures, and the fact that this phase only buys.
- Roll out in gates: run the full test suite; run `trade-check`; collect at least 24 hourly observations in `observe`; run `dry_run` until trigger/skip logs and idempotency are verified; fund only the capped wallet; explicitly approve the capped allowance; enable `live`; stop automatically at 10 USDT0 cumulative spend and review every receipt before raising any limit.
- Keep `trade-once` as the only scheduler target. Document a macOS `launchd` example that invokes it at minute 2 of each UTC hour, but do not install or start background jobs automatically.