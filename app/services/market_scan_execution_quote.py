"""Immutable unadjusted quote evidence captured inside a market-scan snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from math import isfinite
from typing import Literal

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market import Kline, Quote
from app.models.market_scan import MarketScanMode, MarketScanResultItem
from app.models.market_scan_execution_quote import (
    MARKET_SCAN_EXECUTION_QUOTE_CONTRACT_VERSION,
    MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY,
    MARKET_SCAN_EXECUTION_QUOTE_SCHEMA_VERSION,
    MarketScanExecutionQuoteEvidence,
    market_scan_execution_quote_evidence_digest,
    verify_market_scan_execution_quote_evidence,
)
from app.models.paper_trading import PaperInstrumentMetadata, PaperTradeRuleProfile
from app.services.paper_trading_rules import assess_daily_tradeability, resolve_trade_rule_profile
from app.utils.clock import ASHARE_TIMEZONE


def build_market_scan_execution_quote_evidence(
    item: MarketScanResultItem,
    quote: Quote,
    *,
    mode: MarketScanMode,
    quote_date: date | str,
    captured_at: str,
) -> dict[str, object]:
    """Normalize one frozen provider quote without claiming exchange authority."""

    effective_date = _date_text(quote_date)
    if item.symbol != f"{quote.code}.{quote.market}":
        raise ValueError("execution quote does not match scan item")
    event_at = _aware_timestamp_text(quote.timestamp, "quote.timestamp")
    captured = _aware_timestamp_text(captured_at, "captured_at")
    list_date = _normalized_list_date(item.list_date)
    profile = _quote_rule_profile(item, effective_date, list_date)
    bar = _quote_bar(quote, profile, effective_date)
    instrument = _quote_instrument(item, effective_date, list_date, profile)
    payload = _quote_evidence_payload(
        item=item,
        quote=quote,
        mode=mode,
        effective_date=effective_date,
        event_at=event_at,
        captured_at=captured,
        instrument=instrument,
        bar=bar,
    )
    payload["evidence_digest"] = market_scan_execution_quote_evidence_digest(payload)
    return MarketScanExecutionQuoteEvidence.model_validate(payload).model_dump(mode="json")


def _quote_rule_profile(
    item: MarketScanResultItem,
    effective_date: str,
    list_date: str | None,
) -> PaperTradeRuleProfile:
    metadata = PaperInstrumentMetadata(
        symbol=item.symbol,
        name=item.name,
        market=item.market,
        list_date=list_date,
        is_st=item.is_st,
        source=item.metadata_source,
        status_effective_date=effective_date,
    )
    return resolve_trade_rule_profile(
        item.symbol,
        date.fromisoformat(effective_date),
        metadata,
    )


def _quote_bar(
    quote: Quote,
    profile: PaperTradeRuleProfile,
    effective_date: str,
) -> dict[str, object]:
    session_status = _session_status(quote)
    buy_state, sell_state = _open_states(
        quote,
        session_status,
        profile,
        session_date=effective_date,
    )
    return {
        "adjustment_mode": "none",
        "open": float(quote.open),
        "high": float(quote.high),
        "low": float(quote.low),
        "close": float(quote.price),
        "previous_close_reference": float(quote.prev_close),
        "volume": float(quote.volume),
        "amount": float(quote.amount),
        "session_status": session_status,
        "buy_open_state": buy_state,
        "sell_open_state": sell_state,
        "corporate_action_status": "unknown",
    }


def _quote_instrument(
    item: MarketScanResultItem,
    effective_date: str,
    list_date: str | None,
    profile: PaperTradeRuleProfile,
) -> dict[str, object]:
    profile_payload = profile.model_dump(mode="json")
    return {
        "board": _board(item.symbol),
        "listing_status": _listing_status(list_date, effective_date),
        "list_date": list_date,
        "is_st": item.is_st,
        "metadata_source": item.metadata_source,
        "metadata_effective_date": effective_date,
        "rule_profile_id": profile.profile_id,
        "rule_profile_quality": profile.quality,
        "rule_profile_digest": sha256_hex(canonical_json_bytes(profile_payload)),
        "rule_source_url": profile.source_url,
    }


def _quote_evidence_payload(
    *,
    item: MarketScanResultItem,
    quote: Quote,
    mode: MarketScanMode,
    effective_date: str,
    event_at: str,
    captured_at: str,
    instrument: Mapping[str, object],
    bar: Mapping[str, object],
) -> dict[str, object]:
    quote_identity = _quote_identity(
        symbol=item.symbol,
        quote_date=effective_date,
        provider_event_at=event_at,
        source=quote.source,
        fallback_used=bool(quote.fallback_used),
        bar=bar,
    )
    return {
        "schema_version": MARKET_SCAN_EXECUTION_QUOTE_SCHEMA_VERSION,
        "contract_version": MARKET_SCAN_EXECUTION_QUOTE_CONTRACT_VERSION,
        "mode": mode,
        "symbol": item.symbol,
        "code": item.code,
        "market": item.market,
        "quote_date": effective_date,
        "provider_event_at": event_at,
        "captured_at": captured_at,
        "source": quote.source,
        "source_authority": "vendor_normalized_quote",
        "fallback_used": bool(quote.fallback_used),
        "instrument": dict(instrument),
        "bar": dict(bar),
        "normalized_quote_digest": sha256_hex(canonical_json_bytes(quote_identity)),
    }


def _quote_identity(
    *,
    symbol: str,
    quote_date: str,
    provider_event_at: str,
    source: str,
    fallback_used: bool,
    bar: Mapping[str, object],
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "quote_date": quote_date,
        "provider_event_at": provider_event_at,
        "source": source,
        "fallback_used": fallback_used,
        "bar": dict(bar),
    }


def _session_status(quote: Quote) -> Literal["trading", "suspended", "unknown"]:
    prices = tuple(float(value) for value in (quote.open, quote.high, quote.low, quote.price))
    if all(value > 0 and isfinite(value) for value in prices) and quote.volume > 0 and quote.amount > 0:
        return "trading"
    if quote.volume == 0 and quote.amount == 0:
        return "suspended"
    return "unknown"


def _open_states(
    quote: Quote,
    session_status: Literal["trading", "suspended", "unknown"],
    profile: PaperTradeRuleProfile,
    *,
    session_date: str,
) -> tuple[
    Literal["executable", "locked_limit", "unavailable", "unknown"],
    Literal["executable", "locked_limit", "unavailable", "unknown"],
]:
    if session_status == "suspended":
        return "unavailable", "unavailable"
    if session_status != "trading" or getattr(profile, "quality", None) != "ok":
        return "unknown", "unknown"
    row = Kline(
        date=session_date,
        open=quote.open,
        high=quote.high,
        low=quote.low,
        close=quote.price,
        volume=quote.volume,
        adjustment_mode="none",
    )
    assessment = assess_daily_tradeability(
        row,
        previous_close=float(quote.prev_close),
        profile=profile,
    )
    buy: Literal["executable", "locked_limit"] = "executable" if assessment.can_buy else "locked_limit"
    sell: Literal["executable", "locked_limit"] = "executable" if assessment.can_sell else "locked_limit"
    return buy, sell


def _board(symbol: str) -> Literal["main", "chinext", "star", "beijing"]:
    code, market = symbol.split(".")
    if market == "BJ":
        return "beijing"
    if market == "SH" and code.startswith(("688", "689")):
        return "star"
    if market == "SZ" and code.startswith(("300", "301")):
        return "chinext"
    return "main"


def _listing_status(
    list_date: str | None,
    effective_date: str,
) -> Literal["listed", "not_listed", "unknown"]:
    if list_date is None:
        return "unknown"
    listed = _date(list_date, "list_date")
    return "listed" if listed <= date.fromisoformat(effective_date) else "not_listed"


def _normalized_list_date(value: object) -> str | None:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return _date(text, "list_date").isoformat()
    except ValueError:
        return None


def _date_text(value: date | str) -> str:
    parsed = value if isinstance(value, date) else _date(value, "quote_date")
    return parsed.isoformat()


def _date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _aware_timestamp_text(value: object, label: str) -> str:
    text = str(value or "").strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ASHARE_TIMEZONE)
    return parsed.isoformat()


def _timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include timezone offset")
    return parsed


__all__ = [
    "MARKET_SCAN_EXECUTION_QUOTE_CONTRACT_VERSION",
    "MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY",
    "MARKET_SCAN_EXECUTION_QUOTE_SCHEMA_VERSION",
    "MarketScanExecutionQuoteEvidence",
    "build_market_scan_execution_quote_evidence",
    "market_scan_execution_quote_evidence_digest",
    "verify_market_scan_execution_quote_evidence",
]
