"""Cross-process serialization for runtime market-scan artifacts and retention."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import threading
from urllib.parse import quote

from app.db.market_scan_action_source import require_market_scan_action_source
from app.db.market_scan_artifact_rollback import ArtifactRollbackError, unlink_exact_artifact
from app.db.market_scan_artifact_paths import (
    ManagedArtifactPathError,
    market_scan_artifact_lock_path,
    require_project_managed_artifact_database as _require_project_binding,
    require_restored_market_scan_artifact_bindings as _require_restored_bindings,
    stable_market_scan_artifact_lock_root,
)
from app.artifacts.io import exclusive_atomic_publish


class MarketScanArtifactLeaseError(RuntimeError):
    pass


@dataclass
class _LeaseState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    local: threading.local = field(default_factory=threading.local)


@dataclass(frozen=True)
class _FileLease:
    sidecar: int
    directory: int
    path: Path
    identity: tuple[int, int]
    namespace_identity: tuple[int, int]


@dataclass(eq=False)
class MarketScanArtifactBatch:
    database: Path
    targets: frozenset[Path]
    run_ids: tuple[int, ...]
    publications: list[tuple[Path, int, str]] = field(default_factory=list)


def rollback_market_scan_artifact_batch(
    batch: MarketScanArtifactBatch,
    publications: Sequence[tuple[Path, int, str]],
) -> None:
    """Remove only exact files newly linked by a failed authorized batch."""

    state = _lease_state(batch.database)
    if int(getattr(state.local, "depth", 0)) <= 0:
        raise MarketScanArtifactLeaseError("artifact 批次回滚必须持有发布租约")
    failure: MarketScanArtifactLeaseError | None = None
    for target, size, digest in reversed(publications):
        normalized = target.expanduser().absolute()
        if normalized not in batch.targets:
            failure = failure or MarketScanArtifactLeaseError("artifact 批次回滚目标未获授权")
            continue
        try:
            unlink_exact_artifact(normalized, size, digest)
        except ArtifactRollbackError as exc:
            failure = failure or MarketScanArtifactLeaseError(str(exc))
    if failure is not None:
        raise failure


def publish_market_scan_artifact(
    path: str | Path,
    encoded: bytes,
    *,
    max_bytes: int,
    before_publish: Callable[[], None] | None = None,
) -> bool:
    """Publish and record only files newly linked by every enclosing batch."""

    target = Path(path).expanduser().absolute()
    active_batches = tuple(getattr(_ACTIVE_BATCHES, "items", ()))
    if active_batches and not any(target in batch.targets for batch in active_batches):
        raise MarketScanArtifactLeaseError("活动 artifact 批次内不能发布未授权目标")
    for batch in reversed(active_batches):
        if target in batch.targets:
            require_market_scan_artifact_lease_namespace_current(batch.database)
            break
    created = exclusive_atomic_publish(
        target,
        encoded,
        max_bytes=max_bytes,
        before_publish=before_publish,
    )
    if not created:
        return False
    batches = tuple(getattr(_ACTIVE_BATCHES, "items", ()))
    for batch in reversed(batches):
        if target in batch.targets:
            batch.publications.append((target, len(encoded), hashlib.sha256(encoded).hexdigest()))
            break
    return True


_LEASE_STATES: dict[Path, _LeaseState] = {}
_LEASE_STATES_GUARD = threading.Lock()
_ACTIVE_BATCHES = threading.local()
_HELD_DATABASE = threading.local()


def _lease_database_hint(database_path: str | Path, allow_missing: bool) -> tuple[Path, Path, bool]:
    hint = Path(database_path).expanduser().absolute()
    initially_missing = allow_missing and not hint.exists()
    if not initially_missing:
        return hint, validated_market_scan_database_path(hint), False
    if hint.is_symlink() or hint.parent.resolve(strict=True) != hint.parent:
        raise MarketScanArtifactLeaseError("全市场 artifact 运行库路径存在别名")
    return hint, hint, True


def _revalidate_acquired_database(hint: Path, database: Path, initially_missing: bool) -> Path:
    if initially_missing:
        if hint.exists():
            raise MarketScanArtifactLeaseError("全市场 artifact 运行库在等待缺失路径租约期间被创建")
        return database
    return validated_market_scan_database_path(hint)


@contextmanager
def market_scan_artifact_retention_lease(
    database_path: str | Path,
    *,
    allow_missing: bool = False,
) -> Iterator[Path]:
    """Hold one re-entrant thread/fcntl lease for a canonical runtime database."""

    hint, database, initially_missing = _lease_database_hint(database_path, allow_missing)
    state = _lease_state(database)
    with state.lock:
        held_database = getattr(_HELD_DATABASE, "path", None)
        if held_database is not None and held_database != database:
            raise MarketScanArtifactLeaseError("同一线程不能嵌套不同运行库的 artifact 租约")
        depth = int(getattr(state.local, "depth", 0))
        if depth:
            state.local.depth = depth + 1
            try:
                yield database
            finally:
                state.local.depth = depth
            return
        handle = _acquire_file_lease(database)
        try:
            database = _revalidate_acquired_database(hint, database, initially_missing)
        except BaseException:
            _release_file_lease(handle)
            raise
        state.local.depth = 1
        state.local.handle = handle
        _HELD_DATABASE.path = database
        try:
            yield database
        finally:
            try:
                require_market_scan_artifact_lease_namespace_current(database)
            finally:
                _clear_lease_state(state)
                _release_file_lease(handle)


def _clear_lease_state(state: _LeaseState) -> None:
    state.local.depth = 0
    state.local.handle = None
    _HELD_DATABASE.path = None


@contextmanager
def verified_market_scan_artifact_publication(
    database_path: str | Path,
    target_path: str | Path,
    run_ids: Sequence[int],
    *,
    managed_directory: str | Path,
) -> Iterator[bool]:
    """Verify every runtime run under the lease; external targets remain offline."""

    with verified_market_scan_artifact_batch_publication(
        database_path,
        (target_path,),
        run_ids,
        managed_directory=managed_directory,
    ) as managed:
        yield managed is not None


@contextmanager
def verified_market_scan_artifact_batch_publication(
    database_path: str | Path,
    target_paths: Sequence[str | Path],
    run_ids: Sequence[int],
    *,
    managed_directory: str | Path,
    managed_files: Sequence[str | Path] = (),
) -> Iterator[MarketScanArtifactBatch | None]:
    """Keep one lease across a complete managed artifact-set publication."""
    prepared = _prepare_managed_publication(database_path, target_paths, managed_directory, managed_files)
    if prepared is None:
        yield None
        return
    database, normalized_targets = prepared
    normalized = _batch_run_ids(database, normalized_targets, run_ids, managed_files)
    with market_scan_artifact_retention_lease(database) as database:
        _require_publication_runs(database, normalized)
        batch = MarketScanArtifactBatch(database, frozenset(normalized_targets), normalized)
        active = _active_batches_with(batch)
        active.append(batch)
        _ACTIVE_BATCHES.items = active
        completed = False
        try:
            yield batch
            require_market_scan_artifact_lease_namespace_current(database)
            completed = True
        except BaseException as original:
            try:
                rollback_market_scan_artifact_batch(batch, tuple(batch.publications))
            except MarketScanArtifactLeaseError as rollback_error:
                raise MarketScanArtifactLeaseError(f"artifact 批次失败且回滚不完整：{rollback_error}") from original
            raise
        finally:
            remaining = [item for item in getattr(_ACTIVE_BATCHES, "items", ()) if item is not batch]
            _ACTIVE_BATCHES.items = remaining
            if completed:
                _transfer_publications(batch, remaining)


def _batch_run_ids(
    database: Path,
    targets: tuple[Path, ...],
    run_ids: Sequence[int],
    managed_files: Sequence[str | Path],
) -> tuple[int, ...]:
    exact_files = {database.parent / Path(path) for path in managed_files}
    return _normalized_run_ids(run_ids, allow_empty=bool(managed_files) and all(path in exact_files for path in targets))


def _active_batches_with(batch: MarketScanArtifactBatch) -> list[MarketScanArtifactBatch]:
    active = list(getattr(_ACTIVE_BATCHES, "items", ()))
    if not active:
        return active
    parent = active[-1]
    authorized = batch.database == parent.database and batch.targets.issubset(parent.targets) and set(batch.run_ids).issubset(parent.run_ids)
    if not authorized:
        raise MarketScanArtifactLeaseError("嵌套 artifact 批次必须是父批次授权子集")
    return active


def _prepare_managed_publication(
    database_path: str | Path,
    target_paths: Sequence[str | Path],
    managed_directory: str | Path,
    managed_files: Sequence[str | Path],
) -> tuple[Path, tuple[Path, ...]] | None:
    database_hint = Path(database_path).expanduser().absolute()
    targets = tuple(Path(target).expanduser().absolute() for target in target_paths)
    lexical_directory = database_hint.parent / Path(managed_directory)
    lexical_files = frozenset(database_hint.parent / Path(path) for path in managed_files)
    lexical_flags = {target.parent == lexical_directory or target in lexical_files for target in targets}
    if not lexical_flags or lexical_flags == {False}:
        if database_hint.exists():
            database = validated_market_scan_database_path(database_hint)
            _reject_managed_target_aliases(
                targets,
                database.parent / Path(managed_directory),
                frozenset(database.parent / Path(path) for path in managed_files),
            )
        return None
    database = validated_market_scan_database_path(database_hint)
    managed = database.parent / Path(managed_directory)
    exact_files = frozenset(database.parent / Path(path) for path in managed_files)
    _reject_managed_target_aliases(targets, managed, exact_files)
    flags = {target.parent == managed or target in exact_files for target in targets}
    if flags != {True}:
        raise MarketScanArtifactLeaseError("同一 artifact 批次不能混用受管与外部目录")
    return database, targets


def _transfer_publications(
    batch: MarketScanArtifactBatch,
    active: Sequence[MarketScanArtifactBatch],
) -> None:
    for publication in batch.publications:
        target = publication[0]
        for parent in reversed(active):
            if target in parent.targets:
                parent.publications.append(publication)
                break


def _reject_managed_target_aliases(
    targets: Sequence[Path],
    managed: Path,
    exact_files: frozenset[Path],
) -> None:
    managed_resolved = managed.resolve(strict=False)
    for target in targets:
        try:
            parent_resolved = target.parent.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise MarketScanArtifactLeaseError("artifact 发布目录身份无法验证") from exc
        if parent_resolved == managed_resolved and target.parent != managed:
            raise MarketScanArtifactLeaseError("受管 artifact 不能通过目录别名发布")
        if target.parent == managed and parent_resolved != managed_resolved:
            raise MarketScanArtifactLeaseError("受管 artifact 发布目录包含不可信链接")
        _require_exact_file_identity(target, exact_files)
        for exact in exact_files:
            if target.resolve(strict=False) == exact.resolve(strict=False) and target != exact:
                raise MarketScanArtifactLeaseError("受管 artifact 文件不能通过路径别名发布")


def _require_exact_file_identity(target: Path, exact_files: frozenset[Path]) -> None:
    if target not in exact_files:
        return
    try:
        facts = target.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise MarketScanArtifactLeaseError("受管 artifact 文件身份无法验证") from exc
    if not stat.S_ISREG(facts.st_mode):
        raise MarketScanArtifactLeaseError("受管 artifact 文件必须是普通文件")


def validated_market_scan_database_path(database_path: str | Path) -> Path:
    """Reject aliases/hard links so every process derives the same lock identity."""

    path = Path(database_path).expanduser().absolute()
    try:
        facts = path.lstat()
    except OSError as exc:
        raise MarketScanArtifactLeaseError("全市场 artifact 运行库不存在或无法读取") from exc
    if not stat.S_ISREG(facts.st_mode) or path.is_symlink():
        raise MarketScanArtifactLeaseError("全市场 artifact 运行库必须是非链接普通文件")
    if facts.st_nlink != 1:
        raise MarketScanArtifactLeaseError("全市场 artifact 运行库存在硬链接别名")
    return path.resolve(strict=True)


def require_project_managed_artifact_database(
    target_path: str | Path,
    database_path: str | Path | None,
    managed_directory: str | Path,
) -> None:
    try:
        _require_project_binding(target_path, database_path, managed_directory)
    except ManagedArtifactPathError as exc:
        raise MarketScanArtifactLeaseError(str(exc)) from exc


def _lease_state(database: Path) -> _LeaseState:
    with _LEASE_STATES_GUARD:
        return _LEASE_STATES.setdefault(database, _LeaseState())


def require_market_scan_artifact_lease_namespace_current(
    database_path: str | Path,
) -> None:
    database = Path(database_path).expanduser().absolute()
    handle = getattr(_lease_state(database).local, "handle", None)
    if not isinstance(handle, _FileLease):
        raise MarketScanArtifactLeaseError("当前线程未持有 artifact namespace 租约")
    try:
        current = database.parent.stat()
        sidecar = handle.path.lstat()
    except OSError as exc:
        raise MarketScanArtifactLeaseError("artifact namespace 无法复验") from exc
    if (current.st_dev, current.st_ino) != handle.namespace_identity:
        raise MarketScanArtifactLeaseError("artifact namespace 在租约期间被替换")
    if (sidecar.st_dev, sidecar.st_ino) != handle.identity:
        raise MarketScanArtifactLeaseError("artifact sidecar 在租约期间被替换")


def _acquire_file_lease(database: Path) -> _FileLease:
    descriptor: int | None = None
    directory_descriptor: int | None = None
    try:
        lock_root = stable_market_scan_artifact_lock_root()
        lock_path = market_scan_artifact_lock_path(database)
        lock_name = lock_path.name
        directory_descriptor = os.open(
            lock_root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        fcntl.flock(directory_descriptor, fcntl.LOCK_EX)
        descriptor = os.open(
            lock_name,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        facts = os.fstat(descriptor)
        path_facts = os.stat(lock_name, dir_fd=directory_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(facts.st_mode) or facts.st_nlink != 1 or (facts.st_dev, facts.st_ino) != (path_facts.st_dev, path_facts.st_ino):
            raise MarketScanArtifactLeaseError("全市场 artifact 租约 sidecar 身份不可信")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked_path_facts = os.stat(lock_name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (facts.st_dev, facts.st_ino) != (
            locked_path_facts.st_dev,
            locked_path_facts.st_ino,
        ):
            raise MarketScanArtifactLeaseError("全市场 artifact 租约 sidecar 被并发替换")
        namespace = database.parent.stat()
        return _FileLease(
            descriptor,
            directory_descriptor,
            lock_path,
            (facts.st_dev, facts.st_ino),
            (namespace.st_dev, namespace.st_ino),
        )
    except MarketScanArtifactLeaseError:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        raise
    except (OSError, ManagedArtifactPathError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        raise MarketScanArtifactLeaseError("无法取得全市场 artifact 发布/保留租约") from exc


def _release_file_lease(lease: _FileLease) -> None:
    replaced = False
    try:
        try:
            facts = lease.path.lstat()
            replaced = (facts.st_dev, facts.st_ino) != lease.identity
        except OSError:
            replaced = True
        fcntl.flock(lease.sidecar, fcntl.LOCK_UN)
        fcntl.flock(lease.directory, fcntl.LOCK_UN)
    finally:
        os.close(lease.sidecar)
        os.close(lease.directory)
    if replaced:
        raise MarketScanArtifactLeaseError("全市场 artifact 租约 sidecar 在持有期间被替换")


def _normalized_run_ids(
    run_ids: Sequence[int],
    *,
    allow_empty: bool = False,
) -> tuple[int, ...]:
    normalized = tuple(sorted(set(run_ids)))
    if (not normalized and not allow_empty) or any(isinstance(value, bool) or value <= 0 for value in normalized):
        raise MarketScanArtifactLeaseError("受管 artifact 必须绑定正整数 run_id")
    return normalized


def require_restored_market_scan_artifact_bindings(database_path: str | Path) -> None:
    """Fail closed when DB-only restore would orphan managed research evidence."""
    try:
        _require_restored_bindings(database_path)
    except ManagedArtifactPathError as exc:
        raise MarketScanArtifactLeaseError(str(exc)) from exc


def _require_publication_runs(database: Path, run_ids: tuple[int, ...]) -> None:
    if not run_ids:
        return
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=15) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            for run_id in run_ids:
                require_market_scan_action_source(connection, run_id)
    except (sqlite3.Error, ValueError) as exc:
        raise MarketScanArtifactLeaseError("受管 artifact 来源批次无法通过原发布快照复验") from exc
