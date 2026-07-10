from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable, Iterator, Mapping
from dataclasses import dataclass
import time
from typing import Generic, TypeVar

from app.services.datahub_status import _provider_error_text


T = TypeVar("T")


@dataclass(frozen=True)
class ProviderAttempt:
    index: int
    name: str
    provider: object


@dataclass(frozen=True)
class TimedProviderCall(Generic[T]):
    value: T
    latency_ms: float


class ProviderRuntime:
    def __init__(self, cache, settings) -> None:
        self.cache = cache
        self.settings = settings
        self._cooldowns: dict[tuple[str, str], float] = {}

    async def call(self, awaitable: Awaitable[T]) -> T:
        return await asyncio.wait_for(awaitable, timeout=self.settings.provider_call_timeout_seconds)

    async def timed_call(self, awaitable: Awaitable[T]) -> TimedProviderCall[T]:
        started = time.perf_counter()
        value = await self.call(awaitable)
        return TimedProviderCall(value=value, latency_ms=round((time.perf_counter() - started) * 1000, 2))

    def attempts(
        self,
        priority_rows: Iterable[tuple[int, str]],
        providers: Mapping[str, object],
        kind: str,
        errors: list[str],
    ) -> Iterator[ProviderAttempt]:
        for index, name in priority_rows:
            if self.is_cooling(name, kind):
                errors.append(f"{name}: 最近失败，短暂冷却中")
                continue
            provider = providers.get(name)
            if provider is None:
                errors.append(f"{name}: 数据源未注册")
                continue
            yield ProviderAttempt(index=index, name=name, provider=provider)

    def is_cooling(self, name: str, kind: str = "general") -> bool:
        until = self._cooldowns.get((name, kind))
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        self._cooldowns.pop((name, kind), None)
        return False

    def record_success(self, name: str, index: int, latency_ms: float, kind: str) -> None:
        try:
            self.cache.update_provider_capability_success(name, kind, index, latency_ms)
        except Exception:
            pass
        self.clear_cooldown(name, kind)

    def record_attempt_success(self, attempt: ProviderAttempt, kind: str, latency_ms: float) -> None:
        self.record_success(attempt.name, attempt.index, latency_ms, kind)

    def record_failure(self, name: str, index: int, exc: Exception, kind: str) -> None:
        try:
            self.cache.update_provider_capability_failure(name, kind, index, _provider_error_text(exc))
        except Exception:
            pass
        cooldown_seconds = max(0, self.settings.provider_failure_cooldown_seconds)
        if cooldown_seconds:
            self._cooldowns[(name, kind)] = time.monotonic() + cooldown_seconds

    def record_attempt_failure(
        self,
        attempt: ProviderAttempt,
        kind: str,
        exc: Exception,
        errors: list[str] | None = None,
        record_failure=None,
    ) -> None:
        if errors is not None:
            errors.append(f"{attempt.name}: {_provider_error_text(exc)}")
        if record_failure is None:
            self.record_failure(attempt.name, attempt.index, exc, kind)
        else:
            record_failure(attempt.name, attempt.index, exc)

    def clear_cooldown(self, name: str, kind: str = "general") -> None:
        self._cooldowns.pop((name, kind), None)


def provider_source_name(provider: object, fallback: str) -> str:
    source = getattr(provider, "source_name", None)
    if isinstance(source, str) and source.strip():
        return source
    return fallback
