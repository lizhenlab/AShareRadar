from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


def test_real_uvicorn_serves_app_static_assets_and_temporary_sqlite(tmp_path: Path) -> None:
    port = _available_port()
    cache_path = tmp_path / "uvicorn-smoke.sqlite3"
    env = {
        **os.environ,
        "ASHARE_RADAR_CACHE_PATH": str(cache_path),
        "ASHARE_RADAR_CORS_ALLOW_ORIGINS": f"http://127.0.0.1:{port}",
        "ASHARE_RADAR_LLM_ENABLED": "0",
        "ASHARE_RADAR_MARKET_SCAN_AUTO_ENABLED": "0",
        "ASHARE_RADAR_SCHEDULER_ENABLED": "0",
        "PYTHONNOUSERSITE": "1",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--timeout-graceful-shutdown",
            "2",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stream_socket: socket.socket | None = None
    shutdown_elapsed = 0.0
    try:
        root = _wait_for_response(f"http://127.0.0.1:{port}/")
        latest = _wait_for_response(f"http://127.0.0.1:{port}/api/market-scans/latest")
        static = _wait_for_response(f"http://127.0.0.1:{port}/static/js/api.js")

        assert "AShareRadar" in root[0]
        assert root[1].get("cache-control") == "no-store"
        assert json.loads(latest[0]) is None
        assert latest[1].get("cache-control") == "no-store"
        assert "export async function fetchJson" in static[0]
        assert static[1].get("cache-control") == "no-cache"
        assert cache_path.exists()

        stream_socket = _open_quote_stream(port)
    finally:
        shutdown_started = time.monotonic()
        process.send_signal(signal.SIGINT)
        try:
            _stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _stdout, stderr = process.communicate(timeout=5)
        shutdown_elapsed = time.monotonic() - shutdown_started
        if stream_socket is not None:
            stream_socket.close()

    assert process.returncode == 0
    assert shutdown_elapsed < 8
    assert "Traceback" not in stderr


def test_uvicorn_stuck_provider_worker_shutdown_is_bounded_and_traceback_free(tmp_path: Path) -> None:
    port = _available_port()
    cache_path = tmp_path / "stuck-worker.sqlite3"
    env = {
        **os.environ,
        "STUCK_WORKER_CACHE_PATH": str(cache_path),
        "STUCK_WORKER_PORT": str(port),
        "STUCK_WORKER_ROOT": str(ROOT),
        "PYTHONNOUSERSITE": "1",
    }
    script = """
import asyncio
from pathlib import Path
import os
import threading
from types import SimpleNamespace

import uvicorn

from app import main as app_main
from app.config import Settings
from app.services.datahub import DataHub
from app.services.datahub_runtime import ProviderCallTimeoutError, run_provider_io


class Scheduler:
    def __init__(self, hub):
        self.hub = hub
        self.blocker = threading.Event()

    async def start(self):
        try:
            await self.hub._provider_runtime.call_provider(
                "stuck",
                "quote",
                lambda: run_provider_io(self.blocker.wait),
            )
        except ProviderCallTimeoutError:
            pass
        return True

    async def stop(self):
        return True


settings = Settings(
    cache_path=Path(os.environ["STUCK_WORKER_CACHE_PATH"]),
    provider_call_timeout_seconds=0.01,
    scheduler_enabled=False,
    llm_enabled=False,
    market_scan_auto_enabled=False,
)
hub = DataHub(settings=settings)
container = SimpleNamespace(
    settings=settings,
    datahub=hub,
    scheduler=Scheduler(hub),
    workbench_contexts=hub.workbench_contexts,
    market_scanner=None,
    runtime_coordinator=None,
)
app_main.DATAHUB_SHUTDOWN_TIMEOUT_SECONDS = 0.15
application = app_main.create_app(
    settings=settings,
    container_factory=lambda: container,
    static_dir=Path(os.environ["STUCK_WORKER_ROOT"]) / "static",
)
uvicorn.run(
    application,
    host="127.0.0.1",
    port=int(os.environ["STUCK_WORKER_PORT"]),
    log_level="warning",
    timeout_graceful_shutdown=1,
)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    shutdown_elapsed = 0.0
    try:
        _wait_for_response(f"http://127.0.0.1:{port}/")
    finally:
        shutdown_started = time.monotonic()
        process.send_signal(signal.SIGINT)
        try:
            _stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            _stdout, stderr = process.communicate(timeout=2)
        shutdown_elapsed = time.monotonic() - shutdown_started

    assert process.returncode == 0
    assert shutdown_elapsed < 2
    assert stderr.count("DataHub shutdown did not finish within the bounded application shutdown window") == 1
    assert "Traceback" not in stderr
    assert str(cache_path) not in stderr


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_response(url: str, *, timeout: float = 15.0) -> tuple[str, dict[str, str]]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as response:  # noqa: S310 - fixed loopback URL in test
                headers = {key.lower(): value for key, value in response.headers.items()}
                return response.read().decode("utf-8"), headers
        except Exception as exc:
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"Uvicorn did not serve {url}: {last_error}")


def _open_quote_stream(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.settimeout(15)
    client.sendall(
        (
            "GET /api/stream/quotes?symbols=600519.SH HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Accept: text/event-stream\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode("ascii")
    )
    response_head = bytearray()
    while b"\r\n\r\n" not in response_head:
        chunk = client.recv(4096)
        if not chunk:
            client.close()
            raise AssertionError("Uvicorn closed the quote stream before sending headers")
        response_head.extend(chunk)
    assert bytes(response_head).startswith(b"HTTP/1.1 200")
    return client
