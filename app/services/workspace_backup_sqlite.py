"""Offline SQLite archive helpers; leases do not quiesce external file writers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from app.db.alert_stream import ALERT_STREAM_TABLE
from app.db.alert_stream_restore import rotate_private_restore_stream
from app.services.workspace_backup_progress import backup_checkpoint
from app.db.market_scan_artifact_lease import market_scan_artifact_retention_lease
from app.models.local_data import RuntimeBackupManifest
from app.services.runtime_backup import (
    BACKUP_OPERATION_LOCK_SUFFIX, RESTORE_LOCK_PATH_SUFFIXES, RuntimeBackupError,
    create_runtime_backup, verify_runtime_database_file,
)
from app.services.workspace_storage_policy import admitted_lexical_path
from app.utils.clock import monotonic_now


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CHUNK_BYTES = 1024 * 1024


class WorkspaceBackupDatabaseError(RuntimeBackupError):
    """A database archive cannot be admitted or safely completed."""


@dataclass(frozen=True)
class WorkspaceDatabaseLeases:
    identities: tuple[tuple[Path, tuple[int, int]], ...]

    def validate(self) -> None:
        """Recheck source identities before releasing the archive's capture leases."""
        for path, identity in self.identities:
            if _identity(_file_facts(path)) != identity:
                raise WorkspaceBackupDatabaseError("declared database path changed while leased")


@contextmanager
def workspace_database_leases(databases: Sequence[Path], primary: Path) -> Iterator[WorkspaceDatabaseLeases]:
    """Hold fail-fast service/artifact guards and SQLite write reservations only."""
    paths = tuple(_safe_path(path) for path in databases)
    if not paths or len(paths) > 64 or len(set(paths)) != len(paths):
        raise WorkspaceBackupDatabaseError("declare between 1 and 64 distinct databases")
    primary_path = _safe_path(primary)
    if primary_path not in paths:
        raise WorkspaceBackupDatabaseError("primary database must be explicitly declared")
    identities = tuple((path, _identity(_file_facts(path))) for path in sorted(paths))
    if len({identity for _path, identity in identities}) != len(identities):
        raise WorkspaceBackupDatabaseError("database aliases cannot share an inode")
    lease = WorkspaceDatabaseLeases(identities)
    with ExitStack() as stack:
        for suffix in RESTORE_LOCK_PATH_SUFFIXES:
            stack.enter_context(_service_guard(Path(f"{primary_path}{suffix}")))
        stack.enter_context(market_scan_artifact_retention_lease(primary_path, blocking=False))
        for path, identity in identities:
            _prepare_backup_guard(Path(f"{path}{BACKUP_OPERATION_LOCK_SUFFIX}"))
            stack.enter_context(_sqlite_write_reservation(path, identity))
        lease.validate()
        yield lease
        lease.validate()


@contextmanager
def _sqlite_write_reservation(path: Path, identity: tuple[int, int]) -> Iterator[None]:
    connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("BEGIN IMMEDIATE")
        if _identity(_file_facts(path)) != identity:
            raise WorkspaceBackupDatabaseError("database changed before write reservation")
        yield
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


@contextmanager
def _service_guard(path: Path) -> Iterator[None]:
    with _parent_descriptor(path) as parent:
        descriptor = _open_guard(parent, path.name)
        try:
            opened = os.fstat(descriptor)
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _identity(opened) != _identity(current):
                raise WorkspaceBackupDatabaseError("service guard is not a stable regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkspaceBackupDatabaseError("a known application service is still active") from exc
            yield
            if _identity(_file_facts(path)) != _identity(opened):
                raise WorkspaceBackupDatabaseError("service guard changed while leased")
        finally:
            os.close(descriptor)


def _open_guard(parent: int, name: str) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        return os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
    except FileExistsError:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise WorkspaceBackupDatabaseError("operation guard is not a private regular file") from None
        return os.open(name, flags, dir_fd=parent)


def _prepare_backup_guard(path: Path) -> None:
    with _parent_descriptor(path) as parent:
        descriptor = _open_guard(parent, path.name)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise WorkspaceBackupDatabaseError("backup operation guard is unsafe")
            if _identity(_file_facts(path)) != _identity(opened):
                raise WorkspaceBackupDatabaseError("backup operation guard changed")
        finally:
            os.close(descriptor)


def create_database_snapshot(
    source: Path, target_file: Path, *, timeout_seconds: float = 30.0, max_bytes: int | None = None,
) -> dict[str, object]:
    _byte_limit(max_bytes)
    source, target = _safe_path(source), _new_target(target_file)
    original = _file_facts(source)
    deadline = _deadline(timeout_seconds)
    with tempfile.TemporaryDirectory(prefix=".workspace-db-", dir=target.parent) as temporary:
        result = create_runtime_backup(source, Path(temporary) / "snapshot", snapshot_timeout_seconds=_remaining(deadline),
                                       snapshot_max_bytes=max_bytes)
        snapshot = Path(result.database_path)
        manifest = result.manifest.model_copy(update={"database_file": target.name})
        verify_runtime_database_file(snapshot, manifest, timeout_seconds=_remaining(deadline))
        if _fingerprint(_file_facts(source)) != _fingerprint(original):
            raise WorkspaceBackupDatabaseError("source database changed during snapshot")
        _publish_private_snapshot(snapshot, target)
    return manifest.model_dump(mode="json")


def verify_database_snapshot(
    file: Path, manifest: Mapping[str, object], *, timeout_seconds: float = 30.0,
) -> dict[str, object]:
    path = _safe_path(file)
    model = _manifest(manifest, path)
    original = _file_facts(path)
    _require_snapshot_sidecars_absent(path)
    verify_runtime_database_file(path, model, timeout_seconds=timeout_seconds)
    _require_snapshot_sidecars_absent(path)
    if _fingerprint(_file_facts(path)) != _fingerprint(original):
        raise WorkspaceBackupDatabaseError("archive database changed during verification")
    return model.model_dump(mode="json")


def restore_database_snapshot(
    source_file: Path, target_file: Path, manifest: Mapping[str, object], *, primary: bool,
    timeout_seconds: float = 30.0, max_bytes: int | None = None,
) -> dict[str, object]:
    if type(primary) is not bool:
        raise WorkspaceBackupDatabaseError("primary restore flag must be a boolean")
    source, target = _safe_path(source_file), _new_target(target_file)
    _byte_limit(max_bytes)
    _require_file_bytes(_file_facts(source).st_size, max_bytes)
    deadline = _deadline(timeout_seconds)
    verified = verify_database_snapshot(source, manifest, timeout_seconds=_remaining(deadline))
    original = RuntimeBackupManifest.model_validate(verified)
    identity = _copy_exclusively(source, target, deadline, max_bytes)
    try:
        restored = original.model_copy(update={"database_file": target.name})
        if primary:
            verify_database_snapshot(target, restored.model_dump(mode="json"), timeout_seconds=_remaining(deadline))
            backup_checkpoint("sqlite_rotate")
            restored = _rotate_primary_snapshot(target, restored, deadline)
        _require_file_bytes(_file_facts(target).st_size, max_bytes)
        verify_database_snapshot(target, restored.model_dump(mode="json"), timeout_seconds=_remaining(deadline))
        return restored.model_dump(mode="json")
    except BaseException:
        _remove_owned_target(target, identity)
        raise


def _rotate_primary_snapshot(path: Path, original: RuntimeBackupManifest, deadline: float) -> RuntimeBackupManifest:
    _remaining(deadline)
    rotate_private_restore_stream(path)
    return original.model_copy(update={
        "sha256": _file_digest(path, deadline), "database_size_bytes": _file_facts(path).st_size,
        "schema_version": original.schema_version + int(ALERT_STREAM_TABLE not in original.table_row_counts),
        "table_row_counts": {**original.table_row_counts, ALERT_STREAM_TABLE: 1},
    })


def _manifest(value: Mapping[str, object], path: Path) -> RuntimeBackupManifest:
    # JSON-shaped mappings are accepted; Pydantic retains the strict runtime contract.
    model = RuntimeBackupManifest.model_validate(dict(value))
    if model.database_file != path.name:
        raise WorkspaceBackupDatabaseError("database filename disagrees with its manifest")
    return model


def _require_snapshot_sidecars_absent(path: Path) -> None:
    with _parent_descriptor(path) as parent:
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(path.name + suffix, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise WorkspaceBackupDatabaseError("archive database must not have SQLite sidecars")


def _safe_path(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise WorkspaceBackupDatabaseError("database paths must be absolute without parent traversal")
    admitted = admitted_lexical_path(path)
    if admitted is None:
        raise WorkspaceBackupDatabaseError("database paths cannot contain symbolic links")
    return admitted


def _file_facts(path: Path) -> os.stat_result:
    path = _safe_path(path)
    with _parent_descriptor(path) as parent:
        facts = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(facts.st_mode) or facts.st_nlink != 1:
        raise WorkspaceBackupDatabaseError("database must be a regular file without hardlink aliases")
    return facts


def _new_target(path: Path) -> Path:
    path = _safe_path(path)
    with _parent_descriptor(path) as parent:
        try:
            os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return path
    raise WorkspaceBackupDatabaseError("restore or snapshot target already exists")


@contextmanager
def _parent_descriptor(path: Path) -> Iterator[int]:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _publish_private_snapshot(source: Path, target: Path) -> None:
    with _parent_descriptor(target) as parent:
        os.link(source, target.name, dst_dir_fd=parent, follow_symlinks=False)
        identity = _identity(os.stat(target.name, dir_fd=parent, follow_symlinks=False))
        try:
            source.unlink()
            os.fsync(parent)
            if _identity(_file_facts(target)) != identity:
                raise WorkspaceBackupDatabaseError("snapshot target changed during publication")
        except BaseException:
            _remove_owned_target(target, identity)
            raise


def _copy_exclusively(source: Path, target: Path, deadline: float, max_bytes: int | None) -> tuple[int, int]:
    backup_checkpoint("sqlite_copy")
    original = _file_facts(source)
    _require_file_bytes(original.st_size, max_bytes)
    with _parent_descriptor(source) as parent, _parent_descriptor(target) as destination:
        reader = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        try:
            if _fingerprint(os.fstat(reader)) != _fingerprint(original):
                raise WorkspaceBackupDatabaseError("archive database changed before restore")
            return _copy_to_new_file(reader, source, target, destination, original, deadline, max_bytes)
        finally:
            os.close(reader)


def _copy_to_new_file(
    reader: int, source: Path, target: Path, parent: int, original: os.stat_result, deadline: float, max_bytes: int | None,
) -> tuple[int, int]:
    writer = os.open(target.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    identity = _identity(os.fstat(writer))
    try:
        copied = 0
        with os.fdopen(writer, "wb", closefd=False) as output:
            while chunk := os.read(reader, _CHUNK_BYTES if max_bytes is None else min(_CHUNK_BYTES, max_bytes - copied + 1)):
                _remaining(deadline)
                copied += len(chunk)
                _require_file_bytes(copied, max_bytes)
                output.write(chunk)
                backup_checkpoint(current=copied, total=original.st_size, unit="bytes")
            output.flush()
            os.fsync(writer)
        if _fingerprint(_file_facts(source)) != _fingerprint(original):
            raise WorkspaceBackupDatabaseError("archive database changed during restore")
        os.fsync(parent)
        return identity
    except BaseException:
        _remove_owned_target(target, identity)
        raise
    finally:
        os.close(writer)


def _file_digest(path: Path, deadline: float) -> str:
    backup_checkpoint("sqlite_hash")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            _remaining(deadline)
            digest.update(chunk)
            backup_checkpoint()
    _remaining(deadline)
    return digest.hexdigest()


def _remove_owned_target(path: Path, identity: tuple[int, int]) -> None:
    try:
        with _parent_descriptor(path) as parent:
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if _identity(current) == identity:
                os.unlink(path.name, dir_fd=parent)
    except FileNotFoundError:
        pass


def _identity(facts: os.stat_result) -> tuple[int, int]:
    return facts.st_dev, facts.st_ino


def _fingerprint(facts: os.stat_result) -> tuple[int, ...]:
    return (*_identity(facts), facts.st_mode, facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns)


def _deadline(seconds: float) -> float:
    if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise WorkspaceBackupDatabaseError("database timeout must be positive and finite")
    return monotonic_now() + seconds


def _byte_limit(max_bytes: int | None) -> None:
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise WorkspaceBackupDatabaseError("database max_bytes must be a nonnegative integer")


def _require_file_bytes(size: int, max_bytes: int | None) -> None:
    if max_bytes is not None and size > max_bytes:
        raise WorkspaceBackupDatabaseError("database archive byte budget exhausted")


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic_now()
    if remaining <= 0:
        raise WorkspaceBackupDatabaseError("database archive time budget exhausted")
    return remaining


__all__ = [
    "WorkspaceBackupDatabaseError", "WorkspaceDatabaseLeases", "workspace_database_leases",
    "create_database_snapshot", "verify_database_snapshot", "restore_database_snapshot",
]
