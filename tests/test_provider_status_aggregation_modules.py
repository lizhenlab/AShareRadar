from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.schemas import ProviderCapabilityStatus, ProviderStatus
from app.repositories.provider_status_aggregation import aggregate_provider_status
from app.services.cache import SQLiteCache


def test_config_only_disabled_capability_preserves_history_but_disables_provider() -> None:
    fallback = _provider_status(
        enabled=True,
        healthy=True,
        priority=5,
        last_success="2026-05-13 09:30:00",
        latency_ms=12.5,
        success_count=7,
        failure_count=1,
        updated_at="2026-05-13 09:30:00",
    )
    capability = _capability_status(
        kind="quote",
        enabled=False,
        healthy=False,
        priority=1,
        updated_at="2026-05-13 09:35:00",
    )

    aggregate = aggregate_provider_status("akshare", [capability], fallback)

    assert aggregate.enabled is False
    assert aggregate.healthy is False
    assert aggregate.priority == 1
    assert aggregate.last_success == "2026-05-13 09:30:00"
    assert aggregate.latency_ms == 12.5
    assert aggregate.success_count == 7
    assert aggregate.failure_count == 1
    assert aggregate.updated_at == "2026-05-13 09:35:00"


def test_disabled_capability_activity_does_not_override_fallback_metrics() -> None:
    fallback = _provider_status(
        enabled=True,
        healthy=True,
        priority=5,
        last_success="2026-05-13 09:30:00",
        latency_ms=12.5,
        success_count=7,
        failure_count=1,
        updated_at="2026-05-13 09:30:00",
    )
    disabled_failure = _capability_status(
        kind="quote",
        enabled=False,
        healthy=False,
        priority=1,
        last_error="old disabled failure",
        failure_count=9,
        updated_at="2026-05-13 09:35:00",
    )

    aggregate = aggregate_provider_status("akshare", [disabled_failure], fallback)

    assert aggregate.enabled is False
    assert aggregate.healthy is False
    assert aggregate.last_success == "2026-05-13 09:30:00"
    assert aggregate.last_error is None
    assert aggregate.success_count == 7
    assert aggregate.failure_count == 1


def test_config_only_enabled_capability_preserves_fallback_health_and_updates_priority() -> None:
    fallback = _provider_status(enabled=True, healthy=True, priority=5, success_count=3, updated_at="2026-05-13 09:30:00")
    capability = _capability_status(kind="quote", enabled=True, healthy=False, priority=1, updated_at="2026-05-13 09:35:00")

    aggregate = aggregate_provider_status("akshare", [capability], fallback)

    assert aggregate.enabled is True
    assert aggregate.healthy is True
    assert aggregate.priority == 1
    assert aggregate.success_count == 3


def test_disabled_fallback_provider_keeps_aggregate_disabled_with_active_capability() -> None:
    fallback = _provider_status(enabled=False, healthy=False, priority=5, updated_at="2026-05-13 09:30:00")
    capability = _capability_status(
        kind="quote",
        enabled=True,
        healthy=True,
        priority=1,
        last_success="2026-05-13 09:40:00",
        success_count=1,
        updated_at="2026-05-13 09:40:00",
    )

    aggregate = aggregate_provider_status("akshare", [capability], fallback)

    assert aggregate.enabled is False
    assert aggregate.healthy is False
    assert aggregate.success_count == 1


def test_active_capabilities_drive_health_error_latency_and_counts() -> None:
    quote = _capability_status(
        kind="quote",
        enabled=True,
        healthy=True,
        priority=1,
        last_success="2026-05-13 09:40:00",
        latency_ms=20,
        success_count=2,
        updated_at="2026-05-13 09:40:00",
    )
    kline = _capability_status(
        kind="kline",
        enabled=True,
        healthy=False,
        priority=2,
        last_error="kline down",
        failure_count=3,
        updated_at="2026-05-13 09:45:00",
    )

    aggregate = aggregate_provider_status("akshare", [quote, kline], None)

    assert aggregate.enabled is True
    assert aggregate.healthy is True
    assert aggregate.last_success == "2026-05-13 09:40:00"
    assert aggregate.last_error == "kline down"
    assert aggregate.latency_ms == 20
    assert aggregate.success_count == 2
    assert aggregate.failure_count == 3


def test_latest_error_and_latency_tie_break_by_priority_not_input_order() -> None:
    timestamp = "2026-05-13 09:45:00"
    lower_priority = _capability_status(
        kind="kline",
        enabled=True,
        healthy=False,
        priority=2,
        last_error="kline down",
        latency_ms=40,
        failure_count=1,
        updated_at=timestamp,
    )
    higher_priority = _capability_status(
        kind="quote",
        enabled=True,
        healthy=False,
        priority=1,
        last_error="quote down",
        latency_ms=20,
        failure_count=1,
        updated_at=timestamp,
    )

    aggregate = aggregate_provider_status("akshare", [lower_priority, higher_priority], None)

    assert aggregate.last_error == "quote down"
    assert aggregate.latency_ms == 20
    assert aggregate.failure_count == 2


def test_invalid_negative_counts_do_not_create_activity_or_reduce_history() -> None:
    fallback = _provider_status(
        enabled=True,
        healthy=True,
        priority=5,
        success_count=3,
        failure_count=1,
        updated_at="2026-05-13 09:30:00",
    )
    capability = _capability_status(
        kind="quote",
        enabled=True,
        healthy=False,
        priority=1,
        success_count=-5,
        failure_count=-2,
        updated_at="2026-05-13 09:35:00",
    )

    aggregate = aggregate_provider_status("akshare", [capability], fallback)

    assert aggregate.healthy is True
    assert aggregate.priority == 1
    assert aggregate.success_count == 3
    assert aggregate.failure_count == 1


def test_unprobed_enabled_capability_without_fallback_is_not_recent_failure() -> None:
    capability = _capability_status(kind="quote", enabled=True, healthy=False, priority=2, updated_at="2026-05-13 09:35:00")

    aggregate = aggregate_provider_status("new_source", [capability], None)

    assert aggregate.enabled is True
    assert aggregate.healthy is True
    assert aggregate.last_error is None
    assert aggregate.success_count == 0
    assert aggregate.failure_count == 0


def test_provider_status_repository_orders_provider_and_capability_ties_stably() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.ensure_provider("zeta", 1, enabled=True)
        cache.ensure_provider("alpha", 1, enabled=True)
        cache.ensure_provider_capability("zeta", "quote", 1, enabled=True)
        cache.ensure_provider_capability("alpha", "quote", 1, enabled=True)
        cache.ensure_provider_capability("alpha", "kline", 1, enabled=True)

        providers = cache.provider_statuses()
        capabilities = cache.provider_capability_statuses()

    assert [item.name for item in providers] == ["alpha", "zeta"]
    assert [(item.name, item.kind) for item in capabilities] == [
        ("alpha", "kline"),
        ("alpha", "quote"),
        ("zeta", "quote"),
    ]


def _provider_status(
    *,
    enabled: bool,
    healthy: bool,
    priority: int,
    last_success: str | None = None,
    last_error: str | None = None,
    latency_ms: float | None = None,
    success_count: int = 0,
    failure_count: int = 0,
    updated_at: str | None = None,
) -> ProviderStatus:
    return ProviderStatus(
        name="akshare",
        enabled=enabled,
        priority=priority,
        healthy=healthy,
        last_success=last_success,
        last_error=last_error,
        latency_ms=latency_ms,
        success_count=success_count,
        failure_count=failure_count,
        updated_at=updated_at,
    )


def _capability_status(
    *,
    kind: str,
    enabled: bool,
    healthy: bool,
    priority: int,
    last_success: str | None = None,
    last_error: str | None = None,
    latency_ms: float | None = None,
    success_count: int = 0,
    failure_count: int = 0,
    updated_at: str | None = None,
) -> ProviderCapabilityStatus:
    return ProviderCapabilityStatus(
        name="akshare",
        kind=kind,
        enabled=enabled,
        priority=priority,
        healthy=healthy,
        last_success=last_success,
        last_error=last_error,
        latency_ms=latency_ms,
        success_count=success_count,
        failure_count=failure_count,
        updated_at=updated_at,
    )
