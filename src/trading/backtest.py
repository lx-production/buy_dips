from __future__ import annotations

import csv

from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field

from typing import Any, Callable

import pandas as pd

from ..config import AppConfig
from ..db import connect, load_candles_df
from ..utils import ms_to_iso
from .aggregate_4h import OverdueIncompleteFourHourError, aggregate_four_hour_bucket
from .constants import DETECTOR_VERSION, FOUR_HOURS_MS, HOURLY_TIMEFRAME, ONE_HOUR_MS, STRATEGY_VERSION, ZONE_TIMEFRAME
from .signal import BUY, evaluate_support_close_v1
from .backtest_zone_cache import ZoneCacheIdentity, build_four_hour_input_hashes, build_zone_cache_identity, load_cached_zone_snapshot, prune_incompatible_zone_cache, store_cached_zone_snapshot
from .zone_refresh import ZoneRefreshError, build_fingerprinted_support_zones


BUY_CSV_COLUMNS = [
    "trigger_time",
    "trigger_close",
    "entry_region",
    "fingerprint_version",
    "selected_zone_fingerprint",
    "zone_low",
    "zone_mid",
    "zone_high",
    "higher_zone_fingerprint",
    "higher_zone_low",
    "internal_range_midpoint",
    "next_lower_zone_fingerprint",
    "next_lower_zone_high",
    "below_zone_pct",
    "dip_origin_time",
    "dip_origin_close",
    "zone_set_as_of",
]


class BacktestError(RuntimeError):
    pass


@dataclass
class PriorBuy:
    trigger_open_time: int
    zone_fingerprint: str
    dip_origin_open_time: int


@dataclass
class ZoneSnapshot:
    zone_set_as_of: int
    zones: list[dict[str, Any]]
    valid_from: int | None = None


@dataclass(frozen=True)
class BacktestResult:
    start_ms: int
    end_ms: int
    candles: list[dict[str, Any]]
    buys: list[dict[str, Any]]
    zone_snapshots: list[ZoneSnapshot]
    zone_segments: list[dict[str, Any]]
    evaluated_candles: int
    zone_snapshot_count: int
    zone_rebuild_count: int
    zone_cache_hit_count: int
    buy_count: int
    prior_buys: list[PriorBuy] = field(default_factory=list)


def parse_backtest_bound(raw: str, *, label: str) -> int:
    """Parse an ISO-8601 timestamp with timezone and require a UTC hour boundary."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BacktestError(f"Invalid {label} timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise BacktestError(f"{label} must include a timezone offset (ISO-8601 with tz)")
    ms = int(parsed.timestamp() * 1000)
    if ms % ONE_HOUR_MS != 0:
        raise BacktestError(f"{label} must fall on a UTC hour boundary")
    return ms


def default_backtest_end_ms(hourly_df: pd.DataFrame) -> int:
    """Return exclusive end after the latest closed 1h candle open_time."""
    if hourly_df is None or hourly_df.empty:
        raise BacktestError("No closed 1h candles are available to default --end")
    last_open = int(hourly_df["open_time"].astype("int64").max())
    return last_open + ONE_HOUR_MS


def run_backtest(
    config: AppConfig,
    database_path: str | Path,
    *,
    start_ms: int,
    end_ms: int | None = None,
    detector: Callable[..., dict[str, list[dict[str, Any]]]] | None = None,
) -> BacktestResult:
    """Replay support_close_v1 over closed 1h candles without touching live tables."""
    _validate_bound_ms(start_ms, "start")
    if end_ms is not None:
        _validate_bound_ms(end_ms, "end")
        if end_ms <= start_ms:
            raise BacktestError("end must be strictly after start")

    # First-trigger dip-origin scan needs this many hours of closed 1h candles before start.
    lookback_start = start_ms - config.strategy.dip_lookback_hours * ONE_HOUR_MS
    hourly_all = load_candles_df(
        database_path,
        config.exchange,
        config.symbol,
        HOURLY_TIMEFRAME,
        only_closed=True,
    )
    if hourly_all.empty:
        raise BacktestError("No closed 1h candles found in SQLite")

    resolved_end = int(end_ms) if end_ms is not None else default_backtest_end_ms(hourly_all)
    _validate_bound_ms(resolved_end, "end")
    if resolved_end <= start_ms:
        raise BacktestError("end must be strictly after start")

    # Keep warm-up lookback plus the evaluated window; never include candles at/after end.
    hourly = hourly_all[
        (hourly_all["open_time"].astype("int64") >= lookback_start)
        & (hourly_all["open_time"].astype("int64") < resolved_end)
    ].sort_values("open_time").reset_index(drop=True)
    if hourly.empty:
        raise BacktestError("No closed 1h candles fall inside the requested backtest window")
    _assert_continuous_hourly(hourly, lookback_start, resolved_end)

    display = hourly[
        (hourly["open_time"].astype("int64") >= start_ms)
        & (hourly["open_time"].astype("int64") < resolved_end)
    ].reset_index(drop=True)
    if display.empty:
        raise BacktestError("No closed 1h trigger candles fall in [start, end)")

    warm_up_4h = _load_warm_up_four_hour(
        database_path,
        exchange=config.exchange,
        symbol=config.symbol,
        hourly_start_ms=lookback_start,
    )
    four_hour_history = _build_four_hour_history(
        hourly,
        warm_up_4h,
        through_ms=resolved_end,
    )
    cache_identity: ZoneCacheIdentity | None = None
    input_hashes: dict[int, str] = {}
    if detector is None:
        cache_identity = build_zone_cache_identity(config.zones)
        input_hashes = build_four_hour_input_hashes(four_hour_history)
        prune_incompatible_zone_cache(
            database_path,
            exchange=config.exchange,
            symbol=config.symbol,
            identity=cache_identity,
        )

    prior_buys: list[PriorBuy] = []
    buys: list[dict[str, Any]] = []
    snapshots: list[ZoneSnapshot] = []
    current_zones: list[dict[str, Any]] = []
    watermark: int | None = None
    zone_snapshot_count = 0
    zone_rebuild_count = 0
    zone_cache_hit_count = 0
    detector_fn = detector

    for _, row in display.iterrows():
        trigger = row.to_dict()
        trigger_open = int(trigger["open_time"])
        now_ms = trigger_open + ONE_HOUR_MS

        rebuilt = False
        latest_due = (now_ms // FOUR_HOURS_MS) * FOUR_HOURS_MS - FOUR_HOURS_MS
        if watermark is None or latest_due > watermark:
            # Slice the precomputed history only when the completed 4h watermark advances.
            derived_frame, latest_completed = _four_hour_frame_as_of(
                four_hour_history,
                now_ms=now_ms,
            )
            if latest_completed is None:
                raise BacktestError(
                    f"Insufficient completed 4h history at trigger {ms_to_iso(trigger_open)}"
                )
            input_hash = input_hashes.get(latest_completed)
            cached_zones = None
            if cache_identity is not None and input_hash is not None:
                cached_zones = load_cached_zone_snapshot(
                    database_path,
                    exchange=config.exchange,
                    symbol=config.symbol,
                    zone_set_as_of=latest_completed,
                    input_hash=input_hash,
                    identity=cache_identity,
                )
            if cached_zones is not None:
                zones = cached_zones
                zone_cache_hit_count += 1
            else:
                try:
                    if detector_fn is None:
                        zones = build_fingerprinted_support_zones(
                            derived_frame,
                            zone_config=config.zones,
                            zone_set_as_of=latest_completed,
                            exchange=config.exchange,
                            symbol=config.symbol,
                            detector_version=DETECTOR_VERSION,
                        )
                    else:
                        zones = build_fingerprinted_support_zones(
                            derived_frame,
                            zone_config=config.zones,
                            zone_set_as_of=latest_completed,
                            exchange=config.exchange,
                            symbol=config.symbol,
                            detector_version=DETECTOR_VERSION,
                            detector=detector_fn,
                        )
                except ZoneRefreshError as exc:
                    raise BacktestError(str(exc)) from exc
                zone_rebuild_count += 1
                if cache_identity is not None and input_hash is not None:
                    store_cached_zone_snapshot(
                        database_path,
                        zones,
                        exchange=config.exchange,
                        symbol=config.symbol,
                        zone_set_as_of=latest_completed,
                        input_hash=input_hash,
                        identity=cache_identity,
                    )
            current_zones = zones
            watermark = latest_completed
            snapshots.append(ZoneSnapshot(zone_set_as_of=latest_completed, zones=zones))
            zone_snapshot_count += 1
            rebuilt = True

        assert watermark is not None
        # Snapshot becomes valid at the first trigger that actually evaluates against it.
        if snapshots and snapshots[-1].valid_from is None and (
            rebuilt or snapshots[-1].zone_set_as_of == watermark
        ):
            snapshots[-1].valid_from = trigger_open

        decision = evaluate_support_close_v1(
            trigger,
            hourly,
            current_zones,
            zone_set_as_of=watermark,
            setup_already_bought=lambda fingerprint, dip_origin: _setup_already_bought(
                prior_buys, fingerprint, dip_origin
            ),
            zones_rebuilt=rebuilt,
            mode="backtest",
            strategy_version=config.strategy.version or STRATEGY_VERSION,
            config_version=config.strategy.config_version,
            dip_lookback_hours=config.strategy.dip_lookback_hours,
            below_zone_min_pct=config.strategy.below_zone_min_pct,
            inside_zone_max_pct=config.strategy.inside_zone_max_pct,
        )
        if decision["decision"] == BUY:
            fingerprint = str(decision["selected_zone_fingerprint"])
            prior_buys.append(
                PriorBuy(
                    trigger_open_time=trigger_open,
                    zone_fingerprint=fingerprint,
                    dip_origin_open_time=int(decision["dip_origin_open_time"]),
                )
            )
            buys.append(_buy_row(decision))

    # Close the final snapshot validity window at the exclusive end bound.
    if snapshots and snapshots[-1].valid_from is None:
        snapshots[-1].valid_from = int(display.iloc[0]["open_time"])

    candles = [
        {
            "time": int(row.open_time),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume) if row.volume is not None and not pd.isna(row.volume) else None,
        }
        for row in display.itertuples(index=False)
    ]
    segments = build_zone_segments(snapshots, end_ms=resolved_end)
    return BacktestResult(
        start_ms=start_ms,
        end_ms=resolved_end,
        candles=candles,
        buys=buys,
        zone_snapshots=snapshots,
        zone_segments=segments,
        evaluated_candles=len(display),
        zone_snapshot_count=zone_snapshot_count,
        zone_rebuild_count=zone_rebuild_count,
        zone_cache_hit_count=zone_cache_hit_count,
        buy_count=len(buys),
        prior_buys=prior_buys,
    )


def write_buy_csv(path: str | Path, buys: list[dict[str, Any]]) -> Path:
    """Write only BUY rows with the locked column order; zero buys still emits a header."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BUY_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in buys:
            writer.writerow({column: row.get(column) for column in BUY_CSV_COLUMNS})
    return output


def build_zone_segments(snapshots: list[ZoneSnapshot], *, end_ms: int) -> list[dict[str, Any]]:
    """Expand snapshots into per-zone validity segments and merge identical consecutive bands."""
    raw: list[dict[str, Any]] = []
    ordered = [snap for snap in snapshots if snap.valid_from is not None]
    for index, snap in enumerate(ordered):
        valid_from = int(snap.valid_from)  # type: ignore[arg-type]
        if index + 1 < len(ordered):
            valid_to = int(ordered[index + 1].valid_from)  # type: ignore[arg-type]
        else:
            valid_to = int(end_ms)
        if valid_to <= valid_from:
            continue
        for zone in snap.zones:
            raw.append(
                {
                    "fingerprint": zone["fingerprint"],
                    "low": float(zone["low"]),
                    "mid": float(zone["mid"]),
                    "high": float(zone["high"]),
                    "touches": int(zone.get("touches", 0)),
                    "source_timeframe": str(zone.get("source_timeframe", "4h")),
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "zone_set_as_of": int(snap.zone_set_as_of),
                }
            )
    raw.sort(key=lambda item: (item["fingerprint"], item["low"], item["high"], item["valid_from"]))
    merged: list[dict[str, Any]] = []
    for segment in raw:
        if not merged:
            merged.append(dict(segment))
            continue
        previous = merged[-1]
        same_band = (
            previous["fingerprint"] == segment["fingerprint"]
            and previous["low"] == segment["low"]
            and previous["mid"] == segment["mid"]
            and previous["high"] == segment["high"]
            and previous["source_timeframe"] == segment["source_timeframe"]
            and previous["valid_to"] == segment["valid_from"]
        )
        if same_band:
            previous["valid_to"] = segment["valid_to"]
            previous["touches"] = max(int(previous["touches"]), int(segment["touches"]))
            previous["zone_set_as_of"] = segment["zone_set_as_of"]
        else:
            merged.append(dict(segment))
    merged.sort(key=lambda item: (item["valid_from"], item["low"], item["fingerprint"]))
    return merged


def backtest_api_payload(result: BacktestResult, *, config: AppConfig) -> dict[str, Any]:
    """Build the chart/API payload: meta, 1h candles, BUY markers, and zone segments only."""
    return {
        "meta": {
            "exchange": config.exchange,
            "symbol": config.symbol,
            "timeframe": HOURLY_TIMEFRAME,
            "strategy_version": config.strategy.version or STRATEGY_VERSION,
            "start": ms_to_iso(result.start_ms),
            "end": ms_to_iso(result.end_ms),
            "start_ms": result.start_ms,
            "end_ms": result.end_ms,
            "evaluated_candles": result.evaluated_candles,
            "zone_snapshot_count": result.zone_snapshot_count,
            "zone_rebuild_count": result.zone_rebuild_count,
            "zone_cache_hit_count": result.zone_cache_hit_count,
            "buy_count": result.buy_count,
        },
        "candles": result.candles,
        "buys": result.buys,
        "zone_segments": result.zone_segments,
    }


def assert_backtest_does_not_touch_live_tables(
    database_path: str | Path,
    *,
    before_counts: dict[str, int],
) -> None:
    """Fail closed if a replay mutated live bot_state / zones / zone_sets / decisions rows."""
    after = _table_counts(database_path)
    for table, before in before_counts.items():
        if after.get(table) != before:
            raise BacktestError(f"Backtest mutated live table {table}: {before} -> {after.get(table)}")


def live_table_counts(database_path: str | Path) -> dict[str, int]:
    """Capture row counts for the live tables backtest must leave untouched."""
    return _table_counts(database_path)


def _table_counts(database_path: str | Path) -> dict[str, int]:
    tables = ("bot_state", "zones", "zone_sets", "decisions")
    counts: dict[str, int] = {}
    with connect(database_path) as conn:
        for table in tables:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
    return counts


def _buy_row(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "trigger_time": ms_to_iso(int(decision["candle_open_time"])),
        "trigger_open_time": int(decision["candle_open_time"]),
        "trigger_close": decision["reference_close"],
        "entry_region": decision["entry_region"],
        "fingerprint_version": decision["fingerprint_version"],
        "selected_zone_fingerprint": decision["selected_zone_fingerprint"],
        "zone_low": decision["selected_zone_low"],
        "zone_mid": decision["selected_zone_mid"],
        "zone_high": decision["selected_zone_high"],
        "higher_zone_fingerprint": decision["higher_zone_fingerprint"],
        "higher_zone_low": decision["higher_zone_low"],
        "internal_range_midpoint": decision["internal_range_midpoint"],
        "next_lower_zone_fingerprint": decision["next_lower_zone_fingerprint"],
        "next_lower_zone_high": decision["next_lower_zone_high"],
        "below_zone_pct": decision["below_zone_pct"],
        "dip_origin_time": (
            ms_to_iso(int(decision["dip_origin_open_time"]))
            if decision.get("dip_origin_open_time") is not None
            else None
        ),
        "dip_origin_open_time": decision.get("dip_origin_open_time"),
        "dip_origin_close": decision["dip_origin_close"],
        "zone_set_as_of": decision["zone_set_as_of"],
        "zone_set_as_of_iso": ms_to_iso(int(decision["zone_set_as_of"])),
    }


def _setup_already_bought(prior_buys: list[PriorBuy], fingerprint: str, dip_origin_open_time: int) -> bool:
    """Return True when this replay already bought the same zone + dip-origin setup."""
    for prior in prior_buys:
        if prior.zone_fingerprint == fingerprint and prior.dip_origin_open_time == int(dip_origin_open_time):
            return True
    return False


def _validate_bound_ms(value: int, label: str) -> None:
    if value < 0 or value % ONE_HOUR_MS != 0:
        raise BacktestError(f"{label} must be a non-negative UTC hour boundary in milliseconds")


def _assert_continuous_hourly(hourly: pd.DataFrame, start_ms: int, end_ms: int) -> None:
    expected = list(range(int(start_ms), int(end_ms), ONE_HOUR_MS))
    actual = [int(value) for value in hourly["open_time"].astype("int64").tolist()]
    if not actual:
        raise BacktestError("Hourly candle window is empty after filtering")
    if actual[0] != start_ms:
        raise BacktestError(
            f"Missing warm-up 1h candles; expected first open_time {ms_to_iso(start_ms)}, "
            f"got {ms_to_iso(actual[0])}"
        )
    if actual != expected:
        actual_set = set(actual)
        missing = [ms_to_iso(value) for value in expected if value not in actual_set][:5]
        raise BacktestError(f"1h candle series has gaps; example missing open_times: {missing}")


def _load_warm_up_four_hour(
    database_path: str | Path,
    *,
    exchange: str,
    symbol: str,
    hourly_start_ms: int,
) -> pd.DataFrame:
    """Load historical 4h bars that end before the first 1h-covered 4h bucket."""
    first_derived_bucket = _first_fully_covered_four_hour_bucket(hourly_start_ms)
    four_hour = load_candles_df(
        database_path,
        exchange,
        symbol,
        ZONE_TIMEFRAME,
        only_closed=True,
    )
    if four_hour.empty:
        return four_hour
    return four_hour[four_hour["open_time"].astype("int64") < first_derived_bucket].reset_index(drop=True)


def _build_four_hour_history(
    hourly: pd.DataFrame,
    warm_up_4h: pd.DataFrame,
    *,
    through_ms: int,
) -> pd.DataFrame:
    """Aggregate each due 4h bucket once and combine it with older warm-up history."""
    if hourly.empty:
        raise BacktestError("Cannot derive 4h bars without 1h candles")
    hourly_start = int(hourly["open_time"].astype("int64").min())
    first_derived_bucket = _first_fully_covered_four_hour_bucket(hourly_start)
    latest_due = (int(through_ms) // FOUR_HOURS_MS) * FOUR_HOURS_MS - FOUR_HOURS_MS

    derived_rows: list[dict[str, Any]] = []
    for bucket in range(first_derived_bucket, latest_due + 1, FOUR_HOURS_MS):
        try:
            bar = aggregate_four_hour_bucket(hourly, bucket, now_ms=through_ms)
        except OverdueIncompleteFourHourError as exc:
            raise BacktestError(str(exc)) from exc
        if bar is not None:
            derived_rows.append(bar)

    frames = []
    if warm_up_4h is not None and not warm_up_4h.empty:
        frames.append(warm_up_4h.copy())
    if derived_rows:
        frames.append(pd.DataFrame(derived_rows))
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values("open_time").drop_duplicates("open_time", keep="last").reset_index(drop=True)
    if "is_closed" in combined.columns:
        combined = combined[combined["is_closed"].astype(int) == 1].reset_index(drop=True)
    return combined


def _first_fully_covered_four_hour_bucket(hourly_start_ms: int) -> int:
    """Return the first 4h bucket whose four constituent hours are present in the 1h window."""
    start = int(hourly_start_ms)
    return ((start + FOUR_HOURS_MS - 1) // FOUR_HOURS_MS) * FOUR_HOURS_MS


def _four_hour_frame_as_of(
    four_hour_history: pd.DataFrame,
    *,
    now_ms: int,
) -> tuple[pd.DataFrame, int | None]:
    """Return the precomputed closed 4h history that was available at now_ms."""
    if four_hour_history is None or four_hour_history.empty:
        return pd.DataFrame(), None
    latest_due = (int(now_ms) // FOUR_HOURS_MS) * FOUR_HOURS_MS - FOUR_HOURS_MS
    if latest_due < 0:
        return pd.DataFrame(), None
    # Never expose a 4h bar newer than the latest completed bucket at this trigger.
    available = four_hour_history[
        four_hour_history["open_time"].astype("int64") <= latest_due
    ].reset_index(drop=True)
    if available.empty:
        return available, None
    return available, int(available.iloc[-1]["open_time"])
