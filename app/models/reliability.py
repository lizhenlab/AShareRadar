from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ReliabilityStatus = Literal["met", "breached", "insufficient_data"]


class ReliabilityIndicator(BaseModel):
    name: str
    window_seconds: int = Field(gt=0)
    target_ratio: float = Field(ge=0, le=1)
    minimum_samples: int = Field(gt=0)
    samples: int = Field(ge=0)
    assessment_samples: int = Field(ge=0)
    good: int = Field(ge=0)
    ratio: float | None = Field(default=None, ge=0, le=1)
    status: ReliabilityStatus


class ReliabilityDuration(BaseModel):
    name: str
    window_seconds: int = Field(gt=0)
    target_p95_ms: int = Field(gt=0)
    minimum_samples: int = Field(gt=0)
    samples: int = Field(ge=0)
    p50_ms: int | None = Field(default=None, ge=0)
    p95_ms: int | None = Field(default=None, ge=0)
    max_ms: int | None = Field(default=None, ge=0)
    status: ReliabilityStatus


class ReliabilityReport(BaseModel):
    checked_at: str
    indicators: list[ReliabilityIndicator] = Field(default_factory=list)
    durations: list[ReliabilityDuration] = Field(default_factory=list)


__all__ = [
    "ReliabilityDuration",
    "ReliabilityIndicator",
    "ReliabilityReport",
    "ReliabilityStatus",
]
