"""Observed financial facts; these records never attest historical availability."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

FinancialSourceKind = Literal["income", "balance", "cashflow", "indicators"]
FinancialPeriodType = Literal["annual", "quarterly"]
FINANCIAL_PARTIAL_WARNING = "部分报告期三张报表未齐；不同报告期的数据不拼接为同一期结论。"


class FinancialFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str
    label: str
    value: FiniteFloat | None = None
    raw_value: str | None = None
    unit: str | None = None
    source_kind: FinancialSourceKind
    ability: str | None = None


class FinancialSourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_kind: FinancialSourceKind
    supplier_report_date: str | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    currency: str | None = None


class FinancialPeriodRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    period_end: str
    period_type: FinancialPeriodType
    currency: str | None = None
    statements: list[FinancialSourceRecord] = Field(default_factory=list)
    metrics: list[FinancialFact] = Field(default_factory=list)
    alignment: Literal["complete", "partial", "indicators_only"] = "partial"
    supplier_report_dates: list[str] = Field(default_factory=list)
    fetched_at: str | None = None
    source: str | None = None


class FinancialReportBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["fuyao-financial-v1"] = "fuyao-financial-v1"
    symbol: str
    fetched_at: str
    source: str = "同花顺扶摇"
    periods: list[FinancialPeriodRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    score: None = None
    metric_scope: Literal["observed_financial_facts"] = "observed_financial_facts"
    point_in_time: Literal[False] = False
