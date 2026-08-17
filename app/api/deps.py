from __future__ import annotations

from fastapi import Request

from app.api.container import AppContainer
from app.config import Settings
from app.services.datahub import DataHub
from app.services.domain_service_bundle import DomainServiceBundle
from app.services.local_data_import_guard import LocalDataImportPreviewRegistry
from app.services.market_scan_manager import MarketScanManager
from app.services.scheduler import LocalDataScheduler
from app.services.workbench_context import WorkbenchContextCache
from app.api.market_scan_read_admission import MarketScanHeavyReadAdmission


def get_container(request: Request) -> AppContainer:
    try:
        return request.app.state.container
    except AttributeError as exc:
        raise RuntimeError("应用容器尚未初始化，请在 FastAPI lifespan 内访问") from exc


def get_app_settings(request: Request) -> Settings:
    return get_container(request).settings


def get_datahub(request: Request) -> DataHub:
    return get_container(request).datahub


def get_domain_services(request: Request) -> DomainServiceBundle:
    services = get_container(request).domain_services
    if services is None:
        raise RuntimeError("领域服务尚未初始化")
    return services


def get_scheduler(request: Request) -> LocalDataScheduler:
    return get_container(request).scheduler


def get_market_scanner(request: Request) -> MarketScanManager:
    scanner = get_container(request).market_scanner
    if scanner is None:
        raise RuntimeError("全市场扫描管理器尚未初始化")
    return scanner


def get_market_scan_heavy_read_admission(request: Request) -> MarketScanHeavyReadAdmission:
    return get_container(request).market_scan_heavy_read_admission


def get_workbench_context_cache(request: Request) -> WorkbenchContextCache:
    return get_container(request).workbench_contexts


def get_local_data_import_previews(request: Request) -> LocalDataImportPreviewRegistry:
    return get_container(request).local_data_import_previews
