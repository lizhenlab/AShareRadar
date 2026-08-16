from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from app.config import Settings, get_settings
from app.repositories.bundle import RepositoryBundle
from app.services.cache import SQLiteCache, resolve_cache_settings
from app.services.datahub import DataHub
from app.services.domain_service_bundle import DomainServiceBundle
from app.services.local_data_import_guard import LocalDataImportPreviewRegistry
from app.services.market_scan_manager import MarketScanManager
from app.services.runtime_coordinator import RuntimeCoordinator, RuntimeLeadership
from app.services.scheduler import LocalDataScheduler
from app.services.workbench_context import WorkbenchContextCache
from app.api.market_scan_read_admission import MarketScanHeavyReadAdmission


_MISSING = object()


@dataclass
class AppContainer:
    settings: Settings
    datahub: DataHub
    scheduler: LocalDataScheduler
    workbench_contexts: WorkbenchContextCache
    repositories: RepositoryBundle | None = None
    domain_services: DomainServiceBundle | None = None
    market_scanner: MarketScanManager | None = None
    runtime_coordinator: RuntimeCoordinator | None = None
    local_data_import_previews: LocalDataImportPreviewRegistry = field(default_factory=LocalDataImportPreviewRegistry)
    market_scan_heavy_read_admission: MarketScanHeavyReadAdmission = field(default_factory=MarketScanHeavyReadAdmission)

    def __post_init__(self) -> None:
        _require_settings_owner("datahub", self.datahub, self.settings)
        _require_settings_owner("scheduler", self.scheduler, self.settings)
        datahub_cache = getattr(self.datahub, "cache", _MISSING)
        if datahub_cache is not _MISSING:
            if isinstance(datahub_cache, SQLiteCache):
                resolve_cache_settings(datahub_cache, self.settings, owner="datahub.cache")
            _require_settings_owner("datahub.cache", datahub_cache, self.settings)
        scheduler_datahub = getattr(self.scheduler, "datahub", _MISSING)
        if scheduler_datahub is not _MISSING and scheduler_datahub is not self.datahub:
            raise ValueError("scheduler.datahub 必须与容器 datahub 使用同一实例")
        datahub_contexts = getattr(self.datahub, "workbench_contexts", _MISSING)
        if datahub_contexts is not _MISSING and datahub_contexts is not self.workbench_contexts:
            raise ValueError("workbench_contexts 必须与 datahub.workbench_contexts 使用同一实例")
        cache = getattr(self.datahub, "cache", None)
        cache_repositories = getattr(cache, "repositories", None)
        cache_services = getattr(cache, "domain_services", None)
        if self.repositories is None:
            self.repositories = cache_repositories
        if self.domain_services is None:
            self.domain_services = cache_services
        if cache_repositories is not self.repositories:
            raise ValueError("repositories 必须与 datahub.cache 使用同一实例")
        if cache_services is not self.domain_services:
            raise ValueError("domain_services 必须与 datahub.cache 使用同一实例")


def build_container(
    *,
    settings: Settings | None = None,
    cache: SQLiteCache | None = None,
    datahub: DataHub | None = None,
    scheduler: LocalDataScheduler | None = None,
) -> AppContainer:
    datahub = _resolve_datahub_injection(datahub, scheduler)
    effective_cache = _resolve_cache_injection(datahub, cache)
    resolved_settings = _resolve_settings(settings, datahub, effective_cache, scheduler)
    datahub = _build_datahub(datahub, cache, resolved_settings)
    domain_services = _compose_domain_services(datahub.cache)
    leadership = RuntimeLeadership.for_cache_path(datahub.cache.path)
    market_scanner = MarketScanManager(datahub, instance_guard=leadership.service_guard())
    scheduler = _build_scheduler(
        scheduler,
        datahub,
        resolved_settings,
        market_scanner,
        domain_services,
        instance_guard=leadership.service_guard(),
    )
    runtime_coordinator = RuntimeCoordinator(leadership, scheduler, market_scanner)
    return AppContainer(
        settings=resolved_settings,
        datahub=datahub,
        scheduler=scheduler,
        workbench_contexts=datahub.workbench_contexts,
        repositories=datahub.cache.repositories,
        domain_services=domain_services,
        market_scanner=market_scanner,
        runtime_coordinator=runtime_coordinator,
        local_data_import_previews=LocalDataImportPreviewRegistry(),
    )


def _compose_domain_services(cache: SQLiteCache) -> DomainServiceBundle:
    services = cache.bound_domain_services
    if services is None:
        services = DomainServiceBundle.build(cache.path, cache.repositories)
    return cache.bind_domain_services(services)


def _resolve_datahub_injection(
    datahub: DataHub | None,
    scheduler: LocalDataScheduler | None,
) -> DataHub | None:
    scheduler_datahub = getattr(scheduler, "datahub", _MISSING)
    if datahub is None and scheduler_datahub is not _MISSING and scheduler_datahub is not None:
        return cast(DataHub, scheduler_datahub)
    if datahub is not None and scheduler_datahub is not _MISSING and scheduler_datahub is not None and scheduler_datahub is not datahub:
        raise ValueError("不能注入绑定到其他 datahub 的 scheduler")
    return datahub


def _resolve_cache_injection(datahub: DataHub | None, cache: SQLiteCache | None) -> SQLiteCache | None:
    if datahub is None:
        return cache
    datahub_cache = getattr(datahub, "cache", _MISSING)
    if cache is not None and datahub_cache is not cache:
        raise ValueError("不能同时注入不一致的 datahub 和 cache")
    return None if datahub_cache is _MISSING else cast(SQLiteCache, datahub_cache)


def _build_datahub(
    datahub: DataHub | None,
    cache: SQLiteCache | None,
    settings: Settings,
) -> DataHub:
    if datahub is None:
        return DataHub(cache=cache, settings=settings)
    _require_settings_owner("datahub", datahub, settings)
    datahub_cache = getattr(datahub, "cache", _MISSING)
    if isinstance(datahub_cache, SQLiteCache):
        resolve_cache_settings(datahub_cache, settings, owner="datahub.cache")
    elif datahub_cache is not _MISSING:
        _require_settings_owner("datahub.cache", datahub_cache, settings)
    return datahub


def _build_scheduler(
    scheduler: LocalDataScheduler | None,
    datahub: DataHub,
    settings: Settings,
    market_scanner: MarketScanManager,
    domain_services: DomainServiceBundle,
    *,
    instance_guard,
) -> LocalDataScheduler:
    if scheduler is None:
        return LocalDataScheduler(
            datahub,
            market_scanner=market_scanner,
            instance_guard=instance_guard,
            strategy_automation_service=domain_services.strategy_automation,
        )
    scheduler_datahub = getattr(scheduler, "datahub", _MISSING)
    if scheduler_datahub is not _MISSING and scheduler_datahub is not datahub:
        raise ValueError("scheduler.datahub 必须与容器 datahub 使用同一实例")
    _require_settings_owner("scheduler", scheduler, settings)
    scanner = getattr(scheduler, "market_scanner", None)
    if scanner is not None and scanner is not market_scanner:
        raise ValueError("scheduler.market_scanner 必须与容器使用同一实例")
    scheduler.market_scanner = market_scanner
    scheduler.bind_strategy_automation_service(domain_services.strategy_automation)
    scheduler.bind_instance_guard(instance_guard)
    return scheduler


def _resolve_settings(
    settings: Settings | None,
    datahub: DataHub | None,
    cache: SQLiteCache | None,
    scheduler: LocalDataScheduler | None,
) -> Settings:
    if settings is not None:
        return settings
    for owner in (datahub, cache, scheduler):
        owner_settings = getattr(owner, "settings", None)
        if owner_settings is not None:
            return owner_settings
    return resolve_cache_settings(cache) if cache is not None else get_settings()


def _require_settings_owner(name: str, owner: object, settings: Settings) -> None:
    owner_settings = getattr(owner, "settings", _MISSING)
    if owner_settings is _MISSING:
        return
    if owner_settings is not settings:
        raise ValueError(f"{name}.settings 必须与容器使用同一 Settings 实例")
