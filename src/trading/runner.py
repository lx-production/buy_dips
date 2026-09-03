from __future__ import annotations

import logging
import uuid

from dataclasses import dataclass
from pathlib import Path

from typing import Any

from ..binance_client import BinanceSpotClient
from ..config import AppConfig
from ..db import connect, init_db, load_candles_df, upsert_candles
from ..utils import utc_ms, utc_seconds
from .aggregate_4h import aggregate_four_hour_bucket, aggregate_overdue_buckets, latest_overdue_bucket_open_time
from .approval import ApprovalError, ensure_swap_allowance
from .audit_logging import configure_audit_logger, log_event
from .binance_hourly import fetch_closed_hourly_candles
from .constants import DETECTOR_VERSION, HOURLY_TIMEFRAME, ZONE_TIMEFRAME
from .contract_checks import ContractCheckError, run_contract_checks
from .prana_swap import QuoteError, fetch_swap_quote
from .risk import LiveModeNotAllowed, RiskCheckError, assert_live_mode_allowed, check_gas_reserve, check_pre_execution_risk, check_wallet_funds
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
    """Run one auditable cycle and record a safe abort event before re-raising errors."""
    logger = configure_audit_logger(config.logging)
    cycle_id = uuid.uuid4().hex
    log_event(logger, "cycle_started", cycle_id=cycle_id, mode=mode)
    try:
        return _run_trade_once(
            config,
            database_path,
            mode=mode,
            now_ms=now_ms,
            client=client,
            fetch=fetch,
            password=password,
            web3=web3,
            quote_session=quote_session,
            live_confirmation=live_confirmation,
            cycle_id=cycle_id,
            logger=logger,
        )
    except Exception as exc:
        # Log only the exception class because provider messages can contain secret URLs.
        log_event(logger, "cycle_aborted", cycle_id=cycle_id, mode=mode, error_type=type(exc).__name__)
        raise


def _run_trade_once(
    config: AppConfig,
    database_path: str | Path,
    *,
    mode: str,
    now_ms: int | None,
    client: BinanceSpotClient | None,
    fetch: bool,
    password: str | None,
    web3: Any | None,
    quote_session: Any | None,
    live_confirmation: str | None,
    cycle_id: str,
    logger: logging.Logger,
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
    log_event(
        logger,
        "decision_persisted",
        cycle_id=cycle_id,
        decision_id=decision_id,
        decision=decision["decision"],
        reason_code=decision["reason_code"],
        zone_set_as_of=decision["zone_set_as_of"],
        fingerprint_version=decision["fingerprint_version"],
        selected_zone_fingerprint=decision.get("selected_zone_fingerprint"),
        higher_zone_fingerprint=decision.get("higher_zone_fingerprint"),
        next_lower_zone_fingerprint=decision.get("next_lower_zone_fingerprint"),
        zones_rebuilt=decision["zones_rebuilt"],
    )
    if decision["decision"] != "BUY" or mode == "observe":
        log_event(
            logger,
            "cycle_no_trade",
            cycle_id=cycle_id,
            decision_id=decision_id,
            mode=mode,
            reason_code=decision["reason_code"] if decision["decision"] != "BUY" else "OBSERVE_MODE",
        )
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
        cycle_id=cycle_id,
        logger=logger,
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
    cycle_id: str | None = None,
    logger: logging.Logger | None = None,
) -> tuple[int, str]:
    """Quote and simulate a BUY, signing only in live mode with durable hash reservation."""
    audit_logger = configure_audit_logger(config.logging) if logger is None else logger
    correlation_id = uuid.uuid4().hex if cycle_id is None else cycle_id
    with connect(database_path) as conn:
        # Serialize execution admission so concurrent cycles cannot both pass the in-flight gate.
        conn.execute("BEGIN IMMEDIATE")
        execution_id, created = create_trade_execution(conn, decision_id, mode)
        existing = get_trade_execution(conn, execution_id)
        if created:
            try:
                exposure = check_pre_execution_risk(
                    conn,
                    config,
                    execution_id,
                    now_s=utc_seconds(),
                )
            except RiskCheckError as exc:
                update_trade_execution(conn, execution_id, status="skipped", reason=exc.code)
                conn.commit()
                log_event(
                    audit_logger,
                    "execution_skipped",
                    cycle_id=correlation_id,
                    decision_id=decision_id,
                    execution_id=execution_id,
                    reason=exc.code,
                )
                return execution_id, "skipped"
            update_trade_execution(conn, execution_id, status="risk_checked")
        conn.commit()
    if not created:
        if existing is None:
            raise TradingCycleError("Existing trade execution could not be loaded")
        resumed_id, resumed_status = _resume_existing_execution(
            config,
            database_path,
            existing,
            password=password,
            web3=web3,
        )
        log_event(
            audit_logger,
            "execution_resumed",
            cycle_id=correlation_id,
            decision_id=decision_id,
            execution_id=resumed_id,
            status=resumed_status,
        )
        return resumed_id, resumed_status

    log_event(
        audit_logger,
        "execution_risk_checked",
        cycle_id=correlation_id,
        decision_id=decision_id,
        execution_id=execution_id,
        mode=mode,
        utc_day_trade_count=exposure.utc_day_trade_count,
        cumulative_spend_raw=exposure.cumulative_spend_raw,
    )

    try:
        checked = run_contract_checks(config, password=password, web3=web3)
        if mode == "live":
            assert_live_mode_allowed(config, checked.wallet_address, live_confirmation)
        check_wallet_funds(
            config,
            usdt_balance_raw=checked.usdt_balance_raw,
            pol_balance_raw=checked.pol_balance_raw,
        )

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
        log_event(
            audit_logger,
            "quote_validated",
            cycle_id=correlation_id,
            decision_id=decision_id,
            execution_id=execution_id,
            quote_router=quote.router_address,
            amount_in_raw=quote.amount_in_raw,
            minimum_amount_out=str(quote.minimum_amount_out),
            verification_version=quote.verification_version,
        )
        check_gas_reserve(
            config,
            pol_balance_raw=checked.pol_balance_raw,
            gas_price_wei=int(checked.web3.eth.gas_price),
            gas_limit=config.execution.max_swap_gas,
            include_approval=mode == "live" and checked.allowance_raw < quote.amount_in_raw,
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
            log_event(
                audit_logger,
                "allowance_ready",
                cycle_id=correlation_id,
                decision_id=decision_id,
                execution_id=execution_id,
                action=approval.action,
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
            log_event(
                audit_logger,
                "swap_simulated",
                cycle_id=correlation_id,
                decision_id=decision_id,
                execution_id=execution_id,
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
        log_event(
            audit_logger,
            "swap_signed",
            cycle_id=correlation_id,
            decision_id=decision_id,
            execution_id=execution_id,
            transaction_hash=signed.transaction_hash,
            nonce=signed.nonce,
        )
        transaction_hash = broadcast_signed_swap(checked, signed)
        _save_execution(
            database_path,
            execution_id,
            status="broadcast",
            transaction_hash=transaction_hash,
        )
        log_event(
            audit_logger,
            "swap_broadcast",
            cycle_id=correlation_id,
            decision_id=decision_id,
            execution_id=execution_id,
            transaction_hash=transaction_hash,
        )
        receipt = wait_for_swap_receipt(
            checked,
            transaction_hash,
            recipient=checked.wallet_address,
            timeout_seconds=config.execution.receipt_timeout_seconds,
        )
        _save_receipt(database_path, execution_id, receipt)
        log_event(
            audit_logger,
            "swap_receipt",
            cycle_id=correlation_id,
            decision_id=decision_id,
            execution_id=execution_id,
            transaction_hash=receipt.transaction_hash,
            status=receipt.status,
            block_number=receipt.block_number,
            gas_used=receipt.gas_used,
            actual_prana_output_raw=receipt.actual_prana_output_raw,
        )
        return execution_id, receipt.status
    except RiskCheckError as exc:
        status = _record_execution_outcome(
            database_path,
            execution_id,
            status="skipped",
            reason=exc.code,
        )
        log_event(audit_logger, "execution_skipped", cycle_id=correlation_id, decision_id=decision_id, execution_id=execution_id, reason=exc.code)
        return execution_id, status
    except LiveModeNotAllowed:
        status = _record_execution_outcome(
            database_path,
            execution_id,
            status="skipped",
            reason="LIVE_MODE_NOT_ALLOWED",
        )
        log_event(audit_logger, "execution_skipped", cycle_id=correlation_id, decision_id=decision_id, execution_id=execution_id, reason="LIVE_MODE_NOT_ALLOWED")
        return execution_id, status
    except (ApprovalError, ContractCheckError, QuoteError, TransactionError, WalletError) as exc:
        reason = _execution_failure_code(exc)
        status = _record_execution_failure(database_path, execution_id, reason)
        log_event(audit_logger, "execution_failed", cycle_id=correlation_id, decision_id=decision_id, execution_id=execution_id, reason=reason, status=status)
        return execution_id, status
    except Exception:
        # Do not persist unexpected provider details because they may contain RPC credentials.
        reason = "UNEXPECTED_EXECUTION_FAILURE"
        status = _record_execution_failure(database_path, execution_id, reason)
        log_event(audit_logger, "execution_failed", cycle_id=correlation_id, decision_id=decision_id, execution_id=execution_id, reason=reason, status=status)
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


def _record_execution_outcome(
    database_path: str | Path,
    execution_id: int,
    *,
    status: str,
    reason: str,
) -> str:
    """Persist a terminal skip/failure reason without copying exception details."""
    with connect(database_path) as conn:
        update_trade_execution(conn, execution_id, status=status, reason=reason)
        conn.commit()
    return status


def _execution_failure_code(exc: Exception) -> str:
    """Map known safe exception types to stable redacted persistence codes."""
    if isinstance(exc, ApprovalError):
        return "APPROVAL_FAILED"
    if isinstance(exc, ContractCheckError):
        return "CONTRACT_CHECK_FAILED"
    if isinstance(exc, QuoteError):
        return "QUOTE_FAILED"
    if isinstance(exc, TransactionError):
        return "TRANSACTION_FAILED"
    if isinstance(exc, WalletError):
        return "WALLET_FAILED"
    return "EXECUTION_FAILED"


def _save_execution(
    database_path: str | Path,
    execution_id: int,
    **fields: Any,
) -> None:
    """Commit one transition so a crash cannot erase the reserved transaction identity."""
    with connect(database_path) as conn:
        update_trade_execution(conn, execution_id, **fields)
        conn.commit()
