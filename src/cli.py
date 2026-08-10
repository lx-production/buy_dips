from __future__ import annotations

import argparse
from pathlib import Path

from .candles import backfill_12_months
from .config import AppConfig, load_config
from .db import init_db, load_candles_df
from .trading.runner import run_trade_once
from .utils import resolve_path
from .zones import detect_support_resistance_zones


def main(argv: list[str] | None = None) -> int:
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
    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PRANA Buy the Dips bot")
    parser.add_argument("--config", default=None, help="Path to config YAML. Defaults to CONFIG_PATH or config.yaml.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("init-db", help="Create SQLite tables.")
    backfill = subparsers.add_parser("backfill", help="Backfill approximately 12 months of BTCUSDT candles.")
    backfill.add_argument("--timeframe", choices=("1h", "4h"), default=None)
    subparsers.add_parser("zones", help="Print support zones detected from closed 4h candles.")
    trade_once = subparsers.add_parser("trade-once", help="Run one fetch/zone/decision cycle.")
    trade_once.add_argument("--mode", choices=("observe",), default="observe")
    return parser


def _cmd_backfill(config: AppConfig, database_path: Path, timeframe: str) -> int:
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
        atr_period=zc.atr_period,
        break_atr_mult=zc.break_atr_mult,
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
    try:
        result = run_trade_once(config, database_path, mode=mode)
    except Exception as exc:
        print(f"Trade cycle aborted: {exc}")
        return 2
    print(f"Decision row id: {result.decision_id}")
    print(f"Decision: {result.decision['decision']}")
    print(f"Reason: {result.decision['reason_code']}")
    print(f"Zones rebuilt: {result.decision['zones_rebuilt']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
