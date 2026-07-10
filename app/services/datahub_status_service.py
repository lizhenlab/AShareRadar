from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.models.schemas import (
    CacheStats,
    DataSourcePlan,
    DataStatus,
    ProviderCapability,
    ProviderCapabilityStatus,
    ProviderDecision,
    ProviderStatus,
)
from app.services.datahub_source_plan import SourcePlanBuilder
from app.services.provider_registry import (
    MarketProvider,
    provider_capabilities,
    provider_enabled_for,
    provider_is_enabled,
    supported_provider_kinds,
)


class DataStatusCache(Protocol):
    def ensure_provider(self, name: str, priority: int, enabled: bool = True) -> None:
        ...

    def ensure_provider_capability(self, name: str, kind: str, priority: int, enabled: bool = True) -> None:
        ...

    def provider_statuses(self) -> list[ProviderStatus]:
        ...

    def provider_capability_statuses(self) -> list[ProviderCapabilityStatus]:
        ...

    def stats(self) -> CacheStats:
        ...


class DataStatusService:
    def __init__(
        self,
        *,
        cache: DataStatusCache,
        providers: dict[str, MarketProvider],
        provider_names: Callable[[], list[str]],
        provider_index: Callable[[str], int],
        source_plan_builder: SourcePlanBuilder,
    ) -> None:
        self.cache = cache
        self.providers = providers
        self.provider_names = provider_names
        self.provider_index = provider_index
        self.source_plan_builder = source_plan_builder

    def status(self) -> DataStatus:
        providers = self.cache.provider_statuses()
        capability_statuses = self.cache.provider_capability_statuses()
        capabilities = self.capabilities()
        return DataStatus(
            providers=providers,
            cache=self.cache.stats(),
            capabilities=capabilities,
            capability_statuses=capability_statuses,
            source_plan=self.source_plan(providers, capabilities, capability_statuses),
        )

    def capabilities(self) -> list[ProviderCapability]:
        return provider_capabilities(self.providers)

    def source_plan(
        self,
        providers: list[ProviderStatus],
        capabilities: list[ProviderCapability],
        capability_statuses: list[ProviderCapabilityStatus] | None = None,
    ) -> DataSourcePlan:
        return self.source_plan_builder.build(providers, capabilities, capability_statuses)

    def provider_decision(
        self,
        name: str,
        status: ProviderStatus | None,
        capability: ProviderCapability | None,
        quote_names: list[str],
        kline_names: list[str],
        minute_names: list[str],
        capability_statuses: dict[tuple[str, str], ProviderCapabilityStatus],
    ) -> ProviderDecision:
        return self.source_plan_builder.provider_decision(
            name,
            status,
            capability,
            quote_names,
            kline_names,
            minute_names,
            capability_statuses,
        )

    def sync_provider_enabled_flags(self) -> None:
        existing_kinds = _existing_provider_capability_kinds(self.cache.provider_capability_statuses())
        for name in self.provider_names():
            provider = self.providers.get(name)
            if provider is None:
                continue
            priority = self.provider_index(name)
            self.cache.ensure_provider(name, priority, enabled=provider_is_enabled(provider))
            for kind in _synced_capability_kinds(supported_provider_kinds(provider), existing_kinds.get(name, set())):
                self.cache.ensure_provider_capability(name, kind, priority, enabled=provider_enabled_for(provider, kind))


def _existing_provider_capability_kinds(statuses: list[ProviderCapabilityStatus]) -> dict[str, set[str]]:
    kinds_by_provider: dict[str, set[str]] = {}
    for status in statuses:
        if not status.name or not status.kind:
            continue
        kinds_by_provider.setdefault(status.name, set()).add(status.kind)
    return kinds_by_provider


def _synced_capability_kinds(supported_kinds: list[str], existing_kinds: set[str]) -> list[str]:
    kinds = list(supported_kinds)
    kinds.extend(kind for kind in sorted(existing_kinds) if kind not in kinds)
    return kinds
