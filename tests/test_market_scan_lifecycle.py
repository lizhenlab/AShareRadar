from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

from app.services.instance_guard import FileInstanceGuard
from tests.market_scan_test_support import (
    SCAN_AS_OF,
    _BlockingTaskRunCache,
    _MarketScanHub,
    _configure_clean_full_market,
    _rule_version,
    _scanner,
    _wait_for_status,
    _wait_for_terminal,
)


def test_market_scan_deduplicates_active_start_and_can_cancel_then_resume(tmp_path: Path) -> None:
    async def scenario():
        gate = asyncio.Event()
        hub = _MarketScanHub(tmp_path, block_klines=gate)
        scanner = _scanner(hub)
        await scanner.start()
        first = await scanner.create_scan(as_of=SCAN_AS_OF)
        await _wait_for_status(scanner, first.run.id, {"running"})
        duplicate = await scanner.create_scan(as_of=SCAN_AS_OF)
        cancelled = await scanner.cancel_scan(first.run.id)
        gate.set()
        retried = await scanner.retry_scan(first.run.id)
        final = await _wait_for_terminal(scanner, retried.run.id)
        original = scanner.run(first.run.id)
        await scanner.stop()
        return first, duplicate, cancelled, retried, final, original

    first, duplicate, cancelled, retried, final, original = asyncio.run(scenario())

    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.deduplicated is True
    assert duplicate.run.id == first.run.id
    assert cancelled.status == "cancelled"
    assert retried.accepted is True
    assert retried.run.id != first.run.id
    assert retried.run.retry_of_run_id == first.run.id
    assert retried.run.retry_count == 1
    assert final.status == "failed"
    assert final.processed_count == final.total_count
    assert original.status == "cancelled"


def test_market_scan_start_does_not_wait_for_probability_runtime_warmup(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _MarketScanHub(tmp_path)
        scanner = _scanner(hub)
        entered = asyncio.Event()
        release = asyncio.Event()
        activated = False

        async def refresh() -> int:
            entered.set()
            await release.wait()
            return 0

        async def activate() -> None:
            nonlocal activated
            activated = True

        scanner.refresh_probability_research_cache = refresh  # type: ignore[method-assign]
        scanner._activate_probability_capture_leader = activate  # type: ignore[method-assign]  # noqa: SLF001

        assert await asyncio.wait_for(scanner.start(), timeout=0.5) == 0
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        assert activated is False
        assert scanner._probability_source_research_store.refresh_pending() is True  # noqa: SLF001
        assert scanner._historical_probability_store.refresh_pending() is True  # noqa: SLF001
        await asyncio.wait_for(scanner.stop(), timeout=0.5)
        assert scanner._probability_runtime_warmup_task is None  # noqa: SLF001
        assert scanner._probability_source_research_store.refresh_pending() is False  # noqa: SLF001
        assert scanner._historical_probability_store.refresh_pending() is False  # noqa: SLF001

    asyncio.run(scenario())


def test_joint_probability_maintenance_normalizes_legacy_naive_market_time(
    tmp_path: Path,
) -> None:
    async def scenario() -> datetime:
        scanner = _scanner(_MarketScanHub(tmp_path))
        observed: list[datetime] = []

        class Store:
            def run(self, *, now: datetime) -> object:
                observed.append(now)
                return object()

        scanner._joint_probability_store = Store()  # type: ignore[assignment]  # noqa: SLF001
        await scanner.maintain_joint_execution_probability(
            now=datetime(2026, 8, 23, 1, 30),
        )
        return observed[0]

    normalized = asyncio.run(scenario())

    assert normalized.utcoffset() == timedelta(hours=8)
    assert normalized.replace(tzinfo=None) == datetime(2026, 8, 23, 1, 30)


def test_probability_preloads_serialize_and_refresh_new_capture_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scanner = _scanner(_MarketScanHub(tmp_path))
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = Event()
        bindings = {1: "a" * 64}
        observed: list[dict[int, str]] = []
        cancellations: list[Event] = []

        def preload(*, archive_bindings, cancel_event) -> int:
            observed.append(dict(archive_bindings))
            cancellations.append(cancel_event)
            if len(observed) == 1:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(3)
            return len(observed)

        source = _preload_probe(preload, isolated=True)
        history = _preload_probe(lambda: 0)
        scanner._probability_source_research_store = source  # noqa: SLF001
        scanner._historical_probability_store = history  # noqa: SLF001
        monkeypatch.setattr(scanner.cache, "probability_source_capture_archive_bindings", lambda: dict(bindings))
        first = asyncio.create_task(scanner.refresh_probability_research_cache())
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(scanner.refresh_probability_research_cache())
        discarded = asyncio.create_task(scanner.refresh_probability_research_cache())
        await asyncio.sleep(0)
        discarded.cancel()
        with pytest.raises(asyncio.CancelledError):
            await discarded
        assert observed == [{1: "a" * 64}]
        assert cancellations[0].is_set() is False
        bindings[2] = "b" * 64
        release.set()
        assert await asyncio.wait_for(asyncio.gather(first, second), 2) == [1, 2]
        assert observed == [{1: "a" * 64}, bindings]
        assert source.pending is False and history.pending is False

    asyncio.run(scenario())


def test_probability_preload_failure_drains_other_worker_before_clearing_pending(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        scanner = _scanner(_MarketScanHub(tmp_path))
        loop = asyncio.get_running_loop()
        history_entered = asyncio.Event()
        source_failed = asyncio.Event()
        release = Event()
        history_finished = Event()

        def failing(**_arguments) -> int:
            loop.call_soon_threadsafe(source_failed.set)
            raise ValueError("invalid archive")

        def history_preload() -> int:
            loop.call_soon_threadsafe(history_entered.set)
            assert release.wait(3)
            history_finished.set()
            return 1

        source = _preload_probe(failing, isolated=True)
        history = _preload_probe(history_preload)
        scanner._probability_source_research_store = source  # noqa: SLF001
        scanner._historical_probability_store = history  # noqa: SLF001
        refresh = asyncio.create_task(scanner.refresh_probability_research_cache())
        await asyncio.wait_for(asyncio.gather(history_entered.wait(), source_failed.wait()), 1)
        await asyncio.sleep(0)
        assert refresh.done() is False
        assert source.pending is True and history.pending is True
        release.set()
        with pytest.raises(ValueError, match="invalid archive"):
            await asyncio.wait_for(refresh, 2)
        assert history_finished.is_set()
        assert source.pending is False and history.pending is False

    asyncio.run(scenario())


def test_probability_preload_repeated_cancellation_drains_real_workers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        scanner = _scanner(_MarketScanHub(tmp_path))
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = Event()
        source_finished = Event()
        history_finished = Event()

        def preload(*, cancel_event, **_arguments) -> int:
            loop.call_soon_threadsafe(entered.set)
            assert cancel_event.wait(3)
            loop.call_soon_threadsafe(cancelled.set)
            assert release.wait(3)
            source_finished.set()
            raise ValueError("source worker cancelled before publication")

        def history_preload() -> int:
            assert release.wait(3)
            history_finished.set()
            return 1

        source = _preload_probe(preload, isolated=True)
        history = _preload_probe(history_preload)
        scanner._probability_source_research_store = source  # noqa: SLF001
        scanner._historical_probability_store = history  # noqa: SLF001
        refresh = asyncio.create_task(scanner.refresh_probability_research_cache())
        await asyncio.wait_for(entered.wait(), 1)
        refresh.cancel()
        await asyncio.wait_for(cancelled.wait(), 1)
        assert refresh.done() is False
        assert source.pending is True and history.pending is True
        refresh.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(refresh, 2)
        assert source_finished.is_set() and history_finished.is_set()
        assert source.pending is False and history.pending is False
        assert scanner._probability_preload_lock.locked() is False  # noqa: SLF001

    asyncio.run(scenario())


def _preload_probe(read: Callable[..., int], *, isolated: bool = False) -> SimpleNamespace:
    probe = SimpleNamespace(pending=False)
    probe.mark_preload_pending = lambda: setattr(probe, "pending", True)
    probe.clear_preload_pending = lambda: setattr(probe, "pending", False)
    probe.refresh_pending = lambda: probe.pending
    if isolated:
        probe.preload_isolated = read
        probe.preload = lambda: pytest.fail("isolated preload must be preferred")
    else:
        probe.preload = read
    return probe


def test_market_scan_stop_cancels_and_reaps_isolated_warmup_before_returning(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        scanner = _scanner(_MarketScanHub(tmp_path))
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        finished = Event()

        def preload(*, cancel_event, **_arguments) -> int:
            loop.call_soon_threadsafe(entered.set)
            assert cancel_event.wait(3)
            finished.set()
            raise ValueError("cancelled worker")

        source = _preload_probe(preload, isolated=True)
        history = _preload_probe(lambda: 1)
        scanner._probability_source_research_store = source  # noqa: SLF001
        scanner._historical_probability_store = history  # noqa: SLF001
        await scanner.start()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(scanner.stop(), 1)
        assert finished.is_set()
        assert scanner.is_quiescent is True
        assert scanner._probability_runtime_warmup_task is None  # noqa: SLF001
        assert source.pending is False and history.pending is False

    asyncio.run(scenario())


@pytest.mark.parametrize("close", [True, False])
def test_market_scan_stop_repeated_cancellation_still_waits_for_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close: bool,
) -> None:
    async def scenario() -> None:
        scanner = _scanner(_MarketScanHub(tmp_path))
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = False

        async def cleanup(*, close: bool) -> None:
            nonlocal finished
            entered.set()
            await release.wait()
            finished = True

        monkeypatch.setattr(scanner, "_stop", cleanup)
        stop = asyncio.create_task(scanner.stop() if close else scanner.rollback_activation())
        await asyncio.wait_for(entered.wait(), 1)
        for _attempt in range(3):
            stop.cancel()
            await asyncio.sleep(0)
            assert stop.done() is False
            assert finished is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop, 1)
        assert finished is True

    asyncio.run(scenario())


def test_real_probability_store_keeps_batch_pending_after_its_preload_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scanner = _scanner(_MarketScanHub(tmp_path))
        loop = asyncio.get_running_loop()
        source_entered = asyncio.Event()
        history_finished = asyncio.Event()
        release_source = Event()
        source = scanner._probability_source_research_store  # noqa: SLF001
        historical = scanner._historical_probability_store  # noqa: SLF001
        original_source = source.preload_isolated
        original_history = historical.preload
        reads: list[bool] = []

        def source_preload(**arguments) -> int:
            loop.call_soon_threadsafe(source_entered.set)
            assert release_source.wait(3)
            return original_source(**arguments)

        def history_preload() -> int:
            result = original_history()
            loop.call_soon_threadsafe(history_finished.set)
            return result

        monkeypatch.setattr(source, "preload_isolated", source_preload)
        monkeypatch.setattr(historical, "preload", history_preload)
        refresh = asyncio.create_task(scanner.refresh_probability_research_cache())
        try:
            await asyncio.wait_for(asyncio.gather(source_entered.wait(), history_finished.wait()), 1)
            assert historical.refresh_pending() is True
            monkeypatch.setattr(historical, "_refresh_if_changed", lambda *, blocking: reads.append(blocking))
            historical.research_projection()
            assert reads == [], "completed store must not begin a competing interactive refresh"
        finally:
            release_source.set()
            await asyncio.wait_for(refresh, 2)
        assert source.refresh_pending() is False
        assert historical.refresh_pending() is False

    asyncio.run(scenario())


def test_market_scan_cancellation_closes_atomically_linked_task_returned_late(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        cache = _BlockingTaskRunCache(hub.settings)
        hub.cache = cache
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        assert await asyncio.to_thread(cache.start_entered.wait, 1)
        cancellation = asyncio.create_task(scanner.cancel_scan(started.run.id))
        await _wait_for_status(scanner, started.run.id, {"cancelling", "cancelled"})
        cache.allow_start.set()
        cancelled = await cancellation
        task_runs = cache.recent_task_runs(limit=10)
        await scanner.stop()
        return cancelled, task_runs

    cancelled, task_runs = asyncio.run(scenario())

    assert cancelled.status == "cancelled"
    assert len(task_runs) == 1
    assert task_runs[0].task_name == "full_market_scan"
    assert task_runs[0].status == "cancelled"
    assert "已取消" in (task_runs[0].message or "")


def test_market_scan_task_attach_failure_rolls_back_task_and_finishes_scan(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        with sqlite3.connect(hub.cache.path) as conn:
            conn.execute(
                """
                CREATE TRIGGER reject_market_scan_task_attach
                BEFORE UPDATE OF task_run_id ON market_scan_run
                WHEN NEW.task_run_id IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'simulated task attach failure');
                END
                """
            )
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        failed = await _wait_for_terminal(scanner, started.run.id)
        task_runs = hub.cache.recent_task_runs(limit=10)
        await scanner.stop()
        return failed, task_runs

    failed, task_runs = asyncio.run(scenario())

    assert failed.status == "failed"
    assert failed.task_run_id is None
    assert "simulated task attach failure" in (failed.last_error or "")
    assert task_runs == []


def test_market_scan_graceful_shutdown_marks_run_interrupted_not_user_cancelled(tmp_path: Path) -> None:
    async def scenario():
        gate = asyncio.Event()
        hub = _MarketScanHub(tmp_path, block_klines=gate)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        await _wait_for_status(scanner, started.run.id, {"running"})
        await scanner.stop()
        return scanner.run(started.run.id), hub.cache.recent_task_runs(limit=10)

    interrupted, task_runs = asyncio.run(scenario())

    assert interrupted.status == "interrupted"
    assert interrupted.last_error == "应用关闭时终止后台扫描任务"
    linked = next(item for item in task_runs if item.task_name == "full_market_scan")
    assert linked.status == "cancelled"
    assert "应用关闭中断" in (linked.message or "")


@pytest.mark.parametrize(
    ("finish_method", "expected_status", "persistence_error"),
    [
        ("_finish_cancelled", "cancelled", "attempt to write a readonly database"),
        ("_finish_interrupted", "interrupted", "database or disk is full"),
        ("_finish_failed", "failed", "database is locked"),
    ],
)
def test_market_scan_terminal_persistence_failure_is_visible_and_sanitized(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    finish_method: str,
    expected_status: str,
    persistence_error: str,
) -> None:
    secret = "fake-sensitive-value-for-redaction-test"
    hub = _MarketScanHub(tmp_path)
    hub.settings = hub.settings.model_copy(update={"llm_api_key": secret})

    def fail_terminal_write(*args, **kwargs):
        del args, kwargs
        raise sqlite3.OperationalError(f"{persistence_error}; context={secret}; " "https://db.example/write?token=private-token&mode=full")

    hub.cache.finish_market_scan_run = fail_terminal_write  # type: ignore[method-assign]
    scanner = _scanner(hub)

    async def scenario() -> None:
        finish = getattr(scanner, finish_method)
        if finish_method == "_finish_failed":
            await finish(42, RuntimeError("扫描执行失败"))
        else:
            await finish(42)

    asyncio.run(scenario())
    stderr = capsys.readouterr().err

    assert "terminal persistence failed" in stderr
    assert "run_id=42" in stderr
    assert f"target_status={expected_status}" in stderr
    assert persistence_error in stderr
    assert "OperationalError" in stderr
    assert "https://db.example/write" in stderr
    assert secret not in stderr
    assert "private-token" not in stderr
    assert "token=" not in stderr
    assert "mode=" not in stderr


def test_market_scan_retries_transient_terminal_write_and_commits_linked_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        _configure_clean_full_market(hub)
        original_finish = hub.cache.finish_market_scan_run
        finish_calls = 0

        def fail_once(*args, **kwargs):
            nonlocal finish_calls
            finish_calls += 1
            if finish_calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return original_finish(*args, **kwargs)

        hub.cache.finish_market_scan_run = fail_once  # type: ignore[method-assign]
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        task_runs = hub.cache.recent_task_runs(limit=10)
        await scanner.stop()
        return final, finish_calls, task_runs

    final, finish_calls, task_runs = asyncio.run(scenario())

    assert final.status == "success"
    assert finish_calls == 2
    assert len(task_runs) == 1
    assert task_runs[0].status == "success"
    assert "terminal persistence failed" not in capsys.readouterr().err


def test_market_scan_permanent_terminal_failure_recovers_only_on_explicit_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        _configure_clean_full_market(hub)
        original_finish = hub.cache.finish_market_scan_run

        def fail_terminal_write(*args, **kwargs):
            del args, kwargs
            raise sqlite3.OperationalError("database is locked")

        hub.cache.finish_market_scan_run = fail_terminal_write  # type: ignore[method-assign]
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        for _attempt in range(200):
            if started.run.id not in scanner._lifecycle.active_run_ids:
                break
            await asyncio.sleep(0.01)
        assert started.run.id not in scanner._lifecycle.active_run_ids
        assert scanner._lifecycle.cancel_local(started.run.id) is None
        current = scanner.run(started.run.id)
        assert current.status == "running"

        hub.cache.finish_market_scan_run = original_finish  # type: ignore[method-assign]
        assert scanner.run(started.run.id).status == "running"
        assert scanner.recover_terminal_failures(started.run.id) == 1
        recovered = scanner.run(started.run.id)
        task_runs = hub.cache.recent_task_runs(limit=10)
        await scanner.stop()
        return started.run.id, recovered, task_runs

    run_id, recovered, task_runs = asyncio.run(scenario())
    stderr = capsys.readouterr().err

    assert recovered.status == "interrupted"
    assert recovered.message == "本地扫描任务已退出，终态写入失败后自动中断；可从断点重试"
    assert recovered.last_error == "本地后台扫描已退出，但原终态未能持久化"
    linked = next(item for item in task_runs if item.task_name == "full_market_scan")
    assert linked.status == "cancelled"
    assert f"run_id={run_id}" in stderr
    assert "target_status=success" in stderr
    assert "database is locked" in stderr


def test_market_scan_start_reconciles_orphaned_runs(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    run = hub.cache.create_market_scan_run(
        trigger="manual",
        rule_version=_rule_version(hub),
        as_of="2026-07-18 16:30:00",
        data_date="2026-07-17",
        scope="test",
    )
    task_run_id = hub.cache.start_task_run("full_market_scan")
    hub.cache.attach_market_scan_task_run(run.id, task_run_id)
    hub.cache.start_market_scan_run(run.id)

    async def scenario():
        scanner = _scanner(hub)
        reconciled = await scanner.start()
        current = scanner.run(run.id)
        await scanner.stop()
        return reconciled, current, hub.cache.recent_task_runs(limit=10)

    reconciled, current, task_runs = asyncio.run(scenario())

    assert reconciled == 1
    assert current.status == "interrupted"
    assert "断点重试" in (current.message or "")
    linked = next(item for item in task_runs if item.id == task_run_id)
    assert linked.status == "cancelled"
    assert linked.finished_at is not None
    assert linked.message == "应用重启时终止遗留全市场扫描记录"


def test_market_scan_lock_blocks_non_owner_mutations_and_reconciliation(tmp_path: Path) -> None:
    async def scenario():
        owner_hub = _MarketScanHub(tmp_path)
        standby_hub = _MarketScanHub(tmp_path)
        owner = _scanner(owner_hub)
        standby = _scanner(standby_hub)
        assert await owner.start() == 0
        retryable = owner_hub.cache.create_market_scan_run(
            trigger="manual",
            rule_version=_rule_version(owner_hub),
            as_of="2026-07-17 16:30:00",
            data_date="2026-07-17",
            scope="test",
        )
        owner_hub.cache.start_market_scan_run(retryable.id)
        owner_hub.cache.finish_market_scan_run(retryable.id, "failed", message="可重试")
        active = owner_hub.cache.create_market_scan_run(
            trigger="manual",
            rule_version=_rule_version(owner_hub),
            as_of="2026-07-17 16:30:00",
            data_date="2026-07-17",
            scope="test",
        )
        owner_hub.cache.start_market_scan_run(active.id)

        assert await standby.start() == 0
        assert standby.run(active.id).status == "running"
        with pytest.raises(RuntimeError, match="其他进程"):
            await standby.create_scan(as_of=SCAN_AS_OF)
        with pytest.raises(RuntimeError, match="其他进程"):
            await standby.retry_scan(retryable.id)
        with pytest.raises(RuntimeError, match="其他进程"):
            await standby.cancel_scan(active.id)
        assert standby.run(active.id).status == "running"
        assert standby_hub.cache.market_scan_runs(page=1, page_size=20).total == 2

        await owner.stop()
        reconciled = await standby.start()
        interrupted = standby.run(active.id)
        await standby.stop()
        return reconciled, interrupted

    reconciled, interrupted = asyncio.run(scenario())

    assert reconciled == 1
    assert interrupted.status == "interrupted"
    assert (tmp_path / "market-scan.sqlite3.market-scan.lock").exists()


def test_market_scan_status_recovery_never_interrupts_another_leader_run(tmp_path: Path) -> None:
    async def scenario():
        owner_hub = _MarketScanHub(tmp_path)
        standby_hub = _MarketScanHub(tmp_path)
        owner = _scanner(owner_hub)
        standby = _scanner(standby_hub)
        assert await owner.start() == 0
        active = owner_hub.cache.create_market_scan_run(
            trigger="manual",
            rule_version=_rule_version(owner_hub),
            as_of="2026-07-17 16:30:00",
            data_date="2026-07-17",
            scope="test",
        )
        owner_hub.cache.start_market_scan_run(active.id)
        assert await standby.start() == 0

        standby._track_terminal_persistence(active.id, False)
        observed = standby.run(active.id)

        owner_hub.cache.finish_market_scan_run(active.id, "failed", message="测试收尾")
        await standby.stop()
        await owner.stop()
        return observed

    observed = asyncio.run(scenario())

    assert observed.status == "running"


def test_market_scan_crash_takeover_reconciles_once_before_creating(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path, block_klines=asyncio.Event())
        lock = FileInstanceGuard(Path(f"{hub.cache.path}.market-scan.lock"))
        assert lock.acquire() is True
        orphaned = hub.cache.create_market_scan_run(
            trigger="manual",
            rule_version=_rule_version(hub),
            as_of="2026-07-17 16:30:00",
            data_date="2026-07-17",
            scope="test",
        )
        hub.cache.start_market_scan_run(orphaned.id)
        reconcile_calls = 0
        original_reconcile = hub.cache.reconcile_incomplete_market_scans

        def reconcile() -> int:
            nonlocal reconcile_calls
            reconcile_calls += 1
            return original_reconcile()

        hub.cache.reconcile_incomplete_market_scans = reconcile  # type: ignore[method-assign]
        standby = _scanner(hub)
        assert await standby.start() == 0
        assert reconcile_calls == 0

        lock.release()
        created = await standby.create_scan(as_of=SCAN_AS_OF)
        duplicate = await standby.create_scan(as_of=SCAN_AS_OF)
        old_run = standby.run(orphaned.id)
        await standby.stop()
        return reconcile_calls, created, duplicate, old_run

    reconcile_calls, created, duplicate, old_run = asyncio.run(scenario())

    assert reconcile_calls == 1
    assert old_run.status == "interrupted"
    assert created.accepted is True
    assert created.run.id != old_run.id
    assert duplicate.deduplicated is True
    assert duplicate.run.id == created.run.id
