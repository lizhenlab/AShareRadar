from __future__ import annotations

from datetime import datetime
from typing import Iterable

from app.models.schemas import CacheStats, ProviderCapabilityStatus, ProviderStatus
from app.services.cache_freshness import assess_cache_freshness
from app.services.provider_failure_status import (
    capability_recently_failed as provider_capability_recently_failed,
    provider_recently_failed,
)
from app.services.scheduler_contracts import _CAPABILITY_LABELS, PROVIDER_FAILURE_DETAIL_LIMIT, HealthEvent
from app.services.scheduler_schedule import _positive_int_or_zero


def _data_health_events(
    stats: CacheStats,
    capability_rows: list[ProviderCapabilityStatus],
    provider_rows: list[ProviderStatus],
    settings,
    *,
    now: datetime | None = None,
) -> list[HealthEvent]:
    assessment = assess_cache_freshness(
        stats,
        now=now or datetime.now(),
        stock_pool_cache_seconds=getattr(settings, "stock_pool_cache_seconds", 24 * 60 * 60),
        plate_rank_cache_seconds=getattr(settings, "plate_rank_cache_seconds", 10 * 60),
    )
    events = [
        *_provider_health_events(capability_rows, provider_rows),
        *(HealthEvent("warning", issue.category, issue.message) for issue in assessment.issues),
    ]
    if events:
        return events
    checked_domains = list(assessment.checked_domains)
    if capability_rows or provider_rows:
        checked_domains.append("数据源状态")
    return [HealthEvent("info", "health", f"{'、'.join(checked_domains)}均正常")]


def _provider_health_events(
    capability_rows: list[ProviderCapabilityStatus],
    provider_rows: list[ProviderStatus],
) -> list[HealthEvent]:
    failures = _recent_provider_failures(capability_rows, provider_rows)
    if not failures:
        return []
    return [
        HealthEvent(
            "warning",
            "provider",
            "数据源最近存在失败：" + "、".join(failures[:PROVIDER_FAILURE_DETAIL_LIMIT]),
        )
    ]


def _recent_provider_failures(
    capability_rows: list[ProviderCapabilityStatus],
    provider_rows: list[ProviderStatus],
) -> list[str]:
    capability_failures = _recent_capability_failures(capability_rows)
    if capability_failures:
        return capability_failures
    return _unhealthy_provider_failures(provider_rows)


def _recent_capability_failures(capability_rows: list[ProviderCapabilityStatus]) -> list[str]:
    return _unique_texts(
        f"{item.name} {_capability_label(item.kind)}" for item in sorted(capability_rows, key=_capability_sort_key) if provider_capability_recently_failed(item)
    )


def _unhealthy_provider_failures(provider_rows: list[ProviderStatus]) -> list[str]:
    return _unique_texts(item.name for item in sorted(provider_rows, key=_provider_sort_key) if provider_recently_failed(item))


def _runtime_cleanup_message(removed: dict[str, int]) -> str | None:
    cleanup_total = sum(_positive_int_or_zero(count) for count in removed.values())
    if not cleanup_total:
        return None
    return f"已清理 {cleanup_total} 条过期运行记录"


def _provider_sort_key(item: ProviderStatus) -> tuple[int, str]:
    return (item.priority, item.name.casefold())


def _capability_sort_key(item: ProviderCapabilityStatus) -> tuple[int, str, str]:
    return (item.priority, item.name.casefold(), item.kind.casefold())


def _unique_texts(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _capability_label(kind: str) -> str:
    return _CAPABILITY_LABELS.get(kind, kind)
