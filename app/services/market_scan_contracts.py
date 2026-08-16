from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from app.models.market import Kline, Quote, StockInfo
from app.models.market_scan import (
    MarketScanFilterValues,
    MarketScanAutomaticState,
    MarketScanPublicationDiagnostics,
    MarketScanPublicationSummary,
    MarketScanProductionScoreContract,
    MarketScanMode,
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanResultWrite,
    MarketScanRetryPlan,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanRunStatus,
    MarketScanScoreDistributionObservation,
    MarketScanSeed,
    MarketScanSortOrderValues,
    MarketScanSortValues,
    MarketScanStage,
    MarketScanTrigger,
)
from app.models.market_scan_probability_capture import ProbabilitySourceCaptureState
from app.models.market_scan_polling import MarketScanPollingIdentity
from app.repositories.market_scan_screening import (
    MarketScanBreadthRow,
    MarketScanScreeningRow,
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

    def success_raw_scores(self, run_id: int) -> tuple[object, ...]: ...

    def success_score_observations(
        self,
        run_id: int,
    ) -> tuple[MarketScanScoreDistributionObservation, ...]: ...

    def success_score_contract(
        self,
        run_id: int,
    ) -> MarketScanProductionScoreContract | None: ...

    def market_scan_action_source_digest(self, run_id: int) -> str | None: ...


class MarketScanVerifiedReadProtocol(Protocol):
    """One request-local, linearized read of a market-scan publication."""

    @property
    def run(self) -> MarketScanRun: ...

    @property
    def snapshot_digest(self) -> str | None: ...

    @property
    def action_source_digest(self) -> str | None: ...

    @property
    def probability_source_capture_state(
        self,
    ) -> Mapping[str, object] | None: ...

    @property
    def success_score_contract(
        self,
    ) -> MarketScanProductionScoreContract | None: ...

    def results_page(self, **query: object) -> MarketScanResultPage: ...


@runtime_checkable
class MarketScanCacheProtocol(Protocol):
    @property
    def market_scan_repo(self) -> MarketScanPublicationRepositoryProtocol: ...

    @property
    def path(self) -> Path: ...

    def active_market_scan_run(self) -> MarketScanRun | None: ...

    def reconcile_incomplete_market_scans(self) -> int: ...

    def reconcile_probability_source_capture_outbox(self) -> int: ...

    def probability_source_capture_status(
        self,
        run_id: int,
    ) -> ProbabilitySourceCaptureState | None: ...

    def market_scan_action_source_digest(self, run_id: int) -> str | None: ...

    def verified_market_scan_read(
        self,
        run_id: int,
    ) -> AbstractContextManager[MarketScanVerifiedReadProtocol]: ...

    def claim_probability_source_capture(
        self,
        *,
        owner: str,
        lease_expires_at: str,
    ) -> dict[str, object] | None: ...

    def finish_probability_source_capture(
        self,
        run_id: int,
        **kwargs: object,
    ) -> None: ...

    def retry_probability_source_capture(
        self,
        run_id: int,
        **kwargs: object,
    ) -> None: ...

    def save_monitor_event(
        self,
        level: str,
        category: str,
        message: str,
        symbol: str | None = None,
    ) -> None: ...

    def create_market_scan_run(
        self,
        *,
        trigger: MarketScanTrigger,
        mode: MarketScanMode,
        rule_version: str,
        as_of: str,
        data_date: str,
        quote_date: str,
        scope: str,
        rule_contract: Mapping[str, object] | None = None,
    ) -> MarketScanRun: ...

    def finish_market_scan_run(
        self,
        run_id: int,
        status: MarketScanRunStatus,
        *,
        message: str,
        error: str | None = None,
        publication_diagnostics: MarketScanPublicationDiagnostics | None = None,
        task_status: str | None = None,
        validate_before_commit: Callable[[], None] | None = None,
    ) -> MarketScanRun: ...

    def latest_market_scan_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None: ...

    def latest_full_market_scan_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None: ...

    def latest_full_market_scan_automatic_state(self) -> MarketScanAutomaticState | None: ...

    def market_scan_polling_identity(self, *, mode: MarketScanMode) -> MarketScanPollingIdentity: ...

    def latest_published_market_scan_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None: ...

    def market_scan_degraded_result_count(self, run_id: int) -> int: ...

    def market_scan_success_raw_scores(self, run_id: int) -> tuple[object, ...]: ...

    def market_scan_success_score_observations(
        self,
        run_id: int,
    ) -> tuple[MarketScanScoreDistributionObservation, ...]: ...

    def market_scan_success_score_contract(
        self,
        run_id: int,
    ) -> MarketScanProductionScoreContract | None: ...

    def market_scan_results(
        self,
        run_id: int,
        *,
        page: int,
        page_size: int,
        status: MarketScanResultStatus | None,
        market: MarketScanFilterValues,
        industry: MarketScanFilterValues,
        is_st: bool | None,
        is_new: bool | None,
        min_score: int | None = None,
        max_score: int | None = None,
        min_trend_score: int | None = None,
        max_trend_score: int | None = None,
        min_change_pct: float | None = None,
        max_change_pct: float | None = None,
        min_turnover_rate: float | None = None,
        max_turnover_rate: float | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        min_data_quality_score: int | None,
        max_data_quality_score: int | None = None,
        min_confidence: float | None = None,
        max_risk: float | None = None,
        min_tradability: float | None = None,
        keyword: str | None,
        symbols: MarketScanFilterValues = None,
        sort: MarketScanSortValues,
        order: MarketScanSortOrderValues,
    ) -> MarketScanResultPage: ...

    def market_scan_screening_breadth_snapshot(
        self,
        run_id: int,
    ) -> tuple[MarketScanRun, list[MarketScanBreadthRow]]: ...

    def market_scan_screening_evaluation_snapshot(
        self,
        run_id: int,
    ) -> tuple[MarketScanRun, list[MarketScanScreeningRow]]: ...

    def market_scan_screening_result_items(
        self,
        run_id: int,
        symbols: Sequence[str],
    ) -> list[MarketScanResultItem]: ...

    def market_scan_retry_plan(self, run_id: int) -> MarketScanRetryPlan: ...

    def market_scan_run(self, run_id: int) -> MarketScanRun: ...

    def market_scan_runs(
        self,
        *,
        page: int,
        page_size: int,
        mode: MarketScanMode | None = None,
        status: MarketScanRunStatus | Literal["published"] | None = None,
        data_date: str | None = None,
    ) -> MarketScanRunPage: ...

    def pending_market_scan_items(self, run_id: int) -> list[MarketScanResultItem]: ...

    def prepare_market_scan_retry(
        self,
        run_id: int,
        expected_plan: MarketScanRetryPlan | None = None,
        *,
        as_of: str | None = None,
        rule_contract: Mapping[str, object] | None = None,
    ) -> MarketScanRun: ...

    def prepare_market_scan_top100_refresh(
        self,
        source_run_id: int,
        *,
        rule_version: str,
        as_of: str,
        data_date: str,
        quote_date: str,
        limit: int,
        rule_contract: Mapping[str, object] | None = None,
    ) -> MarketScanRun: ...

    def record_market_scan_stock_pool_source(self, run_id: int, source: str) -> MarketScanRun: ...

    def update_market_scan_observability(
        self,
        run_id: int,
        *,
        stage: MarketScanStage,
        stage_items: int = 0,
        work_metrics: dict[MarketScanStage, tuple[int, int]] | None = None,
        message: str | None = None,
    ) -> MarketScanRun: ...

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

    def begin_market_scan_quote_capture(self, run_id: int, started_at: str) -> MarketScanRun: ...

    def seal_market_scan_quote_capture(
        self,
        run_id: int,
        *,
        finished_at: str,
        decision_as_of: str,
        duration_ms: int,
        count: int,
    ) -> MarketScanRun: ...

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
class MarketScanKlinePrefetchProtocol(Protocol):
    async def prefetch_market_scan_klines(
        self,
        symbols: list[str],
        *,
        limit: int,
    ) -> dict[str, list[Kline]]: ...

    async def market_scan_kline_from_prefetch(
        self,
        symbol: str,
        prefetched_cache: list[Kline],
        *,
        limit: int,
        allow_stale: bool,
        require_provider_response: bool,
    ) -> list[Kline]: ...


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
    "MarketScanKlinePrefetchProtocol",
    "MarketScanProviderStateProtocol",
    "MarketScanPublicationRepositoryProtocol",
    "MarketScanSettingsProtocol",
    "MarketScanStockPoolResolutionProtocol",
]
