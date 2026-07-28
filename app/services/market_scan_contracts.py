from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

from app.models.market import Kline, Quote, StockInfo
from app.models.market_scan import (
    MarketScanPublicationSummary,
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanResultWrite,
    MarketScanRetryPlan,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanRunStatus,
    MarketScanSeed,
    MarketScanSort,
    MarketScanSortOrder,
    MarketScanTrigger,
)
from app.services.datahub_metadata import StockPoolResolution
from app.services.datahub_runtime import ProviderChainState


@runtime_checkable
class MarketScanSettingsProtocol(Protocol):
    market_scan_auto_retry_delays_seconds: tuple[int, ...]
    market_scan_auto_retry_max_attempts: int
    market_scan_auto_enabled: bool
    market_scan_batch_retry_attempts: int
    market_scan_batch_size: int
    market_scan_concurrency: int
    market_scan_kline_limit: int
    market_scan_min_bj_count: int
    market_scan_min_data_quality_score: int
    market_scan_min_history_rows: int
    market_scan_min_sh_count: int
    market_scan_min_sz_count: int
    market_scan_min_universe_count: int
    market_scan_new_stock_days: int
    market_scan_provider_wait_budget_seconds: float
    market_scan_preflight_enabled: bool
    market_scan_preflight_timeout_seconds: float
    market_scan_quote_batch_timeout_seconds: float
    market_scan_retry_attempts: int
    market_scan_retry_backoff_seconds: float
    market_scan_schedule_hour: int
    market_scan_schedule_minute: int
    market_scan_symbol_timeout_seconds: float
    scheduler_shutdown_timeout_seconds: float


class MarketScanPublicationRepositoryProtocol(Protocol):
    def publication_summary(self, run_id: int) -> MarketScanPublicationSummary: ...


@runtime_checkable
class MarketScanCacheProtocol(Protocol):
    market_scan_repo: MarketScanPublicationRepositoryProtocol

    def active_market_scan_run(self) -> MarketScanRun | None: ...

    def create_market_scan_run(
        self,
        *,
        trigger: MarketScanTrigger,
        rule_version: str,
        as_of: str,
        data_date: str,
        scope: str,
    ) -> MarketScanRun: ...

    def finish_market_scan_run(
        self,
        run_id: int,
        status: MarketScanRunStatus,
        *,
        message: str,
        error: str | None = None,
        task_status: str | None = None,
    ) -> MarketScanRun: ...

    def latest_market_scan_run(self) -> MarketScanRun | None: ...

    def latest_published_market_scan_run(self) -> MarketScanRun | None: ...

    def market_scan_degraded_result_count(self, run_id: int) -> int: ...

    def market_scan_results(
        self,
        run_id: int,
        *,
        page: int,
        page_size: int,
        status: MarketScanResultStatus | None,
        market: str | None,
        industry: str | None,
        is_st: bool | None,
        is_new: bool | None,
        min_data_quality_score: int | None,
        keyword: str | None,
        sort: MarketScanSort,
        order: MarketScanSortOrder,
    ) -> MarketScanResultPage: ...

    def market_scan_retry_plan(self, run_id: int) -> MarketScanRetryPlan: ...

    def market_scan_run(self, run_id: int) -> MarketScanRun: ...

    def market_scan_runs(self, *, page: int, page_size: int) -> MarketScanRunPage: ...

    def pending_market_scan_items(self, run_id: int) -> list[MarketScanResultItem]: ...

    def prepare_market_scan_retry(
        self,
        run_id: int,
        expected_plan: MarketScanRetryPlan | None = None,
    ) -> MarketScanRun: ...

    def record_market_scan_stock_pool_source(self, run_id: int, source: str) -> MarketScanRun: ...

    def refresh_pending_market_scan_metadata(
        self,
        run_id: int,
        seeds: list[MarketScanSeed],
    ) -> int: ...

    def request_market_scan_cancel(self, run_id: int) -> MarketScanRun: ...

    def save_market_scan_result_batch(
        self,
        run_id: int,
        results: list[MarketScanResultWrite],
    ) -> MarketScanRun: ...

    def seed_market_scan_results(
        self,
        run_id: int,
        seeds: list[MarketScanSeed],
        *,
        excluded_count: int,
    ) -> int: ...

    def start_market_scan_run(self, run_id: int) -> MarketScanRun: ...

    def start_market_scan_task_run(self, run_id: int, task_name: str) -> int: ...


@runtime_checkable
class MarketScanDataHubProtocol(Protocol):
    @property
    def cache(self) -> MarketScanCacheProtocol: ...

    @property
    def settings(self) -> MarketScanSettingsProtocol: ...

    async def kline(
        self,
        symbol: str,
        limit: int = 120,
        use_cache: bool = True,
        *,
        allow_stale: bool = False,
        require_provider_response: bool = False,
    ) -> list[Kline]: ...

    async def partial_quotes_with_errors(
        self,
        symbols: Iterable[str],
        use_cache: bool = True,
    ) -> tuple[list[Quote], tuple[str, ...]]: ...

    async def stock_pool(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets: Iterable[str] | None = None,
        minimum_market_counts: Mapping[str, int] | None = None,
    ) -> list[StockInfo]: ...


@runtime_checkable
class MarketScanStockPoolResolutionProtocol(Protocol):
    async def stock_pool_resolution(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets: Iterable[str] | None = None,
        minimum_market_counts: Mapping[str, int] | None = None,
    ) -> StockPoolResolution: ...


@runtime_checkable
class MarketScanProviderStateProtocol(Protocol):
    def provider_chain_state(self, kind: str) -> ProviderChainState: ...


__all__ = [
    "MarketScanCacheProtocol",
    "MarketScanDataHubProtocol",
    "MarketScanProviderStateProtocol",
    "MarketScanPublicationRepositoryProtocol",
    "MarketScanSettingsProtocol",
    "MarketScanStockPoolResolutionProtocol",
]
