from __future__ import annotations

import asyncio
from collections.abc import Awaitable
import time
from typing import TypeVar

from app.services.datahub_status import _provider_error_text


T = TypeVar("T")


class ProviderRuntime:
    def __init__(self, cache, settings) -> None:
        self.cache = cache
        self.settings = settings
        self._cooldowns: dict[tuple[str, str], float] = {}

    async def call(self, awaitable: Awaitable[T]) -> T:
        return await asyncio.wait_for(awaitable, timeout=self.settings.provider_call_timeout_seconds)

    def is_cooling(self, name: str, kind: str = "general") -> bool:
        until = self._cooldowns.get((name, kind))
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        self._cooldowns.pop((name, kind), None)
        return False

    def record_success(self, name: str, index: int, latency_ms: float, kind: str) -> None:
        self.cache.update_provider_capability_success(name, kind, index, latency_ms)
        self.clear_cooldown(name, kind)

    def record_failure(self, name: str, index: int, exc: Exception, kind: str) -> None:
        self.cache.update_provider_capability_failure(name, kind, index, _provider_error_text(exc))
        cooldown_seconds = max(0, self.settings.provider_failure_cooldown_seconds)
        if cooldown_seconds:
            self._cooldowns[(name, kind)] = time.monotonic() + cooldown_seconds

    def clear_cooldown(self, name: str, kind: str = "general") -> None:
        self._cooldowns.pop((name, kind), None)
