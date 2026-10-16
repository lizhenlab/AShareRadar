"""Offline archive SQLite boundaries use temporary databases exclusively."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any

import pytest

from app.db.alert_stream import ALERT_STREAM_TABLE, ensure_alert_stream_state, read_alert_stream_state
from app.db.market_scan_artifact_lease import MarketScanArtifactLeaseError, market_scan_artifact_retention_lease
from app.models.local_data import RuntimeBackupManifest
from app.services import runtime_backup
from app.services import workspace_backup_sqlite as archive
from app.services.instance_guard import FileInstanceGuard


def _database(path: Path, *, wal: bool = False) -> Path:
    with sqlite3.connect(path) as connection:
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES(1,'original')")
    return path


def _writer_is_blocked(path: Path) -> bool:
    connection = sqlite3.connect(path, timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            connection.execute("INSERT INTO evidence VALUES(2,'concurrent')")
        return True
    finally:
        connection.close()


def test_all_declared_databases_remain_write_leased_until_context_exit(tmp_path: Path) -> None:
    primary = _database(tmp_path / "primary.sqlite3", wal=True)
    auxiliary = _database(tmp_path / "research.sqlite3")
    with archive.workspace_database_leases([auxiliary, primary], primary) as lease:
        with ThreadPoolExecutor(max_workers=2) as workers:
            assert all(workers.map(_writer_is_blocked, [primary, auxiliary]))
        lease.validate()
        before = set(tmp_path.iterdir())
        snapshot = tmp_path / "snapshot.sqlite3"
        archive.create_database_snapshot(primary, snapshot)
        assert set(tmp_path.iterdir()) - before == {snapshot}
    for path in (primary, auxiliary):
        with sqlite3.connect(path, timeout=0) as connection:
            connection.execute("INSERT INTO evidence VALUES(2,'released')")


def test_busy_second_database_releases_prior_reservations_and_service_guards(tmp_path: Path) -> None:
    primary = _database(tmp_path / "a.sqlite3")
    auxiliary = _database(tmp_path / "z.sqlite3")
    blocker = sqlite3.connect(auxiliary)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with archive.workspace_database_leases([primary, auxiliary], primary):
                pytest.fail("entered a partial lease set")
        with sqlite3.connect(primary, timeout=0) as connection:
            connection.execute("INSERT INTO evidence VALUES(2,'released')")
        guard = FileInstanceGuard(Path(f"{primary}.runtime-leader.lock"))
        assert guard.acquire()
        guard.release()
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize("suffix", runtime_backup.RESTORE_LOCK_PATH_SUFFIXES)
def test_known_running_service_rejects_archive_immediately(tmp_path: Path, suffix: str) -> None:
    primary = _database(tmp_path / "primary.sqlite3")
    guard = FileInstanceGuard(Path(f"{primary}{suffix}"))
    assert guard.acquire()
    try:
        with pytest.raises(archive.WorkspaceBackupDatabaseError, match="still active"):
            with archive.workspace_database_leases([primary], primary):
                pytest.fail("service lock ignored")
    finally:
        guard.release()


def test_nonblocking_artifact_lease_refuses_another_thread(tmp_path: Path) -> None:
    primary = _database(tmp_path / "primary.sqlite3")
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with market_scan_artifact_retention_lease(primary):
            held.set()
            assert release.wait(timeout=5)

    worker = threading.Thread(target=holder)
    worker.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(MarketScanArtifactLeaseError):
            with archive.workspace_database_leases([primary], primary):
                pytest.fail("busy artifact lock ignored")
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_nonblocking_artifact_lease_refuses_a_flock_holder(tmp_path: Path) -> None:
    primary = _database(tmp_path / "primary.sqlite3")
    from app.db.market_scan_artifact_paths import market_scan_artifact_lock_path
    sidecar = market_scan_artifact_lock_path(primary)
    descriptor = os.open(sidecar, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(MarketScanArtifactLeaseError):
            with market_scan_artifact_retention_lease(primary, blocking=False):
                pytest.fail("busy process lock ignored")
    finally:
        os.close(descriptor)


def test_leases_reject_aliases_missing_and_duplicate_declarations(tmp_path: Path) -> None:
    primary = _database(tmp_path / "primary.sqlite3")
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(primary)
    for paths in ([], [primary, primary], [alias], [tmp_path / "missing"]):
        with pytest.raises((archive.WorkspaceBackupDatabaseError, FileNotFoundError)):
            with archive.workspace_database_leases(paths, primary):
                pytest.fail("invalid database scope accepted")
    alias.unlink()
    os.link(primary, alias)
    with pytest.raises(archive.WorkspaceBackupDatabaseError, match="hardlink"):
        with archive.workspace_database_leases([primary, alias], primary):
            pass


def test_lease_detects_database_path_replacement_and_releases_resources(tmp_path: Path) -> None:
    primary = _database(tmp_path / "primary.sqlite3")
    with pytest.raises(archive.WorkspaceBackupDatabaseError, match="changed"):
        with archive.workspace_database_leases([primary], primary) as lease:
            primary.rename(tmp_path / "original.sqlite3")
            _database(primary)
            lease.validate()
    with archive.workspace_database_leases([primary], primary):
        pass


@pytest.mark.parametrize("wal_header", [False, True])
def test_snapshot_is_single_file_and_verification_is_read_only(tmp_path: Path, wal_header: bool) -> None:
    source = _database(tmp_path / "source.sqlite3")
    destination = tmp_path / "snapshot.sqlite3"
    manifest = archive.create_database_snapshot(source, destination)
    if wal_header:
        connection = sqlite3.connect(destination)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
        manifest["sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
        manifest["database_size_bytes"] = destination.stat().st_size
    before = {path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in tmp_path.iterdir()}

    assert archive.verify_database_snapshot(destination, manifest) == manifest
    runtime_backup.verify_runtime_database_file(destination, RuntimeBackupManifest.model_validate(manifest))

    after = {path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in tmp_path.iterdir()}
    assert before == after
    assert manifest["database_file"] == destination.name
    assert destination.stat().st_nlink == 1
    assert source.stat().st_ino != destination.stat().st_ino
    assert not any(path.name.startswith(".workspace-db-") for path in tmp_path.iterdir())


@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("existing_stream", [False, True])
def test_restore_rotates_only_primary_notification_state(tmp_path: Path, primary: bool, existing_stream: bool) -> None:
    source = _database(tmp_path / "source.sqlite3")
    old_stream = None
    if existing_stream:
        with sqlite3.connect(source) as connection:
            old_stream = ensure_alert_stream_state(connection)
    snapshot, restored = tmp_path / "snapshot.sqlite3", tmp_path / "restored.sqlite3"
    manifest = archive.create_database_snapshot(source, snapshot)
    updated = archive.restore_database_snapshot(snapshot, restored, manifest, primary=primary)

    assert archive.verify_database_snapshot(restored, updated) == updated
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT * FROM evidence").fetchall() == [(1, "original")]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if primary:
            state = read_alert_stream_state(connection)
            assert state != old_stream
            assert updated["schema_version"] == manifest["schema_version"] + int(not existing_stream)
        else:
            assert restored.read_bytes() == snapshot.read_bytes()
            assert (ALERT_STREAM_TABLE in tables) == existing_stream


def test_restore_verification_failure_removes_only_owned_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _database(tmp_path / "source.sqlite3")
    snapshot, restored = tmp_path / "snapshot.sqlite3", tmp_path / "restored.sqlite3"
    manifest = archive.create_database_snapshot(source, snapshot)

    def illegal_mutation(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO evidence VALUES(2,'unexpected')")

    monkeypatch.setattr(archive, "rotate_private_restore_stream", illegal_mutation)
    with pytest.raises(runtime_backup.RuntimeBackupError):
        archive.restore_database_snapshot(snapshot, restored, manifest, primary=True)
    assert not restored.exists()
    assert archive.verify_database_snapshot(snapshot, manifest) == manifest


def test_snapshot_and_restore_never_overwrite_existing_targets(tmp_path: Path) -> None:
    source = _database(tmp_path / "source.sqlite3")
    snapshot = tmp_path / "snapshot.sqlite3"
    manifest = archive.create_database_snapshot(source, snapshot)
    for operation in (lambda: archive.create_database_snapshot(source, snapshot),
                      lambda: archive.restore_database_snapshot(snapshot, source, manifest, primary=False)):
        with pytest.raises(archive.WorkspaceBackupDatabaseError, match="already exists"):
            operation()


@pytest.mark.parametrize("operation", ["snapshot", "restore"])
def test_target_created_after_preflight_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    source = _database(tmp_path / "source.sqlite3")
    snapshot, target = tmp_path / "snapshot.sqlite3", tmp_path / "target.sqlite3"
    manifest = archive.create_database_snapshot(source, snapshot)
    original_target = archive._new_target

    def occupied_after_preflight(path: Path) -> Path:
        accepted = original_target(path)
        accepted.write_bytes(b"another operation")
        return accepted

    monkeypatch.setattr(archive, "_new_target", occupied_after_preflight)
    with pytest.raises(FileExistsError):
        if operation == "snapshot":
            archive.create_database_snapshot(source, target)
        else:
            archive.restore_database_snapshot(snapshot, target, manifest, primary=False)
    assert target.read_bytes() == b"another operation"
    assert not any(path.name.startswith(".workspace-db-") for path in tmp_path.iterdir())


def test_restore_copy_fsync_failure_removes_new_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _database(tmp_path / "source.sqlite3")
    snapshot, target = tmp_path / "snapshot.sqlite3", tmp_path / "target.sqlite3"
    manifest = archive.create_database_snapshot(source, snapshot)

    def failed_fsync(_descriptor: int) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(archive.os, "fsync", failed_fsync)
    with pytest.raises(OSError, match="disk failure"):
        archive.restore_database_snapshot(snapshot, target, manifest, primary=False)
    assert not target.exists()
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == manifest["sha256"]


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_archive_verification_rejects_unbound_sidecars(tmp_path: Path, suffix: str) -> None:
    source = _database(tmp_path / "source.sqlite3")
    target = tmp_path / "snapshot.sqlite3"
    manifest = archive.create_database_snapshot(source, target)
    Path(f"{target}{suffix}").write_bytes(b"unbound")
    with pytest.raises(archive.WorkspaceBackupDatabaseError, match="sidecars"):
        archive.verify_database_snapshot(target, manifest)


def test_changed_source_during_snapshot_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _database(tmp_path / "source.sqlite3")
    target = tmp_path / "snapshot.sqlite3"
    original_create = archive.create_runtime_backup

    def replace_source(*args: Any, **kwargs: Any) -> Any:
        result = original_create(*args, **kwargs)
        source.rename(tmp_path / "original.sqlite3")
        _database(source)
        return result

    monkeypatch.setattr(archive, "create_runtime_backup", replace_source)
    with pytest.raises(archive.WorkspaceBackupDatabaseError, match="changed"):
        archive.create_database_snapshot(source, target)
    assert not target.exists()
    assert not any(path.name.startswith(".workspace-db-") for path in tmp_path.iterdir())


def test_sqlite_backup_progress_enforces_deadline_and_removes_temporary_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _database(tmp_path / "source.sqlite3")
    original_open = runtime_backup._open_read_only
    deadline = 1.0

    class SlowSource:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        def backup(self, _destination: Any, **kwargs: Any) -> None:
            monkeypatch.setattr(runtime_backup, "monotonic_now", lambda: deadline + 1)
            kwargs["progress"](sqlite3.SQLITE_BUSY, 1, 1)

        def close(self) -> None:
            self.connection.close()

    monkeypatch.setattr(runtime_backup, "monotonic_now", lambda: 0)
    monkeypatch.setattr(runtime_backup, "_open_read_only", lambda path, **_kwargs: SlowSource(original_open(path)))
    with pytest.raises(runtime_backup.RuntimeBackupError, match="时间预算"):
        runtime_backup.create_runtime_backup(source, tmp_path / "bundle", snapshot_timeout_seconds=deadline)
    assert not (tmp_path / "bundle").exists()
    assert not any("runtime-backup-tmp" in path.name for path in tmp_path.iterdir())


def test_read_only_verification_checks_hash_and_metadata_deadlines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _database(tmp_path / "source.sqlite3")
    target = tmp_path / "snapshot.sqlite3"
    manifest = archive.create_database_snapshot(source, target)
    original_hash = runtime_backup._sha256

    def delayed_hash(path: Path, **kwargs: Any) -> str:
        result = original_hash(path, **kwargs)
        monkeypatch.setattr(runtime_backup, "monotonic_now", lambda: 2)
        return result

    monkeypatch.setattr(runtime_backup, "monotonic_now", lambda: 0)
    monkeypatch.setattr(runtime_backup, "_sha256", delayed_hash)
    with pytest.raises(runtime_backup.RuntimeBackupError, match="时间预算"):
        archive.verify_database_snapshot(target, manifest, timeout_seconds=1)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == manifest["sha256"]


def test_sqlite_integrity_work_is_interrupted_by_progress_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _database(tmp_path / "source.sqlite3")
    with sqlite3.connect(source) as connection:
        connection.executemany("INSERT INTO evidence VALUES(?,?)", ((index, "large fixture") for index in range(2, 5000)))
    target = tmp_path / "snapshot.sqlite3"
    manifest = archive.create_database_snapshot(source, target)
    original_open = runtime_backup._open_read_only
    callbacks = []

    class ExpiringConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        def set_progress_handler(self, callback: Any, interval: int) -> None:
            def observe() -> int:
                result = callback()
                callbacks.append(result)
                return result
            self.connection.set_progress_handler(observe, interval)
            monkeypatch.setattr(runtime_backup, "monotonic_now", lambda: 2)

    monkeypatch.setattr(runtime_backup, "monotonic_now", lambda: 0)
    monkeypatch.setattr(runtime_backup, "_open_read_only", lambda path, **kwargs: ExpiringConnection(original_open(path, **kwargs)))
    with pytest.raises(runtime_backup.RuntimeBackupError, match="时间预算"):
        archive.verify_database_snapshot(target, manifest, timeout_seconds=1)
    assert callbacks and callbacks[-1] == 1


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan"), True])
def test_invalid_time_budgets_fail_before_creating_snapshot(tmp_path: Path, timeout: float) -> None:
    source = _database(tmp_path / "source.sqlite3")
    target = tmp_path / "snapshot.sqlite3"
    with pytest.raises((archive.WorkspaceBackupDatabaseError, ValueError)):
        archive.create_database_snapshot(source, target, timeout_seconds=timeout)
    assert not target.exists()


def test_wal_page_budget_rejects_before_sqlite_backup_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _database(tmp_path / "source.sqlite3", wal=True)
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO evidence VALUES(2,zeroblob(1048576))")
    writer.commit()
    cap = source.stat().st_size
    assert writer.execute("PRAGMA page_count").fetchone()[0] * writer.execute("PRAGMA page_size").fetchone()[0] > cap
    original_open = runtime_backup._open_read_only

    class NoBackupExpected:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        def backup(self, *_args: Any, **_kwargs: Any) -> None:
            pytest.fail("oversized WAL database reached SQLite backup")

    monkeypatch.setattr(runtime_backup, "_open_read_only", lambda path, **kwargs: NoBackupExpected(original_open(path, **kwargs)))
    try:
        with pytest.raises(runtime_backup.RuntimeBackupError, match="字节预算"):
            archive.create_database_snapshot(source, tmp_path / "snapshot.sqlite3", max_bytes=cap)
    finally:
        writer.close()
    assert not (tmp_path / "snapshot.sqlite3").exists()
    assert not any(path.name.startswith(".workspace-db-") for path in tmp_path.iterdir())


def test_sqlite_backup_progress_rechecks_page_growth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _database(tmp_path / "source.sqlite3")
    cap = source.stat().st_size
    original_open = runtime_backup._open_read_only
    checks = []

    class GrowingSource:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        def backup(self, _destination: Any, **kwargs: Any) -> None:
            checks.append(kwargs["pages"])
            kwargs["progress"](sqlite3.SQLITE_OK, 1, cap // 4096 + 1)
            pytest.fail("SQLite page growth exceeded the byte cap without interruption")

    monkeypatch.setattr(runtime_backup, "_open_read_only", lambda path, **kwargs: GrowingSource(original_open(path, **kwargs)))
    with pytest.raises(runtime_backup.RuntimeBackupError, match="字节预算"):
        archive.create_database_snapshot(source, tmp_path / "snapshot.sqlite3", max_bytes=cap)
    assert checks == [256]
    assert not (tmp_path / "snapshot.sqlite3").exists()


def test_restore_byte_budget_rejects_before_copying(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _database(tmp_path / "source.sqlite3")
    snapshot, target = tmp_path / "snapshot.sqlite3", tmp_path / "target.sqlite3"
    manifest = archive.create_database_snapshot(source, snapshot)

    def unexpected_copy(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("oversized restore started copying")

    monkeypatch.setattr(archive, "_copy_exclusively", unexpected_copy)
    with pytest.raises(archive.WorkspaceBackupDatabaseError, match="byte budget"):
        archive.restore_database_snapshot(snapshot, target, manifest, primary=False, max_bytes=snapshot.stat().st_size - 1)
    assert not target.exists()


def test_primary_rotation_growth_respects_byte_cap_and_cleans_target(tmp_path: Path) -> None:
    source = _database(tmp_path / "source.sqlite3")
    snapshot, target = tmp_path / "snapshot.sqlite3", tmp_path / "target.sqlite3"
    manifest = archive.create_database_snapshot(source, snapshot)
    cap = snapshot.stat().st_size

    with pytest.raises(archive.WorkspaceBackupDatabaseError, match="byte budget"):
        archive.restore_database_snapshot(snapshot, target, manifest, primary=True, max_bytes=cap)
    assert not target.exists()
    archive.restore_database_snapshot(snapshot, target, manifest, primary=False, max_bytes=cap)
    assert target.stat().st_size == cap


@pytest.mark.parametrize("cap", [-1, True, 1.5, float("inf")])
def test_invalid_byte_caps_fail_without_snapshot(tmp_path: Path, cap: Any) -> None:
    source = _database(tmp_path / "source.sqlite3")
    target = tmp_path / "snapshot.sqlite3"
    with pytest.raises(archive.WorkspaceBackupDatabaseError, match="nonnegative integer"):
        archive.create_database_snapshot(source, target, max_bytes=cap)
    assert not target.exists()
