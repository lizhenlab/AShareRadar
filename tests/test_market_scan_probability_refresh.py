"""Queries never own cold verification, including refresh and shutdown races."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
import json
from threading import Event
import time

import pytest

from app.services.market_scan_probability_refresh import ProbabilityResearchRefreshCoordinator
from app.services.market_scan_research_stores import MarketScanResearchStores
from app.services import market_scan_probability_source_research as research
from app.services import market_scan_probability_runtime as runtime_module
from app.services import market_scan_probability_historical_context as historical_context
from app.services.market_scan_probability_replay import HISTORICAL_REPLAY_SUPERSEDED_ARTIFACT_SCHEMA_VERSION
from tests import test_market_scan_probability_historical_context as historical_support
from tests import test_market_scan_probability_source_research as source_support
from tests.market_scan_test_support import _MarketScanHub, _scanner


async def _quiet_joint(**_kwargs):
    return None


async def _quiet_report(_exc):
    return None


async def _wait_idle(coordinator):
    async def wait():
        while coordinator.pending:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), timeout=2)


def test_requests_coalesce_across_threads_and_during_one_refresh():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls, active, maximum = 0, 0, 0

        async def refresh():
            nonlocal calls, active, maximum
            calls += 1
            active += 1
            maximum = max(maximum, active)
            entered.set()
            await release.wait()
            active -= 1

        coordinator = ProbabilityResearchRefreshCoordinator(refresh, _quiet_report)
        request = coordinator.start()
        assert request()
        await entered.wait()
        assert all(await asyncio.to_thread(lambda: [request() for _ in range(200)]))
        release.set()
        await _wait_idle(coordinator)
        assert calls == 2 and maximum == 1
        await coordinator.stop()
    asyncio.run(scenario())


def test_failed_refresh_retries_without_another_query_and_reports_state(caplog):
    async def scenario():
        calls = 0
        finished = asyncio.Event()

        async def refresh():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("invalid archive")
            finished.set()

        async def report(_error):
            raise OSError("monitor unavailable")

        coordinator = ProbabilityResearchRefreshCoordinator(refresh, report, retry_seconds=0.001)
        coordinator.start()()
        await asyncio.wait_for(finished.wait(), timeout=1)
        await _wait_idle(coordinator)
        assert coordinator.status["attempt_count"] == 2
        assert coordinator.status["failure_count"] == 0
        assert coordinator.status["last_failure_type"] == "ValueError"
        assert coordinator.status["last_failure_at"] and coordinator.status["reporting_failed"]
        await coordinator.stop()
    asyncio.run(scenario())
    assert "Probability refresh failure reporting failed: OSError" in caplog.text


def test_stop_before_dispatch_and_restart_reject_old_generation_callbacks():
    async def scenario():
        calls = []

        async def refresh():
            calls.append(True)

        coordinator = ProbabilityResearchRefreshCoordinator(refresh, _quiet_report)
        old = coordinator.start()
        assert old() and coordinator.start()()
        await coordinator.stop()
        assert not old()
        current = coordinator.start()
        assert not old() and current()
        await _wait_idle(coordinator)
        assert calls == [True]
        await coordinator.stop()
    asyncio.run(scenario())


def test_request_between_worker_exit_and_done_callback_is_not_lost(monkeypatch):
    async def scenario():
        calls = []

        async def refresh():
            calls.append(True)

        coordinator = ProbabilityResearchRefreshCoordinator(refresh, _quiet_report)
        request = coordinator.start()
        take = coordinator._take_request
        injected = False

        def take_then_request(epoch):
            nonlocal injected
            ready = take(epoch)
            if not ready and not injected:
                injected = True
                assert request()
            return ready

        monkeypatch.setattr(coordinator, "_take_request", take_then_request)
        request()
        await _wait_idle(coordinator)
        assert calls == [True, True]
        await coordinator.stop()
    asyncio.run(scenario())


def test_repeated_shutdown_cancellation_waits_for_the_actual_worker():
    async def scenario():
        entered, release, completed = Event(), Event(), Event()
        cancelled = asyncio.Event()

        def blocking():
            entered.set()
            assert release.wait(2)
            completed.set()

        async def refresh():
            worker = asyncio.create_task(asyncio.to_thread(blocking))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled.set()
                await worker
                raise

        coordinator = ProbabilityResearchRefreshCoordinator(refresh, _quiet_report)
        request = coordinator.start()
        request()
        assert await asyncio.to_thread(entered.wait, 1)
        stopping = asyncio.create_task(coordinator.stop())
        await cancelled.wait()
        for _ in range(3):
            stopping.cancel()
            await asyncio.sleep(0)
        assert not stopping.done() and coordinator.pending and not request()
        with pytest.raises(RuntimeError, match="still stopping"):
            coordinator.start()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert completed.is_set() and not coordinator.pending
    asyncio.run(scenario())


def test_cold_projection_is_fast_and_never_reads_an_archive(tmp_path, monkeypatch):
    source_support._source_file(tmp_path, 71, "a")
    store = research.MarketScanProbabilitySourceResearchStore(tmp_path)
    requests = []
    store.set_refresh_request(lambda: requests.append(True) or True)
    monkeypatch.setattr(research, "load_probability_source_snapshot", lambda *_: pytest.fail("query attempted deep verification"))
    monkeypatch.setattr(store, "preload", lambda **_: pytest.fail("query attempted preload"))
    started = time.monotonic()
    result = store.research_projection(71)
    assert time.monotonic() - started < 0.5
    assert result["availability"] == "source_index_verification_pending" and requests == [True]


@pytest.mark.parametrize("change", ["file", "date", "binding"])
def test_changed_snapshot_hides_previous_projection_until_full_refresh(tmp_path, monkeypatch, change):
    source = source_support._source_file(tmp_path, 71, "a")
    artifact = source_support._artifact(71, "2026-08-11", 10, captured_at="2026-08-11T16:05:00+08:00")
    monkeypatch.setattr(research, "load_probability_source_snapshot", lambda _path: artifact)
    store = research.MarketScanProbabilitySourceResearchStore(tmp_path)
    store.preload()
    assert store.research_projection(71)["status"] == "insufficient_data"
    previous = deepcopy(store._research_by_run)
    if change == "file":
        source.write_bytes(source.read_bytes() + b"changed")
    elif change == "date":
        monkeypatch.setattr(research, "latest_expected_daily_kline_date", lambda: date(2026, 9, 24))
    else:
        store._archive_bindings = {71: "b" * 64}
    monkeypatch.setattr(research, "load_probability_source_snapshot", lambda *_: pytest.fail("query reread archive"))
    assert store.research_projection(71)["availability"] == "source_index_verification_pending"
    assert store._research_by_run == previous


def test_manager_coalesces_queries_with_warmup_and_reloads_persistent_bindings(tmp_path, monkeypatch):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))
        source = scanner._research_stores.probability_source
        entered, release = Event(), Event()
        seen, active = [], 0
        bindings = {}
        preload = source.preload

        def bound_preload(*, archive_bindings, cancel_event):
            nonlocal active
            active += 1
            assert active == 1
            seen.append(dict(archive_bindings))
            entered.set()
            assert release.wait(2)
            try:
                return preload(archive_bindings=archive_bindings)
            finally:
                active -= 1

        async def activate():
            return None

        monkeypatch.setattr(source, "preload_isolated", bound_preload)
        monkeypatch.setattr(scanner.cache, "probability_source_capture_archive_bindings", lambda: dict(bindings))
        monkeypatch.setattr(scanner._probability_runtime, "_activate_capture", activate)
        await scanner.start()
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(20):
            assert source.research_projection(71)["availability"] == "source_index_verification_pending"
        bindings[71] = "a" * 64
        assert scanner.probability_research_refresh_pending
        release.set()
        await _wait_idle(scanner._probability_runtime._refresh_coordinator)
        assert seen == [{}, {71: "a" * 64}]
        await scanner.stop()
        assert not scanner.probability_research_refresh_pending
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["delete", "digest"])
def test_completed_index_distinguishes_missing_capture_from_pending_binding(tmp_path, monkeypatch, change):
    digest = f"{71:064x}"
    source = source_support._source_file(tmp_path, 71, digest)
    artifact = source_support._artifact(71, "2026-08-11", 10, captured_at="2026-08-11T16:05:00+08:00")
    monkeypatch.setattr(research, "load_probability_source_snapshot", lambda _path: artifact)
    store = research.MarketScanProbabilitySourceResearchStore(tmp_path)
    store.preload(archive_bindings={71: digest})
    previous = store.research_projection(71)
    assert store.has_current_archive_binding(71, digest)
    if change == "delete":
        source.unlink()
    else:
        digest = "b" * 64
    assert not store.has_current_archive_binding(71, digest)
    store.preload(archive_bindings={71: digest})
    assert store.has_current_archive_binding(71, digest)
    assert not store.has_current_archive_binding(71, digest, previous)
    assert store.research_projection(71)["status"] == "not_generated"
    assert store.research_projection(71).get("availability") != "source_index_verification_pending"


def test_coordinator_shutdown_drains_manager_failure_log_worker(tmp_path, monkeypatch):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))
        entered, release, finished = Event(), Event(), Event()

        def record(*_args):
            entered.set()
            assert release.wait(2)
            finished.set()

        async def refresh():
            raise ValueError("invalid archive")

        monkeypatch.setattr(scanner.cache, "save_monitor_event", record)
        coordinator = ProbabilityResearchRefreshCoordinator(refresh, scanner._probability_runtime._report_failure)
        request = coordinator.start()
        request()
        assert await asyncio.to_thread(entered.wait, 1)
        stopping = asyncio.create_task(coordinator.stop())
        await asyncio.sleep(0.01)
        assert not stopping.done() and not finished.is_set() and not request()
        release.set()
        await stopping
        assert finished.is_set() and not coordinator.pending
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["reconcile", "audit", "legacy_audit"])
def test_coordinator_shutdown_drains_capture_activation_writes(tmp_path, monkeypatch, phase):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path), research_stores=(
            MarketScanResearchStores(probability=None, probability_source=None, future_range=None)
            if phase == "legacy_audit" else None
        ))
        await scanner._lifecycle.start()
        if scanner._research_stores.probability_source is not None:
            scanner._research_stores.probability_source.preload(archive_bindings={})
        entered, release, finished = Event(), Event(), Event()
        calls = []

        def write(name):
            calls.append(name)
            if name == phase:
                entered.set()
                assert release.wait(2)
                finished.set()
            return 0

        monkeypatch.setattr(scanner.cache, "reconcile_probability_source_capture_outbox", lambda: write("reconcile"))
        monkeypatch.setattr(scanner.cache, "audit_probability_source_capture_archives", lambda _archives: write("audit"))
        if phase == "legacy_audit":
            monkeypatch.setattr(runtime_module, "audit_market_scan_probability_source_archives", lambda _cache: write("legacy_audit"))
        monkeypatch.setattr(scanner._probability_runtime, "_start_capture", lambda: calls.append("capture"))
        coordinator = ProbabilityResearchRefreshCoordinator(scanner._probability_runtime._activate_capture, _quiet_report)
        coordinator.start()()
        assert await asyncio.to_thread(entered.wait, 1)
        stopping = asyncio.create_task(coordinator.stop())
        await asyncio.sleep(0.01)
        assert not stopping.done() and not finished.is_set()
        release.set()
        await stopping
        assert finished.is_set() and not coordinator.pending
        assert calls == (["reconcile"] if phase == "reconcile" else ["reconcile", phase])
        assert not scanner._probability_runtime._archives_audited
        await scanner.stop()
    asyncio.run(scenario())


def test_coordinator_shutdown_drains_persistent_binding_read_before_preload(tmp_path, monkeypatch):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))
        entered, release, finished = Event(), Event(), Event()

        def bindings():
            entered.set()
            assert release.wait(2)
            finished.set()
            return {}

        monkeypatch.setattr(scanner.cache, "probability_source_capture_archive_bindings", bindings)
        monkeypatch.setattr(scanner._probability_runtime, "_start_source_preload", lambda *_args, **_kwargs: pytest.fail("preload after cancellation"))
        coordinator = ProbabilityResearchRefreshCoordinator(scanner._probability_runtime._resume, _quiet_report)
        coordinator.start()()
        assert await asyncio.to_thread(entered.wait, 1)
        stopping = asyncio.create_task(coordinator.stop())
        await asyncio.sleep(0.01)
        assert not stopping.done() and not finished.is_set()
        release.set()
        await stopping
        assert finished.is_set() and not coordinator.pending
        assert not scanner._probability_runtime._preload_lock.locked()
        assert not scanner._research_stores.probability_source.refresh_pending()
    asyncio.run(scenario())


def _write_superseded_historical_context(directory, monkeypatch):
    directory.mkdir(parents=True, exist_ok=True)
    source = historical_support._source_path(directory)
    source.write_bytes(b"{}")
    monkeypatch.setattr(historical_context, "verify_historical_replay_artifact", lambda _: historical_support._verified_replay())
    artifact = historical_context.build_historical_probability_context(source)
    artifact["payload"]["source_artifact"]["schema_version"] = HISTORICAL_REPLAY_SUPERSEDED_ARTIFACT_SCHEMA_VERSION
    digest = historical_context.sha256_hex(historical_context.canonical_json_bytes({
        key: value for key, value in artifact.items() if key != "integrity"
    }))
    artifact["integrity"]["integrity_digest"] = digest
    target = directory / f"{historical_context.HISTORICAL_CONTEXT_PREFIX}-{digest}.json"
    target.write_text(json.dumps(artifact), encoding="utf-8")
    return target


@pytest.mark.parametrize("entrypoint", ["_warm", "_resume"])
def test_rejected_historical_reference_does_not_block_live_capture(tmp_path, monkeypatch, entrypoint):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))
        historical = scanner._research_stores.historical_probability
        target = _write_superseded_historical_context(historical.directory, monkeypatch)
        original = target.read_bytes()
        calls, reports = [], []
        source = scanner._research_stores.probability_source
        digest = f"{71:064x}"
        source.directory.mkdir(parents=True, exist_ok=True)
        source_support._source_file(source.directory, 71, digest)
        artifact = source_support._artifact(71, "2026-08-11", 10, captured_at="2026-08-11T16:05:00+08:00")
        monkeypatch.setattr(research, "load_probability_source_snapshot", lambda _path: artifact)
        monkeypatch.setattr(source, "preload_isolated", lambda *, archive_bindings, cancel_event: source.preload(archive_bindings=archive_bindings))
        monkeypatch.setattr(scanner.cache, "probability_source_capture_archive_bindings", lambda: {71: digest})
        monkeypatch.setattr(scanner._probability_runtime, "_owns_instance_guard", lambda: True)
        monkeypatch.setattr(scanner._probability_runtime, "_start_capture", lambda: calls.append("capture"))
        monkeypatch.setattr(scanner._probability_runtime, "maintain_joint_execution", _quiet_joint)
        monkeypatch.setattr(scanner.cache, "save_monitor_event", lambda *args: reports.append(args))

        await getattr(scanner._probability_runtime, entrypoint)()
        assert historical.research_projection()["availability"] == "historical_context_integrity_unavailable"
        assert source.research_projection(71)["status"] == "insufficient_data"
        assert source.has_current_archive_binding(71, digest)
        assert scanner._probability_runtime._archives_audited and calls == ["capture"]
        assert len(reports) == 1 and "superseded-fit-contract" in reports[0][2]
        assert "历史概率参考不可用，当前来源归档继续" in reports[0][2]
        monkeypatch.setattr(historical_context, "_load_newest_projection", lambda *_: pytest.fail("unchanged rejection replayed"))
        await getattr(scanner._probability_runtime, entrypoint)()
        assert len(reports) == 1 and calls == ["capture", "capture"]
        assert target.read_bytes() == original
    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [ValueError, historical_context.HistoricalProbabilityContextError])
def test_historical_failure_isolation_does_not_swallow_source_errors(tmp_path, monkeypatch, error_type):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))

        def fail_source(**_kwargs):
            raise error_type("source remains rejected")

        monkeypatch.setattr(scanner._research_stores.probability_source, "preload_isolated", fail_source)
        monkeypatch.setattr(scanner._probability_runtime, "_activate_capture", lambda: pytest.fail("source failure activated capture"))
        with pytest.raises(error_type, match="source remains rejected"):
            await scanner._probability_runtime._resume()
        assert not scanner.probability_research_refresh_pending
    asyncio.run(scenario())


def test_simultaneous_source_and_historical_failures_preserve_source_gate(tmp_path, monkeypatch):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))
        historical_failed = Event()

        def fail_source(**_kwargs):
            assert historical_failed.wait(2)
            raise ValueError("live source integrity rejected")

        def fail_history():
            historical_failed.set()
            raise historical_context.HistoricalProbabilityContextError("optional historical read failed first")

        monkeypatch.setattr(scanner._research_stores.probability_source, "preload_isolated", fail_source)
        monkeypatch.setattr(scanner._research_stores.historical_probability, "preload", fail_history)
        monkeypatch.setattr(scanner._probability_runtime, "_activate_capture", lambda: pytest.fail("simultaneous failure activated capture"))
        with pytest.raises(ValueError, match="live source integrity rejected"):
            await scanner._probability_runtime._resume()
        assert historical_failed.is_set() and not scanner.probability_research_refresh_pending
        assert not scanner._research_stores.historical_probability.refresh_pending()
    asyncio.run(scenario())


@pytest.mark.parametrize("blocked_phase", ["historical_read", "historical_report"])
def test_shutdown_drains_isolated_historical_failure_before_capture(tmp_path, monkeypatch, blocked_phase):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))
        entered, release, finished = Event(), Event(), Event()

        def block(phase):
            if phase == blocked_phase:
                entered.set()
                assert release.wait(2)
                finished.set()

        def historical_preload():
            block("historical_read")
            raise historical_context.HistoricalProbabilityContextRejectedError("historical reference rejected")

        monkeypatch.setattr(scanner._research_stores.historical_probability, "preload", historical_preload)
        monkeypatch.setattr(scanner.cache, "save_monitor_event", lambda *_: block("historical_report"))
        monkeypatch.setattr(scanner._probability_runtime, "_activate_capture", lambda: pytest.fail("cancelled refresh activated capture"))
        coordinator = ProbabilityResearchRefreshCoordinator(scanner._probability_runtime._resume, _quiet_report)
        coordinator.start()()
        assert await asyncio.to_thread(entered.wait, 1)
        stopping = asyncio.create_task(coordinator.stop())
        await asyncio.sleep(0.01)
        assert not stopping.done() and not finished.is_set() and coordinator.pending
        release.set()
        await stopping
        assert finished.is_set() and not coordinator.pending
        assert not scanner._probability_runtime._preload_lock.locked()
        assert not scanner._research_stores.historical_probability.refresh_pending()
    asyncio.run(scenario())


def test_transient_historical_failure_retries_without_blocking_live_capture(tmp_path, monkeypatch):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))
        historical = scanner._research_stores.historical_probability
        preload = historical.preload
        attempts, calls, reports = [], [], []

        def flaky_preload():
            attempts.append(True)
            if len(attempts) == 1:
                raise historical_context.HistoricalProbabilityContextError("temporary historical read failed")
            assert "capture" in calls, "live activation must precede the optional reference retry"
            return preload()

        monkeypatch.setattr(historical, "preload", flaky_preload)
        monkeypatch.setattr(scanner._probability_runtime, "_owns_instance_guard", lambda: True)
        monkeypatch.setattr(scanner._probability_runtime, "_start_capture", lambda: calls.append("capture"))
        monkeypatch.setattr(scanner.cache, "save_monitor_event", lambda *args: reports.append(args))
        coordinator = ProbabilityResearchRefreshCoordinator(
            scanner._probability_runtime._resume, scanner._probability_runtime._report_failure, retry_seconds=0.001,
        )
        assert coordinator.start()()
        await _wait_idle(coordinator)
        assert len(attempts) == 2 and len(reports) == 1
        assert coordinator.status["attempt_count"] == 2 and coordinator.status["failure_count"] == 0
        assert calls == ["capture", "capture"] and scanner._probability_runtime._archives_audited
        assert historical.research_projection()["availability"] == "historical_context_not_generated"
        await coordinator.stop()
    asyncio.run(scenario())
