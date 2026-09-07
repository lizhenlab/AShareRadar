"""Bounded, independently pinned axes for descriptive execution sensitivity."""

from __future__ import annotations

from datetime import date
from math import prod
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, sha256_hex
from app.services.market_scan_research_challengers import ResearchChallenger
from app.services.market_scan_trial_registry_contract import registry_hash


class ResearchSensitivityPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)

    schema_version: Literal["market-scan-execution-sensitivity-plan-v1"]
    signal_dates: list[str] = Field(min_length=1, max_length=1024)
    variants: list[ResearchChallenger] = Field(min_length=1, max_length=4)
    initial_cash_values: list[float] = Field(min_length=1, max_length=8)
    cost_profiles: list[Literal["base", "conservative", "stress"]] = Field(min_length=1, max_length=3)
    participation_rates: list[float] = Field(min_length=1, max_length=8)
    top_n: int = Field(ge=1, le=1000)
    horizon: int = Field(ge=1, le=20)

    @model_validator(mode="after")
    def validate_axes(self) -> Self:
        axes = (self.variants, self.initial_cash_values, self.cost_profiles, self.participation_rates)
        if any(len(set(axis)) != len(axis) for axis in axes):
            raise ValueError("scenario axes must be unique; duplicate cells are not new evidence")
        if "production_v5" not in self.variants:
            raise ValueError("production_v5 must remain in the declared grid")
        if prod(len(axis) for axis in axes) > 64:
            raise ValueError("scenario grid exceeds 64 account cells")
        if any(not .01 <= value <= 1e12 or round(value, 2) != value for value in self.initial_cash_values):
            raise ValueError("cash must be an exact cent between 0.01 and 1e12")
        if any(not 0 < value <= 1 for value in self.participation_rates):
            raise ValueError("participation rates must be in (0, 1]")
        validate_sensitivity_dates(self.signal_dates)
        return self


def validate_sensitivity_dates(dates: list[str]) -> None:
    if dates != sorted(set(dates)) or any(date.fromisoformat(day).isoformat() != day for day in dates):
        raise ValueError("signal dates must be unique ordered ISO dates")


def admit_sensitivity_plan(value: object, expected_digest: str) -> tuple[ResearchSensitivityPlan, dict[str, object], str]:
    encoded = canonical_json_bytes(value)
    if len(encoded) > 1024 * 1024:
        raise ValueError("sensitivity plan exceeds byte limit")
    digest = sha256_hex(encoded)
    if digest != registry_hash(expected_digest, "expected_plan_digest"):
        raise ValueError("sensitivity plan digest mismatch")
    raw = decode_json_bytes(encoded)
    if not isinstance(raw, dict):
        raise ValueError("sensitivity plan must be a JSON object")
    return ResearchSensitivityPlan.model_validate(raw), raw, digest
