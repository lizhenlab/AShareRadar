from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace

import anyio
from fastapi import FastAPI
import httpx
import pytest

from app.api.container import AppContainer
from app.api.deps import get_datahub
from app.api.routes import fuyao
from app.config import Settings
import app.config_settings as config_module
from app.services.fuyao_service import FuyaoService
from app.services.workbench_context import WorkbenchContextCache


class Runtime:
    async def call_provider(self, _name, _kind, start, **_kwargs):
        return await start()


class Client:
    def __init__(self):
        self.calls = 0

    def status(self):
        return {"configured": True}

    async def request(self, _path, _params=None):
        self.calls += 1
        await asyncio.Event().wait()

    async def aclose(self):
        pass


@pytest.fixture
def context(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "_SHELL_ENV_VALUES", {})
    settings = Settings(cache_path=tmp_path / "runtime.sqlite3", fuyao_enabled=True, fuyao_api_key=None, fuyao_api_key_file=None)
    client = Client()
    service = FuyaoService(settings, Runtime(), client=client)
    app = FastAPI()
    app.include_router(fuyao.router)
    app.dependency_overrides[get_datahub] = lambda: SimpleNamespace(fuyao=service)
    return app, service, client


def test_read_endpoints_only_use_cached_records_and_skip_dump_file_hashing(context, monkeypatch):
    app, service, upstream = context
    service.repository.save_observation("valuations", "600519.SH", "2026-09-10T15:00:00+08:00",
        {"symbol": "600519.SH", "values": {"pe_ttm": -2}, "individual_timestamp": None})
    flags = []
    def dump_status(_root, *, verify_files):
        flags.append(verify_files)
        return None
    monkeypatch.setattr(fuyao, "read_dump_status", dump_status)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/fuyao/stock", params={"symbol": "600519.sh"})
            assert response.status_code == 200 and response.json()["symbol"] == "600519.SH"
            assert response.json()["available"] is True
            assert response.json()["valuation"]["payload"]["individual_timestamp"] is None
            for path in ("/api/fuyao/market", "/api/fuyao/status"):
                response = await client.get(path)
                assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert upstream.calls == 0
        await service.aclose()
    asyncio.run(run())
    assert flags == [False]


def test_submit_returns_actual_task_metadata_and_duplicate_job_is_conflict(context):
    app, service, _upstream = context
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/fuyao/jobs", json={"kind": "valuations", "symbols": ["600519.sh", "600519.SH"]})
            assert response.status_code == 202
            payload = response.json()
            assert payload["id"] in service._tasks and payload["status"] == "running" and payload["total"] == 1
            assert response.headers["cache-control"] == "no-store"
            response = await client.post("/api/fuyao/jobs", json={"kind": "valuations", "symbols": ["000001.SZ"]})
            assert response.status_code == 409
        await service.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("symbol", ["600519.SZ", "000001.SH", "invalid"])
def test_invalid_stock_identity_is_422_without_calling_provider(context, symbol):
    app, service, upstream = context
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/fuyao/stock", params={"symbol": symbol})
            assert response.status_code == 422
            response = await client.post("/api/fuyao/jobs", json={"kind": "financials", "symbols": [symbol]})
            assert response.status_code == 422 and not service._tasks
        assert upstream.calls == 0
        await service.aclose()
    asyncio.run(run())


def test_disabled_source_rejects_start_but_keeps_local_reads(context):
    app, service, upstream = context
    service.settings.fuyao_enabled = False
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/fuyao/jobs", json={"kind": "valuations", "symbols": ["600519.SH"]})
            assert response.status_code == 422
            assert (await client.get("/api/fuyao/stock", params={"symbol": "600519.SH"})).status_code == 200
        assert upstream.calls == 0
        await service.aclose()
    asyncio.run(run())


@asynccontextmanager
async def occupied_default_worker_pool():
    limiter = anyio.to_thread.current_default_thread_limiter()
    previous = limiter.total_tokens
    entered, release = threading.Event(), threading.Event()
    def hold_worker():
        entered.set()
        assert release.wait(5), "test worker was not released"
    limiter.total_tokens = 1
    worker = asyncio.create_task(anyio.to_thread.run_sync(hold_worker))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert limiter.borrowed_tokens == limiter.total_tokens == 1
        yield
    finally:
        release.set()
        try:
            await asyncio.wait_for(worker, 2)
        finally:
            limiter.total_tokens = previous


@pytest.mark.parametrize("path", ["/api/fuyao/status", "/api/fuyao/market", "/api/fuyao/stock?symbol=600519.SH"])
def test_cached_get_with_real_dependency_does_not_wait_for_anyio_workers(context, path):
    app, service, upstream = context
    hub = SimpleNamespace(settings=service.settings, fuyao=service)
    app.state.container = AppContainer(
        settings=service.settings, datahub=hub,
        scheduler=SimpleNamespace(settings=service.settings, datahub=hub),
        workbench_contexts=WorkbenchContextCache(),
    )
    app.dependency_overrides.clear()
    async def run():
        try:
            async with occupied_default_worker_pool():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    response = await asyncio.wait_for(client.get(path), 1)
                    assert response.status_code == 200
                    assert upstream.calls == 0
        finally:
            await service.aclose()
    asyncio.run(run())


def test_job_lookup_cancel_retry_routes_have_safe_codes_and_no_store(context, monkeypatch):
    from app.models.fuyao_research import FuyaoJob, FuyaoJobRequest
    app, service, upstream = context
    old = FuyaoJob(id='legacy', kind='financials', status='failed', created_at='2026-09-10T00:00:00Z')
    service.repository.save_job(old)
    parent = old.model_copy(update={'id': 'partial', 'kind': 'valuations', 'status': 'degraded', 'total': 2, 'completed': 1,
        'completed_symbols': ['600519.SH'], 'request': FuyaoJobRequest(kind='valuations', symbols=['600519.SH', '000001.SZ'])})
    service.repository.save_job(parent)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for suffix, method in (('', 'GET'), ('/cancel', 'POST'), ('/retry', 'POST')):
                result = await client.request(method, '/api/fuyao/jobs/missing' + suffix)
                assert result.status_code == 404
            result = await client.get('/api/fuyao/jobs/legacy')
            assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
            assert result.json()['request'] is None and upstream.calls == 0
            assert (await client.post('/api/fuyao/jobs/legacy/retry')).status_code == 409
            assert (await client.post('/api/fuyao/jobs/legacy/cancel')).json()['status'] == 'failed'
            retry = await client.post('/api/fuyao/jobs/partial/retry')
            assert retry.status_code == 202 and retry.headers['cache-control'] == 'no-store'
            child = retry.json()
            assert child['request']['symbols'] == ['000001.SZ'] and child['parent_job_id'] == 'partial'
            duplicate = await client.post('/api/fuyao/jobs/partial/retry')
            assert duplicate.status_code == 202 and duplicate.json()['id'] == child['id']
            assert (await client.post(f"/api/fuyao/jobs/{child['id']}/retry")).status_code == 409
            stopped = await client.post(f"/api/fuyao/jobs/{child['id']}/cancel")
            assert stopped.status_code == 202 and stopped.headers['cache-control'] == 'no-store'
            assert stopped.json()['status'] in {'cancelling', 'cancelled'}
        await service.aclose()
    asyncio.run(run())
