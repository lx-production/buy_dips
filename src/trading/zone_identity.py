from __future__ import annotations

import json
import hashlib

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from typing import Any

import numpy as np
import pandas as pd

from .constants import DETECTOR_VERSION, EXCHANGE, FINGERPRINT_VERSION, SYMBOL


PRICE_QUANTUM = Decimal("0.00000001")
SUPPORTED_SOURCE_TIMEFRAMES = frozenset({"4h", "1d"})
SUPPORTED_BOUNDS_STYLES = frozenset({"body", "support_floor", "local_reaction"})


class ZoneIdentityError(ValueError):
    pass


@dataclass
class ZoneFingerprintCache:
    """Reuse source-time resolution and zf1 hashes when a zone revision does not change."""

    source_times: dict[tuple[str, tuple[int, ...]], list[int]] = field(default_factory=dict)
    enrichments: dict[tuple[Any, ...], dict[str, Any]] = field(default_factory=dict)


# Quantize a zone price so lineage and revision hashes stay deterministic.
def canonical_price(value: Any) -> str:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ZoneIdentityError(f"Invalid zone price: {value!r}") from exc
    if not price.is_finite():
        raise ZoneIdentityError(f"Invalid zone price: {value!r}")
    return format(price.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN), ".8f")


# Deduplicate and sort source candle open times used by the revision hash.
def canonical_source_open_times(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ZoneIdentityError("source_open_times must be a non-empty list")
    times: set[int] = set()
    for value in values:
        if isinstance(value, bool):
            raise ZoneIdentityError("source_open_times must contain integer Unix milliseconds")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ZoneIdentityError("source_open_times must contain integer Unix milliseconds") from exc
        if parsed < 0 or str(value).strip() != str(parsed):
            raise ZoneIdentityError("source_open_times must contain non-negative exact integers")
        times.add(parsed)
    return sorted(times)


# Map detector source_indexes onto open_time values from the matching OHLC frame or array.
def resolve_source_open_times(
    zone: dict[str, Any],
    source_df: pd.DataFrame | None = None,
    *,
    open_times: np.ndarray | None = None,
) -> list[int]:
    if open_times is None:
        if source_df is None or source_df.empty or "open_time" not in source_df.columns:
            raise ZoneIdentityError("source candle frame is missing open_time data")
        open_times = np.asarray(source_df["open_time"].to_numpy())
    return _resolve_source_open_times_from_array(zone.get("source_indexes"), open_times)


# Direct integer lookup of source open times; used by the fingerprint hot path.
def _resolve_source_open_times_from_array(indexes: Any, open_times: np.ndarray) -> list[int]:
    if not isinstance(indexes, (list, tuple)) or not indexes:
        raise ZoneIdentityError("zone source_indexes must be non-empty")
    count = len(open_times)
    resolved: list[int] = []
    for raw_index in indexes:
        if isinstance(raw_index, bool):
            raise ZoneIdentityError("source index must be an integer")
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ZoneIdentityError(f"Invalid source index: {raw_index!r}") from exc
        if str(raw_index).strip() != str(index) or index < 0 or index >= count:
            raise ZoneIdentityError(f"Source index is out of range: {raw_index!r}")
        value = open_times[index]
        if pd.isna(value):
            raise ZoneIdentityError(f"Source candle {index} has no open_time")
        resolved.append(int(value))
    return canonical_source_open_times(resolved)


def canonical_bounds_style(value: Any) -> str:
    """Normalize the band-anchor style used by the stable lineage identity."""
    style = "body" if value is None or value == "" else str(value)
    if style not in SUPPORTED_BOUNDS_STYLES:
        raise ZoneIdentityError(f"Unsupported zone bounds style: {value!r}")
    return style


def make_zone_lineage_id(
    *,
    low: Any,
    high: Any,
    source_timeframe: str,
    bounds_style: Any = "body",
    exchange: str = EXCHANGE,
    symbol: str = SYMBOL,
    detector_version: str = DETECTOR_VERSION,
) -> str:
    """Hash the stable shelf identity: band, timeframe, and bounds style.

    Source candles are omitted on purpose. A later touch on the same band must
    keep this id so chart segments stay continuous and the 24h cooldown does
    not reset just because the zone gained evidence.
    """
    if not exchange or not symbol or not detector_version:
        raise ZoneIdentityError("Fingerprint scope fields must be non-empty")
    if source_timeframe not in SUPPORTED_SOURCE_TIMEFRAMES:
        raise ZoneIdentityError(f"Unsupported zone source timeframe: {source_timeframe}")
    payload = {
        "bounds_style": canonical_bounds_style(bounds_style),
        "detector_version": detector_version,
        "exchange": exchange,
        "fingerprint_version": FINGERPRINT_VERSION,
        "high": canonical_price(high),
        "identity": "lineage",
        "low": canonical_price(low),
        "source_timeframe": source_timeframe,
        "symbol": symbol,
    }
    return _hash_identity(payload)


def make_zone_fingerprint(
    *,
    low: Any,
    high: Any,
    source_open_times: Any,
    source_timeframe: str,
    exchange: str = EXCHANGE,
    symbol: str = SYMBOL,
    detector_version: str = DETECTOR_VERSION,
) -> str:
    """Hash one zone revision, including source_open_times, for audit and cache."""
    if not exchange or not symbol or not detector_version:
        raise ZoneIdentityError("Fingerprint scope fields must be non-empty")
    if source_timeframe not in SUPPORTED_SOURCE_TIMEFRAMES:
        raise ZoneIdentityError(f"Unsupported zone source timeframe: {source_timeframe}")
    times = canonical_source_open_times(source_open_times)
    payload = {
        "detector_version": detector_version,
        "exchange": exchange,
        "fingerprint_version": FINGERPRINT_VERSION,
        "high": canonical_price(high),
        "low": canonical_price(low),
        "source_open_times": times,
        "source_timeframe": source_timeframe,
        "symbol": symbol,
    }
    return _hash_identity(payload)


def fingerprint_zone(
    zone: dict[str, Any],
    *,
    four_hour_df: pd.DataFrame | None = None,
    daily_df: pd.DataFrame | None = None,
    four_hour_open_times: np.ndarray | None = None,
    daily_open_times: np.ndarray | None = None,
    cache: ZoneFingerprintCache | None = None,
    exchange: str = EXCHANGE,
    symbol: str = SYMBOL,
    detector_version: str = DETECTOR_VERSION,
) -> dict[str, Any]:
    """Attach lineage, revision, and resolved source times to one detector zone.

    `fingerprint` / `zone_lineage_id` stay stable when touches are added.
    `revision_fingerprint` changes with `source_open_times` for audit/cache.
    """
    source_timeframe = str(zone.get("source_timeframe", "4h"))
    if source_timeframe not in SUPPORTED_SOURCE_TIMEFRAMES:
        raise ZoneIdentityError(f"Unsupported zone source timeframe: {source_timeframe}")
    source_open_times = _cached_source_open_times(
        zone,
        source_timeframe=source_timeframe,
        four_hour_df=four_hour_df,
        daily_df=daily_df,
        four_hour_open_times=four_hour_open_times,
        daily_open_times=daily_open_times,
        cache=cache,
    )
    bounds_style = canonical_bounds_style(zone.get("bounds_style", "body"))
    enrichment_key = (
        source_timeframe,
        bounds_style,
        canonical_price(zone.get("low")),
        canonical_price(zone.get("high")),
        tuple(source_open_times),
        exchange,
        symbol,
        detector_version,
    )
    if cache is not None:
        cached = cache.enrichments.get(enrichment_key)
        if cached is not None:
            enriched = dict(zone)
            enriched.update(cached)
            return enriched
    lineage_id = make_zone_lineage_id(
        low=zone.get("low"),
        high=zone.get("high"),
        source_timeframe=source_timeframe,
        bounds_style=bounds_style,
        exchange=exchange,
        symbol=symbol,
        detector_version=detector_version,
    )
    revision = make_zone_fingerprint(
        low=zone.get("low"),
        high=zone.get("high"),
        source_open_times=source_open_times,
        source_timeframe=source_timeframe,
        exchange=exchange,
        symbol=symbol,
        detector_version=detector_version,
    )
    identity_fields = {
        "bounds_style": bounds_style,
        "source_timeframe": source_timeframe,
        "source_open_times": source_open_times,
        "zone_source_time": max(source_open_times),
        "fingerprint_version": FINGERPRINT_VERSION,
        "zone_lineage_id": lineage_id,
        "revision_fingerprint": revision,
        "fingerprint": lineage_id,
    }
    if cache is not None:
        cache.enrichments[enrichment_key] = identity_fields
    enriched = dict(zone)
    enriched.update(identity_fields)
    return enriched


# Resolve source times from arrays when possible, and cache by timeframe plus source indexes.
def _cached_source_open_times(
    zone: dict[str, Any],
    *,
    source_timeframe: str,
    four_hour_df: pd.DataFrame | None,
    daily_df: pd.DataFrame | None,
    four_hour_open_times: np.ndarray | None,
    daily_open_times: np.ndarray | None,
    cache: ZoneFingerprintCache | None,
) -> list[int]:
    indexes = zone.get("source_indexes")
    cache_key: tuple[str, tuple[int, ...]] | None = None
    if isinstance(indexes, (list, tuple)) and indexes:
        try:
            cache_key = (source_timeframe, tuple(int(value) for value in indexes))
        except (TypeError, ValueError):
            cache_key = None
    if cache is not None and cache_key is not None:
        cached_times = cache.source_times.get(cache_key)
        if cached_times is not None:
            return cached_times
    open_times = daily_open_times if source_timeframe == "1d" else four_hour_open_times
    source_df = daily_df if source_timeframe == "1d" else four_hour_df
    source_open_times = resolve_source_open_times(zone, source_df, open_times=open_times)
    if cache is not None and cache_key is not None:
        cache.source_times[cache_key] = source_open_times
    return source_open_times


def _hash_identity(payload: dict[str, Any]) -> str:
    """SHA-256 a canonical JSON payload and prefix it with the zf1 scheme."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{FINGERPRINT_VERSION}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
