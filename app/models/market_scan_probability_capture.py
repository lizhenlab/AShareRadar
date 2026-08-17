"""Read-only probability-source capture state shared by query boundaries."""

from __future__ import annotations

from typing import Literal, TypedDict


ProbabilitySourceCaptureStatus = Literal[
    "pending",
    "processing",
    "succeeded",
    "skipped",
]


class ProbabilitySourceCaptureState(TypedDict):
    status: ProbabilitySourceCaptureStatus
    archive_digest: str | None
    last_error: str | None


__all__ = ["ProbabilitySourceCaptureState", "ProbabilitySourceCaptureStatus"]
