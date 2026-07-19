from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from app.services.provider_errors import sanitize_provider_error
from app.services.scheduler_contracts import (
    KLINE_FAILURE_DETAIL_LIMIT,
    PROVIDER_FAILURE_DETAIL_LIMIT,
    TASK_ERROR_MAX_LENGTH,
    FileSchedulerInstanceGuard,
    KlineRefreshSummary,
    NoopSchedulerInstanceGuard,
    QuoteRefreshSummary,
    SchedulerInstanceGuard,
)
from app.services.scheduler_schedule import _positive_int_or_none
from app.utils.fallback_logging import report_persistence_failure
from app.utils.symbols import standard_symbol_list


T = TypeVar("T")


async def _offload(call: Callable[..., T], *args, **kwargs) -> T:
    return await asyncio.to_thread(partial(call, *args, **kwargs))


async def _wait_for_tasks_bounded(
    tasks: Iterable[asyncio.Task],
    *,
    timeout: float,
    cancel_first: bool = False,
) -> set[asyncio.Task]:
    pending_tasks = set(tasks)
    if not pending_tasks:
        return set()
    for task in pending_tasks:
        task.add_done_callback(_consume_future_exception)
        if cancel_first:
            task.cancel()
    done, pending = await asyncio.wait(pending_tasks, timeout=timeout)
    for task in done:
        _consume_future_exception(task)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.sleep(0)
    return {task for task in pending if not task.done()}


def _consume_future_exception(future: asyncio.Future) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except asyncio.CancelledError:
        pass


def _default_instance_guard(datahub) -> SchedulerInstanceGuard:
    cache_path = getattr(getattr(datahub, "cache", None), "path", None)
    if cache_path is None:
        return NoopSchedulerInstanceGuard()
    return FileSchedulerInstanceGuard(Path(f"{cache_path}.scheduler.lock"))


def _scheduler_cache_symbols(
    cache,
    seed_symbols: Iterable[object] | None,
    *,
    limit: int | None = None,
) -> tuple[list[str], int]:
    selection_reader = getattr(cache, "watchlist_symbol_selection", None)
    if not callable(selection_reader):
        return _scheduler_symbols(cache.watchlist_symbols(), seed_symbols, limit=limit)
    selection = selection_reader()
    return _scheduler_symbols(
        selection.active_symbols,
        seed_symbols,
        excluded_symbols=selection.excluded_symbols,
        has_entries=selection.has_entries,
        limit=limit,
    )


def _scheduler_symbols(
    watchlist_symbols: Iterable[object] | None,
    seed_symbols: Iterable[object] | None,
    *,
    excluded_symbols: Iterable[object] | None = None,
    has_entries: bool | None = None,
    limit: int | None = None,
) -> tuple[list[str], int]:
    watchlist = list(watchlist_symbols or [])
    if has_entries is not None:
        symbols, skipped_count = _normalize_unique_symbols(watchlist)
        excluded, excluded_skipped_count = _normalize_unique_symbols(excluded_symbols or [])
        skipped_count += excluded_skipped_count
        if symbols:
            return _limit_symbols(symbols, limit), skipped_count
        if has_entries:
            return [], skipped_count
        seeds, seed_skipped_count = _normalize_unique_symbols(seed_symbols or [])
        skipped_count += seed_skipped_count
        excluded_set = set(excluded)
        return _limit_symbols([symbol for symbol in seeds if symbol not in excluded_set], limit), skipped_count

    raw_symbols = watchlist if watchlist else list(seed_symbols or [])
    symbols, skipped_count = _normalize_unique_symbols(raw_symbols)
    if watchlist and not symbols:
        symbols, seed_skipped_count = _normalize_unique_symbols(seed_symbols or [])
        skipped_count += seed_skipped_count
    return _limit_symbols(symbols, limit), skipped_count


def _normalize_unique_symbols(symbols: Iterable[object]) -> tuple[list[str], int]:
    result = standard_symbol_list(symbols, skip_invalid=True, count_duplicates_as_skipped=True)
    return result.symbols, result.skipped_count


def _limit_symbols(symbols: list[str], limit: int | None) -> list[str]:
    limit_count = _positive_int_or_none(limit)
    if limit_count is None:
        return symbols
    return symbols[:limit_count]


def _save_symbol_skip_event(cache, category: str, context: str, skipped_count: int) -> None:
    if skipped_count:
        cache.save_monitor_event(
            "warning",
            category,
            f"{context}剔除 {skipped_count} 个重复或无效股票代码",
        )


def _kline_failure_detail(failures: tuple[str, ...]) -> str:
    return "；".join(failures[:KLINE_FAILURE_DETAIL_LIMIT])


def _quote_refresh_summary(symbols: list[str], quotes: Iterable[object]) -> QuoteRefreshSummary:
    requested_symbols = tuple(dict.fromkeys(symbols))
    requested_set = set(requested_symbols)
    returned: dict[str, object] = {}
    for quote in quotes:
        symbol = _quote_symbol(quote)
        if symbol in requested_set:
            returned[symbol] = quote
    fallback_symbols = tuple(symbol for symbol in requested_symbols if symbol in returned and _item_used_fallback_cache(returned[symbol]))
    missing_symbols = tuple(symbol for symbol in requested_symbols if symbol not in returned)
    return QuoteRefreshSummary(
        requested=len(requested_symbols),
        refreshed=len(returned) - len(fallback_symbols),
        fallback_symbols=fallback_symbols,
        missing_symbols=missing_symbols,
    )


def _quote_refresh_message(summary: QuoteRefreshSummary) -> str:
    if summary.returned == 0:
        return f"观察池报价全部缺失 {summary.requested} 只：{_quote_missing_detail(summary.missing_symbols)}"
    message = f"已刷新 {summary.refreshed} 只观察个股报价"
    if summary.fallback_symbols:
        message += f"，兜底缓存 {len(summary.fallback_symbols)} 只：{_quote_fallback_detail(summary.fallback_symbols)}"
    if summary.missing_symbols:
        message += f"，缺失 {len(summary.missing_symbols)} 只：{_quote_missing_detail(summary.missing_symbols)}"
    return message


def _quote_fallback_detail(symbols: tuple[str, ...]) -> str:
    return "、".join(symbols[:PROVIDER_FAILURE_DETAIL_LIMIT])


def _quote_missing_detail(symbols: tuple[str, ...]) -> str:
    return "、".join(symbols[:PROVIDER_FAILURE_DETAIL_LIMIT])


def _kline_refresh_message(summary: KlineRefreshSummary) -> str:
    message = f"已刷新 {summary.refreshed} 只关键个股日K线"
    if summary.fallback_cache:
        message += f"，兜底缓存 {summary.fallback_cache} 只"
    if summary.failures:
        message += f"，失败 {len(summary.failures)} 只"
    return message


def _quote_symbol(item: object) -> str:
    code = str(getattr(item, "code", "") or "").strip()
    market = str(getattr(item, "market", "") or "").strip()
    raw = f"{code}.{market}" if code and market else ""
    symbols = standard_symbol_list([raw], skip_invalid=True).symbols
    return symbols[0] if symbols else "--"


def _rows_used_fallback_cache(rows: Iterable[object]) -> bool:
    return any(_item_used_fallback_cache(item) for item in rows)


def _item_used_fallback_cache(item: object) -> bool:
    return bool(getattr(item, "fallback_used", False))


def _task_error_message(exc: Exception) -> str:
    text = " ".join(sanitize_provider_error(exc).strip().split())
    if not text:
        return exc.__class__.__name__
    return text[:TASK_ERROR_MAX_LENGTH]


def _record_task_end(cache, run_id: int | None, status: str, message: str) -> None:
    if run_id is not None:
        _finish_task_run_quietly(cache, run_id, status, message)
    try:
        cache.save_monitor_event("warning", "task", message, symbol=None)
    except Exception as exc:
        report_persistence_failure("scheduler task event persistence failed", exc)


def _finish_task_run_quietly(cache, run_id: int, status: str, message: str) -> None:
    try:
        cache.finish_task_run(run_id, status, message)
    except Exception as exc:
        report_persistence_failure("scheduler task run persistence failed", exc)


def _short_task_error(exc: Exception) -> str:
    return _task_error_message(exc)
