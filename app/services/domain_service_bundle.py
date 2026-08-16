"""Composition of domain services backed by a repository bundle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.repositories.bundle import RepositoryBundle
from app.services.discovery import DiscoveryService
from app.services.market_scan_executable_shadow import MarketScanExecutableShadowService
from app.services.market_scan_screen_alert import MarketScanScreenAlertService
from app.services.strategy_automation import StrategyAutomationService
from app.services.strategy_evidence import StrategyEvidenceService
from app.services.strategy_execution import StrategyExecutionService
from app.services.strategy_lab import StrategyLabService


@dataclass(frozen=True)
class DomainServiceBundle:
    discovery: DiscoveryService
    market_scan_executable_shadow: MarketScanExecutableShadowService
    market_scan_screen_alert: MarketScanScreenAlertService
    strategy_lab: StrategyLabService
    strategy_execution: StrategyExecutionService
    strategy_evidence: StrategyEvidenceService
    strategy_automation: StrategyAutomationService

    @classmethod
    def build(cls, path: Path, repositories: RepositoryBundle) -> DomainServiceBundle:
        strategy_lab = StrategyLabService(repositories.strategy_lab)
        market_scan_screen_alert = MarketScanScreenAlertService(
            repositories.market_scan_screen_alert
        )
        strategy_execution = StrategyExecutionService(
            repositories.strategy_execution,
            strategy_lab,
        )
        return cls(
            discovery=DiscoveryService(repositories.discovery, market_scan_screen_alert),
            market_scan_executable_shadow=MarketScanExecutableShadowService(
                repositories.strategy_execution
            ),
            market_scan_screen_alert=market_scan_screen_alert,
            strategy_lab=strategy_lab,
            strategy_execution=strategy_execution,
            strategy_evidence=StrategyEvidenceService(
                path,
                repositories.strategy_evidence,
                strategy_lab,
            ),
            strategy_automation=StrategyAutomationService(
                repositories.strategy_automation,
                strategy_lab,
                strategy_execution,
            ),
        )


__all__ = ["DomainServiceBundle"]
