"""Composition of domain services backed by a repository bundle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.repositories.bundle import RepositoryBundle
from app.services.discovery import DiscoveryService
from app.services.strategy_automation import StrategyAutomationService
from app.services.strategy_evidence import StrategyEvidenceService
from app.services.strategy_execution import StrategyExecutionService
from app.services.strategy_lab import StrategyLabService


@dataclass(frozen=True)
class DomainServiceBundle:
    discovery: DiscoveryService
    strategy_lab: StrategyLabService
    strategy_execution: StrategyExecutionService
    strategy_evidence: StrategyEvidenceService
    strategy_automation: StrategyAutomationService

    @classmethod
    def build(cls, path: Path, repositories: RepositoryBundle) -> DomainServiceBundle:
        strategy_lab = StrategyLabService(repositories.strategy_lab)
        strategy_execution = StrategyExecutionService(
            repositories.strategy_execution,
            strategy_lab,
        )
        return cls(
            discovery=DiscoveryService(repositories.discovery),
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
