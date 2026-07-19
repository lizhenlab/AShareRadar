from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from functools import partial
import logging
from pathlib import Path

from app.runtime_environment import isolate_user_site_packages

isolate_user_site_packages()

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
    market_scan,
    monitoring,
    notes,
    quotes,
    reviews,
    stock,
    watchlist,
    watchlist_scan,
)
from app.api.security import SameOriginMutationMiddleware
from app.api.static_assets import RevalidatingStaticFiles
from app.config import PROJECT_ROOT, Settings, get_settings, resolve_project_path


STATIC_DIR = PROJECT_ROOT / "static"
DATAHUB_SHUTDOWN_TIMEOUT_SECONDS = 2.0
ContainerFactory = Callable[[], AppContainer]
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container: AppContainer = await run_in_threadpool(app.state.container_factory)
    await _validate_container_settings(app, container)
    app.state.container = container
    try:
        await _start_runtime(container)
    except BaseException:
        await _cleanup_failed_start(container)
        raise
    try:
        yield
    finally:
        await _shutdown_container(container)


async def _validate_container_settings(app: FastAPI, container: AppContainer) -> None:
    if container.settings is app.state.settings:
        return
    await _close_container_resources_safely(container)
    raise ValueError("container_factory 必须复用 create_app 的同一 Settings 实例")


async def _start_runtime(container: AppContainer) -> None:
    if container.runtime_coordinator is not None:
        await container.runtime_coordinator.start()
        return
    if container.market_scanner is not None:
        await container.market_scanner.start()
    await container.scheduler.start()


async def _stop_runtime(container: AppContainer) -> None:
    if container.runtime_coordinator is not None:
        await container.runtime_coordinator.stop()
        return
    errors: list[BaseException] = []
    try:
        await container.scheduler.stop()
    except BaseException as exc:
        errors.append(exc)
    if container.market_scanner is not None:
        try:
            await container.market_scanner.stop()
        except BaseException as exc:
            errors.append(exc)
    _raise_cleanup_errors("runtime shutdown failed", errors)


async def _cleanup_failed_start(container: AppContainer) -> None:
    with suppress(BaseException):
        await _stop_runtime(container)
    await _close_container_resources_safely(container)


async def _close_container_resources_safely(container: AppContainer) -> None:
    with suppress(BaseException):
        await container.workbench_contexts.aclose()
    with suppress(BaseException):
        await _close_datahub(container.datahub)


async def _shutdown_container(container: AppContainer) -> None:
    errors: list[BaseException] = []
    try:
        await _stop_runtime(container)
    except BaseException as exc:
        errors.append(exc)
    try:
        await container.workbench_contexts.aclose()
    except BaseException as exc:
        errors.append(exc)
    try:
        await _close_datahub(container.datahub)
    except BaseException as exc:
        errors.append(exc)
    _raise_cleanup_errors("application shutdown failed", errors)


async def _close_datahub(datahub: object) -> None:
    close = getattr(datahub, "aclose", None)
    if not callable(close):
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DATAHUB_SHUTDOWN_TIMEOUT_SECONDS
    result, timed_out = await _call_close_before_deadline(close, deadline)
    if result is False and not timed_out:
        result, timed_out = await _call_close_before_deadline(close, deadline)
    if timed_out or result is False:
        logger.warning(
            "DataHub shutdown did not finish within the bounded application shutdown window; "
            "daemon workers may remain until process exit"
        )


async def _call_close_before_deadline(close: Callable[[], Awaitable[object]], deadline: float) -> tuple[object, bool]:
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    if remaining <= 0:
        return False, True
    try:
        return await asyncio.wait_for(close(), timeout=remaining), False
    except TimeoutError:
        return False, True


def _raise_cleanup_errors(message: str, errors: list[BaseException]) -> None:
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup(message, errors)


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
    app.mount("/static", RevalidatingStaticFiles(directory=resolved_static_dir), name="static")
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
    app.include_router(market_scan.router)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})


app = create_app()
