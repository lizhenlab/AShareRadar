from __future__ import annotations

from pathlib import Path

from app.runtime_environment import isolate_user_site_packages

isolate_user_site_packages()

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.config import get_settings
from app.api.deps import get_scheduler
from app.api.errors import validation_exception_handler
from app.api.routes import alerts, analysis, data, health, monitoring, notes, quotes, stock, watchlist


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="AShareRadar", version="0.1.0")
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(ValidationError, validation_exception_handler)
    _register_routes(app)
    _register_lifecycle(app)
    return app


def _register_routes(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(quotes.router)
    app.include_router(analysis.router)
    app.include_router(stock.router)
    app.include_router(alerts.router)
    app.include_router(notes.router)
    app.include_router(watchlist.router)
    app.include_router(monitoring.router)
    app.include_router(data.router)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


def _register_lifecycle(app: FastAPI) -> None:
    @app.on_event("startup")
    async def startup() -> None:
        await get_scheduler().start()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await get_scheduler().stop()


app = create_app()
