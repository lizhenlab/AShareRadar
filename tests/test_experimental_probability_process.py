from __future__ import annotations

import asyncio
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
from threading import Event, Thread
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_market_scanner
from app.api.market_scan_read_admission import MarketScanHeavyReadAdmission, run_admitted_market_scan_read
from app.api.routes.market_scan import router
from app.artifacts.io import canonical_json_bytes, exclusive_atomic_publish, sha256_hex
from app.db.market_scan_integrity import MarketScanSnapshotSealError
from app.services import experimental_probability_process as worker
from app.services.experimental_probability_model import MODEL_DIRECTORY, ExperimentalEstimator, ExperimentalProbabilityUnavailable
from app.utils.errors import NotFoundError
from tests.test_experimental_direction_probability import _direction_estimator
from tests.test_strategy_execution import _disable_market_scan_immutability, _environment


def test_experimental_route_has_independent_bounded_admission_while_formal_read_is_busy():
    formal, experimental = MarketScanHeavyReadAdmission(), MarketScanHeavyReadAdmission()
    started, release = Event(), Event()

    def blocked_results(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=10)
        return {"mode": "personal_experimental"}

    application = FastAPI()
    application.include_router(router)
    application.state.container = SimpleNamespace(market_scan_heavy_read_admission=formal,
        market_scan_experimental_read_admission=experimental)
    application.dependency_overrides[get_market_scanner] = lambda: SimpleNamespace(experimental_probability_results=blocked_results)
    assert formal._slot.acquire(blocking=False)  # Simulate a slow formal snapshot verifier.
    uri = "/api/market-scans/3/experimental-probability?acknowledge_experimental=true&prediction_kind=close_d5"
    responses = []
    with TestClient(application) as client:
        first = Thread(target=lambda: responses.append(client.get(uri)))
        try:
            first.start()
            assert started.wait(timeout=5), "formal verifier must not block the experimental lane"
            assert formal.active_count == experimental.active_count == 1
            busy = client.get(uri)
            assert busy.status_code == 503 and busy.headers["retry-after"] == "2"
            assert busy.headers["cache-control"] == "no-store"
        finally:
            release.set()
            first.join(timeout=5)
            formal._slot.release()
    assert not first.is_alive() and responses[0].status_code == 200
    assert experimental.active_count == 0


def test_shutdown_drains_both_read_lanes_before_returning():
    from app.main import _close_market_scan_read_admissions

    async def scenario():
        lanes = [MarketScanHeavyReadAdmission(), MarketScanHeavyReadAdmission()]
        started, release = [Event(), Event()], [Event(), Event()]

        def read(index):
            started[index].set()
            assert release[index].wait(timeout=5)
            return index

        reads = [asyncio.create_task(run_admitted_market_scan_read(lane, lambda index=index: read(index))) for index, lane in enumerate(lanes)]
        try:
            assert all(await asyncio.gather(*(asyncio.to_thread(event.wait, 2) for event in started)))
            closing = asyncio.create_task(_close_market_scan_read_admissions(SimpleNamespace(
                market_scan_heavy_read_admission=lanes[0], market_scan_experimental_read_admission=lanes[1])))
            await asyncio.sleep(0)
            release[0].set()
            await reads[0]
            assert not closing.done()
            release[1].set()
            await asyncio.wait_for(closing, 2)
            assert [lane.active_count + lane.worker_count for lane in lanes] == [0, 0]
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*reads, return_exceptions=True)

    asyncio.run(scenario())


def test_worker_supervisor_uses_bounded_stdin_not_shell_or_server_threads(tmp_path, monkeypatch):
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout=b'{"result":{"prediction_kind":"close_d2"}}')

    monkeypatch.setattr(worker.subprocess, "run", run)
    result = worker.isolated_experimental_results(tmp_path / "runtime.sqlite3", 3, prediction_kind="close_d2", keyword="600001")
    command, options = calls[0]
    assert command[1:] == ["-m", "app.services.experimental_probability_process"]
    assert options["timeout"] == 60 and options["env"]["PYTHONNOUSERSITE"] == "1"
    assert "shell" not in options and "600001" not in command
    assert json.loads(options["input"])["filters"]["prediction_kind"] == result["prediction_kind"] == "close_d2"


@pytest.mark.parametrize("mode", ["timeout", "start", "exit", "oversize", "malformed"])
def test_worker_supervisor_fails_closed_without_exposing_stderr(tmp_path, monkeypatch, mode):
    def run(*args, **options):
        if mode == "timeout":
            raise subprocess.TimeoutExpired(args[0], options["timeout"])
        if mode == "start":
            raise OSError("secret diagnostic")
        content = b"x" * (worker.WORKER_MAX_OUTPUT_BYTES + 1) if mode == "oversize" else b"invalid"
        return SimpleNamespace(returncode=1 if mode == "exit" else 0, stdout=content, stderr=b"secret diagnostic")

    monkeypatch.setattr(worker.subprocess, "run", run)
    with pytest.raises(RuntimeError) as caught:
        worker.isolated_experimental_results(tmp_path / "runtime.sqlite3", 3)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("kind,error_type", [("snapshot_integrity", MarketScanSnapshotSealError),
    ("not_found", NotFoundError), ("unavailable", ExperimentalProbabilityUnavailable),
    ("invalid_input", ValueError), ("database_unavailable", sqlite3.DatabaseError), ("internal_validation", RuntimeError)])
def test_worker_preserves_public_error_categories(tmp_path, monkeypatch, kind, error_type):
    monkeypatch.setattr(worker.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=canonical_json_bytes({"error": kind, "message": "expected failure"})))
    with pytest.raises(error_type, match="expected failure"):
        worker.isolated_experimental_results(tmp_path / "runtime.sqlite3", 3)


@pytest.mark.parametrize("payload", [{}, {"database": "unused", "run_id": True, "filters": {}},
    {"database": "unused", "run_id": 1, "filters": {"source": "arbitrary"}}])
def test_worker_rejects_invalid_identity_and_unknown_parameters(payload):
    with pytest.raises(ValueError):
        worker._read_worker_request(canonical_json_bytes(payload))


def test_real_worker_reverifies_snapshot_and_never_writes_database(tmp_path):
    cache, _service, _strategy, run_id = _environment(tmp_path)
    for offset in (1, 2, 5):
        payload = _direction_estimator(offset).model_dump()
        payload.update(calibration_end="2026-07-09", latest_label_date="2026-07-10",
                       expires_after_signal_date=(date(2026, 7, 10) + timedelta(days=90)).isoformat())
        model = ExperimentalEstimator.model_validate(payload).model_dump()
        digest = sha256_hex(canonical_json_bytes(model))
        path = Path(cache.path).parent / MODEL_DIRECTORY / f"experimental-close-d{offset}-{digest}.json"
        exclusive_atomic_publish(path, canonical_json_bytes({"payload": model, "sha256": digest}), max_bytes=256 * 1024)
    before = Path(cache.path).read_bytes()
    for kind in ("close_d1", "close_d2", "close_d5"):
        result = worker.isolated_experimental_results(Path(cache.path), run_id, prediction_kind=kind)
        assert result["prediction_kind"] == kind and result["formal_filter_qualified"] is False
        assert result["production_ranking_effect"] == "none" and result["coverage"]["successful_scan_count"] == 4
    assert Path(cache.path).read_bytes() == before
    with sqlite3.connect(cache.path) as connection:
        _disable_market_scan_immutability(connection)
        connection.execute("UPDATE market_scan_result SET amount=amount+1 WHERE run_id=?", (run_id,))
    with pytest.raises(MarketScanSnapshotSealError, match="摘要不一致"):
        worker.isolated_experimental_results(Path(cache.path), run_id, prediction_kind="close_d2")


def test_query_service_passes_every_filter_to_isolated_worker(tmp_path, monkeypatch):
    from app.services.market_scan_query_service import MarketScanQueryService

    calls = []
    monkeypatch.setattr(worker, "isolated_experimental_results", lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True})
    service = MarketScanQueryService(SimpleNamespace(path=tmp_path / "runtime.sqlite3"), SimpleNamespace())
    result = service.experimental_probability_results(7, prediction_kind="close_d1", minimum=.4, market="SZ",
        keyword="test", sort="base_rank", page=2, page_size=20)
    assert result == {"ok": True} and calls == [((tmp_path / "runtime.sqlite3", 7), {
        "prediction_kind": "close_d1", "minimum": .4, "market": "SZ", "keyword": "test",
        "sort": "base_rank", "page": 2, "page_size": 20})]
