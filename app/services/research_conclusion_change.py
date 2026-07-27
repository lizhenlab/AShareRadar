"""Backward-compatible advice comparison exports."""

from app.models.advice_change import (
    CONCLUSION_BASIS,
    LEGACY_CONCLUSION_BASIS,
    LEGACY_SNAPSHOT_CONTRACT_VERSION,
    MODEL_VERSION,
    SNAPSHOT_CONTRACT_VERSION,
    UNKNOWN_VERSION,
    ConclusionComparison,
    build_conclusion_timeline,
    compare_conclusions,
    conclusion_identity,
)


__all__ = [
    "CONCLUSION_BASIS",
    "LEGACY_CONCLUSION_BASIS",
    "LEGACY_SNAPSHOT_CONTRACT_VERSION",
    "MODEL_VERSION",
    "SNAPSHOT_CONTRACT_VERSION",
    "UNKNOWN_VERSION",
    "ConclusionComparison",
    "build_conclusion_timeline",
    "compare_conclusions",
    "conclusion_identity",
]
