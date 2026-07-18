from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal, TypeAlias

from app.models.schemas import (
    AdviceComparisonStatus,
    AdviceTimelineChange,
    AdviceTimelineItem,
)
from app.utils.market_data import finite_float


SNAPSHOT_CONTRACT_VERSION = "conclusion.v1"
CONCLUSION_BASIS = "analysis_action_advice"
MODEL_VERSION = "none"
LEGACY_SNAPSHOT_CONTRACT_VERSION = "legacy"
LEGACY_CONCLUSION_BASIS = "legacy_unknown"
UNKNOWN_VERSION = "unknown"

ConclusionSource: TypeAlias = AdviceTimelineItem | Mapping[str, object]
ConclusionIdentity: TypeAlias = tuple[str | int, ...]
ChangeValueKind = Literal["text", "score", "price"]


@dataclass(frozen=True)
class ConclusionComparison:
    previous_id: int | None
    comparison_status: AdviceComparisonStatus
    has_changes: bool
    changes: tuple[AdviceTimelineChange, ...]


@dataclass(frozen=True)
class ChangeField:
    category: str
    field: str
    value_kind: ChangeValueKind


_CHANGE_FIELDS = (
    ChangeField("action", "action", "text"),
    ChangeField("advice", "confidence", "score"),
    ChangeField("trend", "trend_label", "text"),
    ChangeField("trend", "trend_score", "score"),
    ChangeField("risk", "risk_level", "text"),
    ChangeField("price_level", "support", "price"),
    ChangeField("price_level", "resistance", "price"),
    ChangeField("data_quality", "data_quality_score", "score"),
    ChangeField("data_quality", "data_quality_level", "text"),
    ChangeField("data_quality", "data_quality_source", "text"),
)
_VERSION_FIELDS = (
    "snapshot_contract_version",
    "conclusion_basis",
    "rule_version",
    "model_version",
)


def build_conclusion_timeline(
    items: Sequence[AdviceTimelineItem],
    limit: int,
) -> list[AdviceTimelineItem]:
    if limit <= 0:
        return []
    retained = list(items)
    timeline: list[AdviceTimelineItem] = []
    for index, current in enumerate(retained[:limit]):
        previous = retained[index + 1] if index + 1 < len(retained) else None
        comparison = compare_conclusions(current, previous)
        timeline.append(
            current.model_copy(
                update={
                    "previous_id": comparison.previous_id,
                    "comparison_status": comparison.comparison_status,
                    "has_changes": comparison.has_changes,
                    "changes": list(comparison.changes),
                }
            )
        )
    return timeline


def compare_conclusions(
    current: AdviceTimelineItem,
    previous: AdviceTimelineItem | None,
) -> ConclusionComparison:
    if previous is None:
        return ConclusionComparison(
            previous_id=None,
            comparison_status="no_previous",
            has_changes=False,
            changes=(),
        )

    status = _comparison_status(current, previous)
    comparable = status == "comparable"
    changes = tuple(
        change
        for field in _CHANGE_FIELDS
        if (change := _field_change(current, previous, field, comparable=comparable)) is not None
    )
    return ConclusionComparison(
        previous_id=previous.id,
        comparison_status=status,
        has_changes=bool(changes),
        changes=changes,
    )


def conclusion_identity(item: ConclusionSource) -> ConclusionIdentity | None:
    action = _text_value(_source_value(item, "action"))
    confidence = _score_value(_source_value(item, "confidence"))
    trend_score = _score_value(_source_value(item, "trend_score"))
    trend_label = _text_value(_source_value(item, "trend_label"))
    risk_level = _text_value(_source_value(item, "risk_level"))
    support_cents = _price_cents(_source_value(item, "support"))
    resistance_cents = _price_cents(_source_value(item, "resistance"))
    data_quality_score = _score_value(_source_value(item, "data_quality_score"))
    data_quality_level = _text_value(_source_value(item, "data_quality_level"))
    data_quality_source = _text_value(_source_value(item, "data_quality_source"))
    versions = _version_identity(item)
    values = (
        action,
        confidence,
        trend_score,
        trend_label,
        risk_level,
        support_cents,
        resistance_cents,
        data_quality_score,
        data_quality_level,
        data_quality_source,
    )
    if versions is None or any(value is None for value in values):
        return None
    return (*values, *versions)  # type: ignore[return-value]


def _comparison_status(
    current: AdviceTimelineItem,
    previous: AdviceTimelineItem,
) -> AdviceComparisonStatus:
    current_versions = _version_identity(current)
    previous_versions = _version_identity(previous)
    if _is_legacy(current_versions) or _is_legacy(previous_versions):
        return "legacy"
    if current_versions != previous_versions:
        return "version_changed"
    return "comparable"


def _is_legacy(versions: tuple[str, ...] | None) -> bool:
    if versions is None:
        return True
    contract_version, conclusion_basis, rule_version, model_version = versions
    return (
        contract_version == LEGACY_SNAPSHOT_CONTRACT_VERSION
        or conclusion_basis == LEGACY_CONCLUSION_BASIS
        or rule_version == UNKNOWN_VERSION
        or model_version == UNKNOWN_VERSION
    )


def _version_identity(item: ConclusionSource) -> tuple[str, ...] | None:
    values = tuple(_text_value(_source_value(item, field)) for field in _VERSION_FIELDS)
    if any(value is None for value in values):
        return None
    return values  # type: ignore[return-value]


def _field_change(
    current: AdviceTimelineItem,
    previous: AdviceTimelineItem,
    field: ChangeField,
    *,
    comparable: bool,
) -> AdviceTimelineChange | None:
    before = _normalized_change_value(previous, field)
    after = _normalized_change_value(current, field)
    if before is None or after is None or before == after:
        return None

    delta = _numeric_delta(before, after, field.value_kind) if comparable else None
    return AdviceTimelineChange(
        category=field.category,
        field=field.field,
        before=before,
        after=after,
        delta=delta,
        direction=_direction(before, after, field.value_kind) if comparable else "not_comparable",
        comparable=comparable,
    )


def _normalized_change_value(item: ConclusionSource, field: ChangeField) -> str | int | float | None:
    value = _source_value(item, field.field)
    if field.value_kind == "text":
        return _text_value(value)
    if field.value_kind == "score":
        return _score_value(value)
    cents = _price_cents(value)
    return None if cents is None else cents / 100


def _numeric_delta(before: str | int | float, after: str | int | float, kind: ChangeValueKind) -> int | float | None:
    if kind == "text" or isinstance(before, str) or isinstance(after, str):
        return None
    delta = after - before
    return int(delta) if kind == "score" else round(float(delta), 2)


def _direction(
    before: str | int | float,
    after: str | int | float,
    kind: ChangeValueKind,
) -> Literal["up", "down", "changed"]:
    if kind == "text" or isinstance(before, str) or isinstance(after, str):
        return "changed"
    return "up" if after > before else "down"


def _source_value(item: ConclusionSource, field: str) -> object:
    if isinstance(item, Mapping):
        return item.get(field)
    return getattr(item, field, None)


def _text_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _score_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    parsed = finite_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    result = int(parsed)
    return result if 0 <= result <= 100 else None


def _price_cents(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value))
        if not price.is_finite():
            return None
        rounded = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return int(rounded * 100)


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
