from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import fcntl
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.instance_guard import FileInstanceGuard
from app.services.market_scan_manager import MarketScanManager
from app.services.runtime_coordinator import RuntimeCoordinator, RuntimeLeadership
from app.services.scheduler import LocalDataScheduler


class _GuardHandle:
    def __init__(self, *, fail_write: bool = False) -> None:
        self.closed = False
        self.fail_write = fail_write

    def fileno(self) -> int:
        return 123

    def seek(self, _offset: int) -> None:
        return None

    def truncate(self) -> None:
        return None

    def write(self, _value: str) -> int:
        if self.fail_write:
            raise OSError("pid write failed")
        return 1

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def test_file_instance_guard_closes_handle_when_locking_fails(
    tmp_path,
    monkeypatch,
) -> None:
    handle = _GuardHandle()
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: handle)

    def fail_lock(_fd: int, _operation: int) -> None:
        raise OSError("lock failed")

    monkeypatch.setattr(fcntl, "flock", fail_lock)

    with pytest.raises(OSError, match="lock failed"):
        FileInstanceGuard(tmp_path / "runtime.lock").acquire()

    assert handle.closed is True


def test_file_instance_guard_preserves_write_error_when_cleanup_unlock_fails(
    tmp_path,
    monkeypatch,
) -> None:
    handle = _GuardHandle(fail_write=True)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: handle)
    calls = 0

    def fail_unlock(_fd: int, _operation: int) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("unlock failed")

    monkeypatch.setattr(fcntl, "flock", fail_unlock)

    with pytest.raises(OSError, match="pid write failed"):
        FileInstanceGuard(tmp_path / "runtime.lock").acquire()

    assert calls == 2
    assert handle.closed is True


class _GuardedService:
    def __init__(self, guard, *, datahub=None) -> None:
        self.guard = guard
        self.datahub = datahub
        self.running = False
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self):
        self.start_calls += 1
        if not self.guard.acquire():
            return False
        self.running = True
        return True

    async def stop(self):
        if not self.running:
            return False
        self.running = False
        self.stop_calls += 1
        self.guard.release()
        return True


class _RuntimeCache:
    def __init__(self) -> None:
        self.monitor_events: list[tuple[str, str, str]] = []

    def reconcile_orphaned_task_runs(self) -> int:
        return 0

    def reconcile_incomplete_market_scans(self) -> int:
        return 0

    def save_monitor_event(self, *args, **_kwargs) -> None:
        self.monitor_events.append((str(args[0]), str(args[1]), str(args[2])))

    def market_scan_run(self, _run_id: int):
        return SimpleNamespace(status="success")


class _FailOnceScheduler(_GuardedService):
    def __init__(self, guard, cache: _RuntimeCache) -> None:
        super().__init__(guard, datahub=SimpleNamespace(cache=cache))
        self.failed_once = asyncio.Event()
        self.standby = False

    @property
    def is_quiescent(self) -> bool:
        return not self.running

    async def wait_until_quiescent(self) -> None:
        return None

    def set_runtime_standby(self, standby: bool) -> None:
        self.standby = standby

    async def start(self):
        self.start_calls += 1
        if not self.guard.acquire():
            return False
        if not self.failed_once.is_set():
            self.failed_once.set()
            raise RuntimeError("注入的 scheduler 首次激活失败")
        self.running = True
        return True


class _TrackingGuard:
    def __init__(self) -> None:
        self.acquired = False

    def acquire(self) -> bool:
        self.acquired = True
        return True

    def release(self) -> None:
        self.acquired = False


class _CrossProcessStubbornScheduler:
    def __init__(self, guard) -> None:
        self.guard = guard
        self.release_worker = asyncio.Event()
        self._quiescent = asyncio.Event()
        self._quiescent.set()
        self._worker: asyncio.Task[None] | None = None

    @property
    def is_quiescent(self) -> bool:
        return self._quiescent.is_set()

    async def wait_until_quiescent(self) -> None:
        await self._quiescent.wait()

    def set_runtime_standby(self, _standby: bool) -> None:
        return None

    async def start(self) -> bool:
        if not self.guard.acquire():
            return False
        self._quiescent.clear()
        self._worker = asyncio.create_task(self._stubborn_worker())
        self._worker.add_done_callback(self._worker_done)
        return True

    async def stop(self) -> bool:
        worker = self._worker
        if worker is None:
            return False
        worker.cancel()
        await asyncio.wait((worker,), timeout=0.01)
        return True

    async def _stubborn_worker(self) -> None:
        try:
            await self.release_worker.wait()
        except asyncio.CancelledError:
            await self.release_worker.wait()

    def _worker_done(self, worker: asyncio.Task[None]) -> None:
        if not worker.cancelled():
            worker.exception()
        self._worker = None
        self._quiescent.set()


def test_runtime_leadership_never_splits_scheduler_and_scanner_ownership(tmp_path: Path) -> None:
    first = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
    second = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")

    for _attempt in range(100):
        assert first.try_acquire() is True
        assert second.try_acquire() is False
        assert first.service_guard().acquire() is True
        assert second.service_guard().acquire() is False
        first.release()
        assert second.try_acquire() is True
        assert first.try_acquire() is False
        second.release()


def test_runtime_leadership_is_exclusive_across_two_processes_for_repeated_takeovers(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    lock_path = str(tmp_path / "runtime-leader.lock")
    first_commands = context.Queue()
    second_commands = context.Queue()
    first_results = context.Queue()
    second_results = context.Queue()
    first = context.Process(target=_guard_worker, args=(lock_path, first_commands, first_results))
    second = context.Process(target=_guard_worker, args=(lock_path, second_commands, second_results))
    first.start()
    second.start()
    try:
        for _attempt in range(100):
            assert _guard_command(first_commands, first_results, "acquire") is True
            assert _guard_command(second_commands, second_results, "acquire") is False
            assert _guard_command(first_commands, first_results, "release") is True
            assert _guard_command(second_commands, second_results, "acquire") is True
            assert _guard_command(first_commands, first_results, "acquire") is False
            assert _guard_command(second_commands, second_results, "release") is True
    finally:
        first_commands.put("stop")
        second_commands.put("stop")
        first.join(timeout=5)
        second.join(timeout=5)
        if first.is_alive():
            first.terminate()
        if second.is_alive():
            second.terminate()

    assert first.exitcode == 0
    assert second.exitcode == 0


def test_cross_process_takeover_waits_for_old_non_cooperative_task(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    cache_path = str(tmp_path / "cache.sqlite3")
    commands = context.Queue()
    results = context.Queue()
    owner = context.Process(
        target=_stubborn_coordinator_worker,
        args=(cache_path, commands, results),
    )
    standby = RuntimeLeadership.for_cache_path(Path(cache_path))
    owner.start()
    try:
        assert results.get(timeout=5) == "started"
        commands.put("stop")
        assert results.get(timeout=5) == "stop-returned"
        assert standby.try_acquire() is False

        commands.put("release")
        assert results.get(timeout=5) == "released"
        assert standby.try_acquire() is True
        standby.release()
    finally:
        commands.put("release")
        owner.join(timeout=5)
        if owner.is_alive():
            owner.terminate()
            owner.join(timeout=5)

    assert owner.exitcode == 0


def test_runtime_coordinator_standby_takes_over_both_services(tmp_path: Path) -> None:
    async def scenario():
        cache = SimpleNamespace(save_monitor_event=lambda *_args, **_kwargs: None)
        first_leadership = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        second_leadership = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        first_scanner = _GuardedService(first_leadership.service_guard())
        first_scheduler = _GuardedService(first_leadership.service_guard(), datahub=SimpleNamespace(cache=cache))
        second_scanner = _GuardedService(second_leadership.service_guard())
        second_scheduler = _GuardedService(second_leadership.service_guard(), datahub=SimpleNamespace(cache=cache))
        first = RuntimeCoordinator(
            first_leadership,
            first_scheduler,
            first_scanner,
            takeover_poll_seconds=0.05,
        )
        second = RuntimeCoordinator(
            second_leadership,
            second_scheduler,
            second_scanner,
            takeover_poll_seconds=0.05,
        )

        assert await first.start() is True
        assert await second.start() is False
        assert first_scheduler.running is first_scanner.running is True
        assert second_scheduler.running is second_scanner.running is False

        await first.stop()
        await _wait_until(lambda: second_scheduler.running and second_scanner.running)
        takeover_state = (
            second_leadership.is_leader,
            second_scheduler.running,
            second_scanner.running,
        )
        await second.stop()
        return takeover_state, first_scheduler, first_scanner, second_scheduler, second_scanner

    takeover, first_scheduler, first_scanner, second_scheduler, second_scanner = asyncio.run(scenario())

    assert takeover == (True, True, True)
    assert first_scheduler.stop_calls == first_scanner.stop_calls == 1
    assert second_scheduler.start_calls == second_scanner.start_calls == 1
    assert second_scheduler.stop_calls == second_scanner.stop_calls == 1


def test_runtime_leader_lock_survives_bounded_stop_until_stubborn_scheduler_exits(tmp_path: Path) -> None:
    async def scenario():
        first_leadership = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        second_leadership = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        first_scheduler = _runtime_scheduler(first_leadership)
        second_scheduler = _runtime_scheduler(second_leadership)
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def stubborn_loop() -> None:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

        first_scheduler._loop = stubborn_loop  # type: ignore[method-assign]  # noqa: SLF001
        first = RuntimeCoordinator(first_leadership, first_scheduler, None, takeover_poll_seconds=0.05)
        second = RuntimeCoordinator(second_leadership, second_scheduler, None, takeover_poll_seconds=0.05)

        assert await first.start() is True
        await started.wait()
        assert await second.start() is False
        assert second_scheduler.status().standby is True

        loop = asyncio.get_running_loop()
        fallback_release = loop.call_later(1.0, release.set)
        began = loop.time()
        await first.stop()
        stop_elapsed = loop.time() - began
        await asyncio.sleep(0.15)
        while_stubborn = (
            first_leadership.is_leader,
            second_leadership.is_leader,
            second_scheduler.status().standby,
        )

        release.set()
        fallback_release.cancel()
        await _wait_until(lambda: second_leadership.is_leader and second_scheduler.status().running)
        after_release = (
            first_leadership.is_leader,
            second_leadership.is_leader,
            second_scheduler.status().running,
        )
        await second.stop()
        return stop_elapsed, cancelled.is_set(), while_stubborn, after_release

    elapsed, cancelled, while_stubborn, after_release = asyncio.run(scenario())

    assert elapsed < 0.25
    assert cancelled is True
    assert while_stubborn == (True, False, True)
    assert after_release == (False, True, True)


def test_standby_retries_after_scheduler_activation_failure_without_closing_scanner(tmp_path: Path) -> None:
    async def scenario():
        cache = _RuntimeCache()
        holder = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        standby = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        assert holder.try_acquire() is True
        scheduler = _FailOnceScheduler(standby.service_guard(), cache)
        settings = SimpleNamespace(scheduler_shutdown_timeout_seconds=0.02)
        scanner = MarketScanManager(
            SimpleNamespace(cache=cache, settings=settings),  # type: ignore[arg-type]
            instance_guard=standby.service_guard(),
        )
        coordinator = RuntimeCoordinator(standby, scheduler, scanner, takeover_poll_seconds=0.05)

        assert await coordinator.start() is False
        assert scheduler.standby is True
        holder.release()
        await asyncio.wait_for(scheduler.failed_once.wait(), timeout=1)
        await _wait_until(lambda: scheduler.running)
        state = (
            scheduler.start_calls,
            scheduler.standby,
            scanner._lifecycle.closed,  # noqa: SLF001
            scanner._lifecycle.is_quiescent,  # noqa: SLF001
            standby.is_leader,
        )
        await coordinator.stop()
        return state, cache.monitor_events, scanner._lifecycle.closed  # noqa: SLF001

    active_state, events, closed_after_final_stop = asyncio.run(scenario())

    assert active_state == (2, False, False, False, True)
    assert any(category == "runtime-leadership" for _level, category, _message in events)
    assert closed_after_final_stop is True


def test_market_scanner_stop_is_bounded_but_guard_waits_for_stubborn_task() -> None:
    async def scenario():
        cache = _RuntimeCache()
        guard = _TrackingGuard()
        settings = SimpleNamespace(scheduler_shutdown_timeout_seconds=0.01)
        scanner = MarketScanManager(
            SimpleNamespace(cache=cache, settings=settings),  # type: ignore[arg-type]
            instance_guard=guard,
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def stubborn_executor(_run_id: int, _cancel_event: asyncio.Event) -> None:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        await scanner.start()
        scanner._lifecycle.launch(7, stubborn_executor)  # noqa: SLF001
        await started.wait()
        loop = asyncio.get_running_loop()
        fallback_release = loop.call_later(1.0, release.set)
        began = loop.time()
        await scanner.stop()
        elapsed = loop.time() - began
        while_stubborn = (scanner.is_quiescent, guard.acquired)
        release.set()
        fallback_release.cancel()
        await asyncio.wait_for(scanner.wait_until_quiescent(), timeout=1)
        return elapsed, while_stubborn, scanner.is_quiescent, guard.acquired

    elapsed, while_stubborn, quiescent_after_exit, guard_after_exit = asyncio.run(scenario())

    assert elapsed < 0.25
    assert while_stubborn == (False, True)
    assert quiescent_after_exit is True
    assert guard_after_exit is False


def test_service_guards_cannot_mutate_before_runtime_leadership_is_acquired(tmp_path: Path) -> None:
    leadership = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
    scanner_guard = leadership.service_guard()
    scheduler_guard = leadership.service_guard()

    assert scanner_guard.acquire() is False
    assert scheduler_guard.acquire() is False
    assert leadership.try_acquire() is True
    assert scanner_guard.acquire() is True
    assert scheduler_guard.acquire() is True
    leadership.release()
    assert scanner_guard.acquire() is False
    assert scheduler_guard.acquire() is False


def test_activation_failure_monitor_event_redacts_credentials(tmp_path: Path) -> None:
    events: list[tuple[str, str, str]] = []
    cache = SimpleNamespace(save_monitor_event=lambda *args: events.append(args))
    leadership = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
    scheduler = _GuardedService(leadership.service_guard(), datahub=SimpleNamespace(cache=cache))
    coordinator = RuntimeCoordinator(leadership, scheduler, _GuardedService(leadership.service_guard()))

    asyncio.run(
        coordinator._report_activation_failure(
            RuntimeError("api_key=plain-secret https://example.invalid/start?token=query-secret")
        )
    )

    assert len(events) == 1
    message = events[0][2]
    assert "plain-secret" not in message
    assert "query-secret" not in message
    assert message.count("<redacted>") == 2


def _runtime_scheduler(leadership: RuntimeLeadership) -> LocalDataScheduler:
    settings = SimpleNamespace(
        scheduler_enabled=True,
        scheduler_quote_interval_seconds=3600,
        scheduler_kline_interval_seconds=3600,
        scheduler_plate_interval_seconds=3600,
        scheduler_health_interval_seconds=3600,
        scheduler_shutdown_timeout_seconds=0.01,
    )
    hub = SimpleNamespace(settings=settings, cache=_RuntimeCache())
    scheduler = LocalDataScheduler(  # type: ignore[arg-type]
        hub,
        instance_guard=leadership.service_guard(),
    )
    future = datetime.now() + timedelta(days=1)
    for task in scheduler.tasks.values():
        task.next_run_at = future
    return scheduler


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("等待运行时领导权接管超时")
        await asyncio.sleep(0.01)


def _guard_worker(lock_path: str, commands, results) -> None:
    guard = FileInstanceGuard(Path(lock_path))
    while True:
        command = commands.get()
        if command == "stop":
            guard.release()
            return
        if command == "acquire":
            results.put(guard.acquire())
            continue
        if command == "release":
            guard.release()
            results.put(True)


def _guard_command(commands, results, command: str) -> bool:
    commands.put(command)
    return bool(results.get(timeout=5))


def _stubborn_coordinator_worker(cache_path: str, commands, results) -> None:
    async def scenario() -> None:
        leadership = RuntimeLeadership.for_cache_path(Path(cache_path))
        scheduler = _CrossProcessStubbornScheduler(leadership.service_guard())
        coordinator = RuntimeCoordinator(
            leadership,
            scheduler,
            None,
            takeover_poll_seconds=0.05,
        )
        if not await coordinator.start():
            raise RuntimeError("子进程未能获取初始领导权")
        results.put("started")
        if await asyncio.to_thread(commands.get) != "stop":
            raise RuntimeError("子进程未收到停止指令")
        await coordinator.stop()
        results.put("stop-returned")
        if await asyncio.to_thread(commands.get) != "release":
            raise RuntimeError("子进程未收到任务释放指令")
        scheduler.release_worker.set()
        await asyncio.wait_for(scheduler.wait_until_quiescent(), timeout=2)
        await _wait_until(lambda: not leadership.is_leader)
        results.put("released")

    asyncio.run(scenario())
