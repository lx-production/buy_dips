from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from typing import Any

from ..binance_client import BinanceSpotClient
from ..config import AppConfig
from ..db import connect, init_db, load_candles_df, upsert_candles
from ..utils import utc_ms
from .aggregate_4h import aggregate_four_hour_bucket, aggregate_overdue_buckets, latest_overdue_bucket_open_time
from .approval import ApprovalError, ensure_swap_allowance
from .binance_hourly import fetch_closed_hourly_candles
from .constants import DETECTOR_VERSION, HOURLY_TIMEFRAME, TRADE_AMOUNT_USDT_RAW, ZONE_TIMEFRAME
from .contract_checks import ContractCheckError, run_contract_checks
from .prana_swap import QuoteError, fetch_swap_quote
from .risk import LiveModeNotAllowed, assert_live_mode_allowed
from .signal import evaluate_support_close_v2
from .state_store import get_zone_rebuild_watermark, zone_rebuild_watermark_key
from .store import create_trade_execution, get_trade_execution, has_recent_zone_buy, has_setup_buy, insert_decision, update_trade_execution
from .transaction import TransactionError, broadcast_signed_swap, prepare_signed_swap, reconcile_swap, simulate_swap, wait_for_swap_receipt
from .wallet import WalletError
from .zone_refresh import ZoneRefreshResult, refresh_zones


class TradingCycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class TradingCycleResult:
    decision_id: int
    decision: dict[str, Any]
    fetched_hourly_candles: int
    derived_four_hour_candles: int
    execution_id: int | None = None
    execution_status: str | None = None


def run_trade_once(
    config: AppConfig,
    database_path: str | Path,
    *,
    mode: str = "observe",
    now_ms: int | None = None,
    client: BinanceSpotClient | None = None,
    fetch: bool = True,
    password: str | None = None,
    web3: Any | None = None,
    quote_session: Any | None = None,
    live_confirmation: str | None = None,
) -> TradingCycleResult:
    """Run one idempotent hourly decision and its mode-gated swap execution."""
    if mode not in {"observe", "dry_run", "live"}:
        raise TradingCycleError(f"Unsupported mode: {mode}")
    if config.price_feed.timeframe != HOURLY_TIMEFRAME:
        raise TradingCycleError("The live price feed timeframe must be 1h")
    now_ms = utc_ms() if now_ms is None else int(now_ms)
    init_db(database_path)

    fetched = 0
    if fetch:
        fetched = fetch_closed_hourly_candles(
            database_path,
            exchange=config.exchange,
            symbol=config.symbol,
            limit=config.price_feed.fetch_limit,
            client=client,
        )

    hourly = load_candles_df(
        database_path,
        config.exchange,
        config.symbol,
        HOURLY_TIMEFRAME,
        only_closed=True,
    )
    if hourly.empty:
        raise TradingCycleError("No closed 1h candle exists after the hourly fetch")
    trigger = hourly.iloc[-1].to_dict()

    watermark_key = zone_rebuild_watermark_key(
        config.exchange,
        config.symbol,
        ZONE_TIMEFRAME,
        DETECTOR_VERSION,
    )
    with connect(database_path) as conn:
        watermark = get_zone_rebuild_watermark(conn, watermark_key)

    latest_due = latest_overdue_bucket_open_time(now_ms)
    # Revalidate the latest due bucket even when its snapshot watermark already exists.
    # Missing constituent 1h rows must always abort rather than trade on that snapshot.
    aggregate_four_hour_bucket(hourly, latest_due, now_ms=now_ms)
    derived = aggregate_overdue_buckets(hourly, now_ms=now_ms, after_open_time=watermark)
    if derived:
        upsert_candles(database_path, derived, config.exchange, config.symbol, ZONE_TIMEFRAME)

    four_hour = load_candles_df(
        database_path,
        config.exchange,
        config.symbol,
        ZONE_TIMEFRAME,
        only_closed=True,
    )
    if not four_hour.empty:
        four_hour = four_hour[four_hour["open_time"].astype("int64") <= latest_due].reset_index(drop=True)
    if four_hour.empty or int(four_hour.iloc[-1]["open_time"]) != latest_due:
        raise TradingCycleError("Latest overdue 4h bucket was not derived and persisted")

    refresh: ZoneRefreshResult = refresh_zones(
        database_path,
        four_hour,
        zone_config=config.zones,
        exchange=config.exchange,
        symbol=config.symbol,
    )
    with connect(database_path) as conn:
        decision = evaluate_support_close_v2(
            trigger,
            hourly,
            refresh.zones,
            zone_set_as_of=refresh.zone_set_as_of,
            setup_already_bought=lambda fingerprint, dip_origin: has_setup_buy(
                conn, fingerprint, dip_origin, int(trigger["open_time"])
            ),
            recent_zone_buy=lambda fingerprint: has_recent_zone_buy(
                conn,
                fingerprint,
                int(trigger["open_time"]),
                cooldown_hours=config.strategy.cooldown_hours,
            ),
            zones_rebuilt=refresh.rebuilt,
            mode=mode,
            strategy_version=config.strategy.version,
            config_version=config.strategy.config_version,
            dip_lookback_hours=config.strategy.dip_lookback_hours,
            below_zone_min_pct=config.strategy.below_zone_min_pct,
            inside_zone_max_pct=config.strategy.inside_zone_max_pct,
        )
        decision_id = insert_decision(
            conn,
            decision,
            exchange=config.exchange,
            symbol=config.symbol,
            timeframe=HOURLY_TIMEFRAME,
        )
        conn.commit()
    if decision["decision"] != "BUY" or mode == "observe":
        return TradingCycleResult(decision_id, decision, fetched, len(derived))

    execution_id, execution_status = _run_execution(
        config,
        database_path,
        decision_id,
        mode=mode,
        password=password,
        web3=web3,
        quote_session=quote_session,
        live_confirmation=live_confirmation,
    )
    return TradingCycleResult(
        decision_id,
        decision,
        fetched,
        len(derived),
        execution_id,
        execution_status,
    )


def _run_execution(
    config: AppConfig,
    database_path: str | Path,
    decision_id: int,
    *,
    mode: str,
    password: str | None,
    web3: Any | None,
    quote_session: Any | None,
    live_confirmation: str | None,
) -> tuple[int, str]:
    """Quote and simulate a BUY, signing only in live mode with durable hash reservation."""
    with connect(database_path) as conn:
        execution_id, created = create_trade_execution(conn, decision_id, mode)
        conn.commit()
        existing = get_trade_execution(conn, execution_id)
    if not created:
        if existing is None:
            raise TradingCycleError("Existing trade execution could not be loaded")
        return _resume_existing_execution(
            config,
            database_path,
            existing,
            password=password,
            web3=web3,
        )

    try:
        checked = run_contract_checks(config, password=password, web3=web3)
        if checked.usdt_balance_raw < TRADE_AMOUNT_USDT_RAW:
            raise TransactionError("USDT balance is below the 1 USDT trade amount")
        if mode == "live":
            assert_live_mode_allowed(config, checked.wallet_address, live_confirmation)

        quote = fetch_swap_quote(
            config,
            checked.wallet_address,
            session=quote_session,
        )
        _save_execution(
            database_path,
            execution_id,
            status="quoted",
            quote_router=quote.router_address,
            amount_in_raw=quote.amount_in_raw,
            quoted_amount_out=str(quote.amount_out),
            minimum_amount_out=str(quote.minimum_amount_out),
            quote_deadline=quote.deadline,
            verification_version=quote.verification_version,
        )

        if mode == "live":
            approval = ensure_swap_allowance(
                config,
                checked,
                quote.router_address,
                quote.amount_in_raw,
            )
            _save_execution(
                database_path,
                execution_id,
                status="allowance_ready",
                approval_transaction_hashes_json=list(approval.transaction_hashes),
            )

        simulation = simulate_swap(
            checked,
            quote,
            max_swap_gas=config.execution.max_swap_gas,
        )
        if mode == "dry_run":
            _save_execution(
                database_path,
                execution_id,
                status="simulated",
                gas_estimate=simulation.gas_estimate,
            )
            return execution_id, "simulated"

        signed = prepare_signed_swap(
            checked,
            quote,
            simulation,
            minimum_deadline_seconds=config.execution.quote_min_deadline_seconds,
        )
        # Commit nonce and locally derived hash before the only broadcast attempt.
        _save_execution(
            database_path,
            execution_id,
            status="signed",
            gas_estimate=simulation.gas_estimate,
            nonce=signed.nonce,
            transaction_hash=signed.transaction_hash,
        )
        transaction_hash = broadcast_signed_swap(checked, signed)
        _save_execution(
            database_path,
            execution_id,
            status="broadcast",
            transaction_hash=transaction_hash,
        )
        receipt = wait_for_swap_receipt(
            checked,
            transaction_hash,
            recipient=checked.wallet_address,
            timeout_seconds=config.execution.receipt_timeout_seconds,
        )
        _save_receipt(database_path, execution_id, receipt)
        return execution_id, receipt.status
    except (ApprovalError, ContractCheckError, LiveModeNotAllowed, QuoteError, TransactionError, WalletError) as exc:
        status = _record_execution_failure(
            database_path,
            execution_id,
            str(exc),
        )
        return execution_id, status
    except Exception:
        # Do not persist unexpected provider details because they may contain RPC credentials.
        status = _record_execution_failure(
            database_path,
            execution_id,
            "Unexpected execution failure",
        )
        return execution_id, status


def _resume_existing_execution(
    config: AppConfig,
    database_path: str | Path,
    execution: dict[str, Any],
    *,
    password: str | None,
    web3: Any | None,
) -> tuple[int, str]:
    """Reconcile an existing reserved hash and never create or broadcast a replacement."""
    execution_id = int(execution["id"])
    status = str(execution["status"])
    transaction_hash = execution.get("transaction_hash")
    if execution.get("mode") != "live" or not transaction_hash or status not in {"signed", "broadcast", "pending"}:
        return execution_id, status
    try:
        checked = run_contract_checks(config, password=password, web3=web3)
        receipt = reconcile_swap(
            checked.web3,
            str(transaction_hash),
            recipient=checked.wallet_address,
        )
        _save_receipt(database_path, execution_id, receipt)
        return execution_id, receipt.status
    except (ContractCheckError, TransactionError, WalletError):
        return execution_id, status


def _save_receipt(
    database_path: str | Path,
    execution_id: int,
    receipt: Any,
) -> None:
    """Persist one redacted receipt outcome without raw logs or transaction bytes."""
    _save_execution(
        database_path,
        execution_id,
        status=receipt.status,
        block_number=receipt.block_number,
        gas_used=receipt.gas_used,
        actual_prana_output_raw=receipt.actual_prana_output_raw,
    )


def _record_execution_failure(
    database_path: str | Path,
    execution_id: int,
    reason: str,
) -> str:
    """Preserve a reserved hash state so later cycles reconcile instead of retrying."""
    with connect(database_path) as conn:
        execution = get_trade_execution(conn, execution_id)
        if execution is None:
            raise TradingCycleError("Trade execution disappeared during failure handling")
        current_status = str(execution["status"])
        status = current_status if execution.get("transaction_hash") else "failed"
        update_trade_execution(
            conn,
            execution_id,
            status=status,
            reason=reason,
        )
        conn.commit()
    return status


def _save_execution(
    database_path: str | Path,
    execution_id: int,
    **fields: Any,
) -> None:
    """Commit one transition so a crash cannot erase the reserved transaction identity."""
    with connect(database_path) as conn:
        update_trade_execution(conn, execution_id, **fields)
        conn.commit()
