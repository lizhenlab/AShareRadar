"""Non-scoring value evidence for the current cache and selected financial period."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from app.models.fuyao import FinancialPeriodType


class ValueResearchCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str
    label: str
    status: Literal["observed", "attention", "unavailable"]
    value: FiniteFloat | None = None
    unit: str | None = None
    summary: str
    action: str


class ValueValuationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    available_inputs: int = Field(ge=0, le=2)
    required_inputs: Literal[2] = 2
    coverage: Literal["complete", "partial", "unavailable"]
    source: str
    fetched_at: str | None = None
    earnings_yield_pct: FiniteFloat | None = None
    earnings_yield_reason: str
    book_to_price_pct: FiniteFloat | None = None
    book_to_price_reason: str
    checks: list[ValueResearchCheck]

    @model_validator(mode="after")
    def validate_coverage(self) -> ValueValuationEvidence:
        expected = {0: "unavailable", 1: "partial", 2: "complete"}[self.available_inputs]
        if self.coverage != expected or len(self.checks) != 2:
            raise ValueError("valuation coverage must agree with the two admitted inputs")
        if {item.key for item in self.checks} != {"pe_ttm", "pb_mrq"}:
            raise ValueError("valuation evidence requires PE TTM and PB MRQ")
        if sum(item.status != "unavailable" for item in self.checks) != self.available_inputs:
            raise ValueError("valuation checks disagree with admitted coverage")
        for item in self.checks:
            self._validate_inverse(item)
        return self

    def _validate_inverse(self, item: ValueResearchCheck) -> None:
        if item.status == "unavailable" and item.value is not None:
            raise ValueError("unavailable valuation inputs cannot retain an admitted value")
        if item.status != "unavailable" and (item.value is None or item.value == 0):
            raise ValueError("admitted valuation inputs require meaningful values")
        expected = 100.0 / item.value if item.value is not None and item.value > 0 else None
        if expected is not None and not math.isfinite(expected):
            expected = None
        actual = self.earnings_yield_pct if item.key == "pe_ttm" else self.book_to_price_pct
        if actual != expected:
            raise ValueError("derived valuation inverses must match the admitted positive input")


class ValueFinancialPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    period_end: str
    period_type: FinancialPeriodType
    source: str
    fetched_at: str
    observation_available: bool
    summary: str
    checks: list[ValueResearchCheck]


class ValueResearchReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["value-research-v1"] = "value-research-v1"
    symbol: str
    evaluated_at: str
    valuation: ValueValuationEvidence
    periods: list[ValueFinancialPeriod] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    metric_scope: Literal["current_value_research"] = "current_value_research"
    ranking_effect: Literal["none"] = "none"
    point_in_time: Literal[False] = False
