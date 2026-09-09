from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.errors import MarketScanHeavyReadBusy
from app.api.market_scan_read_admission import MarketScanHeavyReadAdmission, run_admitted_market_scan_read
from app.main import _shutdown_container


class _BlockingRead:
    def __init__(self, events: list[str], *, fail: bool) -> None:
        self.events = events
        self.fail = fail
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> str:
        self.events.append("worker-start")
        self.started.set()
        assert self.release.wait(timeout=5)
        self.events.append("worker-finish")
        if self.fail:
            raise ValueError("read failed")
        return "read result"


async def _assert_shutdown_owns_read(shutdown, admission, events, cancellations, *, initial_turns=1):
    for _ in range(initial_turns):
        await asyncio.sleep(0)
    for _ in range(cancellations):
        shutdown.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not shutdown.done()
    assert admission.active_count == admission.worker_count == 1
    assert events == ["worker-start"]
    with pytest.raises(MarketScanHeavyReadBusy):
        await run_admitted_market_scan_read(admission, lambda: pytest.fail("closed admission started work"))


async def _finish_read_and_shutdown(read, caller, shutdown, admission, cancellations):
    read.release.set()
    if cancellations:
        with pytest.raises(asyncio.CancelledError):
            await shutdown
    else:
        await shutdown
    if read.fail:
        with pytest.raises(HTTPException, match="read failed"):
            await caller
    else:
        assert await caller == "read result"
    assert admission.active_count == admission.worker_count == 0
    with pytest.raises(MarketScanHeavyReadBusy):
        await run_admitted_market_scan_read(admission, lambda: pytest.fail("closed admission reopened"))


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("cancellations", [0, 3])
def test_read_admission_drains_owned_worker_before_propagating_cancellation(fail, cancellations):
    async def check():
        events: list[str] = []
        admission = MarketScanHeavyReadAdmission()
        read = _BlockingRead(events, fail=fail)
        caller = asyncio.create_task(run_admitted_market_scan_read(admission, read))
        shutdown = None
        try:
            assert await asyncio.to_thread(read.started.wait, 1)
            shutdown = asyncio.create_task(admission.aclose())
            await _assert_shutdown_owns_read(shutdown, admission, events, cancellations)
            await _finish_read_and_shutdown(read, caller, shutdown, admission, cancellations)
            assert events == ["worker-start", "worker-finish"]
            await admission.aclose()
        finally:
            read.release.set()
            await asyncio.gather(*(task for task in (caller, shutdown) if task is not None), return_exceptions=True)
            await admission.aclose()

    asyncio.run(check())


def _shutdown_container_stub(events):
    async def stop_runtime():
        events.append("runtime")

    async def close_workbench():
        events.append("workbench")

    async def close_datahub():
        events.append("datahub")

    return SimpleNamespace(
        market_scan_heavy_read_admission=MarketScanHeavyReadAdmission(),
        market_scan_experimental_read_admission=MarketScanHeavyReadAdmission(),
        runtime_coordinator=None,
        scheduler=SimpleNamespace(stop=stop_runtime),
        market_scanner=None,
        workbench_contexts=SimpleNamespace(aclose=close_workbench),
        datahub=SimpleNamespace(aclose=close_datahub),
    )


@pytest.mark.parametrize("admission_name", ["market_scan_heavy_read_admission", "market_scan_experimental_read_admission"])
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("cancellations", [0, 3])
@pytest.mark.parametrize("initial_turns", [1, 5])
def test_application_shutdown_keeps_resources_open_until_read_drain_finishes(admission_name, fail, cancellations, initial_turns):
    async def check():
        events: list[str] = []
        container = _shutdown_container_stub(events)
        admission = getattr(container, admission_name)
        read = _BlockingRead(events, fail=fail)
        caller = asyncio.create_task(run_admitted_market_scan_read(admission, read))
        shutdown = None
        try:
            assert await asyncio.to_thread(read.started.wait, 1)
            shutdown = asyncio.create_task(_shutdown_container(container))
            await _assert_shutdown_owns_read(shutdown, admission, events, cancellations, initial_turns=initial_turns)
            await _finish_read_and_shutdown(read, caller, shutdown, admission, cancellations)
            assert events == ["worker-start", "worker-finish", "runtime", "workbench", "datahub"]
        finally:
            read.release.set()
            await asyncio.gather(*(task for task in (caller, shutdown) if task is not None), return_exceptions=True)
            await admission.aclose()

    asyncio.run(check())


def test_drain_finishes_when_completed_worker_callback_is_still_queued():
    script = """
import asyncio
import app.config_settings as config
config._SHELL_ENV_VALUES = {}
from app.api.market_scan_read_admission import MarketScanHeavyReadAdmission

async def check(outcome):
    admission = MarketScanHeavyReadAdmission()
    async def complete():
        if outcome == 'failure':
            raise ValueError('failed verifier')
        return 'verified'
    worker = asyncio.create_task(complete())
    if outcome == 'cancelled':
        worker.cancel()
    admission._workers.add(worker)
    worker.add_done_callback(admission._finish_worker)
    await asyncio.sleep(0)
    assert worker.done() and admission.worker_count == 1
    await admission._drain()
    assert admission.worker_count == 0
    await asyncio.sleep(0)
    assert admission.worker_count == 0
    await asyncio.gather(admission.aclose(), admission.aclose())
    await admission.aclose()

async def main():
    for outcome in ('success', 'failure', 'cancelled'):
        await check(outcome)

asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr
