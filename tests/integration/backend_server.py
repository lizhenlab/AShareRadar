"""Serve the real application against an owned temporary SQLite fixture only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
MARKER = "AShareRadar isolated backend smoke v1\n"


def _owned_directory(raw: str) -> Path:
    directory = Path(raw).resolve(strict=True)
    temporary = Path(tempfile.gettempdir()).resolve()
    if not directory.is_relative_to(temporary) or not directory.name.startswith("ashare-backend-smoke-"):
        raise ValueError("integration server requires its own temporary directory")
    if (directory / ".integration-owned").read_text() != MARKER:
        raise ValueError("integration directory ownership marker is missing")
    return directory


def _deny_external_network(event: str, arguments: tuple[object, ...]) -> None:
    if event == "socket.getaddrinfo":
        address = arguments[0]
    elif event == "socket.connect":
        target = arguments[1]
        address = target[0] if isinstance(target, tuple) else None
    else:
        return
    if address not in {None, "127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("external network is disabled in the integration fixture")


def _application(directory: Path, origin: str):
    import app.config_settings as configuration
    configuration._SHELL_ENV_VALUES = {}
    from app.api.container import build_container
    from app.main import create_app
    from app.services import datahub as datahub_module
    from fastapi.responses import HTMLResponse
    from tests.factories import make_quote, make_stock_info

    settings = configuration.Settings(
        cache_path=directory / "runtime.sqlite3", cors_allow_origins=(origin,),
        scheduler_enabled=False, market_scan_auto_enabled=False, llm_enabled=False,
        fuyao_enabled=False, futu_enabled=False,
        market_scan_official_execution_registry_path=directory / "official/registry.json",
        market_scan_official_execution_raw_root=directory / "official/raw",
        market_scan_official_execution_session_directory=directory / "official/sessions",
        market_scan_joint_execution_authorization_path=directory / "authorization.json",
        market_scan_probability_ranking_control_path=directory / "ranking-control.json",
    )
    # Only the provider boundary is replaced. API validation, domain services,
    # repositories, SQLite transactions and HTTP middleware remain production code.
    datahub_module.build_providers = lambda _settings: {}

    class FixtureDataHub(datahub_module.DataHub):
        async def quote(self, symbol: str, use_cache: bool = True):
            if symbol not in {"600519", "600519.SH"}:
                raise ValueError("stock is outside the deterministic integration fixture")
            return make_quote(source="isolated-integration-fixture")

    hub = FixtureDataHub(settings=settings)
    hub.cache.save_stock_pool([make_stock_info()])
    application = create_app(settings=settings, container_factory=lambda: build_container(settings=settings, datahub=hub))

    @application.get("/_integration/smoke", include_in_schema=False)
    def smoke_document():
        return HTMLResponse("<!doctype html><title>Isolated backend smoke</title><main>Browser API integration</main>")

    return application


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    directory = _owned_directory(args.directory)
    sys.addaudithook(_deny_external_network)
    import uvicorn

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        application = _application(directory, origin)
        print(json.dumps({"baseURL": origin}), flush=True)
        server = uvicorn.Server(uvicorn.Config(application, log_level="warning", timeout_graceful_shutdown=2))
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            # Uvicorn re-raises its captured SIGINT after lifespan cleanup.
            pass


if __name__ == "__main__":
    main()
