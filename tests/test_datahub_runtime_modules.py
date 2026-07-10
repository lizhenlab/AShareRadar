from __future__ import annotations

import asyncio

from app.config import Settings
from app.services.datahub_runtime import ProviderRuntime, provider_source_name


def test_provider_runtime_attempts_are_lazy_and_skip_unavailable_sources() -> None:
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))
    errors: list[str] = []

    attempts = runtime.attempts([(1, "live"), (2, "missing")], {"live": object()}, "quote", errors)
    first = next(attempts)

    assert first.name == "live"
    assert errors == []
    assert list(attempts) == []
    assert errors == ["missing: 数据源未注册"]


def test_provider_runtime_attempts_report_cooling_without_provider_lookup() -> None:
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))
    runtime.record_failure("cooling", 1, RuntimeError("down"), "quote")
    errors: list[str] = []

    attempts = list(runtime.attempts([(1, "cooling"), (2, "live")], {"live": object()}, "quote", errors))

    assert [attempt.name for attempt in attempts] == ["live"]
    assert errors == ["cooling: 最近失败，短暂冷却中"]


def test_provider_runtime_timed_call_returns_value_and_latency() -> None:
    async def run_check() -> tuple[str, float]:
        cache = _FailingStatusCache()
        runtime = ProviderRuntime(cache, Settings(provider_call_timeout_seconds=1))
        result = await runtime.timed_call(_async_value("ok"))
        return result.value, result.latency_ms

    value, latency_ms = asyncio.run(run_check())

    assert value == "ok"
    assert latency_ms >= 0


def test_provider_source_name_falls_back_for_blank_sources() -> None:
    assert provider_source_name(type("Provider", (), {"source_name": "  "})(), "fallback") == "fallback"
    assert provider_source_name(type("Provider", (), {"source_name": "实时源"})(), "fallback") == "实时源"


def test_provider_runtime_status_write_failures_do_not_escape_or_block_cooldown() -> None:
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))

    runtime.record_success("broken", 1, 12.0, "quote")
    runtime.record_failure("broken", 1, RuntimeError("provider down"), "quote")

    assert runtime.is_cooling("broken", "quote") is True
    assert cache.success_calls == [("broken", "quote", 1, 12.0)]
    assert cache.failure_calls == [("broken", "quote", 1, "provider down")]


def test_provider_runtime_attempt_failure_uses_readable_error_for_blank_exception() -> None:
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))
    errors: list[str] = []
    attempt = next(runtime.attempts([(1, "blank")], {"blank": object()}, "quote", errors))

    runtime.record_attempt_failure(attempt, "quote", RuntimeError(), errors)

    assert errors == ["blank: RuntimeError"]
    assert cache.failure_calls == [("blank", "quote", 1, "RuntimeError")]


class _FailingStatusCache:
    def __init__(self) -> None:
        self.success_calls: list[tuple[str, str, int, float]] = []
        self.failure_calls: list[tuple[str, str, int, str]] = []

    def update_provider_capability_success(self, name: str, kind: str, priority: int, latency_ms: float) -> None:
        self.success_calls.append((name, kind, priority, latency_ms))
        raise RuntimeError("status db readonly")

    def update_provider_capability_failure(self, name: str, kind: str, priority: int, error: str) -> None:
        self.failure_calls.append((name, kind, priority, error))
        raise RuntimeError("status db readonly")


async def _async_value(value: str) -> str:
    return value
