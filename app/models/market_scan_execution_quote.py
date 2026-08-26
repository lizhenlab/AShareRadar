"""Strict immutable contract for normalized market-scan execution quotes."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_scan import MarketScanMode
from app.utils.clock import ASHARE_TIMEZONE


MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY = "execution_quote_evidence"
MARKET_SCAN_EXECUTION_QUOTE_SCHEMA_VERSION = "market-scan-execution-quote-evidence-v1"
MARKET_SCAN_EXECUTION_QUOTE_CONTRACT_VERSION = "published-scan-normalized-unadjusted-quote-v1"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class MarketScanExecutionQuoteBar(_StrictModel):
    adjustment_mode: Literal["none"] = "none"
    open: float = Field(ge=0)
    high: float = Field(ge=0)
    low: float = Field(ge=0)
    close: float = Field(ge=0)
    previous_close_reference: float = Field(gt=0)
    volume: float = Field(ge=0)
    amount: float = Field(ge=0)
    session_status: Literal["trading", "suspended", "unknown"]
    buy_open_state: Literal["executable", "locked_limit", "unavailable", "unknown"]
    sell_open_state: Literal["executable", "locked_limit", "unavailable", "unknown"]
    corporate_action_status: Literal["unknown"] = "unknown"

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        prices = (self.open, self.high, self.low, self.close)
        if self.session_status == "trading":
            if any(value <= 0 for value in prices) or self.volume <= 0 or self.amount <= 0:
                raise ValueError("trading execution quote requires positive OHLCV/amount")
            if self.high < max(self.open, self.close, self.low):
                raise ValueError("execution quote high conflicts with OHLC")
            if self.low > min(self.open, self.close, self.high):
                raise ValueError("execution quote low conflicts with OHLC")
        elif self.session_status == "suspended":
            if self.volume != 0 or self.amount != 0:
                raise ValueError("suspended execution quote must have zero volume and amount")
            if self.buy_open_state != "unavailable" or self.sell_open_state != "unavailable":
                raise ValueError("suspended execution quote must mark both sides unavailable")
        return self


class MarketScanExecutionInstrumentState(_StrictModel):
    board: Literal["main", "chinext", "star", "beijing"]
    listing_status: Literal["listed", "not_listed", "unknown"]
    list_date: str | None = None
    is_st: bool
    metadata_source: str | None = None
    metadata_effective_date: str
    rule_profile_id: str
    rule_profile_quality: Literal["ok", "degraded"]
    rule_profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule_source_url: str

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        effective = _date(self.metadata_effective_date, "metadata_effective_date")
        if self.list_date is not None:
            listed = _date(self.list_date, "list_date")
            if self.listing_status == "listed" and listed > effective:
                raise ValueError("listed instrument has a future list date")
        return self


class MarketScanExecutionQuoteEvidence(_StrictModel):
    schema_version: Literal["market-scan-execution-quote-evidence-v1"] = "market-scan-execution-quote-evidence-v1"
    contract_version: Literal["published-scan-normalized-unadjusted-quote-v1"] = "published-scan-normalized-unadjusted-quote-v1"
    mode: MarketScanMode
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    code: str = Field(pattern=r"^\d{6}$")
    market: Literal["SH", "SZ", "BJ"]
    quote_date: str
    provider_event_at: str
    captured_at: str
    source: str = Field(min_length=1)
    source_authority: Literal["vendor_normalized_quote"] = "vendor_normalized_quote"
    fallback_used: bool
    instrument: MarketScanExecutionInstrumentState
    bar: MarketScanExecutionQuoteBar
    normalized_quote_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        quote_date = _date(self.quote_date, "quote_date")
        event = _timestamp(self.provider_event_at, "provider_event_at")
        captured = _timestamp(self.captured_at, "captured_at")
        if self.symbol != f"{self.code}.{self.market}":
            raise ValueError("execution quote symbol/code/market identity mismatch")
        if event.astimezone(ASHARE_TIMEZONE).date() != quote_date:
            raise ValueError("execution quote provider event date mismatch")
        if captured < event:
            raise ValueError("execution quote cannot be captured before provider event")
        if self.instrument.metadata_effective_date != self.quote_date:
            raise ValueError("execution quote metadata is not effective on quote date")
        if self.normalized_quote_digest != _normalized_quote_digest(self):
            raise ValueError("execution normalized quote digest mismatch")
        if self.evidence_digest != market_scan_execution_quote_evidence_digest(self):
            raise ValueError("execution quote evidence digest mismatch")
        return self


def verify_market_scan_execution_quote_evidence(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("execution quote evidence must be an object")
    return MarketScanExecutionQuoteEvidence.model_validate(dict(value)).model_dump(mode="json")


def market_scan_execution_quote_evidence_digest(
    value: BaseModel | Mapping[str, object],
) -> str:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = deepcopy(dict(value))
    else:
        raise TypeError("execution quote evidence must be a model or mapping")
    payload.pop("evidence_digest", None)
    return sha256_hex(canonical_json_bytes(payload))


def _normalized_quote_digest(evidence: MarketScanExecutionQuoteEvidence) -> str:
    return sha256_hex(
        canonical_json_bytes(
            _quote_identity(
                symbol=evidence.symbol,
                quote_date=evidence.quote_date,
                provider_event_at=evidence.provider_event_at,
                source=evidence.source,
                fallback_used=evidence.fallback_used,
                bar=evidence.bar.model_dump(mode="json"),
            )
        )
    )


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


def _date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


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
    "MarketScanExecutionInstrumentState",
    "MarketScanExecutionQuoteBar",
    "MarketScanExecutionQuoteEvidence",
    "market_scan_execution_quote_evidence_digest",
    "verify_market_scan_execution_quote_evidence",
]
