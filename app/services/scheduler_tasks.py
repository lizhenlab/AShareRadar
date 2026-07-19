from __future__ import annotations

import asyncio
from datetime import datetime

from app.services.scheduler_contracts import (
    TASK_STATUS_DEGRADED,
    KlineRefreshSummary,
    SchedulerRuntimeContext,
    TaskExecutionResult,
)
from app.services.scheduler_health import _data_health_events, _runtime_cleanup_message
from app.services.scheduler_helpers import (
    _kline_failure_detail,
    _kline_refresh_message,
    _offload,
    _quote_refresh_message,
    _quote_refresh_summary,
    _rows_used_fallback_cache,
    _save_symbol_skip_event,
    _scheduler_cache_symbols,
    _short_task_error,
)


class SchedulerTaskHandlersMixin(SchedulerRuntimeContext):
    async def _refresh_watch_quotes(self) -> str:
        symbols, skipped_count = await _offload(
            _scheduler_cache_symbols,
            self.datahub.cache,
            self.settings.seed_symbols,
        )
        await _offload(_save_symbol_skip_event, self.datahub.cache, "quote", "观察池报价刷新", skipped_count)
        if not symbols:
            message = "无有效观察个股，已跳过报价刷新"
            await self._save_monitor_event("warning", "quote", message)
            return message
        quotes = await self.datahub.quotes(symbols, use_cache=False)
        summary = _quote_refresh_summary(symbols, quotes)
        message = _quote_refresh_message(summary)
        level = "warning" if summary.fallback_symbols or summary.missing_symbols else "info"
        await self._save_monitor_event(level, "quote", message)
        if summary.returned == 0:
            raise RuntimeError(message)
        if summary.fallback_symbols or summary.missing_symbols:
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        return message

    async def _refresh_key_klines(self) -> str:
        symbols, skipped_count = await _offload(
            _scheduler_cache_symbols,
            self.datahub.cache,
            self.settings.seed_symbols,
            limit=self.settings.scheduler_kline_symbols_limit,
        )
        await _offload(_save_symbol_skip_event, self.datahub.cache, "kline", "关键个股K线刷新", skipped_count)
        if not symbols:
            message = "无有效关键个股，已跳过日K线刷新"
            await self._save_monitor_event("warning", "kline", message)
            return message
        summary = await self._refresh_key_kline_symbols(symbols)
        await self._save_kline_refresh_failure_event(summary.failures)
        if summary.failures and summary.refreshed == 0:
            raise RuntimeError(f"关键个股日K线全部刷新失败：{_kline_failure_detail(summary.failures)}")
        message = _kline_refresh_message(summary)
        level = "warning" if summary.failures or summary.fallback_cache else "info"
        await self._save_monitor_event(level, "kline", message)
        if summary.failures or summary.fallback_cache:
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        return message

    async def _refresh_key_kline_symbols(self, symbols: list[str]) -> KlineRefreshSummary:
        refreshed = 0
        fallback_cache = 0
        failures = []
        for symbol in symbols:
            failure = await self._refresh_single_key_kline(symbol)
            if failure is None:
                refreshed += 1
            elif failure == "fallback-cache":
                fallback_cache += 1
            else:
                failures.append(failure)
            await asyncio.sleep(0)
        return KlineRefreshSummary(refreshed=refreshed, fallback_cache=fallback_cache, failures=tuple(failures))

    async def _refresh_single_key_kline(self, symbol: str) -> str | None:
        try:
            klines = await self.datahub.kline(symbol, 120, use_cache=False)
        except Exception as exc:
            return f"{symbol}: {_short_task_error(exc)}"
        if not klines:
            return f"{symbol}: 返回空K线"
        if _rows_used_fallback_cache(klines):
            return "fallback-cache"
        return None

    async def _save_kline_refresh_failure_event(self, failures: tuple[str, ...]) -> None:
        if failures:
            await self._save_monitor_event(
                "warning",
                "kline",
                f"关键个股K线刷新失败 {len(failures)} 只：{_kline_failure_detail(failures)}",
            )

    async def _refresh_plate_rank(self) -> str:
        result = await self.datahub.plate_rank_result(limit=20, refresh=True)
        if result.used_fallback_cache:
            message = f"行业背景数据源不可用，使用缓存 {len(result.rows)} 条"
            await self._save_monitor_event("warning", "plate", message)
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        message = f"已刷新 {len(result.rows)} 条行业背景数据"
        await self._save_monitor_event("info", "plate", message)
        return message

    async def _check_data_health(self, *, now: datetime | None = None) -> str:
        stats, capability_rows, provider_rows = await asyncio.gather(
            _offload(self.datahub.cache.stats),
            _offload(self.datahub.cache.provider_capability_statuses),
            _offload(self.datahub.cache.provider_statuses),
        )
        health_events = _data_health_events(stats, capability_rows, provider_rows, self.settings, now=now)
        for event in health_events:
            await self._save_monitor_event(event.level, event.category, event.message)
        events = [event.message for event in health_events]

        removed = await _offload(self.datahub.cache.maintenance_repo.cleanup_regenerable_runtime_rows)
        if cleanup_message := _runtime_cleanup_message(removed):
            events.append(cleanup_message)
        return "；".join(events)

    async def _evaluate_alerts(self) -> str:
        from app.services.alerts import evaluate_alert_rules

        summary = await evaluate_alert_rules(self.datahub)
        message = f"已评估 {summary.checked_count} 条本地预警，" f"当前触发 {summary.triggered_count} 条，新增事件 {summary.new_event_count} 条"
        if summary.failed_count:
            message += f"，失败 {summary.failed_count} 条"
        level = "warning" if summary.triggered_count or summary.failed_count else "info"
        await self._save_monitor_event(level, "alert", message)
        if summary.checked_count and summary.failed_count == summary.checked_count:
            raise RuntimeError(message)
        if summary.failed_count:
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        return message
