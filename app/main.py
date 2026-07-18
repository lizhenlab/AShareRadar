from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from functools import partial
from pathlib import Path

from app.runtime_environment import isolate_user_site_packages

isolate_user_site_packages()

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.api.container import AppContainer, build_container
from app.api.errors import internal_validation_exception_handler, validation_exception_handler
from app.api.routes import (
    alerts,
    analysis,
    data,
    health,
    local_data,
    monitoring,
    notes,
    quotes,
    reviews,
    stock,
    watchlist,
    watchlist_scan,
)
from app.api.security import SameOriginMutationMiddleware
from app.config import PROJECT_ROOT, Settings, get_settings, resolve_project_path


STATIC_DIR = PROJECT_ROOT / "static"
ContainerFactory = Callable[[], AppContainer]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container: AppContainer = await run_in_threadpool(app.state.container_factory)
    if container.settings is not app.state.settings:
        with suppress(BaseException):
            await _close_datahub(container.datahub)
        with suppress(BaseException):
            await container.workbench_contexts.aclose()
        raise ValueError("container_factory 必须复用 create_app 的同一 Settings 实例")
    app.state.container = container
    try:
        await container.scheduler.start()
    except BaseException:
        with suppress(BaseException):
            await container.scheduler.stop()
        with suppress(BaseException):
            await _close_datahub(container.datahub)
        with suppress(BaseException):
            await container.workbench_contexts.aclose()
        raise
    try:
        yield
    finally:
        try:
            await container.scheduler.stop()
        finally:
            try:
                await _close_datahub(container.datahub)
            finally:
                await container.workbench_contexts.aclose()


async def _close_datahub(datahub: object) -> None:
    close = getattr(datahub, "aclose", None)
    if callable(close):
        await close()


def create_app(
    *,
    settings: Settings | None = None,
    container_factory: ContainerFactory | None = None,
    static_dir: str | Path | None = None,
) -> FastAPI:
    resolved_settings = settings if settings is not None else get_settings()
    resolved_static_dir = resolve_project_path(static_dir if static_dir is not None else STATIC_DIR)
    resolved_container_factory = (
        container_factory if container_factory is not None else partial(build_container, settings=resolved_settings)
    )
    app = FastAPI(title=resolved_settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.container_factory = resolved_container_factory
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.cors_allow_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        SameOriginMutationMiddleware,
        allowed_origins=resolved_settings.cors_allow_origins,
    )
    app.mount("/static", StaticFiles(directory=resolved_static_dir), name="static")
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(ValidationError, internal_validation_exception_handler)
    _register_routes(app, resolved_static_dir)
    return app


def _register_routes(app: FastAPI, static_dir: Path) -> None:
    app.include_router(health.router)
    app.include_router(quotes.router)
    app.include_router(analysis.router)
    app.include_router(stock.router)
    app.include_router(alerts.router)
    app.include_router(notes.router)
    app.include_router(watchlist.router)
    app.include_router(watchlist_scan.router)
    app.include_router(reviews.router)
    app.include_router(monitoring.router)
    app.include_router(data.router)
    app.include_router(local_data.router)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})


app = create_app()
