from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from app.services.provider_errors import ProviderChainUnavailable


PROVIDER_RECOVERY_POLL_SECONDS = 0.5


@dataclass
class ProviderWaitBudget:
    remaining_seconds: float


async def wait_for_provider_recovery(
    errors: tuple[ProviderChainUnavailable, ...],
    *,
    kind: str,
    attempt: int,
    max_attempts: int,
    wait_budget: ProviderWaitBudget,
    cancel_event: asyncio.Event,
    retry_backoff_seconds: float,
    chain_state: Callable[[str], object | None],
) -> None:
    if attempt >= max_attempts:
        raise errors[0]
    delay = _recovery_delay(errors, attempt, retry_backoff_seconds)
    if delay <= 0:
        return
    budget = max(0.0, wait_budget.remaining_seconds)
    if budget <= 0 or delay > budget:
        raise errors[0]
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await _wait_until_ready_or_delay(
            errors[0],
            kind=kind,
            delay=delay,
            cancel_event=cancel_event,
            chain_state=chain_state,
        )
    finally:
        wait_budget.remaining_seconds = max(
            0.0,
            wait_budget.remaining_seconds - (loop.time() - started),
        )


def _recovery_delay(
    errors: tuple[ProviderChainUnavailable, ...],
    attempt: int,
    retry_backoff_seconds: float,
) -> float:
    suggested = [
        value
        for error in errors
        if (value := error.retry_after_seconds) is not None and value > 0
    ]
    return max(retry_backoff_seconds * attempt, min(suggested) if suggested else 0.0)


async def _wait_until_ready_or_delay(
    error: ProviderChainUnavailable,
    *,
    kind: str,
    delay: float,
    cancel_event: asyncio.Event,
    chain_state: Callable[[str], object | None],
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while True:
        status = getattr(chain_state(kind), "status", None)
        if status == "ready":
            return
        if status == "permanent_unavailable":
            raise error
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(
                cancel_event.wait(),
                timeout=min(PROVIDER_RECOVERY_POLL_SECONDS, remaining),
            )
        except TimeoutError:
            continue
        raise asyncio.CancelledError


__all__ = ["ProviderWaitBudget", "wait_for_provider_recovery"]
