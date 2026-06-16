from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.config import Settings, get_settings
from app.services.datahub import DataHub
from app.services.scheduler import LocalDataScheduler
from app.services.workbench_context import WorkbenchContextCache


@dataclass
class AppContainer:
    settings: Settings
    datahub: DataHub
    scheduler: LocalDataScheduler
    workbench_contexts: WorkbenchContextCache


def build_container() -> AppContainer:
    settings = get_settings()
    datahub = DataHub()
    workbench_contexts = WorkbenchContextCache()
    setattr(datahub, "workbench_contexts", workbench_contexts)
    scheduler = LocalDataScheduler(datahub)
    return AppContainer(
        settings=settings,
        datahub=datahub,
        scheduler=scheduler,
        workbench_contexts=workbench_contexts,
    )


@lru_cache
def get_container() -> AppContainer:
    return build_container()
