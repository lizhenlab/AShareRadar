"""Repository composition for the local SQLite runtime.

The bundle is deliberately kept in the repository layer: it knows how repository
objects share a database path and lock, but it does not construct application
services or perform domain reads during startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading

from app.config import Settings
from app.repositories.advice import AdviceHistoryRepository
from app.repositories.advice_reviews import AdviceReviewRepository
from app.repositories.alerts import AlertRepository
from app.repositories.cache_stats import CacheStatsRepository
from app.repositories.discovery import DiscoveryRepository
from app.repositories.maintenance import RuntimeMaintenanceRepository
from app.repositories.market_data import MarketDataRepository
from app.repositories.market_scan import MarketScanRepository
from app.repositories.market_scan_delta import MarketScanDeltaRepository
from app.repositories.market_scan_screen_alert import MarketScanScreenAlertRepository
from app.repositories.notes import StockNoteRepository
from app.repositories.paper_trading import PaperTradingRepository
from app.repositories.provider_status import ProviderStatusRepository
from app.repositories.reliability import ReliabilityRepository
from app.repositories.runtime import RuntimeEventRepository
from app.repositories.strategy_automation import StrategyAutomationRepository
from app.repositories.strategy_evidence import StrategyEvidenceRepository
from app.repositories.strategy_execution import StrategyExecutionRepository
from app.repositories.strategy_lab import StrategyLabRepository
from app.repositories.watchlist import WatchlistRepository
from app.repositories.watchlist_scans import WatchlistScanRepository


@dataclass(frozen=True)
class RepositoryBundle:
    """All repositories that share one SQLite database and process lock."""

    cache_stats: CacheStatsRepository
    discovery: DiscoveryRepository
    strategy_lab: StrategyLabRepository
    strategy_execution: StrategyExecutionRepository
    strategy_evidence: StrategyEvidenceRepository
    strategy_automation: StrategyAutomationRepository
    market_data: MarketDataRepository
    market_scan: MarketScanRepository
    market_scan_delta: MarketScanDeltaRepository
    market_scan_screen_alert: MarketScanScreenAlertRepository
    provider_status: ProviderStatusRepository
    reliability: ReliabilityRepository
    runtime_event: RuntimeEventRepository
    watchlist: WatchlistRepository
    advice: AdviceHistoryRepository
    advice_review: AdviceReviewRepository
    paper_trading: PaperTradingRepository
    watchlist_scan: WatchlistScanRepository
    alert: AlertRepository
    note: StockNoteRepository
    maintenance: RuntimeMaintenanceRepository

    def bind_settings(self, settings: Settings) -> None:
        self.watchlist.settings = settings
        self.advice.settings = settings
        self.maintenance.settings = settings

    @classmethod
    def build(
        cls,
        path: Path,
        lock: threading.RLock,
        *,
        settings: Settings,
    ) -> RepositoryBundle:
        return cls(
            cache_stats=CacheStatsRepository(path, lock),
            discovery=DiscoveryRepository(path, lock),
            strategy_lab=StrategyLabRepository(path, lock),
            strategy_execution=StrategyExecutionRepository(path, lock),
            strategy_evidence=StrategyEvidenceRepository(path, lock),
            strategy_automation=StrategyAutomationRepository(path, lock),
            market_data=MarketDataRepository(path, lock),
            market_scan=MarketScanRepository(path, lock),
            market_scan_delta=MarketScanDeltaRepository(path, lock),
            market_scan_screen_alert=MarketScanScreenAlertRepository(path, lock),
            provider_status=ProviderStatusRepository(path, lock),
            reliability=ReliabilityRepository(path, lock),
            runtime_event=RuntimeEventRepository(path, lock),
            watchlist=WatchlistRepository(path, lock, settings=settings),
            advice=AdviceHistoryRepository(path, lock, settings=settings),
            advice_review=AdviceReviewRepository(path, lock),
            paper_trading=PaperTradingRepository(path, lock),
            watchlist_scan=WatchlistScanRepository(path, lock),
            alert=AlertRepository(path, lock),
            note=StockNoteRepository(path, lock),
            maintenance=RuntimeMaintenanceRepository(path, lock, settings=settings),
        )


__all__ = ["RepositoryBundle"]
