"""Integration contracts for the single owner of probability background work."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace

import pytest

from app.services.market_scan_research_stores import MarketScanResearchStores
from tests.market_scan_test_support import _MarketScanHub, _scanner


def test_manager_query_and_runtime_share_one_immutable_research_container(tmp_path):
    hub = _MarketScanHub(tmp_path)
    stores = MarketScanResearchStores.for_cache_path(hub.cache.path)
    scanner = _scanner(hub, research_stores=stores)
    assert scanner._research_stores is stores
    assert scanner._query_service._stores is stores
    assert scanner._probability_runtime._stores is stores
    with pytest.raises(FrozenInstanceError):
        stores.probability_source = None
    assert scanner.probability_research_refresh_status == scanner._probability_runtime.status
    assert not scanner.probability_research_refresh_pending


def test_joint_maintenance_retains_default_clock_microseconds_and_timezone(tmp_path):
    now = datetime(2026, 9, 23, 2, 13, 7, 456789, tzinfo=timezone.utc)
    observed = []
    hub = _MarketScanHub(tmp_path)
    stores = replace(MarketScanResearchStores.for_cache_path(hub.cache.path), joint_probability=SimpleNamespace(
        run=lambda *, now: observed.append(now),
    ))
    scanner = _scanner(hub, now=now, research_stores=stores)
    asyncio.run(scanner.maintain_joint_execution_probability())
    assert observed == [now]
    assert observed[0].hour == 10
    assert observed[0].microsecond == 456789


def test_manager_shutdown_keeps_research_phases_before_guard_release(tmp_path, monkeypatch):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))
        await scanner._lifecycle.start()
        calls = []

        def stage(name):
            async def execute():
                calls.append(name)
            return execute

        finish = scanner._lifecycle.finish_stop

        async def finish_stop():
            calls.append("release_guard")
            await finish()

        monkeypatch.setattr(scanner._probability_runtime, "stop_refresh", stage("stop_refresh"))
        monkeypatch.setattr(scanner._probability_runtime, "drain_capture", stage("drain_capture"))
        monkeypatch.setattr(scanner._probability_runtime, "stop_capture", stage("stop_capture"))
        monkeypatch.setattr(scanner._lifecycle, "finish_stop", finish_stop)
        await scanner.stop()
        assert calls == ["stop_refresh", "drain_capture", "stop_capture", "release_guard"]
        assert scanner.is_quiescent
    asyncio.run(scenario())


def test_stopped_runtime_revokes_both_store_callbacks_and_old_generations(tmp_path):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))
        runtime = scanner._probability_runtime
        source, history = scanner._research_stores.probability_source, scanner._research_stores.historical_probability
        runtime._bind_refresh_requests()
        previous = (source._refresh_request, history._refresh_request)
        await runtime.stop_refresh()
        assert source._refresh_request is None and history._refresh_request is None
        assert all(callback() is False for callback in previous)
        runtime._bind_refresh_requests()
        assert all(callback() is False for callback in previous)
        await runtime.stop_refresh()
        assert not runtime.pending and runtime.status["state"] == "stopped"
    asyncio.run(scenario())


def test_capture_diagnostic_write_blocks_concurrent_repeatedly_cancelled_shutdown(tmp_path, monkeypatch):
    async def scenario():
        scanner = _scanner(_MarketScanHub(tmp_path))
        await scanner._lifecycle.start()
        runtime = scanner._probability_runtime
        entered, release, finished = Event(), Event(), Event()
        attempts = []

        async def drain():
            attempts.append(True)
            if len(attempts) == 1:
                raise ValueError("capture unavailable")
            return {"captured": 0, "skipped": 0, "failed": 0}

        def write(*_args):
            entered.set()
            assert release.wait(3)
            finished.set()

        monkeypatch.setattr(runtime, "drain_capture", drain)
        monkeypatch.setattr(scanner.cache, "save_monitor_event", write)
        runtime._start_capture()
        assert await asyncio.to_thread(entered.wait, 1)
        captured_task = runtime._capture_task
        first = asyncio.create_task(scanner.stop())
        second = asyncio.create_task(runtime.stop_capture())
        try:
            for _ in range(3):
                await asyncio.sleep(0.01)
                first.cancel()
                second.cancel()
                assert not first.done() and not second.done() and not finished.is_set()
                assert runtime._capture_task is captured_task and not scanner.is_quiescent
        finally:
            release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 2)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert finished.is_set() and runtime._capture_task is None
        assert scanner.is_quiescent and not scanner._lifecycle.has_instance_guard
    asyncio.run(scenario())
