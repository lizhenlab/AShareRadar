"""Normalize successful Fuyao envelopes into bounded, auditable financial facts."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import cast

from app.models.fuyao import (
    FinancialFact, FinancialPeriodRecord, FinancialPeriodType, FinancialReportBundle,
    FinancialSourceKind, FinancialSourceRecord,
)
from app.services.fuyao_financials_fields import FINANCIAL_WARNINGS, STATEMENT_FIELDS
from app.services.fuyao_financials_parsing import (
    financial_identity, financial_indicators, financial_ms, financial_number, financial_object,
    financial_payload, financial_time,
)
from app.services.fuyao_financials_qa import financial_fact_answer
from app.services.fuyao_financials_views import financial_health_from_bundle
from app.utils.symbols import standard_a_share_stock_symbol


def normalize_financials(
    symbol: str,
    payloads: dict[str, dict[str, object]],
    fetched_at: str,
) -> FinancialReportBundle:
    symbol, fetched = standard_a_share_stock_symbol(symbol), financial_time(fetched_at)
    if set(payloads) - {*STATEMENT_FIELDS, "indicators"}:
        raise ValueError("unknown financial payload capability")
    periods: dict[tuple[str, str], FinancialPeriodRecord] = {}
    for kind in STATEMENT_FIELDS:
        if kind in payloads:
            _collect_statement(periods, symbol, kind, financial_payload(payloads[kind]), fetched)
    if "indicators" in payloads:
        _collect_indicators(periods, symbol, financial_payload(payloads["indicators"]), fetched)
    ordered = sorted(periods.values(), key=lambda item: (item.period_end, item.period_type == "annual"), reverse=True)
    warnings = list(FINANCIAL_WARNINGS)
    if any(item.alignment != "complete" for item in ordered):
        warnings.append("部分报告期三张报表未齐；不同报告期的数据不拼接为同一期结论。")
    if not ordered:
        warnings.append("本次成功响应中没有财务记录；不表示公司没有财报。")
    return FinancialReportBundle(symbol=symbol, fetched_at=fetched.isoformat(), periods=ordered, warnings=warnings)


def _collect_statement(
    periods: dict[tuple[str, str], FinancialPeriodRecord], symbol: str, kind: str,
    data: dict[str, object], fetched: datetime,
) -> None:
    items = data.get("item")
    if not isinstance(items, list) or len(items) > 80:
        raise ValueError("financial statements require a bounded item array")
    if data.get("timestamp") is not None:
        financial_ms(data["timestamp"], fetched, "batch timestamp")
    for raw in items:
        row = financial_object(raw, "statement row")
        financial_identity(row, symbol)
        period = _statement_period(row, kind, fetched)
        key = period.period_end, period.period_type
        current = periods.get(key)
        periods[key] = _merge_statement(current, period) if current is not None else period


def _statement_period(row: dict[str, object], kind: str, fetched: datetime) -> FinancialPeriodRecord:
    end = financial_ms(row.get("period_end_ms"), fetched, "period end").date()
    period_type = row.get("period")
    if not isinstance(period_type, str) or period_type not in {"annual", "quarterly"}:
        raise ValueError("invalid financial period type")
    expected_ends = {(12, 31)} if period_type == "annual" else {(3, 31), (6, 30), (9, 30), (12, 31)}
    if (end.month, end.day) not in expected_ends:
        raise ValueError("financial period end does not match period type")
    metadata = _statement_metadata(row, kind, end, fetched)
    facts = [FinancialFact(key=key, label=label, value=financial_number(row.get(key)),
                           raw_value=str(row[key]) if row.get(key) is not None else None,
                           source_kind=cast(FinancialSourceKind, kind))
             for key, label in STATEMENT_FIELDS[kind].items()]
    report_dates = [metadata.supplier_report_date] if metadata.supplier_report_date is not None else []
    return FinancialPeriodRecord(period_end=end.isoformat(), period_type=cast(FinancialPeriodType, period_type),
                                 currency=metadata.currency, statements=[metadata], metrics=facts,
                                 supplier_report_dates=report_dates)


def _statement_metadata(row: dict[str, object], kind: str, end: date, fetched: datetime) -> FinancialSourceRecord:
    report = financial_ms(row["report_date_ms"], fetched, "supplier report date") if row.get("report_date_ms") is not None else None
    year, fiscal_period, currency = row.get("fiscal_year"), row.get("fiscal_period"), row.get("currency")
    if year is not None and (type(year) is not int or year != end.year):
        raise ValueError("financial fiscal year disagrees with period end")
    if fiscal_period is not None and (not isinstance(fiscal_period, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", fiscal_period)):
        raise ValueError("invalid financial fiscal period")
    if currency is not None and currency != "CNY":
        raise ValueError("unexpected A-share financial currency")
    if report is not None and report.date() < end:
        raise ValueError("supplier report date precedes period end")
    return FinancialSourceRecord(source_kind=cast(FinancialSourceKind, kind), supplier_report_date=report.isoformat() if report else None,
                                 fiscal_year=year, fiscal_period=fiscal_period,
                                 currency=cast(str | None, currency))


def _merge_statement(current: FinancialPeriodRecord, incoming: FinancialPeriodRecord) -> FinancialPeriodRecord:
    kind = incoming.statements[0].source_kind
    existing = [item for item in current.statements if item.source_kind == kind]
    if existing:
        facts = [item for item in current.metrics if item.source_kind == kind]
        if existing != incoming.statements or facts != incoming.metrics:
            raise ValueError("conflicting financial records for the same statement and period")
        return current
    statements = [*current.statements, *incoming.statements]
    currencies = {item.currency for item in statements}
    return _revalidate_period(current, {
        "statements": statements, "metrics": [*current.metrics, *incoming.metrics],
        "currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "supplier_report_dates": sorted(set(current.supplier_report_dates + incoming.supplier_report_dates)),
        "alignment": "complete" if len(statements) == 3 else "partial",
    })


def _collect_indicators(
    periods: dict[tuple[str, str], FinancialPeriodRecord], symbol: str,
    data: dict[str, object], fetched: datetime,
) -> None:
    financial_identity(data, symbol)
    report = data.get("report")
    if not isinstance(report, str) or not re.fullmatch(r"(?:19|20)\d{2}-[1-4]", report):
        raise ValueError("invalid financial indicator report period")
    year, quarter = (int(value) for value in report.split("-"))
    month, day = ((3, 31), (6, 30), (9, 30), (12, 31))[quarter - 1]
    end = date(year, month, day)
    if end > fetched.date():
        raise ValueError("financial indicator period is in the future")
    period_type: FinancialPeriodType = "annual" if quarter == 4 else "quarterly"
    key = end.isoformat(), period_type
    current = periods.get(key, FinancialPeriodRecord(period_end=end.isoformat(), period_type=period_type, alignment="indicators_only"))
    facts = financial_indicators(data)
    periods[key] = _revalidate_period(current, {"metrics": [*current.metrics, *facts]})


def _revalidate_period(current: FinancialPeriodRecord, updates: dict[str, object]) -> FinancialPeriodRecord:
    values = current.model_dump()
    values.update(updates)
    return FinancialPeriodRecord.model_validate(values)


__all__ = ["normalize_financials", "financial_health_from_bundle", "financial_fact_answer"]
