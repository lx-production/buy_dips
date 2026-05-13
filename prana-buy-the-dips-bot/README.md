# PRANA Buy the Dips Bot

Phase 1 is a local, Python-based foundation for a paper-only Buy the Dips system.
It collects Binance Spot `BTCUSDT` 4H candles, stores raw candle data in SQLite,
detects support and resistance zones from closed candle closes, generates paper
signals, and logs every decision including `HOLD`.

## What Phase 1 Does

- Fetches public Binance Spot `BTCUSDT` 4H klines.
- Stores raw candle data permanently in local SQLite.
- Uses only closed 4H candles for signal generation.
- Detects support and resistance zones using candle close prices only.
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
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

## Initialize The Database

```bash
python -m src.cli init-db
```

Default database path:

```text
data/prana_buy_the_dips.sqlite
```

## Backfill 12 Months Of BTCUSDT 4H Candles

```bash
python -m src.cli backfill
```

The backfill paginates Binance Spot public klines with `limit=1000`, inserts or
updates local rows, and prints the number of candles processed, first candle,
last candle, and database path.

## Print Zones

```bash
python -m src.cli zones
```

This loads closed candles from SQLite, detects pure-close zones, stores the zone
snapshot, and prints support, active, and resistance zones.

## Run One Paper Signal Cycle

```bash
python -m src.cli run-once
```

This fetches the latest candles, stores them, excludes any currently open 4H
candle from signal calculations, detects zones, generates one paper signal, and
stores it in the `signals` table. A `HOLD` decision is stored just like any other
decision.

## Pure Close Support / Resistance Logic

The initial detector intentionally uses close prices only. Wick highs, wick lows,
candle body percentiles, volume filters, RSI, and ML are not part of Phase 1.

Support pivots are local close minima validated by a future close reversal.
Resistance pivots are local close maxima validated by a future close reversal.
Candidate pivot closes are clustered by price using the cluster median, with a
maximum zone width guard to prevent oversized chain merges.

Each zone stores:

- `low`: lowest actual pivot close in the cluster
- `high`: highest actual pivot close in the cluster
- `mid`: average of `low` and `high`
- `touches`: number of pivot closes in the cluster
- `origin`: `support_pivot_close` or `resistance_pivot_close`
- `role`: `support`, `resistance`, or `active` based on current price

## Safety Warning

Phase 1 is paper signal mode only. It has no private key support, no wallet
logic, no smart contract calls, and no real trade execution path.

## Tests

```bash
pytest
```
