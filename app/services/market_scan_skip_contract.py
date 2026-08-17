"""Canonical, replayable evidence for justified production scan skips."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
import re
from typing import Literal, cast

from app.services.market_scan_score_contract import stable_score_spec_hash
from app.services.market_scan_session_coverage import verify_market_scan_session_coverage
from app.services.market_scan_skip_pit import verify_market_scan_skip_pit
from app.services.trading_calendar import trading_date_range
from app.utils.market_time import normalize_market_datetime


MARKET_SCAN_SKIP_EVIDENCE_SCHEMA_VERSION = 1
MARKET_SCAN_SKIP_EVIDENCE_CONTRACT_VERSION = "market-scan-skip-evidence-v1"
MARKET_SCAN_SKIP_EVIDENCE_KEY = "skip_evidence"
MARKET_SCAN_SKIP_REASON_CODES = frozenset(
    {"official_session_gap", "new_listing_insufficient_history"}
)
MarketScanSkipReasonCode = Literal[
    "official_session_gap",
    "new_listing_insufficient_history",
]

_EVIDENCE_KEYS = {
    "schema_version",
    "contract_version",
    "reason_code",
    "symbol",
    "code",
    "market",
    "name",
    "metadata_source",
    "is_new",
    "list_date",
    "mode",
    "run_rule_version",
    "as_of",
    "expected_data_date",
    "expected_quote_date",
    "required_history_rows",
    "new_stock_days",
    "reason",
    "facts",
    "evidence_digest",
}
_SESSION_GAP_FACT_KEYS = {
    "pit",
    "coverage",
    "coverage_digest",
    "observed_session_dates",
}
_NEW_LISTING_FACT_KEYS = {
    "pit",
    "calendar_source",
    "expected_session_count",
    "expected_session_dates",
    "expected_sessions_digest",
    "observed_session_count",
    "observed_session_dates",
    "observed_sessions_digest",
}
_RULE_PATTERN = re.compile(r"full-market-scan-v6:[0-9a-f]{64}")


class MarketScanSkipped(ValueError):
    """A typed exclusion that can be bound to immutable run context."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: MarketScanSkipReasonCode | None = None,
        facts: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.facts = dict(facts or {})
        self.evidence: dict[str, object] | None = None

    def bind(
        self,
        *,
        symbol: str,
        code: str,
        market: str,
        name: str,
        metadata_source: str | None,
        is_new: bool,
        list_date: str | None,
        mode: str,
        run_rule_version: str | None,
        as_of: datetime,
        expected_data_date: date,
        expected_quote_date: date,
        required_history_rows: int,
        new_stock_days: int,
    ) -> None:
        if self.evidence is not None or self.reason_code is None or not run_rule_version:
            return
        self.evidence = build_market_scan_skip_evidence(
            reason_code=self.reason_code,
            symbol=symbol,
            code=code,
            market=market,
            name=name,
            metadata_source=metadata_source,
            is_new=is_new,
            list_date=list_date,
            mode=mode,
            run_rule_version=run_rule_version,
            as_of=as_of,
            expected_data_date=expected_data_date,
            expected_quote_date=expected_quote_date,
            required_history_rows=required_history_rows,
            new_stock_days=new_stock_days,
            reason=str(self),
            facts=self.facts,
        )


def build_market_scan_skip_evidence(
    *,
    reason_code: MarketScanSkipReasonCode,
    symbol: str,
    code: str,
    market: str,
    name: str,
    metadata_source: str | None,
    is_new: bool,
    list_date: str | None,
    mode: str,
    run_rule_version: str,
    as_of: datetime | str,
    expected_data_date: date | str,
    expected_quote_date: date | str,
    required_history_rows: int,
    new_stock_days: int,
    reason: str,
    facts: Mapping[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": MARKET_SCAN_SKIP_EVIDENCE_SCHEMA_VERSION,
        "contract_version": MARKET_SCAN_SKIP_EVIDENCE_CONTRACT_VERSION,
        "reason_code": reason_code,
        "symbol": str(symbol),
        "code": str(code),
        "market": str(market),
        "name": str(name),
        "metadata_source": _optional_text(metadata_source),
        "is_new": bool(is_new),
        "list_date": _optional_date(list_date),
        "mode": str(mode),
        "run_rule_version": str(run_rule_version),
        "as_of": _required_timestamp(as_of),
        "expected_data_date": _required_date(expected_data_date),
        "expected_quote_date": _required_date(expected_quote_date),
        "required_history_rows": required_history_rows,
        "new_stock_days": new_stock_days,
        "reason": str(reason).strip(),
        "facts": dict(facts),
    }
    payload["evidence_digest"] = stable_score_spec_hash(payload)
    _require_valid_built_evidence(
        payload,
        required_history_rows=required_history_rows,
        new_stock_days=new_stock_days,
    )
    return payload


def _require_valid_built_evidence(
    payload: dict[str, object],
    *,
    required_history_rows: int,
    new_stock_days: int,
) -> None:
    pit = payload["facts"].get("pit") if isinstance(payload["facts"], dict) else None
    pit_outer = _pit_outer_values(pit)
    if pit_outer is None or not verify_market_scan_skip_evidence(
        payload,
        expected_symbol=payload["symbol"],
        expected_code=payload["code"],
        expected_market=payload["market"],
        expected_name=payload["name"],
        expected_metadata_source=payload["metadata_source"],
        expected_is_new=payload["is_new"],
        expected_list_date=payload["list_date"],
        expected_mode=payload["mode"],
        expected_rule_version=payload["run_rule_version"],
        expected_as_of=payload["as_of"],
        expected_data_date=payload["expected_data_date"],
        expected_quote_date=payload["expected_quote_date"],
        expected_min_history_rows=required_history_rows,
        expected_new_stock_days=new_stock_days,
        expected_reason=payload["reason"],
        expected_quote_timestamp=pit_outer[0],
        expected_quote_observed_at=pit_outer[1],
        expected_quote_source=pit_outer[2],
        expected_kline_source=pit_outer[3],
        expected_adjustment_mode="qfq",
    ):
        raise ValueError("生产扫描跳过证据不满足规范")


def verify_market_scan_skip_evidence(
    value: object,
    *,
    expected_symbol: object,
    expected_code: object,
    expected_market: object,
    expected_name: object,
    expected_metadata_source: object,
    expected_is_new: object,
    expected_list_date: object,
    expected_mode: object,
    expected_rule_version: object,
    expected_as_of: object,
    expected_data_date: object,
    expected_quote_date: object,
    expected_min_history_rows: object,
    expected_new_stock_days: object,
    expected_reason: object,
    expected_quote_timestamp: object,
    expected_quote_observed_at: object,
    expected_quote_source: object,
    expected_kline_source: object,
    expected_adjustment_mode: object,
) -> bool:
    if _verified_skip_payload(value) is None:
        return False
    expected = _expected_identity(
        expected_symbol, expected_code, expected_market, expected_name,
        expected_metadata_source, expected_is_new, expected_list_date,
        expected_mode, expected_rule_version, expected_as_of,
        expected_data_date, expected_quote_date, expected_min_history_rows,
        expected_new_stock_days, expected_reason,
    )
    if expected is None or _actual_identity(value) != expected:
        return False
    facts = value.get("facts")
    reason_code = value.get("reason_code")
    if not _valid_skip_contract(value, expected=expected, facts=facts):
        return False
    assert isinstance(facts, Mapping)
    dates = _strict_date_list(facts.get("observed_session_dates"))
    if dates is None or not _verified_skip_pit_context(
        facts,
        expected=expected,
        dates=dates,
        outer=(expected_quote_timestamp, expected_quote_observed_at,
               expected_quote_source, expected_kline_source,
               expected_adjustment_mode),
    ):
        return False
    if reason_code == "new_listing_insufficient_history":
        return _verify_new_listing(facts, expected=expected, observed_dates=dates)
    return _verify_session_gap(facts, expected_data_date=expected[10], observed_dates=dates)


def _verified_skip_payload(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or set(value) != _EVIDENCE_KEYS:
        return None
    payload = dict(value)
    digest = payload.pop("evidence_digest", None)
    if not isinstance(digest, str) or digest != stable_score_spec_hash(payload):
        return None
    return payload


def _valid_skip_contract(
    value: Mapping[str, object],
    *,
    expected: tuple[object, ...],
    facts: object,
) -> bool:
    return (
        value.get("schema_version") == MARKET_SCAN_SKIP_EVIDENCE_SCHEMA_VERSION
        and value.get("contract_version") == MARKET_SCAN_SKIP_EVIDENCE_CONTRACT_VERSION
        and value.get("reason_code") in MARKET_SCAN_SKIP_REASON_CODES
        and _valid_skip_mode(value.get("reason_code"), expected[7])
        and _RULE_PATTERN.fullmatch(str(expected[8])) is not None
        and isinstance(facts, Mapping)
    )


def _valid_skip_mode(reason_code: object, mode: object) -> bool:
    if reason_code == "official_session_gap":
        return mode == "official"
    return reason_code == "new_listing_insufficient_history" and mode in {
        "official",
        "intraday",
        "preopen",
    }


def _verified_skip_pit_context(
    facts: Mapping[str, object],
    *,
    expected: tuple[object, ...],
    dates: list[str],
    outer: tuple[object, object, object, object, object],
) -> bool:
    return verify_market_scan_skip_pit(
        facts.get("pit"),
        expected_symbol=str(expected[0]),
        expected_data_date=str(expected[10]),
        expected_quote_date=str(expected[11]),
        expected_as_of=str(expected[9]),
        expected_bar_dates=dates,
        expected_quote_timestamp=outer[0],
        expected_quote_observed_at=outer[1],
        expected_quote_source=outer[2],
        expected_kline_source=outer[3],
        expected_adjustment_mode=outer[4],
    )


def session_gap_skip_facts(
    *,
    pit: Mapping[str, object],
    observed_session_dates: Sequence[str],
    coverage: Mapping[str, object],
) -> dict[str, object]:
    coverage_payload = dict(coverage)
    return {
        "pit": dict(pit),
        "observed_session_dates": list(observed_session_dates),
        "coverage": coverage_payload,
        "coverage_digest": stable_score_spec_hash(coverage_payload),
    }


def new_listing_insufficient_history_facts(
    *,
    pit: Mapping[str, object],
    list_date: str,
    expected_data_date: date | str,
    observed_session_dates: Sequence[str],
) -> dict[str, object]:
    listing = date.fromisoformat(_required_date(list_date))
    target = date.fromisoformat(_required_date(expected_data_date))
    expected_dates, status = trading_date_range(listing, target)
    expected = [item.isoformat() for item in expected_dates]
    observed = list(observed_session_dates)
    return {
        "pit": dict(pit),
        "calendar_source": status.source.value,
        "expected_session_count": len(expected),
        "expected_session_dates": expected,
        "expected_sessions_digest": stable_score_spec_hash(expected),
        "observed_session_count": len(observed),
        "observed_session_dates": observed,
        "observed_sessions_digest": stable_score_spec_hash(observed),
    }


def _verify_new_listing(
    facts: Mapping[str, object],
    *,
    expected: tuple[object, ...],
    observed_dates: list[str],
) -> bool:
    if expected[5] is not True or expected[6] is None or set(facts) != _NEW_LISTING_FACT_KEYS:
        return False
    required, new_stock_days = expected[12], expected[13]
    if not isinstance(required, int) or not isinstance(new_stock_days, int):
        return False
    try:
        listing = date.fromisoformat(cast(str, expected[6]))
        target = date.fromisoformat(cast(str, expected[10]))
        trusted_dates, status = trading_date_range(listing, target)
    except (TypeError, ValueError):
        return False
    trusted = [item.isoformat() for item in trusted_dates]
    listed_age = (target - listing).days
    return (
        1 <= len(trusted) < required
        and 0 <= listed_age <= new_stock_days
        and observed_dates == trusted
        and facts.get("expected_session_dates") == trusted
        and facts.get("expected_session_count") == len(trusted)
        and facts.get("observed_session_count") == len(trusted)
        and facts.get("calendar_source") == status.source.value
        and facts.get("expected_sessions_digest") == stable_score_spec_hash(trusted)
        and facts.get("observed_sessions_digest") == stable_score_spec_hash(trusted)
    )


def _verify_session_gap(
    facts: Mapping[str, object],
    *,
    expected_data_date: object,
    observed_dates: list[str],
) -> bool:
    if set(facts) != _SESSION_GAP_FACT_KEYS:
        return False
    coverage = facts.get("coverage")
    return (
        len(observed_dates) == 61
        and observed_dates[-1] == expected_data_date
        and isinstance(coverage, Mapping)
        and facts.get("coverage_digest") == stable_score_spec_hash(dict(coverage))
        and coverage.get("action_eligible") is False
        and isinstance(coverage.get("missing_session_count"), int)
        and coverage["missing_session_count"] > 0
        and verify_market_scan_session_coverage(
            coverage,
            bar_contract=[[item] for item in observed_dates],
        )
    )


def _expected_identity(*values: object) -> tuple[object, ...] | None:
    try:
        expected = (
            str(values[0]), str(values[1]), str(values[2]), str(values[3]),
            _optional_text(values[4]), values[5] if isinstance(values[5], bool) else None,
            _optional_date(values[6]), str(values[7]), str(values[8]),
            _required_timestamp(values[9]), _required_date(values[10]),
            _required_date(values[11]), _required_positive_int(values[12]),
            _required_positive_int(values[13]), str(values[14]).strip(),
        )
    except (TypeError, ValueError):
        return None
    return expected if expected[5] is not None else None


def _actual_identity(value: Mapping[str, object]) -> tuple[object, ...]:
    keys = (
        "symbol", "code", "market", "name", "metadata_source", "is_new",
        "list_date", "mode", "run_rule_version", "as_of", "expected_data_date",
        "expected_quote_date", "required_history_rows", "new_stock_days", "reason",
    )
    return tuple(value.get(key) for key in keys)


def _pit_outer_values(value: object) -> tuple[object, object, object, object] | None:
    if not isinstance(value, Mapping):
        return None
    quote = value.get("quote_contract")
    bars = value.get("bar_contract")
    if (
        not isinstance(quote, Mapping)
        or not isinstance(bars, Sequence)
        or isinstance(bars, str | bytes)
        or not bars
        or not isinstance(bars[-1], Sequence)
        or isinstance(bars[-1], str | bytes)
        or len(bars[-1]) < 11
    ):
        return None
    return (
        quote.get("timestamp"),
        value.get("quote_observed_at"),
        quote.get("source"),
        bars[-1][10],
    )


def _strict_date_list(value: object) -> list[str] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return None
    dates: list[str] = []
    for item in value:
        try:
            parsed = _required_date(item)
        except ValueError:
            return None
        dates.append(parsed)
    return dates if dates and dates == sorted(set(dates)) else None


def _required_date(value: object) -> str:
    text = str(value)
    if date.fromisoformat(text).isoformat() != text:
        raise ValueError("日期不是规范 ISO 日期")
    return text


def _optional_date(value: object) -> str | None:
    return None if value is None or not str(value).strip() else _required_date(value)


def _required_timestamp(value: object) -> str:
    normalized = normalize_market_datetime(value)
    if normalized is None:
        raise ValueError("跳过证据时点无效")
    return normalized


def _required_positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("跳过证据阈值无效")
    return value


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "MARKET_SCAN_SKIP_EVIDENCE_CONTRACT_VERSION",
    "MARKET_SCAN_SKIP_EVIDENCE_KEY",
    "MARKET_SCAN_SKIP_EVIDENCE_SCHEMA_VERSION",
    "MARKET_SCAN_SKIP_REASON_CODES",
    "MarketScanSkipped",
    "build_market_scan_skip_evidence",
    "new_listing_insufficient_history_facts",
    "session_gap_skip_facts",
    "verify_market_scan_skip_evidence",
]
