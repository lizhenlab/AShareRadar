from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.container import AppContainer
from app.config import Settings
from app.main import create_app


class _SchedulerStub:
    async def start(self) -> bool:
        return True

    async def stop(self) -> bool:
        return True


class _ContextsStub:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _DataHubStub:
    pass


def test_lifespan_rejects_container_factory_with_another_settings_owner(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app_settings = Settings(cache_path=tmp_path / "app.sqlite3", scheduler_enabled=False)
    container_settings = Settings(cache_path=tmp_path / "container.sqlite3", scheduler_enabled=False)
    contexts = _ContextsStub()
    container = AppContainer(
        settings=container_settings,
        datahub=_DataHubStub(),  # type: ignore[arg-type]
        scheduler=_SchedulerStub(),  # type: ignore[arg-type]
        workbench_contexts=contexts,  # type: ignore[arg-type]
    )
    application = create_app(
        settings=app_settings,
        container_factory=lambda: container,
        static_dir=static_dir,
    )

    with pytest.raises(ValueError, match="container_factory.*同一 Settings 实例"):
        with TestClient(application):
            pass

    assert contexts.closed is True
