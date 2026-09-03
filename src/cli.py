from __future__ import annotations

import os
import sys
import argparse

from pathlib import Path

from .db import init_db, load_candles_df
from .config import AppConfig, load_config
from .candles import backfill_12_months
from .utils import ms_to_iso, resolve_path
from .trading.runner import run_trade_once
from .trading.constants import USDT_DECIMALS
from .zones import detect_support_resistance_zones
from .trading.approval import approve_trading, revoke_trading
from .trading.backtest import parse_backtest_bound, run_backtest, write_buy_csv
from .trading.contract_checks import format_token_amount, run_contract_checks
from .trading.wallet import create_encrypted_keystore, load_local_account, resolve_keystore_password


def main(argv: list[str] | None = None) -> int:
    # Parse one thin CLI command and delegate safety-sensitive work to the trading package.
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    database_path = resolve_path(config.database_path)

    if args.command == "init-db":
        init_db(database_path)
        print(f"Initialized database: {database_path}")
        return 0
    if args.command == "backfill":
        return _cmd_backfill(config, database_path, args.timeframe or config.timeframe)
    if args.command == "zones":
        return _cmd_zones(config, database_path)
    if args.command == "trade-once":
        return _cmd_trade_once(config, database_path, args.mode)
    if args.command == "backtest":
        return _cmd_backtest(config, database_path, args.start, args.end, args.csv)
    if args.command == "wallet-create":
        return _cmd_wallet_create(config)
    if args.command == "wallet-status":
        return _cmd_wallet_status(config)
    if args.command == "trade-check":
        return _cmd_trade_check(config)
    if args.command == "approve-trading":
        return _cmd_approve_trading(config)
    if args.command == "revoke-trading":
        return _cmd_revoke_trading(config)
    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    # Expose one decision runner whose mode controls only downstream swap behavior.
    parser = argparse.ArgumentParser(description="PRANA Buy the Dips bot")
    parser.add_argument("--config", default=None, help="Path to config YAML. Defaults to CONFIG_PATH or config.yaml.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("init-db", help="Create SQLite tables.")
    backfill = subparsers.add_parser("backfill", help="Backfill approximately 12 months of BTCUSDT candles.")
    backfill.add_argument("--timeframe", choices=("1h", "4h"), default=None)
    subparsers.add_parser("zones", help="Print support zones detected from closed 4h candles.")
    trade_once = subparsers.add_parser("trade-once", help="Run one fetch/zone/decision cycle.")
    trade_once.add_argument("--mode", choices=("observe", "dry_run", "live"), default="observe")
    backtest = subparsers.add_parser(
        "backtest",
        help="Offline support_close_v2 replay; writes BUY CSV only (no live table writes).",
    )
    backtest.add_argument("--start", required=True, help="Inclusive ISO-8601 start on a UTC hour boundary.")
    backtest.add_argument("--end", default=None, help="Exclusive ISO-8601 end on a UTC hour boundary.")
    backtest.add_argument("--csv", required=True, help="Output path for the BUY CSV export.")
    subparsers.add_parser("wallet-create", help="Create the configured encrypted development keystore.")
    subparsers.add_parser("wallet-status", help="Decrypt and print only the configured wallet address.")
    subparsers.add_parser("trade-check", help="Validate Polygon, contracts, wallet, and allowance.")
    subparsers.add_parser("approve-trading", help="Approve the router for exactly the 10 USDT canary cap.")
    subparsers.add_parser("revoke-trading", help="Reset the configured router's USDT allowance to zero.")
    return parser


def _cmd_backfill(config: AppConfig, database_path: Path, timeframe: str) -> int:
    # Backfill the requested supported candle timeframe and summarize the stored range.
    try:
        result = backfill_12_months(
            database_path=database_path,
            exchange=config.exchange,
            symbol=config.symbol,
            timeframe=timeframe,
        )
    except Exception as exc:
        print(f"Backfill failed: {exc}")
        return 2
    print(f"Candles inserted or updated: {result['upserted']}")
    print(f"First candle timestamp: {result['first_candle_timestamp']} ({result['first_candle_iso']})")
    print(f"Last candle timestamp: {result['last_candle_timestamp']} ({result['last_candle_iso']})")
    print(f"Database path: {result['database_path']}")
    return 0


def _cmd_zones(config: AppConfig, database_path: Path) -> int:
    # Detect and print support zones from persisted closed 4h candles.
    df = load_candles_df(database_path, config.exchange, config.symbol, "4h", only_closed=True)
    if df.empty:
        print("No closed 4h candles found.")
        return 0
    zc = config.zones
    result = detect_support_resistance_zones(
        df,
        min_touches=zc.min_touches,
        current_price=float(df.iloc[-1]["close"]),
        buffer_pct=zc.role_buffer_pct,
        external_swing_order=zc.external_swing_order,
        internal_swing_order=zc.internal_swing_order,
        atr_period=zc.atr_period,
        break_atr_mult=zc.break_atr_mult,
        near_price_gap_fill_edge_clearance=zc.near_price_gap_fill_edge_clearance,
        near_price_gap_fill_midpoint_spacing=zc.near_price_gap_fill_midpoint_spacing,
        near_price_gap_fill_min_touches=zc.near_price_gap_fill_min_touches,
        external_min_swing_atr_mult=zc.external_min_swing_atr_mult,
        external_min_swing_pct=zc.external_min_swing_pct,
    )
    for zone in result["support"]:
        print(
            f"{zone['origin']} low={zone['low']:.2f} high={zone['high']:.2f} "
            f"mid={zone['mid']:.2f} touches={zone['touches']}"
        )
    return 0


def _cmd_trade_once(config: AppConfig, database_path: Path, mode: str) -> int:
    # Run the shared decision cycle and load wallet credentials only for BUY execution modes.
    try:
        result = run_trade_once(
            config,
            database_path,
            mode=mode,
            live_confirmation=os.getenv("LIVE_TRADING_CONFIRMATION"),
        )
    except Exception as exc:
        print(f"Trade cycle aborted: {exc}")
        return 2
    print(f"Decision row id: {result.decision_id}")
    print(f"Decision: {result.decision['decision']}")
    print(f"Reason: {result.decision['reason_code']}")
    print(f"Zones rebuilt: {result.decision['zones_rebuilt']}")
    if result.execution_id is not None:
        print(f"Execution row id: {result.execution_id}")
        print(f"Execution status: {result.execution_status}")
    return 0


def _cmd_backtest(
    config: AppConfig,
    database_path: Path,
    start_raw: str,
    end_raw: str | None,
    csv_path: str,
) -> int:
    # Replay offline, print a compact BUY-only summary, and write the locked CSV columns.
    try:
        start_ms = parse_backtest_bound(start_raw, label="start")
        end_ms = parse_backtest_bound(end_raw, label="end") if end_raw else None
        result = run_backtest(config, database_path, start_ms=start_ms, end_ms=end_ms)
        output = write_buy_csv(resolve_path(csv_path), result.buys)
    except Exception as exc:
        print(f"Backtest failed: {exc}", file=sys.stderr)
        return 2
    print(f"Range: {ms_to_iso(result.start_ms)} -> {ms_to_iso(result.end_ms)} (end exclusive)")
    print(f"Evaluated candles: {result.evaluated_candles}")
    print(f"Zone snapshots: {result.zone_snapshot_count}")
    print(f"Zone cache hits: {result.zone_cache_hit_count}")
    print(f"Zone detector builds: {result.zone_rebuild_count}")
    print(f"Zone state ingested candles: {result.zone_state_ingested_candles}")
    print(f"Zone full history scans: {result.zone_full_history_scans}")
    print(f"BUY count: {result.buy_count}")
    print(f"CSV: {output}")
    return 0


def _cmd_wallet_create(config: AppConfig) -> int:
    # Prompt twice when needed, create atomically, and print only the new public address.
    try:
        password = resolve_keystore_password(config.wallet.password_env, confirm=True)
        address = create_encrypted_keystore(resolve_path(config.wallet.keystore_path), password)
    except Exception as exc:
        print(f"Wallet creation failed: {exc}", file=sys.stderr)
        return 2
    print(f"Wallet address: {address}")
    return 0


def _cmd_wallet_status(config: AppConfig) -> int:
    # Decrypt and verify the configured keystore but reveal only its public checksum address.
    try:
        password = resolve_keystore_password(config.wallet.password_env)
        account = load_local_account(
            resolve_path(config.wallet.keystore_path),
            password,
            expected_address=config.wallet.expected_address,
        )
    except Exception as exc:
        print(f"Wallet status failed: {exc}", file=sys.stderr)
        return 2
    print(f"Wallet address: {account.address}")
    return 0


def _cmd_trade_check(config: AppConfig) -> int:
    # Print an allowlisted public summary after all signer and contract validations pass.
    try:
        checked = run_contract_checks(config)
    except Exception as exc:
        print(f"Trade check failed: {exc}", file=sys.stderr)
        return 2
    print(f"Environment: {checked.environment}")
    print(f"Chain ID: {checked.chain_id}")
    print(f"Wallet: {checked.wallet_address}")
    print(f"USDT balance: {format_token_amount(checked.usdt_balance_raw, USDT_DECIMALS)}")
    print(f"POL balance: {format_token_amount(checked.pol_balance_raw, 18)}")
    print("Router: verified")
    print(f"Allowance: {format_token_amount(checked.allowance_raw, USDT_DECIMALS)} USDT")
    print(f"Live: {'enabled' if config.execution.live_enabled else 'disabled'}")
    return 0


def _cmd_approve_trading(config: AppConfig) -> int:
    # Execute the explicit capped approval flow and reveal only public state and transaction hashes.
    try:
        result = approve_trading(config)
    except Exception as exc:
        print(f"Approval failed: {exc}", file=sys.stderr)
        return 2
    print(f"Approval: {result.action}")
    print(f"Allowance: {format_token_amount(result.current_allowance_raw, USDT_DECIMALS)} USDT")
    for transaction_hash in result.transaction_hashes:
        print(f"Transaction hash: {transaction_hash}")
    return 0


def _cmd_revoke_trading(config: AppConfig) -> int:
    # Execute an explicit zero approval and reveal only public state and transaction hashes.
    try:
        result = revoke_trading(config)
    except Exception as exc:
        print(f"Revocation failed: {exc}", file=sys.stderr)
        return 2
    print(f"Revocation: {result.action}")
    print(f"Allowance: {format_token_amount(result.current_allowance_raw, USDT_DECIMALS)} USDT")
    for transaction_hash in result.transaction_hashes:
        print(f"Transaction hash: {transaction_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
