from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.services.provider_errors import sanitize_provider_error


DEFAULT_OPTIONAL_WORKBENCH_TIMEOUT_SECONDS = 1.5
T = TypeVar("T")


async def optional_workflow_value(
    datahub: object,
    load: Callable[[], Awaitable[T]],
    fallback: Callable[[Exception], T],
) -> T:
    try:
        return await asyncio.wait_for(load(), timeout=optional_timeout_seconds(datahub))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return fallback(exc)


def optional_timeout_seconds(datahub: object) -> float:
    settings = getattr(datahub, "settings", None)
    raw_value = getattr(settings, "workbench_optional_timeout_seconds", DEFAULT_OPTIONAL_WORKBENCH_TIMEOUT_SECONDS)
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_OPTIONAL_WORKBENCH_TIMEOUT_SECONDS
    return max(0.1, value)


def short_error(exc: Exception) -> str:
    text = sanitize_provider_error(exc).strip()
    return text[:140] if text else exc.__class__.__name__


__all__ = [
    "DEFAULT_OPTIONAL_WORKBENCH_TIMEOUT_SECONDS",
    "optional_timeout_seconds",
    "optional_workflow_value",
    "short_error",
]
