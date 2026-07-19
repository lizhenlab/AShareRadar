from __future__ import annotations

import asyncio
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api.container import AppContainer
from app.api.errors import run_sync_api_async
from app.config import Settings
from app.main import create_app
from app.services.workbench_context import WorkbenchContextCache


TEST_CLIENT_ORIGIN = "http://testserver"


class _TrackingScheduler:
    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        close_events: list[str] | None = None,
    ) -> None:
        self.start_error = start_error
        self.stop_error = stop_error
        self.close_events = close_events
        self.start_calls = 0
        self.stop_calls = 0
        self.start_thread_id: int | None = None

    async def start(self) -> bool:
        self.start_calls += 1
        self.start_thread_id = threading.get_ident()
        if self.start_error is not None:
            raise self.start_error
        return True

    async def stop(self) -> bool:
        self.stop_calls += 1
        if self.close_events is not None:
            self.close_events.append("runtime")
        if self.stop_error is not None:
            raise self.stop_error
        return True


class _TrackingContexts(WorkbenchContextCache):
    def __init__(self, close_events: list[str], *, close_error: Exception | None = None) -> None:
        super().__init__()
        self.close_events = close_events
        self.close_error = close_error
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        self.close_events.append("workbench")
        await super().aclose()
        if self.close_error is not None:
            raise self.close_error


class _DataHubStub:
    def __init__(
        self,
        close_events: list[str],
        *,
        close_error: Exception | None = None,
        close_results: list[bool] | None = None,
    ) -> None:
        self.close_events = close_events
        self.close_error = close_error
        self.close_results = list(close_results or [])
        self.closed = False
        self.close_calls = 0

    async def aclose(self) -> bool:
        self.closed = True
        self.close_calls += 1
        self.close_events.append("datahub")
        if self.close_error is not None:
            raise self.close_error
        if self.close_results:
            return self.close_results.pop(0)
        return True


class _StuckDataHubStub:
    def __init__(self, close_events: list[str]) -> None:
        self.close_events = close_events
        self.close_calls = 0

    async def aclose(self) -> bool:
        self.close_calls += 1
        self.close_events.append("datahub")
        if self.close_calls == 1:
            return False
        await asyncio.Event().wait()
        return False


def _static_tree(root: Path) -> Path:
    static_dir = root / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>isolated app</title>", encoding="utf-8")
    (static_dir / "asset.txt").write_text("static-ok", encoding="utf-8")
    return static_dir


def _container(
    settings: Settings,
    *,
    start_error: Exception | None = None,
    stop_error: Exception | None = None,
    workbench_close_error: Exception | None = None,
    datahub_close_error: Exception | None = None,
    datahub_close_results: list[bool] | None = None,
) -> AppContainer:
    close_events: list[str] = []
    contexts = _TrackingContexts(close_events, close_error=workbench_close_error)
    return AppContainer(
        settings=settings,
        datahub=_DataHubStub(
            close_events,
            close_error=datahub_close_error,
            close_results=datahub_close_results,
        ),  # type: ignore[arg-type]
        scheduler=_TrackingScheduler(
            start_error=start_error,
            stop_error=stop_error,
            close_events=close_events,
        ),  # type: ignore[arg-type]
        workbench_contexts=contexts,
    )


def test_create_app_lifespan_routes_static_and_start_stop_are_integrated(tmp_path: Path) -> None:
    static_dir = _static_tree(tmp_path)
    settings = Settings(
        app_name="IsolatedRadar",
        cors_allow_origins=(TEST_CLIENT_ORIGIN,),
        cache_path=tmp_path / "never-created.sqlite3",
        scheduler_enabled=False,
    )
    containers: list[AppContainer] = []
    factory_thread_ids: list[int] = []

    def factory() -> AppContainer:
        factory_thread_ids.append(threading.get_ident())
        container = _container(settings)
        containers.append(container)
        return container

    application = create_app(settings=settings, container_factory=factory, static_dir=static_dir)

    assert containers == []
    assert not settings.cache_path.exists()

    with TestClient(application) as client:
        first = application.state.container
        assert first is containers[0]
        assert client.get("/api/health").json() == {
            "status": "ok",
            "app": "IsolatedRadar",
            "provider": "datahub",
        }
        index = client.get("/")
        assert index.status_code == 200
        assert "isolated app" in index.text
        assert index.headers["cache-control"] == "no-store"
        assert client.get("/static/asset.txt").text == "static-ok"
        assert "/api/tasks/status" in application.openapi()["paths"]
        assert first.scheduler.start_calls == 1  # type: ignore[attr-defined]
        assert first.scheduler.start_thread_id != factory_thread_ids[0]  # type: ignore[attr-defined]

    assert first.scheduler.stop_calls == 1  # type: ignore[attr-defined]
    assert first.datahub.closed is True  # type: ignore[attr-defined]
    assert first.workbench_contexts.closed is True  # type: ignore[attr-defined]
    assert first.workbench_contexts.close_events == ["runtime", "workbench", "datahub"]  # type: ignore[attr-defined]
    assert not settings.cache_path.exists()

    with TestClient(application):
        second = application.state.container
        assert second is containers[1]
        assert second is not first

    assert second.scheduler.start_calls == 1  # type: ignore[attr-defined]
    assert second.scheduler.stop_calls == 1  # type: ignore[attr-defined]
    assert second.datahub.closed is True  # type: ignore[attr-defined]
    assert second.workbench_contexts.closed is True  # type: ignore[attr-defined]
    assert second.workbench_contexts.close_events == ["runtime", "workbench", "datahub"]  # type: ignore[attr-defined]


def test_separate_apps_receive_separate_lifespan_containers(tmp_path: Path) -> None:
    static_dir = _static_tree(tmp_path)
    settings = Settings(cache_path=tmp_path / "never-created.sqlite3", scheduler_enabled=False)
    containers: list[AppContainer] = []

    def factory() -> AppContainer:
        container = _container(settings)
        containers.append(container)
        return container

    first_app = create_app(settings=settings, container_factory=factory, static_dir=static_dir)
    second_app = create_app(settings=settings, container_factory=factory, static_dir=static_dir)

    with TestClient(first_app), TestClient(second_app):
        assert first_app.state.container is containers[0]
        assert second_app.state.container is containers[1]
        assert first_app.state.container is not second_app.state.container


def test_lifespan_cleans_up_when_scheduler_start_fails(tmp_path: Path) -> None:
    static_dir = _static_tree(tmp_path)
    settings = Settings(cache_path=tmp_path / "never-created.sqlite3", scheduler_enabled=False)
    container = _container(settings, start_error=RuntimeError("startup failed"))
    application = create_app(settings=settings, container_factory=lambda: container, static_dir=static_dir)

    with pytest.raises(RuntimeError, match="startup failed"):
        with TestClient(application):
            pass

    assert container.scheduler.start_calls == 1  # type: ignore[attr-defined]
    assert container.scheduler.stop_calls == 1  # type: ignore[attr-defined]
    assert container.datahub.closed is True  # type: ignore[attr-defined]
    assert container.workbench_contexts.closed is True  # type: ignore[attr-defined]
    assert container.workbench_contexts.close_events == ["runtime", "workbench", "datahub"]  # type: ignore[attr-defined]


def test_sync_api_repository_helper_offloads_from_event_loop() -> None:
    async def run_check() -> tuple[int, int]:
        event_loop_thread = threading.get_ident()
        repository_thread = await run_sync_api_async(threading.get_ident)
        return event_loop_thread, repository_thread

    event_loop_thread, repository_thread = asyncio.run(run_check())

    assert repository_thread != event_loop_thread


def test_lifespan_closes_workbench_when_scheduler_stop_fails(tmp_path: Path) -> None:
    static_dir = _static_tree(tmp_path)
    settings = Settings(cache_path=tmp_path / "never-created.sqlite3", scheduler_enabled=False)
    container = _container(settings, stop_error=RuntimeError("shutdown failed"))
    application = create_app(settings=settings, container_factory=lambda: container, static_dir=static_dir)

    with pytest.raises(RuntimeError, match="shutdown failed"):
        with TestClient(application):
            pass

    assert container.scheduler.stop_calls == 1  # type: ignore[attr-defined]
    assert container.datahub.closed is True  # type: ignore[attr-defined]
    assert container.workbench_contexts.closed is True  # type: ignore[attr-defined]
    assert container.workbench_contexts.close_events == ["runtime", "workbench", "datahub"]  # type: ignore[attr-defined]


@pytest.mark.parametrize("failure", ["workbench", "datahub"])
def test_lifespan_closes_both_resources_in_workbench_first_order_when_close_raises(
    tmp_path: Path,
    failure: str,
) -> None:
    static_dir = _static_tree(tmp_path)
    settings = Settings(cache_path=tmp_path / "never-created.sqlite3", scheduler_enabled=False)
    expected = RuntimeError(f"{failure} close failed")
    container = _container(
        settings,
        workbench_close_error=expected if failure == "workbench" else None,
        datahub_close_error=expected if failure == "datahub" else None,
    )
    application = create_app(settings=settings, container_factory=lambda: container, static_dir=static_dir)

    with pytest.raises(RuntimeError, match=f"{failure} close failed"):
        with TestClient(application):
            pass

    assert container.workbench_contexts.close_events == ["runtime", "workbench", "datahub"]  # type: ignore[attr-defined]
    assert container.workbench_contexts.closed is True  # type: ignore[attr-defined]
    assert container.datahub.closed is True  # type: ignore[attr-defined]


def test_failed_start_closes_datahub_after_workbench_close_failure(tmp_path: Path) -> None:
    static_dir = _static_tree(tmp_path)
    settings = Settings(cache_path=tmp_path / "never-created.sqlite3", scheduler_enabled=False)
    startup_error = RuntimeError("startup failed")
    container = _container(
        settings,
        start_error=startup_error,
        stop_error=RuntimeError("runtime stop failed"),
        workbench_close_error=RuntimeError("workbench close failed"),
        datahub_close_error=RuntimeError("datahub close failed"),
    )
    application = create_app(settings=settings, container_factory=lambda: container, static_dir=static_dir)

    with pytest.raises(RuntimeError, match="startup failed") as exc_info:
        with TestClient(application):
            pass

    assert exc_info.value is startup_error
    assert container.workbench_contexts.close_events == ["runtime", "workbench", "datahub"]  # type: ignore[attr-defined]
    assert container.workbench_contexts.closed is True  # type: ignore[attr-defined]
    assert container.datahub.closed is True  # type: ignore[attr-defined]


def test_lifespan_drains_same_datahub_close_after_false_result(tmp_path: Path) -> None:
    static_dir = _static_tree(tmp_path)
    settings = Settings(cache_path=tmp_path / "never-created.sqlite3", scheduler_enabled=False)
    container = _container(settings, datahub_close_results=[False, True])
    application = create_app(settings=settings, container_factory=lambda: container, static_dir=static_dir)

    with TestClient(application):
        pass

    assert container.datahub.close_calls == 2  # type: ignore[attr-defined]
    assert container.workbench_contexts.close_events == [  # type: ignore[attr-defined]
        "runtime",
        "workbench",
        "datahub",
        "datahub",
    ]


def test_lifespan_bounds_stuck_datahub_drain_and_warns_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    static_dir = _static_tree(tmp_path)
    settings = Settings(cache_path=tmp_path / "never-created.sqlite3", scheduler_enabled=False)
    close_events: list[str] = []
    datahub = _StuckDataHubStub(close_events)
    container = AppContainer(
        settings=settings,
        datahub=datahub,  # type: ignore[arg-type]
        scheduler=_TrackingScheduler(close_events=close_events),  # type: ignore[arg-type]
        workbench_contexts=_TrackingContexts(close_events),
    )
    application = create_app(settings=settings, container_factory=lambda: container, static_dir=static_dir)
    monkeypatch.setattr("app.main.DATAHUB_SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    caplog.set_level("WARNING", logger="app.main")

    with TestClient(application):
        shutdown_started = time.monotonic()

    assert time.monotonic() - shutdown_started < 0.5
    assert datahub.close_calls == 2
    assert close_events == ["runtime", "workbench", "datahub", "datahub"]
    warnings = [record.getMessage() for record in caplog.records if record.name == "app.main"]
    assert warnings == [
        "DataHub shutdown did not finish within the bounded application shutdown window; "
        "daemon workers may remain until process exit"
    ]
    assert "sqlite" not in warnings[0].lower()
    assert "key" not in warnings[0].lower()


def test_lifespan_groups_multiple_shutdown_errors_in_stable_order(tmp_path: Path) -> None:
    static_dir = _static_tree(tmp_path)
    settings = Settings(cache_path=tmp_path / "never-created.sqlite3", scheduler_enabled=False)
    runtime_error = RuntimeError("runtime stop failed")
    workbench_error = ValueError("workbench close failed")
    datahub_error = LookupError("datahub close failed")
    container = _container(
        settings,
        stop_error=runtime_error,
        workbench_close_error=workbench_error,
        datahub_close_error=datahub_error,
    )
    application = create_app(settings=settings, container_factory=lambda: container, static_dir=static_dir)

    with pytest.raises(ExceptionGroup) as exc_info:
        with TestClient(application):
            pass

    assert exc_info.value.exceptions == (runtime_error, workbench_error, datahub_error)
    assert container.workbench_contexts.close_events == ["runtime", "workbench", "datahub"]  # type: ignore[attr-defined]


def test_default_container_factory_uses_injected_temporary_sqlite(tmp_path: Path) -> None:
    static_dir = _static_tree(tmp_path)
    settings = Settings(
        cors_allow_origins=(TEST_CLIENT_ORIGIN,),
        cache_path=tmp_path / "lifespan.sqlite3",
        scheduler_enabled=False,
        llm_enabled=False,
    )
    application = create_app(settings=settings, static_dir=static_dir)

    assert not settings.cache_path.exists()

    with TestClient(application) as client:
        assert client.get("/api/health").status_code == 200
        assert application.state.container.settings is settings
        assert application.state.container.datahub.cache.path == settings.cache_path
        assert settings.cache_path.exists()

    assert application.state.container.scheduler.status().running is False
