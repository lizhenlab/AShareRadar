from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
from datetime import datetime, timedelta
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest

import app.repositories.maintenance as maintenance_module
import app.services.runtime_backup as runtime_backup_module
from app.services.cache import SQLiteCache
from app.config import Settings
from app.repositories.market_scan import MarketScanResultWrite, MarketScanSeed
from app.services.runtime_backup import (
    RuntimeBackupError,
    create_runtime_backup,
    restore_runtime_backup,
    runtime_backup_session,
    runtime_backup_storage,
    verify_runtime_backup,
)


def _create_runtime_backups_in_process(database_path: str, start_event, results, count: int) -> None:
    try:
        if not start_event.wait(timeout=10):
            raise RuntimeError("backup worker start timed out")
        for _index in range(count):
            create_runtime_backup(Path(database_path), max_backups=2)
    except Exception as exc:
        results.put((False, exc.__class__.__name__, str(exc)))
        return
    results.put((True, "", ""))


def test_backup_verify_restore_and_automatic_rollback_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    _insert_watchlist(target, "before")
    backup = create_runtime_backup(target, tmp_path / "backup")
    _update_watchlist(target, "after")

    verified = verify_runtime_backup(Path(backup.backup_path))
    restored = restore_runtime_backup(
        Path(backup.backup_path),
        target,
        service_stopped=True,
        rollback_destination=tmp_path / "rollback",
    )

    assert verified.ok is True
    assert _watchlist_note(target) == "before"
    assert restored.rollback_backup_path == str(tmp_path / "rollback")
    rollback = verify_runtime_backup(Path(restored.rollback_backup_path))
    assert _watchlist_note(Path(rollback.database_path)) == "after"


def test_backup_verification_rejects_tampered_database(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    backup = create_runtime_backup(target, tmp_path / "backup")

    with Path(backup.database_path).open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(RuntimeBackupError, match="SHA-256"):
        verify_runtime_backup(Path(backup.backup_path))


def test_backup_manifest_uses_portable_source_name_and_accepts_legacy_absolute_path(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    backup = create_runtime_backup(target, tmp_path / "backup")
    manifest_path = Path(backup.manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert payload["source_path"] == target.name
    assert Path(payload["source_path"]).is_absolute() is False

    payload["source_path"] = str(target.resolve())
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    verified = verify_runtime_backup(Path(backup.backup_path))
    assert verified.manifest.source_path == str(target.resolve())


def test_managed_runtime_backups_are_pruned_to_configured_bound(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)

    backups = [create_runtime_backup(target, max_backups=2) for _index in range(4)]
    storage = runtime_backup_storage(target)
    retained = sorted(path for path in (tmp_path / "backups").iterdir() if path.is_dir())

    assert storage.managed_bundle_count == 2
    assert len(retained) == 2
    assert all(not Path(result.backup_path).exists() for result in backups[:2])
    assert all(verify_runtime_backup(Path(result.backup_path)).ok for result in backups[2:])
    assert storage.size_bytes == sum(
        candidate.stat().st_size
        for candidate in (tmp_path / "backups").rglob("*")
        if candidate.is_file()
    )


def test_storage_counts_corrupt_bundles_and_crash_residual_bytes(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    create_runtime_backup(target, max_backups=4)
    backup_directory = tmp_path / "backups"
    corrupt = backup_directory / "corrupt-bundle"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("{broken", encoding="utf-8")
    (corrupt / "orphan.sqlite3").write_bytes(b"orphan-database")
    (backup_directory / "crash.partial").write_bytes(b"partial")

    storage = runtime_backup_storage(target)
    actual_root_bytes = sum(
        candidate.stat().st_size
        for candidate in backup_directory.rglob("*")
        if candidate.is_file()
    )

    assert storage.managed_bundle_count == 1
    assert storage.size_bytes == actual_root_bytes


def test_storage_stats_tolerate_permission_and_disappearance_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    create_runtime_backup(target, max_backups=4)
    backup_directory = tmp_path / "backups"
    original_scandir = runtime_backup_module.os.scandir

    def denied_scandir(path):
        if Path(path) == backup_directory:
            raise PermissionError("denied")
        return original_scandir(path)

    monkeypatch.setattr(runtime_backup_module.os, "scandir", denied_scandir)
    denied = runtime_backup_storage(target)
    assert denied.managed_bundle_count == 1
    assert denied.size_bytes == 0

    monkeypatch.setattr(runtime_backup_module.os, "scandir", original_scandir)
    original_bundles = runtime_backup_module._managed_backup_bundles

    def remove_after_bundle_scan(source: Path):
        bundles = original_bundles(source)
        runtime_backup_module.shutil.rmtree(backup_directory)
        return bundles

    monkeypatch.setattr(runtime_backup_module, "_managed_backup_bundles", remove_after_bundle_scan)
    vanished = runtime_backup_storage(target)
    assert vanished.managed_bundle_count == 1
    assert vanished.size_bytes == 0


def test_runtime_backup_session_protects_created_bundle_until_release(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    create_runtime_backup(target, max_backups=2)
    create_runtime_backup(target, max_backups=2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with runtime_backup_session(target, max_backups=2) as session:
            rollback = session.create_verified_backup()
            competing = executor.submit(create_runtime_backup, target, max_backups=2)
            time.sleep(0.1)
            assert competing.done() is False
            assert Path(rollback.backup_path).is_dir()
        competing.result(timeout=10)

    assert Path(rollback.backup_path).is_dir()
    assert runtime_backup_storage(target).managed_bundle_count == 2


def test_operation_lease_releases_every_guard_and_thread_lock_after_release_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingGuard:
        def __init__(self, message: str) -> None:
            self.message = message
            self.release_calls = 0

        def release(self) -> None:
            self.release_calls += 1
            raise OSError(self.message)

    guards = [FailingGuard("second release"), FailingGuard("first release")]
    pending = iter(guards)
    monkeypatch.setattr(
        runtime_backup_module,
        "_acquire_operation_file_guard",
        lambda _path, _deadline, **_kwargs: next(pending),
    )
    paths = (tmp_path / "b.lock", tmp_path / "a.lock")

    with pytest.raises(runtime_backup_module.RuntimeBackupLeaseReleaseError) as raised:
        with runtime_backup_module._runtime_backup_operation_lease(*paths):
            pass

    assert isinstance(raised.value.__cause__, OSError)
    assert str(raised.value.__cause__) == "first release"
    assert [guard.release_calls for guard in guards] == [1, 1]
    assert all(
        runtime_backup_module._thread_operation_lock(path.resolve()).locked() is False
        for path in paths
    )


def test_operation_lease_continues_after_a_thread_lock_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingGuard:
        def __init__(self) -> None:
            self.release_calls = 0

        def release(self) -> None:
            self.release_calls += 1

    class FailingThreadLock:
        def __init__(self, message: str | None) -> None:
            self.message = message
            self.release_calls = 0

        def acquire(self, *, timeout: float) -> bool:
            del timeout
            return True

        def release(self) -> None:
            self.release_calls += 1
            if self.message is not None:
                raise RuntimeError(self.message)

    paths = tuple(sorted((tmp_path / "a.lock", tmp_path / "b.lock"), key=str))
    thread_locks = {
        paths[0].resolve(): FailingThreadLock(None),
        paths[1].resolve(): FailingThreadLock("thread release failed"),
    }
    guards = [RecordingGuard(), RecordingGuard()]
    pending_guards = iter(guards)
    monkeypatch.setattr(
        runtime_backup_module,
        "_thread_operation_lock",
        lambda path: thread_locks[path],
    )
    monkeypatch.setattr(
        runtime_backup_module,
        "_acquire_operation_file_guard",
        lambda _path, _deadline, **_kwargs: next(pending_guards),
    )

    with pytest.raises(runtime_backup_module.RuntimeBackupLeaseReleaseError) as raised:
        with runtime_backup_module._runtime_backup_operation_lease(*paths):
            pass

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "thread release failed"
    assert all(guard.release_calls == 1 for guard in guards)
    assert all(lock.release_calls == 1 for lock in thread_locks.values())


def test_restore_guard_attempts_every_release_after_first_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guards = []

    class FailingRestoreGuard:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.release_calls = 0
            guards.append(self)

        def acquire(self) -> bool:
            return True

        def release(self) -> None:
            self.release_calls += 1
            if self.path.name.endswith("market-scan.lock"):
                raise OSError("first restore release")
            if self.path.name.endswith("scheduler.lock"):
                raise OSError("second restore release")

    monkeypatch.setattr(runtime_backup_module, "FileInstanceGuard", FailingRestoreGuard)

    with pytest.raises(runtime_backup_module.RuntimeBackupLeaseReleaseError) as raised:
        with runtime_backup_module._restore_guard(tmp_path / "runtime.sqlite3"):
            pass

    assert isinstance(raised.value.__cause__, OSError)
    assert str(raised.value.__cause__) == "first restore release"
    assert len(guards) == len(runtime_backup_module.RESTORE_LOCK_PATH_SUFFIXES)
    assert all(guard.release_calls == 1 for guard in guards)


def test_restore_reports_post_replace_lease_failure_without_leaving_operation_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    _insert_watchlist(target, "backup-state")
    backup = create_runtime_backup(target, tmp_path / "source-backup")
    _update_watchlist(target, "current-state")

    @contextmanager
    def failing_restore_guard(_target: Path):
        yield
        raise runtime_backup_module.RuntimeBackupLeaseReleaseError("injected release failure")

    monkeypatch.setattr(runtime_backup_module, "_restore_guard", failing_restore_guard)

    with caplog.at_level("CRITICAL"), pytest.raises(RuntimeBackupError, match="恢复已完成"):
        restore_runtime_backup(
            Path(backup.backup_path),
            target,
            service_stopped=True,
            max_backups=2,
        )

    assert _watchlist_note(target) == "backup-state"
    assert "replacement completed" in caplog.text
    assert create_runtime_backup(target, max_backups=2).backup_path


def test_rotation_only_removes_old_strict_temporary_directories_and_tolerates_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    marker = runtime_backup_module.BACKUP_TEMPORARY_MARKER
    prefix = f".{target.stem}.{marker}-"
    valid_path = backup_directory / f"{prefix}{'a' * 32}"
    valid = create_runtime_backup(target, valid_path, max_backups=10)
    old = backup_directory / f"{prefix}{'b' * 32}"
    vanished = backup_directory / f"{prefix}{'c' * 32}"
    permission_denied = backup_directory / f"{prefix}{'d' * 32}"
    fresh = backup_directory / f"{prefix}{'e' * 32}"
    unknown = backup_directory / f"{prefix}{'f' * 31}"
    boundary = backup_directory / f"{prefix}{'0' * 32}"
    for path in (old, vanished, permission_denied, fresh, unknown, boundary):
        path.mkdir()
        (path / "partial").write_bytes(b"partial")
    now = time.time()
    stale_at = now - runtime_backup_module.BACKUP_TEMPORARY_MIN_AGE_SECONDS - 60
    for path in (valid_path, old, vanished, permission_denied, unknown):
        os.utime(path, (stale_at, stale_at))
    boundary_at = now - runtime_backup_module.BACKUP_TEMPORARY_MIN_AGE_SECONDS
    os.utime(boundary, (boundary_at, boundary_at))
    original_rmtree = runtime_backup_module.shutil.rmtree

    def racing_rmtree(path: Path, *args, **kwargs) -> None:
        candidate = Path(path)
        if candidate == permission_denied:
            raise PermissionError("denied")
        if candidate == vanished:
            original_rmtree(candidate, *args, **kwargs)
            raise FileNotFoundError(candidate)
        original_rmtree(candidate, *args, **kwargs)

    monkeypatch.setattr(runtime_backup_module.shutil, "rmtree", racing_rmtree)
    monkeypatch.setattr(runtime_backup_module.time, "time", lambda: now)

    created = create_runtime_backup(target, max_backups=10)

    assert Path(valid.backup_path).is_dir()
    assert Path(created.backup_path).is_dir()
    assert old.exists() is False
    assert vanished.exists() is False
    assert permission_denied.is_dir()
    assert fresh.is_dir()
    assert unknown.is_dir()
    assert boundary.is_dir()


def test_concurrent_thread_backups_are_serialized_and_pruned_to_two(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    worker_count = 18
    start = threading.Barrier(worker_count)

    def create_one_backup():
        start.wait(timeout=10)
        return create_runtime_backup(target, max_backups=2)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(create_one_backup) for _index in range(worker_count)]
        results = [future.result(timeout=30) for future in futures]

    retained = sorted(path for path in (tmp_path / "backups").iterdir() if path.is_dir())
    assert len(results) == worker_count
    assert len(retained) == 2
    assert runtime_backup_storage(target).managed_bundle_count == 2
    assert all(verify_runtime_backup(path).ok for path in retained)


def test_concurrent_process_backups_are_serialized_and_pruned_to_two(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_create_runtime_backups_in_process,
            args=(str(target), start_event, outcomes, 6),
        )
        for _index in range(6)
    ]
    for process in processes:
        process.start()
    start_event.set()
    deadline = time.monotonic() + 60
    try:
        for process in processes:
            process.join(timeout=max(0.0, deadline - time.monotonic()))
        assert all(not process.is_alive() for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert all(process.exitcode == 0 for process in processes)
    assert [outcomes.get(timeout=5) for _process in processes] == [(True, "", "")] * len(processes)
    retained = sorted(path for path in (tmp_path / "backups").iterdir() if path.is_dir())
    assert len(retained) == 2
    assert runtime_backup_storage(target).managed_bundle_count == 2
    assert all(verify_runtime_backup(path).ok for path in retained)


def test_verification_lease_prevents_rotation_from_deleting_in_use_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    oldest = create_runtime_backup(target, max_backups=2)
    create_runtime_backup(target, max_backups=2)
    verification_entered = threading.Event()
    release_verification = threading.Event()
    creation_started = threading.Event()
    original_sha256 = runtime_backup_module._sha256
    oldest_database = Path(oldest.database_path).resolve()

    def blocking_sha256(path: Path) -> str:
        if path.resolve() == oldest_database:
            verification_entered.set()
            if not release_verification.wait(timeout=10):
                raise AssertionError("verification release timed out")
        return original_sha256(path)

    def create_new_backup():
        creation_started.set()
        return create_runtime_backup(target, max_backups=2)

    monkeypatch.setattr(runtime_backup_module, "_sha256", blocking_sha256)
    with ThreadPoolExecutor(max_workers=2) as executor:
        verification = executor.submit(verify_runtime_backup, Path(oldest.backup_path))
        assert verification_entered.wait(timeout=5)
        creation = executor.submit(create_new_backup)
        assert creation_started.wait(timeout=5)
        try:
            time.sleep(0.1)
            assert creation.done() is False
            assert Path(oldest.backup_path).is_dir()
        finally:
            release_verification.set()
        assert verification.result(timeout=5).ok is True
        creation.result(timeout=10)

    assert Path(oldest.backup_path).exists() is False


def test_restore_lease_prevents_rotation_from_deleting_source_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    _insert_watchlist(target, "before")
    oldest = create_runtime_backup(target, max_backups=2)
    _update_watchlist(target, "current")
    create_runtime_backup(target, max_backups=2)
    restore_copy_entered = threading.Event()
    release_restore_copy = threading.Event()
    creation_started = threading.Event()
    original_copy = runtime_backup_module._copy_database_file
    oldest_database = Path(oldest.database_path).resolve()

    def blocking_copy(source: Path, destination: Path) -> None:
        if source.resolve() == oldest_database:
            restore_copy_entered.set()
            if not release_restore_copy.wait(timeout=10):
                raise AssertionError("restore copy release timed out")
        original_copy(source, destination)

    def create_new_backup():
        creation_started.set()
        return create_runtime_backup(target, max_backups=2)

    monkeypatch.setattr(runtime_backup_module, "_copy_database_file", blocking_copy)
    with ThreadPoolExecutor(max_workers=2) as executor:
        restoration = executor.submit(
            restore_runtime_backup,
            Path(oldest.backup_path),
            target,
            service_stopped=True,
            max_backups=2,
        )
        assert restore_copy_entered.wait(timeout=5)
        creation = executor.submit(create_new_backup)
        assert creation_started.wait(timeout=5)
        try:
            time.sleep(0.1)
            assert creation.done() is False
            assert Path(oldest.backup_path).is_dir()
        finally:
            release_restore_copy.set()
        restored = restoration.result(timeout=10)
        creation.result(timeout=10)

    assert restored.restored is True
    assert _watchlist_note(target) == "before"
    assert runtime_backup_storage(target).managed_bundle_count == 2


def test_backup_operation_lease_timeout_is_bounded_readable_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "api_key=very-secret.sqlite3"
    SQLiteCache(target)
    guard = runtime_backup_module.FileInstanceGuard(
        runtime_backup_module._database_operation_lock_path(target)
    )
    assert guard.acquire() is True
    monkeypatch.setattr(runtime_backup_module, "BACKUP_OPERATION_LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(runtime_backup_module, "BACKUP_OPERATION_LOCK_POLL_SECONDS", 0.005)
    started_at = time.monotonic()
    try:
        with pytest.raises(RuntimeBackupError, match="操作繁忙.*超时") as raised:
            create_runtime_backup(target, max_backups=2)
    finally:
        guard.release()

    assert time.monotonic() - started_at < 1
    assert "very-secret" not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


def test_managed_runtime_backup_rejects_an_unsafe_retention_limit_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)

    with pytest.raises(ValueError, match="max_backups"):
        create_runtime_backup(target, max_backups=1)

    assert not any((tmp_path / "backups").iterdir())


def test_restore_uses_private_verified_snapshot_when_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    _insert_watchlist(target, "before")
    backup = create_runtime_backup(target, tmp_path / "backup")
    _update_watchlist(target, "current")
    original_replace = runtime_backup_module._replace_from_verified_backup

    def replace_after_source_mutation(*args: object, **kwargs: object) -> None:
        _update_watchlist(Path(backup.database_path), "mutate")
        original_replace(*args, **kwargs)

    monkeypatch.setattr(runtime_backup_module, "_replace_from_verified_backup", replace_after_source_mutation)

    restored = restore_runtime_backup(Path(backup.backup_path), target, service_stopped=True)

    assert restored.restored is True
    assert _watchlist_note(Path(backup.database_path)) == "mutate"
    assert _watchlist_note(target) == "before"


def test_restore_requires_service_stopped_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    backup = create_runtime_backup(target, tmp_path / "backup")

    with pytest.raises(RuntimeBackupError, match="service_stopped=True"):
        restore_runtime_backup(Path(backup.backup_path), target)


@pytest.mark.parametrize("lock_suffix", ["runtime-leader", "scheduler", "market-scan"])
def test_restore_rejects_held_runtime_lock(tmp_path: Path, lock_suffix: str) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    backup = create_runtime_backup(target, tmp_path / "backup")
    lock_path = Path(f"{target}.{lock_suffix}.lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeBackupError, match="仍在使用目标数据库"):
            restore_runtime_backup(Path(backup.backup_path), target, service_stopped=True)


def test_orphaned_task_runs_are_reconciled_once(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "runtime.sqlite3")
    cache.start_task_run("first")
    cache.start_task_run("second")

    assert cache.reconcile_orphaned_task_runs() == 2
    assert cache.reconcile_orphaned_task_runs() == 0
    runs = cache.recent_task_runs(limit=5)
    assert [run.status for run in runs] == ["cancelled", "cancelled"]
    assert all(run.finished_at is not None for run in runs)
    assert all(run.duration_ms is None or run.duration_ms >= 0 for run in runs)


def test_runtime_cleanup_preview_matches_committed_removal(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_task_run_rows=1)
    cache = SQLiteCache(path, settings=settings)
    for task_name in ("first", "second", "third"):
        run_id = cache.start_task_run(task_name)
        cache.finish_task_run(run_id, "success")

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()

    assert preview["task_run"] == 2
    assert removed["task_run"] == 2
    assert len(cache.recent_task_runs(limit=10)) == 1


def test_runtime_cleanup_keeps_configured_market_scan_snapshots_and_cascades_results(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_market_scan_runs=1)
    cache = SQLiteCache(path, settings=settings)
    for day in (15, 16, 17):
        run = cache.create_market_scan_run(
            trigger="manual",
            rule_version="full-market-score-v1",
            as_of=f"2026-07-{day:02d} 16:30:00",
            data_date=f"2026-07-{day:02d}",
            scope="test",
        )
        cache.start_market_scan_run(run.id)
        cache.seed_market_scan_results(
            run.id,
            [MarketScanSeed("600519.SH", "600519", "SH", "贵州茅台")],
            excluded_count=0,
        )
        cache.save_market_scan_result_batch(
            run.id,
            [MarketScanResultWrite(symbol="600519.SH", status="skipped", reason="test")],
        )
        cache.finish_market_scan_run(run.id, "failed", message="test")

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()
    counts = cache.table_counts()

    assert preview["market_scan_run"] == 2
    assert removed["market_scan_run"] == 2
    assert counts["market_scan_run"] == 1
    assert counts["market_scan_result"] == 1


def test_runtime_cleanup_preserves_market_scan_retry_and_task_lineage(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(
        cache_path=path,
        max_market_scan_runs=1,
        max_task_run_rows=1,
    )
    cache = SQLiteCache(path, settings=settings)
    original = cache.create_market_scan_run(
        trigger="manual",
        rule_version="full-market-score-v1",
        as_of="2026-07-16 16:30:00",
        data_date="2026-07-16",
        scope="test",
    )
    task_run_id = cache.start_task_run("full_market_scan")
    cache.attach_market_scan_task_run(original.id, task_run_id)
    cache.start_market_scan_run(original.id)
    cache.finish_market_scan_run(original.id, "failed", message="test")
    cache.finish_task_run(task_run_id, "failed", "test")
    for day in (17, 18):
        unrelated = cache.create_market_scan_run(
            trigger="manual",
            rule_version="full-market-score-v1",
            as_of=f"2026-07-{day:02d} 16:30:00",
            data_date=f"2026-07-{day:02d}",
            scope="test",
        )
        cache.start_market_scan_run(unrelated.id)
        cache.finish_market_scan_run(unrelated.id, "failed", message="test")

    retry = cache.prepare_market_scan_retry(original.id)
    cache.start_market_scan_run(retry.id)
    cache.finish_market_scan_run(retry.id, "failed", message="test")

    for task_name in ("newer-1", "newer-2"):
        run_id = cache.start_task_run(task_name)
        cache.finish_task_run(run_id, "success")

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()

    assert preview["market_scan_run"] == 2
    assert removed["market_scan_run"] == 2
    assert cache.market_scan_run(retry.id).retry_of_run_id == original.id
    assert preview["task_run"] == 1
    assert removed["task_run"] == 1
    assert cache.market_scan_run(original.id).task_run_id == task_run_id


def test_runtime_cleanup_releases_expired_retry_and_task_lineage_in_one_pass(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_market_scan_runs=1, max_task_run_rows=1)
    cache = SQLiteCache(path, settings=settings)
    original = cache.create_market_scan_run(
        trigger="manual",
        rule_version="full-market-score-v1",
        as_of="2026-07-16 16:30:00",
        data_date="2026-07-16",
        scope="test",
    )
    task_run_id = cache.start_task_run("full_market_scan")
    cache.attach_market_scan_task_run(original.id, task_run_id)
    cache.start_market_scan_run(original.id)
    cache.finish_market_scan_run(original.id, "failed", message="test")
    retry = cache.prepare_market_scan_retry(original.id)
    cache.start_market_scan_run(retry.id)
    cache.finish_market_scan_run(retry.id, "failed", message="test")
    latest = cache.create_market_scan_run(
        trigger="manual",
        rule_version="full-market-score-v1",
        as_of="2026-07-18 16:30:00",
        data_date="2026-07-18",
        scope="test",
    )
    cache.start_market_scan_run(latest.id)
    cache.finish_market_scan_run(latest.id, "failed", message="test")
    latest_task_id = cache.start_task_run("latest")
    cache.finish_task_run(latest_task_id, "success")

    first = cache.cleanup_runtime_rows()
    second = cache.cleanup_runtime_rows()

    assert first["market_scan_run"] == 2
    assert first["task_run"] == 1
    assert second["market_scan_run"] == 0
    assert second["task_run"] == 0
    assert cache.table_counts()["market_scan_run"] == 1
    assert [run.id for run in cache.recent_task_runs(limit=10)] == [latest_task_id]


def test_runtime_cleanup_preserves_active_scan_and_running_task(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_market_scan_runs=1, max_task_run_rows=1)
    cache = SQLiteCache(path, settings=settings)
    active = cache.create_market_scan_run(
        trigger="manual",
        rule_version="full-market-score-v1",
        as_of="2026-07-14 16:30:00",
        data_date="2026-07-14",
        scope="test",
    )
    running_task_id = cache.start_task_run("full_market_scan")
    cache.attach_market_scan_task_run(active.id, running_task_id)
    cache.start_market_scan_run(active.id)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO market_scan_run (
                status, trigger, rule_version, as_of, data_date, scope,
                created_at, updated_at, finished_at
            ) VALUES ('failed', 'manual', 'full-market-score-v1', ?, ?, 'test', ?, ?, ?)
            """,
            [
                ("2026-07-15 16:30:00", "2026-07-15", "2026-07-15 16:30:00", "2026-07-15 16:30:00", "2026-07-15 16:31:00"),
                ("2026-07-16 16:30:00", "2026-07-16", "2026-07-16 16:30:00", "2026-07-16 16:30:00", "2026-07-16 16:31:00"),
            ],
        )
    for task_name in ("newer-1", "newer-2"):
        run_id = cache.start_task_run(task_name)
        cache.finish_task_run(run_id, "success")

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()

    assert preview["task_run"] == removed["task_run"] == 1
    assert preview["market_scan_run"] == removed["market_scan_run"] == 1
    assert cache.market_scan_run(active.id).status == "running"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM task_run WHERE id = ?", (running_task_id,)).fetchone()[0] == "running"


def test_runtime_cleanup_caps_daily_kline_rows_per_symbol_and_adjustment(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_daily_kline_rows=260)
    cache = SQLiteCache(path, settings=settings)
    first_day = datetime(2025, 1, 1)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO kline_daily (
                symbol, adjustment_mode, date, open, close, high, low, volume,
                as_of, data_version, contract_version, fallback_used, source, fetched_at
            ) VALUES (?, ?, ?, 10, 10, 11, 9, 1000, ?, 'test-v1', 'v1', 0, 'test', ?)
            """,
            [
                (
                    "600519.SH",
                    "qfq",
                    f"{(first_day + timedelta(days=offset)):%Y-%m-%d}",
                    "2025-12-31",
                    "2025-12-31 16:00:00",
                )
                for offset in range(262)
            ]
            + [("600519.SH", "none", "2025-01-01", "2025-12-31", "2025-12-31 16:00:00")],
        )

    removed = cache.cleanup_runtime_rows()
    with sqlite3.connect(path) as conn:
        counts = dict(conn.execute("SELECT adjustment_mode, COUNT(*) FROM kline_daily GROUP BY adjustment_mode"))

    assert removed["kline_daily"] == 2
    assert counts == {"none": 1, "qfq": 260}


def test_runtime_cleanup_excludes_advice_with_review_plan_and_cleans_other_tables(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_advice_history_rows=1, max_task_run_rows=1)
    cache = SQLiteCache(path, settings=settings)
    protected_old = _insert_advice(path, "protected", "2026-07-14 10:00:00")
    removable_old = _insert_advice(path, "removable", "2026-07-15 10:00:00")
    retained_new = _insert_advice(path, "retained", "2026-07-16 10:00:00")
    _insert_review_plan(path, protected_old)
    for task_name in ("first", "second", "third"):
        run_id = cache.start_task_run(task_name)
        cache.finish_task_run(run_id, "success")

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()

    assert preview["advice_history"] == 1
    assert removed["advice_history"] == preview["advice_history"]
    assert preview["task_run"] == 2
    assert removed["task_run"] == preview["task_run"]
    with sqlite3.connect(path) as conn:
        remaining_advice = [int(row[0]) for row in conn.execute("SELECT id FROM advice_history ORDER BY id")]
        assert remaining_advice == [protected_old, retained_new]
        assert conn.execute("SELECT advice_id FROM advice_review_plan").fetchone()[0] == protected_old
        assert conn.execute("SELECT COUNT(*) FROM task_run").fetchone()[0] == 1
    assert removable_old not in remaining_advice


def test_runtime_cleanup_caps_only_deleted_symbols_unread_badges_to_remaining_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_advice_history_rows=2)
    cache = SQLiteCache(path, settings=settings)
    _insert_watchlist_state(path, "600519.SH", unread_change_count=1, last_viewed_at=None)
    _insert_watchlist_state(
        path,
        "000001.SZ",
        unread_change_count=0,
        last_viewed_at=None,
    )
    _insert_advice(path, "a-baseline", "2026-07-14 10:00:00", symbol="600519.SH", confidence=80)
    protected = _insert_advice(path, "b-protected", "2026-07-14 10:00:01", symbol="000001.SZ", confidence=70)
    viewed_through = _insert_advice(path, "b-deleted", "2026-07-14 10:00:02", symbol="000001.SZ", confidence=71)
    _insert_advice(path, "a-retained", "2026-07-14 10:00:03", symbol="600519.SH", confidence=81)
    _insert_advice(path, "b-retained", "2026-07-14 10:00:04", symbol="000001.SZ", confidence=72)
    _insert_review_plan(path, protected)
    partially_viewed = cache.mark_watchlist_viewed("000001.SZ", viewed_through_advice_id=viewed_through)
    with sqlite3.connect(path) as conn:
        badges_before_preview = dict(conn.execute("SELECT symbol, unread_change_count FROM watchlist"))
    preview = cache.preview_runtime_cleanup()
    with sqlite3.connect(path) as conn:
        badges_after_preview = dict(conn.execute("SELECT symbol, unread_change_count FROM watchlist"))

    removed = cache.cleanup_runtime_rows()

    with sqlite3.connect(path) as conn:
        badges = dict(conn.execute("SELECT symbol, unread_change_count FROM watchlist"))
        remaining_by_symbol = {
            symbol: [row[0] for row in conn.execute("SELECT id FROM advice_history WHERE symbol = ? ORDER BY id", (symbol,))]
            for symbol in ("600519.SH", "000001.SZ")
        }
        protected_advice = conn.execute("SELECT advice_id FROM advice_review_plan").fetchone()[0]

    assert preview["advice_history"] == 2
    assert partially_viewed is not None
    assert partially_viewed.unread_change_count == 1
    assert badges_before_preview == badges_after_preview == {"000001.SZ": 1, "600519.SH": 1}
    assert removed["advice_history"] == preview["advice_history"]
    assert badges == {"000001.SZ": 1, "600519.SH": 0}
    assert remaining_by_symbol["600519.SH"] == [4]
    assert remaining_by_symbol["000001.SZ"] == [protected, 5]
    assert protected_advice == protected


def test_regenerable_cleanup_does_not_touch_watchlist_unread_state(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_cache_event_rows=1, max_advice_history_rows=1)
    cache = SQLiteCache(path, settings=settings)
    _insert_watchlist_state(path, "600519.SH", unread_change_count=1, last_viewed_at=None)
    _insert_advice(path, "first", "2026-07-14 10:00:00", confidence=80)
    _insert_advice(path, "second", "2026-07-14 10:00:01", confidence=81)
    cache.log_event("runtime", "first")
    cache.log_event("runtime", "second")

    removed = cache.maintenance_repo.cleanup_regenerable_runtime_rows()

    with sqlite3.connect(path) as conn:
        unread_change_count = conn.execute("SELECT unread_change_count FROM watchlist WHERE symbol = '600519.SH'").fetchone()[0]
        advice_count = conn.execute("SELECT COUNT(*) FROM advice_history").fetchone()[0]

    assert removed["cache_event"] == 1
    assert unread_change_count == 1
    assert advice_count == 2


def test_regenerable_cleanup_is_throttled_between_health_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "runtime.sqlite3"
    clock = [1000.0]
    monkeypatch.setattr(maintenance_module.time, "monotonic", lambda: clock[0])
    settings = Settings(
        cache_path=path,
        max_cache_event_rows=1,
        runtime_maintenance_interval_seconds=3600,
    )
    cache = SQLiteCache(path, settings=settings)
    cache.log_event("runtime", "first")
    cache.log_event("runtime", "second")

    first = cache.maintenance_repo.cleanup_regenerable_runtime_rows()
    cache.log_event("runtime", "third")
    cache.log_event("runtime", "fourth")
    throttled = cache.maintenance_repo.cleanup_regenerable_runtime_rows()
    clock[0] += 3600
    after_interval = cache.maintenance_repo.cleanup_regenerable_runtime_rows()

    assert first["cache_event"] == 1
    assert throttled == {}
    assert after_interval["cache_event"] == 2


def _insert_watchlist(path: Path, note: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (
                symbol, code, market, name, note, group_name, pinned,
                research_status, priority, unread_change_count, created_at, updated_at
            ) VALUES (
                '600519.SH', '600519', 'SH', '贵州茅台', ?, '默认', 0,
                'watching', 'medium', 0, '2026-07-16 10:00:00', '2026-07-16 10:00:00'
            )
            """,
            (note,),
        )


def _insert_watchlist_state(
    path: Path,
    symbol: str,
    *,
    unread_change_count: int,
    last_viewed_at: str | None,
) -> None:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (
                symbol, code, market, name, note, group_name, pinned,
                research_status, priority, last_viewed_at, unread_change_count, created_at, updated_at
            ) VALUES (?, ?, ?, '测试股票', NULL, '默认', 0, 'watching', 'medium', ?, ?, ?, ?)
            """,
            (symbol, code, market, last_viewed_at, unread_change_count, "2026-07-16 10:00:00", "2026-07-16 10:00:00"),
        )


def _update_watchlist(path: Path, note: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE watchlist SET note = ? WHERE symbol = '600519.SH'", (note,))


def _watchlist_note(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return str(conn.execute("SELECT note FROM watchlist WHERE symbol = '600519.SH'").fetchone()[0])


def _insert_advice(
    path: Path,
    name: str,
    created_at: str,
    *,
    symbol: str = "600519.SH",
    confidence: int = 80,
) -> int:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO advice_history (
                symbol, code, market, name, action, confidence, trend_score, trend_label,
                risk_level, price, change_pct, support, resistance, data_quality_score,
                data_quality_level, reason, summary, created_at, updated_at,
                snapshot_contract_version, conclusion_basis, rule_version, model_version,
                data_quality_source
            ) VALUES (
                ?, ?, ?, ?, '观望', ?, 60, '偏强', '中',
                100, 0, 90, 110, 90, '良好', 'reason', 'summary', ?, ?,
                'conclusion.v1', 'analysis_action_advice', 'rules.test', 'none', '测试数据'
            )
            """,
            (symbol, code, market, name, confidence, created_at, created_at),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _insert_review_plan(path: Path, advice_id: int) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO advice_review_plan (
                advice_id, symbol, snapshot_market_time, snapshot_price, hypothesis,
                trigger_condition, invalidation_condition, target_price, stop_price,
                horizon_days, evidence_refs_json, revision, created_at, updated_at
            ) VALUES (
                ?, '600519.SH', '2026-07-14 10:00:00', 100, 'hypothesis',
                'trigger', 'invalidation', 110, 90, 5, '[]', 1,
                '2026-07-14 10:00:00', '2026-07-14 10:00:00'
            )
            """,
            (advice_id,),
        )
