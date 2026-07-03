from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import ProviderCapabilityStatus, ProviderStatus
from app.utils.time import now_text

DEFAULT_PROVIDER_PRIORITY = 99


@dataclass(frozen=True)
class ProviderAggregationContext:
    statuses: list[ProviderCapabilityStatus]
    enabled_statuses: list[ProviderCapabilityStatus]
    active_statuses: list[ProviderCapabilityStatus]
    history: ProviderStatus | None
    fallback: ProviderStatus | None


def aggregate_provider_status(
    name: str,
    statuses: list[ProviderCapabilityStatus],
    fallback: ProviderStatus | None,
) -> ProviderStatus:
    context = _provider_aggregation_context(statuses, fallback)
    return ProviderStatus(
        name=name,
        enabled=_aggregate_enabled(context),
        priority=_aggregate_priority(context),
        healthy=_aggregate_healthy(context),
        last_success=_latest_success(context.active_statuses, context.history),
        last_error=_latest_error(context.active_statuses, context.history),
        latency_ms=_latest_latency(context.active_statuses, context.history),
        success_count=_success_count(context.active_statuses, context.history),
        failure_count=_failure_count(context.active_statuses, context.history),
        updated_at=_latest_updated_at(context.statuses, context.fallback),
    )


def capability_has_activity(item: ProviderCapabilityStatus) -> bool:
    return bool(item.last_success or item.last_error or _count(item.success_count) or _count(item.failure_count))


def _provider_aggregation_context(
    statuses: list[ProviderCapabilityStatus],
    fallback: ProviderStatus | None,
) -> ProviderAggregationContext:
    ordered_statuses = _sort_statuses(statuses)
    enabled_statuses = [item for item in ordered_statuses if item.enabled]
    active_statuses = [item for item in enabled_statuses if capability_has_activity(item)]
    history = fallback if fallback and not active_statuses else None
    return ProviderAggregationContext(
        statuses=ordered_statuses,
        enabled_statuses=enabled_statuses,
        active_statuses=active_statuses,
        history=history,
        fallback=fallback,
    )


def _aggregate_enabled(context: ProviderAggregationContext) -> bool:
    if context.fallback and not context.fallback.enabled:
        return False
    if context.statuses:
        return bool(context.enabled_statuses)
    return context.fallback.enabled if context.fallback else False


def _aggregate_priority(context: ProviderAggregationContext) -> int:
    candidates = [_priority(item.priority) for item in _priority_rows(context)]
    fallback_priority = _priority(context.fallback.priority) if context.fallback else DEFAULT_PROVIDER_PRIORITY
    return min(candidates, default=fallback_priority)


def _priority_rows(context: ProviderAggregationContext) -> list[ProviderCapabilityStatus]:
    return context.enabled_statuses or context.statuses


def _aggregate_healthy(context: ProviderAggregationContext) -> bool:
    if not _aggregate_enabled(context):
        return False
    if not context.statuses:
        return context.fallback.healthy if context.fallback else False
    if not context.active_statuses:
        return context.fallback.healthy if context.fallback else True
    return any(item.healthy for item in context.active_statuses)


def _latest_success(statuses: list[ProviderCapabilityStatus], history: ProviderStatus | None) -> str | None:
    return max((item.last_success for item in statuses if item.last_success), default=history.last_success if history else None)


def _latest_error(statuses: list[ProviderCapabilityStatus], history: ProviderStatus | None) -> str | None:
    latest = _latest_by_updated_at([item for item in statuses if item.last_error])
    return latest.last_error if latest else (history.last_error if history else None)


def _latest_latency(statuses: list[ProviderCapabilityStatus], history: ProviderStatus | None) -> float | None:
    latest = _latest_by_updated_at([item for item in statuses if item.latency_ms is not None])
    return latest.latency_ms if latest else (history.latency_ms if history else None)


def _latest_updated_at(statuses: list[ProviderCapabilityStatus], fallback: ProviderStatus | None) -> str:
    return max((item.updated_at for item in statuses if item.updated_at), default=fallback.updated_at if fallback and fallback.updated_at else now_text())


def _latest_by_updated_at(statuses: list[ProviderCapabilityStatus]) -> ProviderCapabilityStatus | None:
    ordered_statuses = _sort_statuses(statuses)
    latest_updated_at = max((item.updated_at or "" for item in ordered_statuses), default=None)
    if latest_updated_at is None:
        return None
    return next((item for item in ordered_statuses if (item.updated_at or "") == latest_updated_at), None)


def _success_count(statuses: list[ProviderCapabilityStatus], history: ProviderStatus | None) -> int:
    return _count(history.success_count) if history else sum(_count(item.success_count) for item in statuses)


def _failure_count(statuses: list[ProviderCapabilityStatus], history: ProviderStatus | None) -> int:
    return _count(history.failure_count) if history else sum(_count(item.failure_count) for item in statuses)


def _sort_statuses(statuses: list[ProviderCapabilityStatus]) -> list[ProviderCapabilityStatus]:
    return sorted(statuses, key=lambda item: (_priority(item.priority), item.name, item.kind))


def _priority(value: int | None) -> int:
    return int(value) if value is not None else DEFAULT_PROVIDER_PRIORITY


def _count(value: int | None) -> int:
    return max(int(value or 0), 0)


__all__ = ["aggregate_provider_status", "capability_has_activity"]
