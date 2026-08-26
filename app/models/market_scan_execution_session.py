"""Digest-bound execution-session evidence for one published market scan.

The contract intentionally distinguishes a normalized vendor quote from an
exchange-authoritative trading-state record.  It is useful for forward data
collection and deterministic replay, but it cannot by itself authorize a
formal point-in-time execution study.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from math import isclose
import re
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_scan import MarketScanMode, MarketScanResultStatus
from app.models.market_scan_execution_quote import (
    MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY,
    MarketScanExecutionQuoteEvidence,
    verify_market_scan_execution_quote_evidence,
)
from app.utils.clock import ASHARE_TIMEZONE


MARKET_SCAN_EXECUTION_SESSION_SCHEMA_VERSION = "market-scan-execution-session-evidence-v1"
MARKET_SCAN_EXECUTION_SESSION_CONTRACT_VERSION = "published-market-scan-complete-result-session-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LIMITATION_ORDER = (
    "vendor_quote_not_exchange_authoritative",
    "corporate_action_evidence_unavailable",
    "source_snapshot_digest_unavailable",
    "incomplete_result_set",
    "quote_evidence_incomplete",
)


@dataclass(frozen=True)
class _SessionRunFacts:
    run_id: int
    mode: MarketScanMode
    quote_date: str
    run_as_of: str
    expected_result_count: int
    expected_success_count: int
    expected_missing_count: int
    expected_skipped_count: int
    snapshot_digest: str | None


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class MarketScanExecutionSessionRow(_StrictModel):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    code: str = Field(pattern=r"^\d{6}$")
    market: Literal["SH", "SZ", "BJ"]
    result_status: MarketScanResultStatus
    result_updated_at: str
    outer_quote_price: float | None = Field(default=None, gt=0)
    outer_quote_amount: float | None = Field(default=None, ge=0)
    outer_quote_timestamp: str | None = None
    outer_quote_observed_at: str | None = None
    outer_quote_source: str | None = None
    outer_quote_fallback_used: bool
    quote_evidence: MarketScanExecutionQuoteEvidence | None = None
    row_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_row(self) -> Self:
        if self.symbol != f"{self.code}.{self.market}":
            raise ValueError("execution session row symbol/code/market mismatch")
        _aware_timestamp(self.result_updated_at, "result_updated_at")
        if self.outer_quote_timestamp is not None:
            _aware_timestamp(self.outer_quote_timestamp, "outer_quote_timestamp")
        if self.outer_quote_observed_at is not None:
            _aware_timestamp(self.outer_quote_observed_at, "outer_quote_observed_at")
        if self.result_status == "success" and any(
            value is None
            for value in (
                self.outer_quote_price,
                self.outer_quote_amount,
                self.outer_quote_timestamp,
                self.outer_quote_observed_at,
                self.outer_quote_source,
            )
        ):
            raise ValueError("successful execution session row lacks persisted quote binding")
        if self.quote_evidence is not None:
            _validate_quote_binding(self)
        if self.row_digest != market_scan_execution_session_content_digest(self):
            raise ValueError("execution session row digest mismatch")
        return self


class MarketScanExecutionSessionEvidence(_StrictModel):
    schema_version: Literal["market-scan-execution-session-evidence-v1"] = "market-scan-execution-session-evidence-v1"
    contract_version: Literal["published-market-scan-complete-result-session-v1"] = "published-market-scan-complete-result-session-v1"
    canonical_published: Literal[True] = True
    run_id: int = Field(gt=0)
    mode: MarketScanMode
    quote_date: str
    run_as_of: str
    source_snapshot_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_snapshot_binding: Literal["verified_digest", "unbound_projection"]
    expected_result_count: int = Field(gt=0)
    expected_success_count: int = Field(ge=0)
    expected_missing_count: int = Field(ge=0)
    expected_skipped_count: int = Field(ge=0)
    observed_result_count: int = Field(ge=0)
    observed_success_count: int = Field(ge=0)
    observed_missing_count: int = Field(ge=0)
    observed_skipped_count: int = Field(ge=0)
    complete_result_set: bool
    quote_evidence_count: int = Field(ge=0)
    quote_evidence_coverage: float = Field(ge=0, le=1)
    fallback_quote_evidence_count: int = Field(ge=0)
    trading_quote_evidence_count: int = Field(ge=0)
    suspended_quote_evidence_count: int = Field(ge=0)
    unknown_quote_evidence_count: int = Field(ge=0)
    quote_source_authority: Literal["vendor_normalized_quote"] = "vendor_normalized_quote"
    official_authority_evidence_count: Literal[0] = 0
    corporate_action_complete_count: Literal[0] = 0
    formal_equivalent_pit: Literal[False] = False
    limitations: list[str] = Field(min_length=2)
    rows: list[MarketScanExecutionSessionRow]
    row_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_session(self) -> Self:
        quote_date = _iso_date(self.quote_date, "quote_date")
        run_as_of = _aware_timestamp(self.run_as_of, "run_as_of")
        complete = _validate_session_counts(self)
        evidence_rows = [row.quote_evidence for row in self.rows if row.quote_evidence is not None]
        _validate_session_evidence_summaries(self, evidence_rows)
        expected_binding = "verified_digest" if self.source_snapshot_digest is not None else "unbound_projection"
        if self.source_snapshot_binding != expected_binding:
            raise ValueError("execution session snapshot binding status mismatch")
        expected_limitations = _limitations(
            snapshot_bound=self.source_snapshot_digest is not None,
            complete_result_set=complete,
            quote_evidence_count=len(evidence_rows),
            expected_result_count=self.expected_result_count,
        )
        if self.limitations != expected_limitations:
            raise ValueError("execution session limitations are inconsistent")
        _validate_session_boundaries(self, evidence_rows, quote_date, run_as_of)
        if self.row_set_digest != _row_set_digest(self.rows):
            raise ValueError("execution session row-set digest mismatch")
        if self.evidence_digest != market_scan_execution_session_content_digest(self):
            raise ValueError("execution session evidence digest mismatch")
        return self


def _validate_session_counts(session: MarketScanExecutionSessionEvidence) -> bool:
    expected_counts = session.expected_success_count + session.expected_missing_count + session.expected_skipped_count
    observed_counts = session.observed_success_count + session.observed_missing_count + session.observed_skipped_count
    if expected_counts != session.expected_result_count:
        raise ValueError("execution session expected status counts do not conserve")
    if observed_counts != session.observed_result_count or len(session.rows) != session.observed_result_count:
        raise ValueError("execution session observed status counts do not conserve")
    symbols = [row.symbol for row in session.rows]
    if symbols != sorted(symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("execution session rows must be unique canonical symbols")
    actual_statuses = Counter(row.result_status for row in session.rows)
    expected_statuses = Counter(
        {
            "success": session.observed_success_count,
            "missing": session.observed_missing_count,
            "skipped": session.observed_skipped_count,
        }
    )
    if actual_statuses != expected_statuses:
        raise ValueError("execution session row statuses do not match observed counts")
    complete = (
        session.observed_result_count == session.expected_result_count
        and session.observed_success_count == session.expected_success_count
        and session.observed_missing_count == session.expected_missing_count
        and session.observed_skipped_count == session.expected_skipped_count
    )
    if session.complete_result_set is not complete:
        raise ValueError("execution session completeness flag is inconsistent")
    return complete


def _validate_session_evidence_summaries(
    session: MarketScanExecutionSessionEvidence,
    evidence_rows: Sequence[MarketScanExecutionQuoteEvidence],
) -> None:
    if session.quote_evidence_count != len(evidence_rows):
        raise ValueError("execution session quote evidence count mismatch")
    expected_coverage = len(evidence_rows) / session.expected_result_count
    if not isclose(session.quote_evidence_coverage, expected_coverage, rel_tol=0, abs_tol=1e-12):
        raise ValueError("execution session quote evidence coverage mismatch")
    states = Counter(item.bar.session_status for item in evidence_rows)
    if (
        session.trading_quote_evidence_count != states["trading"]
        or session.suspended_quote_evidence_count != states["suspended"]
        or session.unknown_quote_evidence_count != states["unknown"]
        or session.fallback_quote_evidence_count != sum(1 for item in evidence_rows if item.fallback_used)
    ):
        raise ValueError("execution session quote evidence summaries cannot be replayed")


def _validate_session_boundaries(
    session: MarketScanExecutionSessionEvidence,
    evidence_rows: Sequence[MarketScanExecutionQuoteEvidence],
    quote_date: date,
    run_as_of: datetime,
) -> None:
    for evidence in evidence_rows:
        if (
            evidence.mode != session.mode
            or evidence.quote_date != quote_date.isoformat()
            or _aware_timestamp(evidence.captured_at, "quote_evidence.captured_at") > run_as_of
        ):
            raise ValueError("execution quote evidence crosses the published run boundary")


def build_market_scan_execution_session_evidence(
    run: object,
    results: Sequence[object],
    *,
    canonical_published: bool,
) -> dict[str, object]:
    """Build an honest forward-capture projection from published result rows."""

    if canonical_published is not True:
        raise ValueError("execution session requires canonical_published=True")
    facts = _session_run_facts(_object_mapping(run, "run"))
    rows = _session_rows(results, facts)
    payload = _session_payload(facts, rows)
    payload["evidence_digest"] = market_scan_execution_session_content_digest(payload)
    return MarketScanExecutionSessionEvidence.model_validate(payload).model_dump(mode="json")


def _session_run_facts(raw_run: Mapping[str, object]) -> _SessionRunFacts:
    run_id = _positive_integer(raw_run.get("id", raw_run.get("run_id")), "run_id")
    mode = str(raw_run.get("mode") or "")
    if mode not in {"official", "intraday", "preopen"}:
        raise ValueError("execution session run mode is invalid")
    quote_date = _iso_date_text(raw_run.get("quote_date") or raw_run.get("data_date"), "quote_date")
    run_as_of = _aware_timestamp_text(raw_run.get("as_of"), "run_as_of")
    expected_success = _nonnegative_integer(raw_run.get("success_count"), "success_count")
    expected_skipped = _nonnegative_integer(raw_run.get("skipped_count", 0), "skipped_count")
    expected_result_count = _positive_integer(raw_run.get("total_count"), "total_count")
    expected_missing = _nonnegative_integer(
        raw_run.get(
            "missing_count",
            expected_result_count - expected_success - expected_skipped,
        ),
        "missing_count",
    )
    return _SessionRunFacts(
        run_id=run_id,
        mode=cast(MarketScanMode, mode),
        quote_date=quote_date,
        run_as_of=run_as_of,
        expected_result_count=expected_result_count,
        expected_success_count=expected_success,
        expected_missing_count=expected_missing,
        expected_skipped_count=expected_skipped,
        snapshot_digest=_optional_sha256(raw_run.get("snapshot_digest")),
    )


def _session_rows(
    results: Sequence[object],
    facts: _SessionRunFacts,
) -> list[dict[str, object]]:
    rows = [
        _build_row(
            _object_mapping(item, "results[]"),
            run_id=facts.run_id,
            mode=facts.mode,
            quote_date=facts.quote_date,
        )
        for item in results
    ]
    rows.sort(key=lambda item: str(item["symbol"]))
    return rows


def _session_summary_fields(
    facts: _SessionRunFacts,
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    statuses = Counter(str(item["result_status"]) for item in rows)
    evidence_values = [cast(Mapping[str, object], item["quote_evidence"]) for item in rows if item["quote_evidence"] is not None]
    evidence_states = Counter(str(cast(Mapping[str, object], item["bar"])["session_status"]) for item in evidence_values)
    complete = (
        len(rows) == facts.expected_result_count
        and statuses["success"] == facts.expected_success_count
        and statuses["missing"] == facts.expected_missing_count
        and statuses["skipped"] == facts.expected_skipped_count
    )
    return {
        "observed_result_count": len(rows),
        "observed_success_count": statuses["success"],
        "observed_missing_count": statuses["missing"],
        "observed_skipped_count": statuses["skipped"],
        "complete_result_set": complete,
        "quote_evidence_count": len(evidence_values),
        "quote_evidence_coverage": len(evidence_values) / facts.expected_result_count,
        "fallback_quote_evidence_count": sum(bool(item["fallback_used"]) for item in evidence_values),
        "trading_quote_evidence_count": evidence_states["trading"],
        "suspended_quote_evidence_count": evidence_states["suspended"],
        "unknown_quote_evidence_count": evidence_states["unknown"],
        "limitations": _limitations(
            snapshot_bound=facts.snapshot_digest is not None,
            complete_result_set=complete,
            quote_evidence_count=len(evidence_values),
            expected_result_count=facts.expected_result_count,
        ),
    }


def _session_payload(
    facts: _SessionRunFacts,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": MARKET_SCAN_EXECUTION_SESSION_SCHEMA_VERSION,
        "contract_version": MARKET_SCAN_EXECUTION_SESSION_CONTRACT_VERSION,
        "canonical_published": True,
        "run_id": facts.run_id,
        "mode": facts.mode,
        "quote_date": facts.quote_date,
        "run_as_of": facts.run_as_of,
        "source_snapshot_digest": facts.snapshot_digest,
        "source_snapshot_binding": ("verified_digest" if facts.snapshot_digest is not None else "unbound_projection"),
        "expected_result_count": facts.expected_result_count,
        "expected_success_count": facts.expected_success_count,
        "expected_missing_count": facts.expected_missing_count,
        "expected_skipped_count": facts.expected_skipped_count,
        "quote_source_authority": "vendor_normalized_quote",
        "official_authority_evidence_count": 0,
        "corporate_action_complete_count": 0,
        "formal_equivalent_pit": False,
        "rows": rows,
        "row_set_digest": _row_set_digest(rows),
    }
    payload.update(_session_summary_fields(facts, rows))
    return payload


def verify_market_scan_execution_session_evidence(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("execution session evidence must be an object")
    return MarketScanExecutionSessionEvidence.model_validate(dict(value)).model_dump(mode="json")


def market_scan_execution_session_content_digest(
    value: BaseModel | Mapping[str, object],
) -> str:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = deepcopy(dict(value))
    else:
        raise TypeError("execution session evidence must be a model or mapping")
    payload.pop("row_digest", None)
    payload.pop("evidence_digest", None)
    return sha256_hex(canonical_json_bytes(payload))


def _build_row(
    item: Mapping[str, object],
    *,
    run_id: int,
    mode: MarketScanMode,
    quote_date: str,
) -> dict[str, object]:
    symbol, code, market, status = _result_identity(item, run_id)
    evidence = _result_quote_evidence(
        item,
        symbol=symbol,
        mode=mode,
        quote_date=quote_date,
    )
    payload = _result_row_payload(item, symbol, code, market, status, evidence)
    payload["row_digest"] = market_scan_execution_session_content_digest(payload)
    return MarketScanExecutionSessionRow.model_validate(payload).model_dump(mode="json")


def _result_identity(
    item: Mapping[str, object],
    run_id: int,
) -> tuple[str, str, str, MarketScanResultStatus]:
    item_run_id = item.get("run_id")
    if item_run_id is not None and _positive_integer(item_run_id, "result.run_id") != run_id:
        raise ValueError("execution session result run_id mismatch")
    symbol = str(item.get("symbol") or "")
    code = str(item.get("code") or symbol.split(".", 1)[0])
    market = str(item.get("market") or symbol.rsplit(".", 1)[-1])
    status = str(item.get("status") or "")
    if status not in {"success", "missing", "skipped"}:
        raise ValueError("execution session result status is invalid")
    return symbol, code, market, cast(MarketScanResultStatus, status)


def _result_quote_evidence(
    item: Mapping[str, object],
    *,
    symbol: str,
    mode: MarketScanMode,
    quote_date: str,
) -> dict[str, object] | None:
    details = item.get("score_details")
    raw_evidence = details.get(MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY) if isinstance(details, Mapping) else None
    evidence = verify_market_scan_execution_quote_evidence(raw_evidence) if raw_evidence is not None else None
    if evidence is not None and (evidence["symbol"] != symbol or evidence["mode"] != mode or evidence["quote_date"] != quote_date):
        raise ValueError("execution quote evidence does not bind result/run identity")
    return evidence


def _result_row_payload(
    item: Mapping[str, object],
    symbol: str,
    code: str,
    market: str,
    status: MarketScanResultStatus,
    evidence: Mapping[str, object] | None,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "code": code,
        "market": market,
        "result_status": status,
        "result_updated_at": _aware_timestamp_text(
            item.get("updated_at") or item.get("quote_observed_at") or item.get("quote_timestamp"),
            "result_updated_at",
        ),
        "outer_quote_price": _optional_float(item.get("price")),
        "outer_quote_amount": _optional_float(item.get("amount")),
        "outer_quote_timestamp": _optional_aware_timestamp_text(item.get("quote_timestamp"), "outer_quote_timestamp"),
        "outer_quote_observed_at": _optional_aware_timestamp_text(item.get("quote_observed_at"), "outer_quote_observed_at"),
        "outer_quote_source": _optional_text(item.get("quote_source")),
        "outer_quote_fallback_used": bool(item.get("quote_fallback_used", False)),
        "quote_evidence": evidence,
    }


def _validate_quote_binding(row: MarketScanExecutionSessionRow) -> None:
    evidence = cast(MarketScanExecutionQuoteEvidence, row.quote_evidence)
    if evidence.symbol != row.symbol:
        raise ValueError("execution session quote evidence symbol mismatch")
    comparisons = (
        (row.outer_quote_price, evidence.bar.close, "price"),
        (row.outer_quote_amount, evidence.bar.amount, "amount"),
    )
    for outer, observed, label in comparisons:
        if outer is not None and not isclose(outer, observed, rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"execution session outer {label} conflicts with quote evidence")
    if row.outer_quote_source is not None and row.outer_quote_source != evidence.source:
        raise ValueError("execution session outer quote source conflicts with quote evidence")
    if row.outer_quote_fallback_used is not evidence.fallback_used:
        raise ValueError("execution session outer fallback flag conflicts with quote evidence")
    if row.outer_quote_timestamp is not None and not _same_instant(row.outer_quote_timestamp, evidence.provider_event_at):
        raise ValueError("execution session outer event timestamp conflicts with quote evidence")
    if row.outer_quote_observed_at is not None and not _same_instant(row.outer_quote_observed_at, evidence.captured_at):
        raise ValueError("execution session outer observed timestamp conflicts with quote evidence")


def _limitations(
    *,
    snapshot_bound: bool,
    complete_result_set: bool,
    quote_evidence_count: int,
    expected_result_count: int,
) -> list[str]:
    active = {
        "vendor_quote_not_exchange_authoritative",
        "corporate_action_evidence_unavailable",
    }
    if not snapshot_bound:
        active.add("source_snapshot_digest_unavailable")
    if not complete_result_set:
        active.add("incomplete_result_set")
    if quote_evidence_count != expected_result_count:
        active.add("quote_evidence_incomplete")
    return [item for item in _LIMITATION_ORDER if item in active]


def _row_set_digest(rows: Sequence[BaseModel | Mapping[str, object]]) -> str:
    normalized = [row.model_dump(mode="json") if isinstance(row, BaseModel) else dict(row) for row in rows]
    return sha256_hex(canonical_json_bytes(normalized))


def _object_mapping(value: object, label: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        dumped = dumper(mode="json")
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raise ValueError(f"{label} must be a mapping or Pydantic model")


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _optional_sha256(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if _SHA256.fullmatch(text) is None:
        raise ValueError("source_snapshot_digest must be sha256")
    return text


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("execution session numeric outer field is invalid")
    return float(value)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _iso_date_text(value: object, label: str) -> str:
    return _iso_date(str(value or ""), label).isoformat()


def _iso_date(value: str, label: str) -> date:
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
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ASHARE_TIMEZONE)
    return parsed.isoformat()


def _optional_aware_timestamp_text(value: object, label: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    return _aware_timestamp_text(value, label)


def _aware_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include timezone")
    return parsed


def _same_instant(left: str, right: str) -> bool:
    return _aware_timestamp(left, "timestamp") == _aware_timestamp(right, "timestamp")


__all__ = [
    "MARKET_SCAN_EXECUTION_SESSION_CONTRACT_VERSION",
    "MARKET_SCAN_EXECUTION_SESSION_SCHEMA_VERSION",
    "MarketScanExecutionSessionEvidence",
    "build_market_scan_execution_session_evidence",
    "market_scan_execution_session_content_digest",
    "verify_market_scan_execution_session_evidence",
]
