from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from app.config import Settings
import app.config_settings as config_module
from app.models.fuyao_research import FuyaoJob, FuyaoJobRequest
from app.repositories.fuyao_research import FuyaoResearchRepository
from app.services.fuyao_client import FuyaoClient
from app.services.fuyao_contracts import FuyaoError
from app.services.fuyao_service import FuyaoPersistentBudget, FuyaoService
import app.services.fuyao_client as client_module
import app.services.fuyao_service as service_module


class Runtime:
    async def call_provider(self, _name, _kind, start, **_kwargs):
        return await start()


class Client:
    def status(self):
        return {"configured": True}

    async def request(self, _path, _params=None):
        return {"code": 0, "data": {"item": []}}

    async def aclose(self):
        pass


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_SHELL_ENV_VALUES", {})
    settings = Settings(cache_path=tmp_path / "runtime.sqlite3", fuyao_enabled=True, fuyao_api_key=None, fuyao_api_key_file=None)
    return FuyaoService(settings, Runtime(), client=Client())


async def completed(service, request):
    job = await service.start_job(request)
    await asyncio.gather(*tuple(service._tasks.values()))
    return job, next(item for item in (await service.status())["jobs"] if item["id"] == job.id)


def test_empty_valuation_batch_fails_instead_of_claiming_partial_success(service):
    async def run():
        submitted, final = await completed(service, FuyaoJobRequest(kind="valuations", symbols=["600519.sh", "600519.SH"]))
        assert submitted.status == "running" and submitted.total == 1
        assert final["status"] == "failed" and final["completed"] == 0
        assert service.repository.latest("valuations", "600519.SH") is None
        await service.aclose()
    asyncio.run(run())


def test_empty_financials_do_not_overwrite_prior_observation(service):
    service.repository.save_observation("financials", "600519.SH", "2026-09-10T00:00:00Z", {"previous": True})
    async def run():
        _, final = await completed(service, FuyaoJobRequest(kind="financials", symbols=["600519.SH"]))
        assert final["status"] == "failed"
        assert service.repository.latest("financials", "600519.SH").payload == {"previous": True}
        await service.aclose()
    asyncio.run(run())


def test_partial_valuation_batch_is_degraded_with_actual_count(service):
    async def request(_path, _params=None):
        return {"code": 0, "data": {"timestamp": None, "item": [{"thscode": "600519.sh", "pe_ttm": -1}]}}
    service.client.request = request
    async def run():
        _, final = await completed(service, FuyaoJobRequest(kind="valuations", symbols=["600519.SH", "000001.SZ"]))
        assert final["status"] == "degraded" and final["completed"] == 1
        assert service.repository.latest("valuations", "600519.SH").payload["individual_timestamp"] is None
        await service.aclose()
    asyncio.run(run())


def test_cancellation_during_write_drains_publication_and_persists_terminal_count(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    save = service.repository.publish_item
    def slow_save(*args):
        entered.set()
        assert release.wait(3)
        return save(*args)
    monkeypatch.setattr(service.repository, "publish_item", slow_save)
    async def collect(job, _request):
        await service._save_item(job, "valuations", "600519.SH", {"values": {"pe_ttm": 2}})
        await asyncio.Event().wait()
    monkeypatch.setattr(service, "_collect", collect)
    async def run():
        job = await service.start_job(FuyaoJobRequest(kind="valuations", symbols=["600519.SH", "000001.SZ"]))
        assert await asyncio.to_thread(entered.wait, 2)
        close = asyncio.create_task(service.aclose())
        await asyncio.sleep(0.01)
        assert not close.done()
        release.set()
        await close
        final = next(item for item in service.repository.jobs() if item.id == job.id)
        assert final.status == "cancelled" and final.completed == 1
        assert service.repository.latest("valuations", "600519.SH") is not None
    asyncio.run(run())


def test_cancelled_job_submission_has_no_persisted_orphan(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    save = service.repository.save_job
    def slow_save(job):
        if job.status == "running":
            entered.set()
            assert release.wait(3)
        return save(job)
    monkeypatch.setattr(service.repository, "save_job", slow_save)
    async def collect(_job, _request):
        await asyncio.Event().wait()
    monkeypatch.setattr(service, "_collect", collect)
    async def run():
        submission = asyncio.create_task(service.start_job(FuyaoJobRequest(kind="valuations", symbols=["600519.SH"])))
        assert await asyncio.to_thread(entered.wait, 2)
        submission.cancel()
        shutdown = asyncio.create_task(service.aclose())
        await asyncio.sleep(0.01)
        assert not submission.done() and not shutdown.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await submission
        await shutdown
        assert all(job.status == "cancelled" for job in service.repository.jobs())
        assert not service._tasks
    asyncio.run(run())


def test_status_recovers_interrupted_jobs_without_new_collection(service):
    service.repository.save_job(FuyaoJob(id="old", kind="valuations", status="running", created_at="2026-09-10T00:00:00Z"))
    async def run():
        assert (await service.status())["jobs"][0]["status"] == "interrupted"
        await service.aclose()
    asyncio.run(run())


def test_terminal_status_storage_failure_is_visible_from_memory(service, monkeypatch):
    save = service.repository.save_job
    def fail_terminal(job):
        if job.status != "running":
            raise OSError("synthetic-storage-failure")
        save(job)
    monkeypatch.setattr(service.repository, "save_job", fail_terminal)
    async def run():
        _, final = await completed(service, FuyaoJobRequest(kind="valuations", symbols=["600519.SH"]))
        assert final["status"] == "failed" and "持久" in final["message"]
        await service.aclose()
    asyncio.run(run())


def test_persistent_budget_counts_retries_and_survives_repository_reopen(service, monkeypatch):
    counter = 0
    async def no_sleep(_seconds):
        pass
    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)
    def handler(_request):
        nonlocal counter
        counter += 1
        return httpx.Response(429, json={"code": 4001}) if counter == 1 else httpx.Response(200, json={"code": 0, "data": {}})
    settings = service.settings.model_copy(update={"fuyao_request_interval_seconds": 0, "fuyao_daily_request_limit": 2})
    from pydantic import SecretStr
    settings.fuyao_api_key = SecretStr("synthetic-budget-key")
    budget = FuyaoPersistentBudget(service.repository, 2)
    async def run():
        client = FuyaoClient(settings, budget=budget, transport=httpx.MockTransport(handler))
        await client.request("/api/a-share/financials/income-statements")
        reopened = FuyaoPersistentBudget(FuyaoResearchRepository(service.repository.path), 2)
        with pytest.raises(FuyaoError, match="daily_request_limit"):
            await reopened.reserve()
        await client.aclose()
        await service.aclose()
    asyncio.run(run())
    assert counter == 2


def test_history_limits_distinct_shanghai_days_before_row_limit(service):
    repository = service.repository
    for instant in ("2026-09-09T15:00:00+08:00", "2026-09-10T16:01:00Z", "2026-09-11T01:00:00+08:00"):
        repository.save_observation("valuations", "600519.SH", instant, {"values": {"pe_ttm": 3}})
    rows = repository.valuation_history("600519.SH", 2)
    assert len(rows) == 2
    assert rows[0].fetched_at == "2026-09-11T01:00:00+08:00"
    assert rows[1].fetched_at == "2026-09-09T15:00:00+08:00"


@pytest.mark.parametrize("code", [1002, 3001, 3004])
def test_symbol_parameter_rejection_does_not_abort_remaining_financials(service, monkeypatch, code):
    seen = []
    async def financials(_client, symbol, _request, _fetched):
        seen.append(symbol)
        if symbol == "430047.BJ":
            raise FuyaoError("business_error", code=code)
        return {"report": {"symbol": symbol}}
    monkeypatch.setattr(service_module, "fetch_financials", financials)
    async def run():
        _, final = await completed(service, FuyaoJobRequest(kind="financials", symbols=["600519.SH", "430047.BJ", "920002.BJ"]))
        assert final["status"] == "degraded" and final["completed"] == 2
        assert len(final["errors"]) == 1 and "430047.BJ" in final["errors"][0] and str(code) in final["errors"][0]
        assert seen == ["600519.SH", "430047.BJ", "920002.BJ"]
        await service.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("code", [1002, 3001, 3004])
def test_mixed_valuation_rejection_splits_bounded_batches_without_excluding_beijing(service, code):
    seen = []
    async def request(_path, params):
        symbols = params["thscodes"].split(",")
        seen.append(symbols)
        if "430047.BJ" in symbols:
            raise FuyaoError("business_error", code=code)
        return {"code": 0, "data": {"timestamp": None, "item": [{"thscode": symbol, "pe_ttm": 3} for symbol in symbols]}}
    service.client.request = request
    async def run():
        _, final = await completed(service, FuyaoJobRequest(kind="valuations", symbols=["600519.SH", "430047.BJ", "920002.BJ"]))
        assert final["status"] == "degraded" and final["completed"] == 2
        assert len(seen) <= 2 * 3 - 1
        assert service.repository.latest("valuations", "920002.BJ") is not None
        assert "430047.BJ" in final["errors"][0] and str(code) in final["errors"][0]
        await service.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("category,code", [("permission_denied", 2003), ("rate_limited", 4001), ("remote_unavailable", 5003),
    ("business_error", 1003), ("business_error", 3002), ("business_error", 9000)])
def test_non_symbol_failures_stop_valuation_collection_without_splitting(service, category, code):
    seen = []
    async def request(_path, params):
        seen.append(params)
        raise FuyaoError(category, code=code)
    service.client.request = request
    async def run():
        _, final = await completed(service, FuyaoJobRequest(kind="valuations", symbols=["600519.SH", "000001.SZ"]))
        assert final["status"] == "failed" and final["completed"] == 0 and len(seen) == 1
        assert str(code) in final["errors"][0]
        await service.aclose()
    asyncio.run(run())
