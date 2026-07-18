"""Consistent SQLite runtime backups, verification, and guarded restore."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Iterator, TextIO

from pydantic import ValidationError

from app.models.local_data import (
    RUNTIME_BACKUP_MANIFEST_VERSION,
    RuntimeBackupManifest,
    RuntimeBackupResult,
    RuntimeBackupVerification,
    RuntimeRestoreResult,
)
from app.services.user_data_portability import available_user_tables


BACKUP_DATABASE_NAME = "runtime.sqlite3"
BACKUP_MANIFEST_NAME = "manifest.json"
BACKUP_READ_CHUNK_BYTES = 1024 * 1024
BACKUP_MANIFEST_MAX_BYTES = 1024 * 1024
SQLITE_BUSY_TIMEOUT_MS = 15_000


class RuntimeBackupError(RuntimeError):
    """Raised when a backup cannot be proven safe to use."""


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


def create_runtime_backup(source_path: Path, destination: Path | None = None) -> RuntimeBackupResult:
    source = _require_database(source_path)
    target = _backup_destination(source, destination)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
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
) -> RuntimeRestoreResult:
    if not service_stopped:
        raise RuntimeBackupError("恢复前必须停止服务并显式确认 service_stopped=True")
    target = Path(target_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _verified_restore_source(backup_path, target) as verified:
        with _restore_guard(target):
            rollback = _create_rollback_backup(target, rollback_destination)
            _replace_from_verified_backup(verified.database_path, target, verified.manifest, rollback)
    return RuntimeRestoreResult(
        restored=True,
        target_path=str(target),
        backup_path=str(verified.backup_path),
        rollback_backup_path=str(rollback.backup_path) if rollback else None,
        integrity_check="ok",
    )


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
    return create_runtime_backup(target, rollback_target)


@contextmanager
def _restore_guard(target: Path) -> Iterator[None]:
    lock_path = Path(f"{target}.scheduler.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        _acquire_restore_lock(handle)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _acquire_restore_lock(handle: TextIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeBackupError("检测到服务或调度任务仍在使用目标数据库，已拒绝恢复") from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()


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
        source_path=str(source),
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
    candidate = Path(path).expanduser().resolve()
    root = candidate.parent if candidate.name == BACKUP_MANIFEST_NAME else candidate
    if not root.is_dir():
        raise RuntimeBackupError(f"备份目录不存在：{root}")
    return root, root / BACKUP_MANIFEST_NAME


def _manifest_database_path(root: Path, manifest: RuntimeBackupManifest) -> Path:
    database = (root / manifest.database_file).resolve()
    if database.parent != root.resolve() or not database.is_file():
        raise RuntimeBackupError("manifest 指向的备份数据库不存在或越出备份目录")
    return database


def _backup_destination(source: Path, destination: Path | None) -> Path:
    target = Path(destination).expanduser().resolve() if destination is not None else source.parent / "backups" / f"{source.stem}_{_filename_timestamp()}"
    if target.exists():
        raise RuntimeBackupError(f"备份目标已存在：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


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
    "RuntimeBackupError",
    "create_runtime_backup",
    "restore_runtime_backup",
    "verify_runtime_backup",
]
