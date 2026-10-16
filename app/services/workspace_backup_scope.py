"""Explicit source selection and declared SQLite boundaries for offline archives."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
import fcntl
import os
from pathlib import Path
import stat

from app.models.workspace_backup import (
    WorkspaceArchiveRoot, WorkspaceBackupDatabase, WorkspaceBackupExclusion, WorkspaceBackupLimits,
    WorkspaceBackupRoot, WorkspaceBackupSpec,
)
from app.services.workspace_backup_io import BackupIOError, read_regular_prefix, safe_absolute_path, walk_regular
from app.services.workspace_backup_manifest import archive_budget, archive_root_path
from app.services.workspace_storage_policy import storage_exclusion


@dataclass(frozen=True)
class ArchiveSource:
    path: Path
    archive_path: str
    is_directory: bool


@dataclass
class ArchiveSelection:
    roots: list[WorkspaceArchiveRoot] = field(default_factory=list)
    entries: list[ArchiveSource] = field(default_factory=list)
    exclusions: list[WorkspaceBackupExclusion] = field(default_factory=list)
    signatures: dict[str, tuple[int, ...]] = field(default_factory=dict)
    lock_files: list[Path] = field(default_factory=list)


def prepare_workspace_scope(spec: WorkspaceBackupSpec, destination: Path) -> WorkspaceBackupSpec:
    target = safe_absolute_path(destination)
    roots = [root.model_copy(update={"path": safe_absolute_path(root.path)}) for root in spec.roots]
    normalized = WorkspaceBackupSpec.model_validate({**spec.model_dump(), "roots": roots})
    for root in roots:
        if target == root.path or root.path in target.parents or target in root.path.parents:
            raise BackupIOError("archive destination and source roots must be disjoint")
        facts = root.path.stat(follow_symlinks=False)
        expected_kind = stat.S_ISDIR(facts.st_mode) if root.kind == "directory" else stat.S_ISREG(facts.st_mode)
        if not expected_kind or storage_exclusion(root.path, root.category, is_root=True):
            raise BackupIOError("source root is excluded or has a different declared kind")
    return normalized


def database_source(spec: WorkspaceBackupSpec, database: WorkspaceBackupDatabase) -> Path:
    root = next(root for root in spec.roots if root.name == database.root)
    return root.path if root.kind == "file" else root.path / database.relative_path


def _signature(path: Path) -> tuple[int, ...]:
    facts = path.stat(follow_symlinks=False)
    return (facts.st_dev, facts.st_ino, facts.st_mode, facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns)


def _sidecar_paths(spec: WorkspaceBackupSpec) -> set[Path]:
    return {Path(str(database_source(spec, db)) + suffix) for db in spec.databases for suffix in ("-wal", "-shm", "-journal")}


def _selection_exclusion(root: WorkspaceBackupRoot, path: Path, sidecars: set[Path], selection: ArchiveSelection) -> bool:
    reason = "sqlite_sidecar_in_snapshot" if path in sidecars else storage_exclusion(path, root.category, is_root=path == root.path)
    if not reason:
        return False
    if reason == "lock" and path.name in {".writer.lock", ".executor.lock", "sync.lock"}:
        selection.lock_files.append(path)
    if root.category == "runtime":
        raise BackupIOError("sealed runtime contains an excluded member; a complete runtime archive is unavailable")
    relative = str(path.relative_to(root.path)) if root.kind == "directory" else "."
    selection.exclusions.append(WorkspaceBackupExclusion(root=root.name, relative_path=relative, reason=reason))
    return True


def scan_workspace_scope(spec: WorkspaceBackupSpec, limits: WorkspaceBackupLimits, deadline: float) -> ArchiveSelection:
    selection = ArchiveSelection()
    budget, sidecars = archive_budget(limits, deadline), _sidecar_paths(spec)
    for root in spec.roots:
        archive_root = archive_root_path(root)
        selection.roots.append(WorkspaceArchiveRoot(**root.model_dump(), archive_path=archive_root))
        selection.signatures[str(root.path)] = _signature(root.path)
        if root.kind == "directory":
            selection.entries.append(ArchiveSource(root.path, archive_root, True))
        def excluded(path: Path, _is_directory: bool, selected: WorkspaceBackupRoot = root) -> bool:
            return _selection_exclusion(selected, path, sidecars, selection)
        for relative, path, is_directory in walk_regular(root.path, budget, excluded=excluded):
            target = str(Path(archive_root) / relative) if root.kind == "directory" else archive_root
            selection.entries.append(ArchiveSource(path, target, is_directory))
            selection.signatures[str(path)] = _signature(path)
    selection.entries.sort(key=lambda entry: entry.archive_path)
    selection.exclusions.sort(key=lambda entry: (entry.root, entry.relative_path))
    available = {entry.path for entry in selection.entries if not entry.is_directory}
    if not {database_source(spec, db) for db in spec.databases}.issubset(available):
        raise BackupIOError("a declared SQLite database is absent or excluded")
    return selection


def require_declared_sqlite(path: Path) -> None:
    """Reject an undeclared database instead of treating an active DB as ordinary bytes."""
    header = read_regular_prefix(path)
    if header == b"SQLite format 3\x00" or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        raise BackupIOError("SQLite files must be explicitly declared for a consistent snapshot")


@contextmanager
def _existing_domain_lock(path: Path) -> Iterator[None]:
    selected = safe_absolute_path(path)
    descriptor = os.open(selected, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise BackupIOError("archive domain lock is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = safe_absolute_path(path).stat(follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise BackupIOError("archive domain lock changed")
        yield
        current = safe_absolute_path(path).stat(follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise BackupIOError("archive domain lock changed")
    finally:
        os.close(descriptor)


@contextmanager
def workspace_file_leases(paths: Sequence[Path]) -> Iterator[None]:
    """Hold existing declared domain locks; no claim about uncooperative file writers."""
    with ExitStack() as stack:
        for path in sorted(set(paths)):
            stack.enter_context(_existing_domain_lock(path))
        yield


__all__ = [
    "ArchiveSelection", "ArchiveSource", "database_source", "prepare_workspace_scope",
    "require_declared_sqlite", "scan_workspace_scope", "workspace_file_leases",
]
