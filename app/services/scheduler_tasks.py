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
from app.services.trading_calendar import DAILY_KLINE_PUBLISH_TIME, is_trading_day
from app.utils.clock import market_now_naive
from app.utils.market_time import market_local_naive


RESEARCH_QUEUE_REFRESH_BATCH_LIMIT = 20
DUE_REVIEW_EVALUATION_BATCH_LIMIT = 20


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

    async def _refresh_research_queue(self, *, now: datetime | None = None) -> str:
        if not _research_maintenance_window_open(now):
            message = "当前不在交易日盘后日K发布窗口，已跳过主动研究刷新"
            await self._save_monitor_event("info", "research", message)
            return message
        from app.workflows.individual import refresh_active_research_queue

        summary = await refresh_active_research_queue(
            self.datahub,
            now=now,
            limit=RESEARCH_QUEUE_REFRESH_BATCH_LIMIT,
        )
        message = (
            f"主动研究队列选取 {summary.selected_count}/{summary.active_count} 只，"
            f"保存 {summary.saved_count} 只，未变化 {summary.unchanged_count} 只，"
            f"跳过 {summary.skipped_count} 只，失败 {summary.failed_count} 只"
        )
        degraded = summary.failed_count > 0 or summary.skipped_count > 0
        await self._save_monitor_event("warning" if degraded else "info", "research", message)
        if summary.selected_count and summary.failed_count == summary.selected_count:
            raise RuntimeError(message)
        if degraded:
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        return message

    async def _evaluate_due_reviews(self, *, now: datetime | None = None) -> str:
        if not _research_maintenance_window_open(now):
            message = "当前不在交易日盘后日K发布窗口，已跳过到期研究计划评估"
            await self._save_monitor_event("info", "review", message)
            return message
        from app.services.advice_review import evaluate_due_advice_reviews

        summary = await evaluate_due_advice_reviews(
            self.datahub,
            as_of=now,
            now=now,
            limit=DUE_REVIEW_EVALUATION_BATCH_LIMIT,
        )
        message = f"到期研究计划候选 {summary.candidate_count} 条，" f"本轮评估 {summary.evaluated_count} 条，失败 {summary.failed_count} 条"
        await self._save_monitor_event("warning" if summary.failed_count else "info", "review", message)
        if summary.attempted_count and summary.failed_count == summary.attempted_count:
            raise RuntimeError(message)
        if summary.failed_count:
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        return message

    async def _maintain_market_scan_probability(self, *, now: datetime | None = None) -> str:
        if not _research_maintenance_window_open(now):
            message = "当前不在交易日盘后日K发布窗口，已跳过上涨概率标签维护"
            await self._save_monitor_event("info", "market_scan_probability", message)
            return message
        from app.services.market_scan_probability_maintenance import (
            MarketScanProbabilityMaintenanceService,
        )

        service: MarketScanProbabilityMaintenanceService | None = (
            getattr(self, "_market_scan_probability_maintenance", None)
        )
        if service is None:
            service = MarketScanProbabilityMaintenanceService(self.datahub.cache)
            self._market_scan_probability_maintenance = service
        summary = await _offload(
            service.run,
            now=now,
        )
        scanner = self.market_scanner
        refresh = getattr(scanner, "refresh_probability_research_cache", None)
        if callable(refresh):
            await refresh()
        message = summary.message()
        if summary.failures:
            message += "；" + "；".join(summary.failures)
        await self._save_monitor_event(
            "warning" if summary.degraded else "info",
            "market_scan_probability",
            message,
        )
        if summary.due_count and summary.failed_count == summary.due_count:
            raise RuntimeError(message)
        if summary.degraded:
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        return message

    async def _run_strategy_schedules(self) -> str:
        service = self._strategy_automation_service
        if service is None:
            raise RuntimeError("策略自动化服务尚未注入")
        summary = await _offload(service.run_due)
        message = (
            f"版本化策略检查 {summary.checked_count} 条，执行 {summary.executed_count} 条，"
            f"跳过 {summary.skipped_count} 条，生成提醒 {summary.event_count} 条，"
            f"失败 {summary.failed_count} 条"
        )
        degraded = summary.failed_count > 0
        await self._save_monitor_event(
            "warning" if degraded else "info",
            "strategy_lab",
            message,
        )
        if summary.checked_count and summary.failed_count == summary.checked_count:
            raise RuntimeError(message)
        if degraded:
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        return message


def _research_maintenance_window_open(now: datetime | None = None) -> bool:
    current = market_local_naive(now) if now is not None else market_now_naive()
    return is_trading_day(current.date()) and current.time() >= DAILY_KLINE_PUBLISH_TIME
