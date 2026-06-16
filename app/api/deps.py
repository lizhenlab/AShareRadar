from __future__ import annotations

from app.api.container import get_container
from app.config import Settings
from app.services.datahub import DataHub
from app.services.scheduler import LocalDataScheduler
from app.services.workbench_context import WorkbenchContextCache


def get_app_settings() -> Settings:
    return get_container().settings


def get_datahub() -> DataHub:
    return get_container().datahub


def get_scheduler() -> LocalDataScheduler:
    return get_container().scheduler


def get_workbench_context_cache() -> WorkbenchContextCache:
    return get_container().workbench_contexts
