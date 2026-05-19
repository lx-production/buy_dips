from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any
from .candles import backfill_12_months, fetch_latest_candles
from .config import AppConfig, load_config
from .db import init_db, insert_signal, insert_zones, load_candles_df
from .paper_trading import PAPER_MODE_WARNING, assert_paper_mode_only
from .signals import generate_buy_the_dips_signal
from .utils import ms_to_iso, resolve_path
from .zones import detect_support_resistance_zones


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    database_path = resolve_path(config.database_path)

    if args.command == "init-db":
        init_db(database_path)
        print(PAPER_MODE_WARNING)
        print(f"Initialized database: {database_path}")
        return 0
    if args.command == "backfill":
        return _cmd_backfill(config, database_path)
    if args.command == "zones":
        return _cmd_zones(config, database_path)
    if args.command == "run-once":
        return _cmd_run_once(config, database_path)

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PRANA Buy the Dips bot Phase 1")
    parser.add_argument("--config", default=None, help="Path to config YAML. Defaults to CONFIG_PATH or config.yaml.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("init-db", help="Create SQLite tables.")
    subparsers.add_parser("backfill", help="Backfill approximately 12 months of BTCUSDT 4H candles.")
    subparsers.add_parser("zones", help="Detect and store support zones from closed candles.")
    subparsers.add_parser("run-once", help="Fetch latest candles, detect zones, generate and store one paper signal.")
    return parser


def _cmd_backfill(config: AppConfig, database_path: Path) -> int:
    print(PAPER_MODE_WARNING)
    try:
        result = backfill_12_months(
            database_path=database_path,
            exchange=config.exchange,
            symbol=config.symbol,
            timeframe=config.timeframe,
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
    print(PAPER_MODE_WARNING)
    zones_result = _detect_and_store_zones(config, database_path)
    _print_zones(zones_result)
    return 0


def _cmd_run_once(config: AppConfig, database_path: Path) -> int:
    assert_paper_mode_only()
    print(PAPER_MODE_WARNING)
    try:
        updated = fetch_latest_candles(
            database_path=database_path,
            exchange=config.exchange,
            symbol=config.symbol,
            timeframe=config.timeframe,
        )
    except Exception as exc:
        print(f"Latest candle fetch failed: {exc}")
        updated = 0

    zones_result = _detect_and_store_zones(config, database_path)
    df = load_candles_df(database_path, config.exchange, config.symbol, config.timeframe, only_closed=True)
    signal = generate_buy_the_dips_signal(df, zones_result, config)
    signal_id = insert_signal(database_path, signal, config.symbol, config.timeframe)
    _print_signal(signal, signal_id=signal_id, updated_candles=updated)
    return 0


def _detect_and_store_zones(config: AppConfig, database_path: Path) -> dict[str, list[dict[str, Any]]]:
    df = load_candles_df(database_path, config.exchange, config.symbol, config.timeframe, only_closed=True)
    if df.empty:
        zones_result = {"support": [], "resistance": [], "active": [], "all": []}
    else:
        current_price = float(df.iloc[-1]["close"])
        zc = config.zones
        zones_result = detect_support_resistance_zones(
            df,
            min_touches=zc.min_touches,
            current_price=current_price,
            internal_swing_order=zc.internal_swing_order,
            external_swing_order=zc.external_swing_order,
            atr_period=zc.atr_period,
            external_min_swing_atr_mult=zc.external_min_swing_atr_mult,
            external_min_swing_pct=zc.external_min_swing_pct,
        )
    inserted = insert_zones(database_path, zones_result["all"], config.symbol, config.timeframe)
    print(f"Closed candles loaded: {len(df)}")
    print("Zone algorithm: support_swing_lows_v1")
    print(f"Zones stored this run: {inserted}")
    return zones_result


def _print_zones(zones_result: dict[str, list[dict[str, Any]]]) -> None:
    print("\nSUPPORT ZONES")
    support_zones = zones_result.get("support", [])
    if not support_zones:
        print("  none")
        return
    for zone in support_zones:
        print(
            "  "
            f"{zone['origin']} low={zone['low']:.2f} high={zone['high']:.2f} "
            f"mid={zone['mid']:.2f} width_pct={zone['width_pct']:.3f}% "
            f"touches={zone['touches']}"
        )


def _print_signal(signal: dict[str, Any], signal_id: int, updated_candles: int) -> None:
    metadata = signal.get("metadata") or {}
    latest_close_time = metadata.get("latest_candle_close_time")
    print("\nPAPER SIGNAL REPORT")
    print(f"Signal row id: {signal_id}")
    print(f"Candles inserted or updated from latest fetch: {updated_candles}")
    print(f"Latest closed candle: {ms_to_iso(latest_close_time)}")
    print(f"Price: {signal['price']:.2f}")
    print(f"Decision: {signal['decision']}")
    print(f"Score: {signal['signal_score']:.2f}")
    print(f"Distance to support: {_fmt_pct(signal.get('distance_to_support_pct'))}")
    print(f"Reason: {signal['reason']}")


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}%"


if __name__ == "__main__":
    raise SystemExit(main())
