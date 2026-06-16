from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from pathlib import Path

from app.config import get_settings
from app.db.connection import SQLiteConnectionFactory
from app.db.schema import initialize_schema
from app.models.schemas import (
    AdviceHistoryItem,
    AnalysisResult,
    AlertEventItem,
    AlertRuleInput,
    AlertRuleItem,
    AlertRuleUpdate,
    CacheStats,
    Kline,
    MinuteKline,
    MonitorEvent,
    ProviderCapabilityStatus,
    PlateItem,
    ProviderStatus,
    Quote,
    StockConceptItem,
    StockInfo,
    StockNoteInput,
    StockNoteItem,
    StockNoteUpdate,
    TaskRun,
    WatchlistItem,
)
from app.repositories.advice import AdviceHistoryRepository
from app.repositories.alerts import AlertRepository
from app.repositories.cache_stats import CacheStatsRepository
from app.repositories.market_data import MarketDataRepository
from app.repositories.maintenance import RuntimeMaintenanceRepository
from app.repositories.notes import StockNoteRepository
from app.repositories.provider_status import ProviderStatusRepository
from app.repositories.runtime import RuntimeEventRepository
from app.repositories.watchlist import WatchlistRepository


class SQLiteCache:
    def __init__(self, path: Path | None = None) -> None:
        settings = get_settings()
        self.path = path or settings.cache_path
        self._connections = SQLiteConnectionFactory(self.path)
        self._lock = threading.RLock()
        self._init_schema()
        self.cache_stats_repo = CacheStatsRepository(self.path, self._lock)
        self.market_data_repo = MarketDataRepository(self.path, self._lock)
        self.provider_status_repo = ProviderStatusRepository(self.path, self._lock)
        self.runtime_event_repo = RuntimeEventRepository(self.path, self._lock)
        self.watchlist_repo = WatchlistRepository(self.path, self._lock)
        self.advice_repo = AdviceHistoryRepository(self.path, self._lock)
        self.alert_repo = AlertRepository(self.path, self._lock)
        self.note_repo = StockNoteRepository(self.path, self._lock)
        self.maintenance_repo = RuntimeMaintenanceRepository(self.path, self._lock)

    def _connect(self) -> AbstractContextManager:
        return self._connections.connect()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            initialize_schema(conn)

    def save_quotes(self, quotes: list[Quote]) -> None:
        self.market_data_repo.save_quotes(quotes)

    def get_quotes(self, symbols: list[str], max_age_seconds: int) -> list[Quote]:
        return self.market_data_repo.get_quotes(symbols, max_age_seconds)

    def quote_history(self, symbol: str, limit: int = 120) -> list[dict[str, float | str | None]]:
        return self.market_data_repo.quote_history(symbol, limit=limit)

    def save_klines(self, symbol: str, klines: list[Kline], source: str) -> None:
        self.market_data_repo.save_klines(symbol, klines, source)

    def get_klines(self, symbol: str, limit: int, max_age_seconds: int) -> list[Kline]:
        return self.market_data_repo.get_klines(symbol, limit, max_age_seconds)

    def save_minute_klines(self, symbol: str, interval: str, rows: list[MinuteKline], source: str) -> None:
        self.market_data_repo.save_minute_klines(symbol, interval, rows, source)

    def get_minute_klines(self, symbol: str, interval: str, limit: int, max_age_seconds: int) -> list[MinuteKline]:
        return self.market_data_repo.get_minute_klines(symbol, interval, limit, max_age_seconds)

    def save_stock_pool(self, rows: list[StockInfo]) -> None:
        self.market_data_repo.save_stock_pool(rows)

    def get_stock_pool(self, max_age_seconds: int, limit: int = 5000, keyword: str | None = None) -> list[StockInfo]:
        return self.market_data_repo.get_stock_pool(max_age_seconds, limit=limit, keyword=keyword)

    def stock_pool_count(self, max_age_seconds: int | None = None) -> int:
        return self.market_data_repo.stock_pool_count(max_age_seconds)

    def save_plate_rank(self, rows: list[PlateItem]) -> None:
        self.market_data_repo.save_plate_rank(rows)

    def get_plate_rank(self, max_age_seconds: int, limit: int = 20) -> list[PlateItem]:
        return self.market_data_repo.get_plate_rank(max_age_seconds, limit=limit)

    def save_stock_concepts(self, symbol: str, rows: list[StockConceptItem]) -> None:
        self.market_data_repo.save_stock_concepts(symbol, rows)

    def get_stock_concepts(self, symbol: str, max_age_seconds: int, limit: int = 8) -> list[StockConceptItem]:
        return self.market_data_repo.get_stock_concepts(symbol, max_age_seconds, limit=limit)

    def provider_enabled(self, name: str) -> bool:
        return self.provider_status_repo.enabled(name)

    def update_provider_success(self, name: str, priority: int, latency_ms: float) -> None:
        self.provider_status_repo.record_success(name, priority, latency_ms)

    def update_provider_failure(self, name: str, priority: int, error: str) -> None:
        self.provider_status_repo.record_failure(name, priority, error)

    def update_provider_capability_success(self, name: str, kind: str, priority: int, latency_ms: float) -> None:
        self.provider_status_repo.record_capability_success(name, kind, priority, latency_ms)

    def update_provider_capability_failure(self, name: str, kind: str, priority: int, error: str) -> None:
        self.provider_status_repo.record_capability_failure(name, kind, priority, error)

    def ensure_provider(self, name: str, priority: int, enabled: bool = True) -> None:
        self.provider_status_repo.ensure(name, priority, enabled=enabled)

    def ensure_provider_capability(self, name: str, kind: str, priority: int, enabled: bool = True) -> None:
        self.provider_status_repo.ensure_capability(name, kind, priority, enabled=enabled)

    def provider_statuses(self) -> list[ProviderStatus]:
        return self.provider_status_repo.items()

    def provider_capability_statuses(self) -> list[ProviderCapabilityStatus]:
        return self.provider_status_repo.capability_items()

    def stats(self) -> CacheStats:
        return self.cache_stats_repo.stats()

    def log_event(self, category: str, message: str) -> None:
        self.runtime_event_repo.log_event(category, message)

    def start_task_run(self, task_name: str) -> int:
        return self.runtime_event_repo.start_task_run(task_name)

    def finish_task_run(self, run_id: int, status: str, message: str | None = None) -> None:
        self.runtime_event_repo.finish_task_run(run_id, status, message)

    def recent_task_runs(self, limit: int = 20) -> list[TaskRun]:
        return self.runtime_event_repo.task_runs(limit=limit)

    def save_monitor_event(self, level: str, category: str, message: str, symbol: str | None = None) -> None:
        self.runtime_event_repo.save_monitor_event(level, category, message, symbol=symbol)

    def recent_monitor_events(self, limit: int = 30) -> list[MonitorEvent]:
        return self.runtime_event_repo.monitor_events(limit=limit)

    def save_watchlist_item(
        self,
        quote: Quote,
        note: str | None = None,
        group_name: str | None = None,
        pinned: bool | None = None,
    ) -> WatchlistItem:
        return self.watchlist_repo.save_item(quote, note=note, group_name=group_name, pinned=pinned)

    def watchlist_item(self, symbol: str) -> WatchlistItem | None:
        return self.watchlist_repo.item(symbol)

    def watchlist(self) -> list[WatchlistItem]:
        return self.watchlist_repo.items()

    def delete_watchlist_item(self, symbol: str) -> bool:
        return self.watchlist_repo.delete(symbol)

    def watchlist_symbols(self) -> list[str]:
        return self.watchlist_repo.symbols()

    def save_advice_snapshot(self, analysis: AnalysisResult) -> AdviceHistoryItem:
        return self.advice_repo.save_snapshot(analysis)

    def advice_history_by_id(self, row_id: int) -> AdviceHistoryItem | None:
        return self.advice_repo.by_id(row_id)

    def advice_history(self, symbol: str, limit: int = 30) -> list[AdviceHistoryItem]:
        return self.advice_repo.items(symbol, limit=limit)

    def create_alert_rule(self, quote: Quote, payload: AlertRuleInput) -> AlertRuleItem:
        return self.alert_repo.create_rule(quote, payload)

    def alert_rules(
        self,
        symbol: str | None = None,
        include_disabled: bool = True,
        limit: int = 200,
    ) -> list[AlertRuleItem]:
        return self.alert_repo.rules(symbol=symbol, include_disabled=include_disabled, limit=limit)

    def alert_rule(self, row_id: int) -> AlertRuleItem | None:
        return self.alert_repo.rule(row_id)

    def delete_alert_rule(self, row_id: int) -> bool:
        return self.alert_repo.delete_rule(row_id)

    def update_alert_rule(self, row_id: int, payload: AlertRuleUpdate) -> AlertRuleItem | None:
        return self.alert_repo.update_rule(row_id, payload)

    def update_alert_rule_state(
        self,
        rule: AlertRuleItem,
        *,
        checked_at: str,
        state: str,
        triggered: bool,
        message: str,
        quote: Quote,
        event_type: str | None = None,
        force_event: bool = False,
    ) -> AlertEventItem | None:
        return self.alert_repo.update_rule_state(
            rule,
            checked_at=checked_at,
            state=state,
            triggered=triggered,
            message=message,
            quote=quote,
            event_type=event_type,
            force_event=force_event,
        )

    def alert_events(self, symbol: str | None = None, limit: int = 100) -> list[AlertEventItem]:
        return self.alert_repo.events(symbol=symbol, limit=limit)

    def create_stock_note(self, quote: Quote, payload: StockNoteInput) -> StockNoteItem:
        return self.note_repo.create(quote, payload)

    def stock_notes(self, symbol: str, limit: int = 100, visible_only: bool = False) -> list[StockNoteItem]:
        return self.note_repo.items(symbol, limit=limit, visible_only=visible_only)

    def stock_note(self, row_id: int) -> StockNoteItem | None:
        return self.note_repo.item(row_id)

    def update_stock_note(self, row_id: int, payload: StockNoteUpdate) -> StockNoteItem | None:
        return self.note_repo.update(row_id, payload)

    def delete_stock_note(self, row_id: int) -> bool:
        return self.note_repo.delete(row_id)

    def cleanup_runtime_rows(self) -> dict[str, int]:
        return self.maintenance_repo.cleanup_runtime_rows()

    def table_counts(self) -> dict[str, int]:
        return self.maintenance_repo.table_counts()
