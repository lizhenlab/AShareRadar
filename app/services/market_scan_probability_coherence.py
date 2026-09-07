"""Logical checks on explicitly comparable probability events; no reranking."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from itertools import combinations
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex


class ProbabilityEventContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    decision_at: str
    horizon_sessions: int = Field(gt=0)
    return_definition: str = Field(min_length=1)
    price_basis: str = Field(min_length=1)
    conditioning_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_time(self) -> Self:
        stamp = datetime.fromisoformat(self.decision_at)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("decision_at requires an explicit timezone")
        try:
            self.decision_at = stamp.astimezone(timezone.utc).isoformat()
        except OverflowError as exc:
            raise ValueError("decision_at exceeds the supported UTC datetime range") from exc
        return self


class ThresholdProbabilityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    event_id: str = Field(min_length=1)
    operator: Literal["gt", "ge", "lt", "le"]
    threshold: float
    probability: float = Field(ge=0, le=1)


class ProbabilityEventGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    context: ProbabilityEventContext
    events: list[ThresholdProbabilityEvent] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_events(self) -> Self:
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("event IDs must be unique within a context")
        if len({(event.operator, event.threshold) for event in self.events}) != len(self.events):
            raise ValueError("duplicate logical events are ambiguous")
        return self


class ProbabilityCoherenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    schema_version: Literal["market-scan-probability-event-input-v1"]
    groups: list[ProbabilityEventGroup] = Field(min_length=1, max_length=5000)


def audit_probability_coherence(payload: Mapping[str, object]) -> dict[str, object]:
    """Check each event context independently, without assuming continuous returns."""
    inputs = ProbabilityCoherenceInput.model_validate(payload)
    contexts = [sha256_hex(canonical_json_bytes(group.context.model_dump())) for group in inputs.groups]
    if len(set(contexts)) != len(contexts):
        raise ValueError("comparable events must be in one unique context group")
    groups = [_audit_group(group, digest) for group, digest in zip(inputs.groups, contexts, strict=True)]
    report: dict[str, object] = {
        "schema_version": "market-scan-probability-coherence-audit-v1",
        "input_digest": sha256_hex(canonical_json_bytes(inputs.model_dump())),
        "status": "incoherent" if any(group["violations"] for group in groups) else "coherent_for_declared_relations",
        "groups": groups, "tolerance": 1e-10, "promotion_eligible": False,
        "limitations": ["logical coherence does not establish calibration or investment performance",
                        "contexts and source digests are caller declarations, not source authentication",
                        "different horizons, return definitions and conditioning sets are never ordered"],
    }
    report["digest"] = sha256_hex(canonical_json_bytes(report))
    return report


def _audit_group(group: ProbabilityEventGroup, digest: str) -> dict[str, object]:
    violations: list[dict[str, object]] = []
    checked = 0
    for left, right in combinations(group.events, 2):
        for first, second in ((left, right), (right, left)):
            if _is_subset(first, second):
                checked += 1
                _violation(violations, "subset", first, second, first.probability - second.probability)
        if left.threshold == right.threshold and {left.operator, right.operator} in ({"gt", "le"}, {"ge", "lt"}):
            checked += 1
            _violation(violations, "complement", left, right, abs(left.probability + right.probability - 1))
        elif _disjoint(left, right):
            checked += 1
            _violation(violations, "disjoint", left, right, left.probability + right.probability - 1)
        elif _cover_space(left, right):
            checked += 1
            _violation(violations, "exhaustive", left, right, 1 - left.probability - right.probability)
    return {"context": group.context.model_dump(), "context_digest": digest, "event_count": len(group.events),
            "checked_relations": checked, "violations": violations}


def _is_subset(left: ThresholdProbabilityEvent, right: ThresholdProbabilityEvent) -> bool:
    if left.operator in {"gt", "ge"} and right.operator in {"gt", "ge"}:
        return left.threshold > right.threshold or (left.threshold == right.threshold and right.operator == "ge")
    if left.operator in {"lt", "le"} and right.operator in {"lt", "le"}:
        return left.threshold < right.threshold or (left.threshold == right.threshold and right.operator == "le")
    return False


def _disjoint(left: ThresholdProbabilityEvent, right: ThresholdProbabilityEvent) -> bool:
    if left.operator in {"lt", "le"}:
        left, right = right, left
    if left.operator not in {"gt", "ge"} or right.operator not in {"lt", "le"}:
        return False
    return left.threshold > right.threshold or (
        left.threshold == right.threshold and (left.operator == "gt" or right.operator == "lt")
    )


def _violation(
    target: list[dict[str, object]], relation: str,
    left: ThresholdProbabilityEvent, right: ThresholdProbabilityEvent, excess: float,
) -> None:
    if excess > 1e-10:
        target.append({"relation": relation, "left": left.event_id, "right": right.event_id, "excess": excess})


def _cover_space(left: ThresholdProbabilityEvent, right: ThresholdProbabilityEvent) -> bool:
    if left.operator in {"lt", "le"}:
        left, right = right, left
    if left.operator not in {"gt", "ge"} or right.operator not in {"lt", "le"}:
        return False
    return left.threshold < right.threshold or (
        left.threshold == right.threshold and (left.operator == "ge" or right.operator == "le")
    )
