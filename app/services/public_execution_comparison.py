"""Compare retained public facts without inferring execution or rewriting signals."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
import re

from app.utils.clock import ASHARE_TIMEZONE


_PROVIDERS = frozenset({"baostock", "sse_public_notice", "szse_public_notice"})
_SOURCE_STATUSES = frozenset({"success", "error", "missing", "pending"})
_PAIR_STATUSES = ("reported", "missing", "pending", "unsupported", "error")
_MAX_PAIRS = 200_000
_MAX_DATES = 250
_MAX_SOURCE_ROWS = 20_000
_SYMBOL = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)")
_LIMITATIONS = (
    "public_facts_do_not_admit_official_execution",
    "vendor_trading_does_not_prove_order_execution",
    "notice_absence_does_not_prove_normal_trading",
    "missing_rows_do_not_prove_suspension",
    "collection_time_is_not_historical_availability",
    "reference_price_is_not_previous_actual_close",
    "no_inferred_limit_prices_or_corporate_actions",
)


@dataclass(frozen=True)
class _Source:
    evidence: dict[str, object]
    rows: dict[str, list[dict[str, object]]]


def compare_public_execution_facts(
    pairs: Sequence[tuple[str, str]], sources: Mapping[tuple[str, str], Mapping[str, object]], *, as_of: datetime,
) -> dict[str, object]:
    """Retain every requested pair, including unsupported, missing and future dates."""
    cutoff = _cutoff(as_of)
    requested = _requested_pairs(pairs)
    prepared = _prepare_sources(requested, sources, cutoff)
    rows = [_compare_pair(symbol, day, prepared, cutoff) for symbol, day in requested]
    counts = Counter(str(row["status"]) for row in rows)
    reported = counts["reported"]
    return {
        "schema_version": "public-execution-facts-comparison-v1", "as_of": cutoff.isoformat(),
        "status": "research_only" if rows and reported == len(rows) else "partial_research_only" if reported else "needs_source",
        "required_pair_count": len(rows), "pair_counts": {status: counts[status] for status in _PAIR_STATUSES},
        "rows": rows, "limitations": list(_LIMITATIONS), "official_execution_admitted": False,
        "point_in_time_verified": False, "provider_calls": 0, "runtime_writes": 0,
    }


def _cutoff(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("public facts require an aware observation cutoff")
    return value.astimezone(UTC)


def _day(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid public facts date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("noncanonical public facts date")
    return value


def _requested_pairs(pairs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    if len(pairs) > _MAX_PAIRS:
        raise ValueError("too many public facts pairs")
    result = []
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError("invalid public facts pair")
        symbol, day = pair
        if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
            raise ValueError("noncanonical public facts symbol")
        result.append((symbol, _day(day)))
    if len(set(result)) != len(result) or len({day for _, day in result}) > _MAX_DATES:
        raise ValueError("duplicate pairs or excessive public facts dates")
    return result


def _prepare_sources(
    pairs: Sequence[tuple[str, str]], sources: Mapping[tuple[str, str], Mapping[str, object]], cutoff: datetime,
) -> dict[tuple[str, str], _Source]:
    if not isinstance(sources, Mapping) or len(sources) > _MAX_DATES * len(_PROVIDERS):
        raise ValueError("invalid or excessive public facts sources")
    for key in sources:
        if not isinstance(key, tuple) or len(key) != 2 or key[0] not in _PROVIDERS:
            raise ValueError("invalid public facts source identity")
        _day(key[1])
    days = {day for _, day in pairs}
    return {(provider, day): _prepare_source(provider, day, sources.get((provider, day)), cutoff)
            for day in sorted(days) for provider in sorted(_PROVIDERS)}


def _unavailable(status: str, reason: str, *, observed_at: str | None = None, digest: str | None = None) -> _Source:
    return _Source({"status": status, "reason": reason, "observed_at": observed_at, "digest": digest}, {})


def _prepare_source(provider: str, day: str, value: Mapping[str, object] | None, cutoff: datetime) -> _Source:
    if day > cutoff.astimezone(ASHARE_TIMEZONE).date().isoformat():
        return _unavailable("pending", "session_after_as_of")
    if value is None:
        return _unavailable("missing", "source_not_collected")
    evidence = _source_evidence(value, cutoff)
    if evidence["status"] != "success":
        return _Source(evidence, {})
    if provider == "baostock" and not _daily_observation_completed(day, evidence):
        return _Source({**evidence, "status": "error", "reason": "source_observed_before_session_close"}, {})
    try:
        rows = _index_rows(provider, day, value.get("rows"))
    except (TypeError, ValueError):
        return _Source({**evidence, "status": "error", "reason": "invalid_normalized_source_rows"}, {})
    return _Source(evidence, rows)


def _daily_observation_completed(day: str, evidence: Mapping[str, object]) -> bool:
    observed = _observed_at(evidence["observed_at"])
    close = datetime.combine(date.fromisoformat(day), time(15), ASHARE_TIMEZONE)
    return observed is not None and observed >= close


def _source_evidence(value: Mapping[str, object], cutoff: datetime) -> dict[str, object]:
    if not isinstance(value, Mapping) or not isinstance(value.get("status"), str) or value["status"] not in _SOURCE_STATUSES:
        return _unavailable("error", "invalid_source_status").evidence
    status = str(value["status"])
    digest = value.get("digest")
    if digest is not None and (not isinstance(digest, str) or not digest or len(digest) > 256):
        return _unavailable("error", "invalid_source_digest").evidence
    observed = _observed_at(value.get("observed_at"))
    if observed is None and (status == "success" or value.get("observed_at") is not None):
        return _unavailable("error", "invalid_source_observed_at", digest=digest).evidence
    if observed is not None and observed > cutoff:
        return _unavailable("pending", "source_observed_after_as_of", observed_at=observed.isoformat(), digest=digest).evidence
    return _available_evidence(status, digest, observed)


def _observed_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        return None
    return stamp.astimezone(UTC)


def _available_evidence(status: str, digest: object, observed: datetime | None) -> dict[str, object]:
    if status == "success" and digest is None:
        return _unavailable("error", "source_digest_unavailable", observed_at=observed.isoformat() if observed else None).evidence
    return {"status": status, "reason": None if status == "success" else "source_" + status,
            "observed_at": observed.isoformat() if observed else None, "digest": digest}


def _index_rows(provider: str, day: str, value: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(value, list) or len(value) >= _MAX_SOURCE_ROWS:
        raise ValueError("invalid or excessive normalized public facts rows")
    indexed: dict[str, list[dict[str, object]]] = {}
    for row in value:
        symbol = _row_symbol(provider, day, row)
        if symbol is None:
            continue
        bucket = indexed.setdefault(symbol, [])
        if provider == "baostock" and bucket:
            raise ValueError("duplicate normalized public facts rows")
        bucket.append(dict(row))
    return indexed


def _row_symbol(provider: str, day: str, row: object) -> str | None:
    if not isinstance(row, Mapping):
        raise ValueError("invalid normalized public facts row")
    if row.get("session_date") != day:
        raise ValueError("normalized public facts row date mismatch")
    symbol = row.get("symbol")
    if provider != "baostock":
        return _notice_symbol(provider, row)
    if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None or symbol.endswith(".BJ"):
        raise ValueError("invalid normalized public facts row symbol")
    _validate_vendor_row(row)
    return symbol


def _notice_symbol(provider: str, row: Mapping[str, object]) -> str | None:
    market = "SH" if provider == "sse_public_notice" else "SZ"
    if (row.get("provider"), row.get("market")) != (provider, market):
        raise ValueError("normalized notice provider mismatch")
    symbol = row.get("symbol")
    if symbol is None and row.get("instrument_type") == "other_or_unknown":
        return None
    if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None or not symbol.endswith("." + market):
        raise ValueError("normalized notice exchange mismatch")
    if row.get("instrument_type") != "stock":
        raise ValueError("normalized notice instrument mismatch")
    return symbol


def _validate_vendor_row(row: Mapping[str, object]) -> None:
    state = row.get("trade_state")
    if not isinstance(state, str) or state not in {"trading", "suspended"} or type(row.get("is_st")) is not bool:
        raise ValueError("invalid normalized vendor state")
    prices = {key: _number(row.get(key), positive=True) for key in ("open", "high", "low", "close", "reference_price")}
    volumes = [row.get(key) for key in ("volume", "amount")]
    for value in volumes:
        if value is not None or state != "suspended":
            _number(value, positive=False)
    if not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]:
        raise ValueError("invalid normalized vendor prices")
    if state == "suspended":
        _suspended_values(prices, volumes)
    _vendor_units(row)


def _suspended_values(prices: Mapping[str, float], volumes: Sequence[object]) -> None:
    if len({prices[key] for key in ("open", "high", "low", "close")}) != 1 or any(value not in (0, None) for value in volumes):
        raise ValueError("invalid normalized vendor suspension values")


def _vendor_units(row: Mapping[str, object]) -> None:
    expected = {"adjustment_mode": "unadjusted", "volume_unit": "shares", "amount_unit": "CNY"}
    if any(key in row and row[key] != value for key, value in expected.items()):
        raise ValueError("invalid normalized vendor price basis or units")


def _number(value: object, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid normalized vendor numeric type")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("invalid normalized vendor numeric range") from None
    if not isfinite(number) or number < 0 or positive and number == 0:
        raise ValueError("invalid normalized vendor numeric range")
    return number


def _compare_pair(symbol: str, day: str, sources: Mapping[tuple[str, str], _Source], cutoff: datetime) -> dict[str, object]:
    result: dict[str, object] = {"symbol": symbol, "session_date": day, "reported_trade_state": "unknown",
                                "baostock_fact": None, "notices": [], "official_execution_admitted": False}
    if symbol.endswith(".BJ"):
        return {**result, "status": "unsupported", "reason": "exchange_not_supported", "notice_status": "unsupported", "source_evidence": {}}
    provider = "sse_public_notice" if symbol.endswith(".SH") else "szse_public_notice"
    vendor, notice = sources[("baostock", day)], sources[(provider, day)]
    evidence = {"baostock": dict(vendor.evidence), provider: dict(notice.evidence)}
    facts = vendor.rows.get(symbol, [])
    notices = [deepcopy(row) for row in notice.rows.get(symbol, [])]
    status, reason = _pair_status(vendor, facts)
    if day > cutoff.astimezone(ASHARE_TIMEZONE).date().isoformat():
        status, reason = "pending", "session_after_as_of"
    return {**result, "status": status, "reason": reason, "reported_trade_state": facts[0]["trade_state"] if facts else "unknown",
            "baostock_fact": dict(facts[0]) if facts else None, "notices": notices,
            "notice_status": _notice_status(notice, notices), "source_evidence": evidence}


def _pair_status(vendor: _Source, facts: Sequence[Mapping[str, object]]) -> tuple[str, object]:
    if vendor.evidence["status"] != "success":
        return str(vendor.evidence["status"]), vendor.evidence["reason"]
    return ("reported", None) if facts else ("missing", "vendor_symbol_row_missing")


def _notice_status(notice: _Source, rows: Sequence[Mapping[str, object]]) -> str:
    if notice.evidence["status"] != "success":
        return str(notice.evidence["status"])
    return "matched" if rows else "no_matching_notice"
