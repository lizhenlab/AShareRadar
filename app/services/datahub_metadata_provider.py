from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.services.datahub_runtime import ProviderAttempt, provider_source_name, run_cache_io_best_effort


T = TypeVar("T")


def _metadata_kind_label(kind: str) -> str:
    return {"stock": "股票池", "plate": "板块", "concept": "概念"}.get(kind, kind)


def _no_provider_message(kind: str) -> str:
    return f"{_metadata_kind_label(kind)}未配置可用数据源"


def _unsupported_provider_message(attempt: ProviderAttempt, kind: str) -> str:
    source = provider_source_name(attempt.provider, attempt.name)
    return f"{attempt.name}: {source} 不支持{_metadata_kind_label(kind)}能力"


def _provider_call(provider: object, method_name: str, *args, **kwargs) -> Awaitable[list[T]] | None:
    method = getattr(provider, method_name, None)
    if not callable(method):
        return None
    return method(*args, **kwargs)


async def _required_provider_call(
    provider: object,
    method_name: str,
    capability_label: str,
    *args,
    **kwargs,
) -> list[T]:
    awaitable: Awaitable[list[T]] | None = _provider_call(provider, method_name, *args, **kwargs)
    if awaitable is None:
        raise RuntimeError(f"数据源不支持{capability_label}能力")
    return await awaitable


async def _required_metadata_call(
    call: Callable[[object], Awaitable[list[T]] | None],
    provider: object,
    kind: str,
) -> list[T]:
    awaitable = call(provider)
    if awaitable is None:
        raise RuntimeError(f"数据源不支持{_metadata_kind_label(kind)}能力")
    return await awaitable


def _non_empty_metadata_rows(rows: list[T], error: str) -> list[T]:
    if not rows:
        raise RuntimeError(error)
    return rows


async def _save_metadata_best_effort(save: Callable[[list[T]], None], rows: list[T]) -> None:
    await run_cache_io_best_effort(save, rows)


async def _safe_log_metadata_event_async(cache: object, category: str, message: str) -> None:
    log_event = getattr(cache, "log_event", None)
    if callable(log_event):
        await run_cache_io_best_effort(log_event, category, message)


def _safe_log_metadata_event(cache: object, category: str, message: str) -> None:
    log_event = getattr(cache, "log_event", None)
    if not callable(log_event):
        return
    try:
        log_event(category, message)
    except Exception:
        pass


def _metadata_error_detail(errors: list[str], fallback: str) -> str:
    return "；".join(errors) if errors else fallback
