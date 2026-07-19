"""Consistent SQLite runtime backups, verification, and guarded restore."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from typing import Iterator

from pydantic import ValidationError

from app.config import MIN_RUNTIME_BACKUP_COUNT, get_settings
from app.models.local_data import (
    RUNTIME_BACKUP_MANIFEST_VERSION,
    RuntimeBackupManifest,
    RuntimeBackupResult,
    RuntimeBackupVerification,
    RuntimeRestoreResult,
)
from app.services.instance_guard import FileInstanceGuard
from app.services.runtime_coordinator import RUNTIME_LEADER_LOCK_SUFFIX
from app.services.user_data_portability import available_user_tables


BACKUP_DATABASE_NAME = "runtime.sqlite3"
BACKUP_MANIFEST_NAME = "manifest.json"
BACKUP_READ_CHUNK_BYTES = 1024 * 1024
BACKUP_MANIFEST_MAX_BYTES = 1024 * 1024
SQLITE_BUSY_TIMEOUT_MS = 15_000
RESTORE_LOCK_PATH_SUFFIXES = (RUNTIME_LEADER_LOCK_SUFFIX, ".scheduler.lock", ".market-scan.lock")
BACKUP_OPERATION_LOCK_NAME = ".runtime-backup-operation.lock"
BACKUP_OPERATION_LOCK_SUFFIX = ".runtime-backup-operation.lock"
BACKUP_OPERATION_LOCK_TIMEOUT_SECONDS = 30.0
BACKUP_OPERATION_LOCK_POLL_SECONDS = 0.05
DESTRUCTIVE_LOCAL_DATA_LOCK_SUFFIX = ".destructive-local-data.lock"
BACKUP_TEMPORARY_MARKER = "runtime-backup-tmp"
BACKUP_TEMPORARY_TOKEN_HEX_LENGTH = 32
BACKUP_TEMPORARY_MIN_AGE_SECONDS = 24 * 60 * 60

_THREAD_OPERATION_LOCKS_GUARD = threading.Lock()
_THREAD_OPERATION_LOCKS: dict[Path, threading.Lock] = {}
LOGGER = logging.getLogger(__name__)


class RuntimeBackupError(RuntimeError):
    """Raised when a backup cannot be proven safe to use."""


class RuntimeBackupLeaseReleaseError(RuntimeBackupError):
    """Raised after every operation lease release has been attempted."""


@dataclass(frozen=True)
class _DatabaseFacts:
    size_bytes: int
    schema_version: int
    user_version: int
    table_row_counts: dict[str, int]
    user_table_row_counts: dict[str, int]
    integrity_check: str


@dataclass(frozen=True)
class _VerifiedRestoreSource:
    backup_path: Path
    database_path: Path
    manifest: RuntimeBackupManifest


@dataclass(frozen=True)
class RuntimeBackupStorage:
    managed_bundle_count: int
    size_bytes: int


@dataclass(frozen=True)
class _ManagedBackupBundle:
    path: Path
    created_at: datetime
    size_bytes: int


class RuntimeBackupSession:
    """Creates rollback bundles while the caller's database transaction is active."""

    def __init__(self, source: Path, max_backups: int) -> None:
        self._source = source
        self._max_backups = max_backups
        self._protected_paths: list[Path] = []
        self._active = True

    def create_verified_backup(self) -> RuntimeBackupResult:
        if not self._active:
            raise RuntimeBackupError("运行时备份保护会话已结束")
        target = _backup_destination(self._source, None)
        result = _create_runtime_backup_bundle(self._source, target)
        self._protected_paths.append(target)
        _verify_runtime_backup(target)
        return result

    def _finish(self) -> None:
        self._active = False
        _prune_managed_runtime_backups(
            self._source,
            max_backups=self._max_backups,
            protected_paths=tuple(self._protected_paths),
        )


@contextmanager
def _runtime_backup_operation_lease(
    *lock_paths: Path,
    acquire_error: str = "无法建立运行时备份操作租约，请检查数据目录权限后重试",
    timeout_error: str = "运行时备份操作繁忙，等待操作租约超时，请稍后重试",
    release_error: str = "运行时备份操作已结束，但释放操作租约失败，请重启服务后重试",
) -> Iterator[None]:
    paths = tuple(sorted({Path(path).expanduser().resolve() for path in lock_paths}, key=str))
    deadline = time.monotonic() + max(0.0, BACKUP_OPERATION_LOCK_TIMEOUT_SECONDS)
    thread_locks: list[threading.Lock] = []
    file_guards: list[FileInstanceGuard] = []
    body_error: BaseException | None = None
    try:
        for path in paths:
            lock = _thread_operation_lock(path)
            remaining = max(0.0, deadline - time.monotonic())
            if not lock.acquire(timeout=remaining):
                raise RuntimeBackupError(timeout_error)
            thread_locks.append(lock)
        for path in paths:
            file_guards.append(
                _acquire_operation_file_guard(
                    path,
                    deadline,
                    acquire_error=acquire_error,
                    timeout_error=timeout_error,
                )
            )
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        first_release_error = _release_operation_resources(file_guards, thread_locks)
        if first_release_error is not None:
            LOGGER.error(
                "Runtime operation lease release failed after all resources were attempted",
                exc_info=(
                    type(first_release_error),
                    first_release_error,
                    first_release_error.__traceback__,
                ),
            )
            if body_error is None:
                raise RuntimeBackupLeaseReleaseError(release_error) from first_release_error


def _thread_operation_lock(path: Path) -> threading.Lock:
    with _THREAD_OPERATION_LOCKS_GUARD:
        lock = _THREAD_OPERATION_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _THREAD_OPERATION_LOCKS[path] = lock
        return lock


def _acquire_operation_file_guard(
    path: Path,
    deadline: float,
    *,
    acquire_error: str,
    timeout_error: str,
) -> FileInstanceGuard:
    guard = FileInstanceGuard(path)
    while True:
        try:
            if guard.acquire():
                return guard
        except Exception:
            raise RuntimeBackupError(acquire_error) from None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeBackupError(timeout_error)
        time.sleep(min(max(0.001, BACKUP_OPERATION_LOCK_POLL_SECONDS), remaining))


def _release_operation_resources(
    file_guards: list[FileInstanceGuard],
    thread_locks: list[threading.Lock],
) -> BaseException | None:
    first_error: BaseException | None = None
    for guard in reversed(file_guards):
        try:
            guard.release()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    for lock in reversed(thread_locks):
        try:
            lock.release()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    return first_error


def _database_operation_lock_path(database: Path) -> Path:
    resolved = Path(database).expanduser().resolve()
    return Path(f"{resolved}{BACKUP_OPERATION_LOCK_SUFFIX}")


def _backup_directory_operation_lock_path(directory: Path) -> Path:
    return Path(directory).expanduser().resolve() / BACKUP_OPERATION_LOCK_NAME


def _destructive_local_data_lock_path(database: Path) -> Path:
    resolved = Path(database).expanduser().resolve()
    return Path(f"{resolved}{DESTRUCTIVE_LOCAL_DATA_LOCK_SUFFIX}")


@contextmanager
def destructive_local_data_lease(database: Path) -> Iterator[None]:
    with _runtime_backup_operation_lease(
        _destructive_local_data_lock_path(database),
        acquire_error="无法建立本地数据破坏性操作租约，请检查数据目录权限后重试",
        timeout_error="本地数据破坏性操作繁忙，等待操作租约超时，请稍后重试",
        release_error="本地数据操作已结束，但释放破坏性操作租约失败，请重启服务后重试",
    ):
        yield


@contextmanager
def runtime_backup_session(
    source_path: Path,
    *,
    max_backups: int | None = None,
) -> Iterator[RuntimeBackupSession]:
    source = _require_database(source_path)
    limit = _runtime_backup_limit(max_backups)
    backup_directory = _managed_backup_directory(source)
    backup_directory.mkdir(parents=True, exist_ok=True)
    with _runtime_backup_operation_lease(
        _database_operation_lock_path(source),
        _backup_directory_operation_lock_path(backup_directory),
    ):
        _remove_stale_managed_backup_temporaries(source)
        session = RuntimeBackupSession(source, limit)
        body_error: BaseException | None = None
        try:
            yield session
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            try:
                session._finish()
            except BaseException:
                if body_error is None:
                    raise
                LOGGER.exception("Runtime backup session finalization failed while propagating caller error")


def create_runtime_backup(
    source_path: Path,
    destination: Path | None = None,
    *,
    max_backups: int | None = None,
) -> RuntimeBackupResult:
    source = _require_database(source_path)
    destination_parent = _backup_destination_parent(source, destination)
    destination_parent.mkdir(parents=True, exist_ok=True)
    managed_limit = (
        _runtime_backup_limit(max_backups)
        if destination_parent == _managed_backup_directory(source)
        else None
    )
    with _runtime_backup_operation_lease(
        _database_operation_lock_path(source),
        _backup_directory_operation_lock_path(destination_parent),
    ):
        if managed_limit is not None:
            _remove_stale_managed_backup_temporaries(source)
        target = _backup_destination(source, destination)
        result = _create_runtime_backup_bundle(source, target)
        if managed_limit is not None:
            _prune_managed_runtime_backups(
                source,
                max_backups=managed_limit,
                protected_paths=(target,),
            )
        return result


def _create_runtime_backup_bundle(source: Path, target: Path) -> RuntimeBackupResult:
    temporary = _new_backup_temporary_directory(source, target.parent)
    try:
        database_path = temporary / BACKUP_DATABASE_NAME
        _sqlite_snapshot(source, database_path)
        facts = _database_facts(database_path)
        manifest = _build_manifest(source, database_path, facts)
        _write_manifest(temporary / BACKUP_MANIFEST_NAME, manifest)
        _fsync_directory(temporary)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return RuntimeBackupResult(
        backup_path=str(target),
        database_path=str(target / BACKUP_DATABASE_NAME),
        manifest_path=str(target / BACKUP_MANIFEST_NAME),
        manifest=manifest,
    )


def verify_runtime_backup(backup_path: Path) -> RuntimeBackupVerification:
    root = _backup_root(backup_path)
    with _runtime_backup_operation_lease(
        _backup_directory_operation_lock_path(root.parent),
    ):
        return _verify_runtime_backup(root)


def _verify_runtime_backup(backup_path: Path) -> RuntimeBackupVerification:
    root, manifest_path = _backup_layout(backup_path)
    manifest = _read_manifest(manifest_path)
    database_path = _manifest_database_path(root, manifest)
    actual_hash, facts = _verify_database_against_manifest(database_path, manifest)
    return RuntimeBackupVerification(
        ok=True,
        backup_path=str(root),
        database_path=str(database_path),
        manifest_path=str(manifest_path),
        sha256=actual_hash,
        integrity_check=facts.integrity_check,
        manifest=manifest,
    )


def restore_runtime_backup(
    backup_path: Path,
    target_path: Path,
    *,
    service_stopped: bool = False,
    rollback_destination: Path | None = None,
    max_backups: int | None = None,
) -> RuntimeRestoreResult:
    if not service_stopped:
        raise RuntimeBackupError("恢复前必须停止服务并显式确认 service_stopped=True")
    target = Path(target_path).expanduser().resolve()
    managed_limit = _runtime_backup_limit(max_backups)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_root = _backup_root(backup_path)
    rollback_parent = _rollback_backup_parent(target, rollback_destination)
    database_replaced = False
    try:
        with _runtime_backup_operation_lease(
            _database_operation_lock_path(target),
            _backup_directory_operation_lock_path(backup_root.parent),
            _backup_directory_operation_lock_path(_managed_backup_directory(target)),
            _backup_directory_operation_lock_path(rollback_parent),
        ):
            _remove_stale_managed_backup_temporaries(target)
            with _verified_restore_source(backup_root, target) as verified:
                with _restore_guard(target):
                    rollback = _create_rollback_backup(target, rollback_destination)
                    _replace_from_verified_backup(verified.database_path, target, verified.manifest, rollback)
                    database_replaced = True
            protected_paths = [verified.backup_path]
            if rollback is not None:
                protected_paths.append(Path(rollback.backup_path))
            _prune_managed_runtime_backups(
                target,
                max_backups=managed_limit,
                protected_paths=tuple(protected_paths),
            )
            return RuntimeRestoreResult(
                restored=True,
                target_path=str(target),
                backup_path=str(verified.backup_path),
                rollback_backup_path=str(rollback.backup_path) if rollback else None,
                integrity_check="ok",
            )
    except RuntimeBackupLeaseReleaseError as exc:
        if not database_replaced:
            raise
        LOGGER.critical(
            "Runtime database replacement completed but an operation lease failed to release",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        raise RuntimeBackupError(
            "数据库恢复已完成，但运行时操作锁释放失败；请重启服务并核验数据库状态"
        ) from exc


@contextmanager
def _verified_restore_source(backup_path: Path, target: Path) -> Iterator[_VerifiedRestoreSource]:
    root, manifest_path = _backup_layout(backup_path)
    manifest = _read_manifest(manifest_path)
    source = _manifest_database_path(root, manifest)
    if target == source:
        raise RuntimeBackupError("不能把备份数据库恢复到其自身")

    private_root = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-source.", dir=target.parent))
    private_root.chmod(0o700)
    try:
        staged = private_root / BACKUP_DATABASE_NAME
        _copy_database_file(source, staged)
        staged.chmod(0o400)
        _verify_database_against_manifest(staged, manifest)
        yield _VerifiedRestoreSource(backup_path=root, database_path=staged, manifest=manifest)
    finally:
        shutil.rmtree(private_root, ignore_errors=True)


def _replace_from_verified_backup(
    source: Path,
    target: Path,
    manifest: RuntimeBackupManifest,
    rollback: RuntimeBackupResult | None,
) -> None:
    staged = _temporary_database_path(target)
    replaced = False
    try:
        _copy_database_file(source, staged)
        _verify_database_against_manifest(staged, manifest)
        _copy_permissions(target, staged)
        _require_quiescent_database(target)
        _remove_sqlite_sidecars(target)
        os.replace(staged, target)
        replaced = True
        _fsync_directory(target.parent)
        _verify_database_against_manifest(target, manifest)
    except BaseException as exc:
        if replaced:
            _recover_failed_restore(target, rollback, exc)
        raise
    finally:
        _remove_sqlite_files(staged)


def _recover_failed_restore(
    target: Path,
    rollback: RuntimeBackupResult | None,
    restore_error: BaseException,
) -> None:
    try:
        if rollback is None:
            _remove_sqlite_files(target)
            return
        with _verified_restore_source(Path(rollback.backup_path), target) as verified_rollback:
            staged = _temporary_database_path(target)
            try:
                _copy_database_file(verified_rollback.database_path, staged)
                _verify_database_against_manifest(staged, verified_rollback.manifest)
                _copy_permissions(target, staged)
                _remove_sqlite_sidecars(target)
                os.replace(staged, target)
                _fsync_directory(target.parent)
                _verify_database_against_manifest(target, verified_rollback.manifest)
            finally:
                _remove_sqlite_files(staged)
    except BaseException as rollback_error:
        raise RuntimeBackupError(f"恢复失败，自动回滚也失败：{rollback_error}；原始错误：{restore_error}") from rollback_error


def _create_rollback_backup(
    target: Path,
    destination: Path | None,
) -> RuntimeBackupResult | None:
    if not target.exists():
        return None
    rollback_target = destination or (target.parent / "backups" / f"{target.stem}_pre_restore_{_filename_timestamp()}")
    source = _require_database(target)
    return _create_runtime_backup_bundle(source, _backup_destination(source, rollback_target))


@contextmanager
def _restore_guard(target: Path) -> Iterator[None]:
    guards: list[FileInstanceGuard] = []
    body_error: BaseException | None = None
    try:
        for suffix in RESTORE_LOCK_PATH_SUFFIXES:
            guard = FileInstanceGuard(Path(f"{target}{suffix}"))
            if not guard.acquire():
                raise RuntimeBackupError("检测到服务或调度任务仍在使用目标数据库，已拒绝恢复")
            guards.append(guard)
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        first_release_error = _release_operation_resources(guards, [])
        if first_release_error is not None:
            LOGGER.error(
                "Restore guard release failed after all guards were attempted",
                exc_info=(
                    type(first_release_error),
                    first_release_error,
                    first_release_error.__traceback__,
                ),
            )
            if body_error is None:
                raise RuntimeBackupLeaseReleaseError(
                    "数据库恢复步骤已结束，但释放服务状态锁失败，请重启服务后核验数据库状态"
                ) from first_release_error


def _require_quiescent_database(path: Path) -> None:
    if not path.exists():
        return
    conn = sqlite3.connect(path, timeout=0)
    try:
        conn.execute("PRAGMA busy_timeout = 0")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise RuntimeBackupError("目标数据库仍有活动连接，无法安全恢复")
        conn.execute("BEGIN EXCLUSIVE")
        conn.rollback()
    except sqlite3.OperationalError as exc:
        raise RuntimeBackupError("目标数据库仍在使用中，无法安全恢复") from exc
    finally:
        conn.close()


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    if destination.exists():
        raise RuntimeBackupError(f"快照目标已存在：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = _open_read_only(source)
    destination_conn = sqlite3.connect(destination, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    try:
        source_conn.backup(destination_conn)
        destination_conn.execute("PRAGMA journal_mode = DELETE")
        destination_conn.commit()
    except BaseException:
        destination_conn.close()
        source_conn.close()
        _remove_sqlite_files(destination)
        raise
    destination_conn.close()
    source_conn.close()
    _fsync_file(destination)


def _copy_database_file(source: Path, destination: Path) -> None:
    if destination.exists():
        raise RuntimeBackupError(f"快照目标已存在：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle, length=BACKUP_READ_CHUNK_BYTES)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
    except BaseException:
        _remove_sqlite_files(destination)
        raise


def _database_facts(path: Path) -> _DatabaseFacts:
    database = _require_database(path)
    conn = _open_read_only(database)
    try:
        integrity = _integrity_check(conn)
        table_counts = _table_row_counts(conn)
        user_counts = {name: table_counts[name] for name in available_user_tables(conn)}
        return _DatabaseFacts(
            size_bytes=database.stat().st_size,
            schema_version=int(conn.execute("PRAGMA schema_version").fetchone()[0]),
            user_version=int(conn.execute("PRAGMA user_version").fetchone()[0]),
            table_row_counts=table_counts,
            user_table_row_counts=user_counts,
            integrity_check=integrity,
        )
    finally:
        conn.close()


def _integrity_check(conn: sqlite3.Connection) -> str:
    messages = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    if messages != ["ok"]:
        raise RuntimeBackupError("SQLite integrity_check 失败：" + "；".join(messages[:10]))
    return "ok"


def _table_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = sorted(str(row[0]) for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall())
    return {name: int(conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(name)}").fetchone()[0]) for name in names}


def _build_manifest(source: Path, database: Path, facts: _DatabaseFacts) -> RuntimeBackupManifest:
    return RuntimeBackupManifest(
        manifest_version=RUNTIME_BACKUP_MANIFEST_VERSION,
        created_at=_utc_now_text(),
        source_path=source.name,
        database_file=database.name,
        database_size_bytes=facts.size_bytes,
        schema_version=facts.schema_version,
        user_version=facts.user_version,
        table_row_counts=facts.table_row_counts,
        user_table_row_counts=facts.user_table_row_counts,
        sha256=_sha256(database),
        integrity_check=facts.integrity_check,
    )


def _verify_database_against_manifest(
    database: Path,
    manifest: RuntimeBackupManifest,
) -> tuple[str, _DatabaseFacts]:
    actual_hash = _sha256(database)
    if actual_hash != manifest.sha256:
        raise RuntimeBackupError("备份数据库 SHA-256 与 manifest 不一致")
    facts = _database_facts(database)
    _require_matching_manifest(manifest, facts)
    return actual_hash, facts


def _require_matching_manifest(
    manifest: RuntimeBackupManifest,
    facts: _DatabaseFacts,
    *,
    check_size: bool = True,
) -> None:
    mismatches: list[str] = []
    if check_size and facts.size_bytes != manifest.database_size_bytes:
        mismatches.append("database_size_bytes")
    if facts.schema_version != manifest.schema_version:
        mismatches.append("schema_version")
    if facts.user_version != manifest.user_version:
        mismatches.append("user_version")
    if facts.table_row_counts != manifest.table_row_counts:
        mismatches.append("table_row_counts")
    if facts.user_table_row_counts != manifest.user_table_row_counts:
        mismatches.append("user_table_row_counts")
    if facts.integrity_check != manifest.integrity_check:
        mismatches.append("integrity_check")
    if mismatches:
        raise RuntimeBackupError("备份内容与 manifest 不一致：" + "、".join(mismatches))


def _read_manifest(path: Path) -> RuntimeBackupManifest:
    if not path.is_file():
        raise RuntimeBackupError(f"备份 manifest 不存在：{path}")
    if path.stat().st_size > BACKUP_MANIFEST_MAX_BYTES:
        raise RuntimeBackupError("备份 manifest 过大")
    try:
        return RuntimeBackupManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as exc:
        raise RuntimeBackupError(f"备份 manifest 无效：{exc}") from exc


def _write_manifest(path: Path, manifest: RuntimeBackupManifest) -> None:
    payload = manifest.model_dump_json(indent=2) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _backup_layout(path: Path) -> tuple[Path, Path]:
    root = _backup_root(path)
    if not root.is_dir():
        raise RuntimeBackupError(f"备份目录不存在：{root}")
    return root, root / BACKUP_MANIFEST_NAME


def _backup_root(path: Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    return candidate.parent if candidate.name == BACKUP_MANIFEST_NAME else candidate


def _manifest_database_path(root: Path, manifest: RuntimeBackupManifest) -> Path:
    database = (root / manifest.database_file).resolve()
    if database.parent != root.resolve() or not database.is_file():
        raise RuntimeBackupError("manifest 指向的备份数据库不存在或越出备份目录")
    return database


def runtime_backup_storage(source_path: Path) -> RuntimeBackupStorage:
    source = Path(source_path).expanduser().resolve()
    backup_directory = _managed_backup_directory(source)
    if not backup_directory.exists():
        return RuntimeBackupStorage(managed_bundle_count=0, size_bytes=0)
    with _runtime_backup_operation_lease(_database_operation_lock_path(source)):
        bundles = _managed_backup_bundles(source)
        return RuntimeBackupStorage(
            managed_bundle_count=len(bundles),
            size_bytes=_directory_size(backup_directory),
        )


def _prune_managed_runtime_backups(
    source: Path,
    *,
    max_backups: int | None = None,
    protected_paths: tuple[Path, ...] = (),
) -> tuple[Path, ...]:
    backup_directory = _managed_backup_directory(source)
    _remove_stale_managed_backup_temporaries(source)
    protected = {Path(path).expanduser().resolve() for path in protected_paths}
    limit = _runtime_backup_limit(max_backups)
    bundles = sorted(_managed_backup_bundles(source), key=lambda bundle: (bundle.created_at, bundle.path.name))
    excess = max(0, len(bundles) - limit)
    removed: list[Path] = []
    for bundle in bundles:
        if excess == 0:
            break
        if bundle.path in protected:
            continue
        try:
            shutil.rmtree(bundle.path)
        except FileNotFoundError:
            excess -= 1
            continue
        except OSError:
            continue
        removed.append(bundle.path)
        excess -= 1
    if removed:
        try:
            _fsync_directory(backup_directory)
        except OSError:
            LOGGER.warning("Managed runtime backup directory fsync failed after rotation", exc_info=True)
    return tuple(removed)


def _runtime_backup_limit(value: int | None) -> int:
    limit = get_settings().max_runtime_backups if value is None else value
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < MIN_RUNTIME_BACKUP_COUNT:
        raise ValueError(f"max_backups 必须是大于等于 {MIN_RUNTIME_BACKUP_COUNT} 的整数")
    return limit


def _managed_backup_bundles(source: Path) -> tuple[_ManagedBackupBundle, ...]:
    backup_directory = _managed_backup_directory(source)
    try:
        children = tuple(backup_directory.iterdir())
    except OSError:
        return ()
    bundles: list[_ManagedBackupBundle] = []
    for child in children:
        try:
            bundle = _managed_backup_bundle(source, child)
        except (OSError, RuntimeBackupError):
            continue
        if bundle is not None:
            bundles.append(bundle)
    return tuple(bundles)


def _managed_backup_bundle(source: Path, child: Path) -> _ManagedBackupBundle | None:
    backup_directory = _managed_backup_directory(source)
    if child.is_symlink() or not child.is_dir() or child.resolve().parent != backup_directory:
        return None
    manifest = _read_manifest(child / BACKUP_MANIFEST_NAME)
    if Path(manifest.source_path).name != source.name:
        return None
    _manifest_database_path(child, manifest)
    return _ManagedBackupBundle(
        path=child.resolve(),
        created_at=_backup_created_at(manifest.created_at, child),
        size_bytes=_directory_size(child),
    )


def _managed_backup_directory(source: Path) -> Path:
    return (source.parent / "backups").resolve()


def _backup_created_at(value: str, path: Path) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _directory_size(path: Path) -> int:
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        facts = entry.stat(follow_symlinks=False)
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        else:
                            total += max(0, facts.st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _new_backup_temporary_directory(source: Path, parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(100):
        token = secrets.token_hex(BACKUP_TEMPORARY_TOKEN_HEX_LENGTH // 2)
        temporary = parent / f".{source.stem}.{BACKUP_TEMPORARY_MARKER}-{token}"
        try:
            temporary.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return temporary
    raise RuntimeBackupError("无法创建唯一的运行时备份临时目录")


def _remove_stale_managed_backup_temporaries(source: Path) -> tuple[Path, ...]:
    backup_directory = _managed_backup_directory(source)
    try:
        children = tuple(backup_directory.iterdir())
    except OSError:
        return ()
    pattern = _managed_backup_temporary_pattern(source)
    now = time.time()
    removed: list[Path] = []
    for child in children:
        try:
            if not _is_stale_managed_backup_temporary(
                source,
                child,
                backup_directory=backup_directory,
                pattern=pattern,
                now=now,
            ):
                continue
            shutil.rmtree(child)
            removed.append(child)
        except OSError:
            continue
    _fsync_after_stale_backup_cleanup(backup_directory, removed)
    return tuple(removed)


def _managed_backup_temporary_pattern(source: Path) -> re.Pattern[str]:
    return re.compile(
        rf"\.{re.escape(source.stem)}\.{BACKUP_TEMPORARY_MARKER}-"
        rf"[0-9a-f]{{{BACKUP_TEMPORARY_TOKEN_HEX_LENGTH}}}\Z"
    )


def _is_stale_managed_backup_temporary(
    source: Path,
    child: Path,
    *,
    backup_directory: Path,
    pattern: re.Pattern[str],
    now: float,
) -> bool:
    if pattern.fullmatch(child.name) is None:
        return False
    if child.is_symlink() or not child.is_dir() or child.resolve().parent != backup_directory:
        return False
    if now - child.stat().st_mtime <= BACKUP_TEMPORARY_MIN_AGE_SECONDS:
        return False
    try:
        return _managed_backup_bundle(source, child) is None
    except RuntimeBackupError:
        return True


def _fsync_after_stale_backup_cleanup(backup_directory: Path, removed: list[Path]) -> None:
    if not removed:
        return
    try:
        _fsync_directory(backup_directory)
    except OSError:
        LOGGER.warning("Managed runtime backup directory fsync failed after stale cleanup", exc_info=True)


def _backup_destination(source: Path, destination: Path | None) -> Path:
    target = Path(destination).expanduser().resolve() if destination is not None else source.parent / "backups" / f"{source.stem}_{_filename_timestamp()}"
    if target.exists():
        raise RuntimeBackupError(f"备份目标已存在：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _backup_destination_parent(source: Path, destination: Path | None) -> Path:
    if destination is None:
        return _managed_backup_directory(source)
    return Path(destination).expanduser().resolve().parent


def _rollback_backup_parent(target: Path, destination: Path | None) -> Path:
    if destination is None:
        return _managed_backup_directory(target)
    return Path(destination).expanduser().resolve().parent


def _temporary_database_path(target: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.restore.", suffix=".sqlite3", dir=target.parent)
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _open_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _require_database(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeBackupError(f"SQLite 数据库不存在：{resolved}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(BACKUP_READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_permissions(source: Path, destination: Path) -> None:
    if source.exists():
        destination.chmod(source.stat().st_mode & 0o777)


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _remove_sqlite_files(path: Path) -> None:
    path.unlink(missing_ok=True)
    _remove_sqlite_sidecars(path)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _filename_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


__all__ = [
    "BACKUP_DATABASE_NAME",
    "BACKUP_MANIFEST_NAME",
    "DESTRUCTIVE_LOCAL_DATA_LOCK_SUFFIX",
    "RuntimeBackupError",
    "RuntimeBackupLeaseReleaseError",
    "RuntimeBackupSession",
    "RuntimeBackupStorage",
    "create_runtime_backup",
    "destructive_local_data_lease",
    "restore_runtime_backup",
    "runtime_backup_session",
    "runtime_backup_storage",
    "verify_runtime_backup",
]
