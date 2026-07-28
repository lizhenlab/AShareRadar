from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3

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


def test_market_scan_permanent_terminal_failure_recovers_on_next_owned_status(
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
