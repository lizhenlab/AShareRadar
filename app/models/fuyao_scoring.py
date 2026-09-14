"""Auditable current-observation valuation inputs, separate from historical ranking."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


class FuyaoScoreContribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: Literal["pe_ttm", "pb_mrq"]
    label: str
    value: FiniteFloat | None
    points: int = Field(ge=-8, le=8)
    reason: str


class FuyaoValuationScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    rule_version: Literal["fuyao-valuation-threshold-v1"] = "fuyao-valuation-threshold-v1"
    symbol: str
    evaluated_at: str
    fetched_at: str | None = None
    source: str = "同花顺扶摇"
    observation_id: int | None = None
    observation_digest: str | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    score_available: bool = False
    base_score: Literal[55] = 55
    pe_ttm: FiniteFloat | None = None
    pb_mrq: FiniteFloat | None = None
    components: list[FuyaoScoreContribution] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unavailable_reason: str | None = None
    max_observation_age_days: Literal[7] = 7
    score_semantics: Literal["heuristic_valuation_pressure"] = "heuristic_valuation_pressure"
    ranking_effect: Literal["individual_research_only"] = "individual_research_only"
    point_in_time: Literal[False] = False

    @model_validator(mode="after")
    def validate_available_score(self) -> FuyaoValuationScore:
        if self.score_available:
            self._validate_observation_components()
            if self.score is None or self.unavailable_reason is not None:
                raise ValueError("available valuation score requires a value and no rejection reason")
            if not any(item.value is not None and item.value != 0 for item in self.components):
                raise ValueError("available valuation score requires meaningful observed components")
            if self.score != self.base_score + sum(item.points for item in self.components):
                raise ValueError("valuation contributions must reconcile to the score")
        elif self.score is not None or not self.unavailable_reason:
            raise ValueError("unavailable valuation score requires a reason and no score")
        return self

    def _validate_observation_components(self) -> None:
        if len(self.components) != 2 or {item.key for item in self.components} != {"pe_ttm", "pb_mrq"}:
            raise ValueError("valuation score requires exactly one component per input basis")
        values = {item.key: item.value for item in self.components}
        if values != {"pe_ttm": self.pe_ttm, "pb_mrq": self.pb_mrq}:
            raise ValueError("valuation component values must match the observed inputs")
        if self.observation_id is None or self.observation_id <= 0 or not self.fetched_at or not self.source.strip():
            raise ValueError("available valuation score requires observation provenance")
        if self.observation_digest is None or not re.fullmatch(r"[0-9a-f]{64}", self.observation_digest):
            raise ValueError("available valuation score requires an observation digest")
