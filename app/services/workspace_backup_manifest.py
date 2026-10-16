"""Bounded archive manifest identity, layout and payload verification."""

from __future__ import annotations

from pathlib import Path

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, read_regular_file, sha256_hex
from app.models.workspace_backup import (
    WorkspaceArchiveRoot, WorkspaceBackupDatabase, WorkspaceBackupLimits, WorkspaceBackupManifest, WorkspaceBackupMember,
    WorkspaceBackupRoot, WorkspaceBackupSpec,
)
from app.services.market_scan_trial_registry_contract import registry_timestamp
from app.services.workspace_backup_io import BackupBudget, BackupIOError, hash_regular, safe_absolute_path, walk_regular
from app.services.workspace_backup_progress import backup_checkpoint
from app.utils.clock import monotonic_now


MANIFEST_FILENAME = "workspace-manifest.json"
MANIFEST_MAX_BYTES = 16 * 1024 * 1024


def remaining_seconds(deadline: float) -> float:
    remaining = deadline - monotonic_now()
    if remaining <= 0:
        raise BackupIOError("workspace archive time budget exhausted")
    return remaining


def archive_budget(limits: WorkspaceBackupLimits, deadline: float) -> BackupBudget:
    return BackupBudget(**{**limits.model_dump(), "max_seconds": remaining_seconds(deadline)})


def archive_root_path(root: WorkspaceBackupRoot) -> str:
    prefix = Path("payload") / root.name
    return str(prefix / root.path.name if root.kind == "file" else prefix)


def load_workspace_manifest(directory: Path, expected_digest: str | None = None) -> WorkspaceBackupManifest:
    root = safe_absolute_path(directory)
    decoded = decode_json_bytes(read_regular_file(root / MANIFEST_FILENAME, max_bytes=MANIFEST_MAX_BYTES))
    manifest = WorkspaceBackupManifest.model_validate(decoded)
    payload = manifest.model_dump(mode="json", exclude={"digest"})
    if manifest.digest != sha256_hex(canonical_json_bytes(payload)):
        raise BackupIOError("workspace manifest digest mismatch")
    if expected_digest is not None and manifest.digest != expected_digest:
        raise BackupIOError("workspace archive does not match the expected digest")
    validate_workspace_layout(manifest)
    return manifest


def validate_workspace_layout(manifest: WorkspaceBackupManifest) -> None:
    registry_timestamp(manifest.created_at, "archive timestamp")
    spec = WorkspaceBackupSpec(schema_version="workspace-backup-scope-v1", offline_writers_stopped=True,
        roots=[WorkspaceBackupRoot.model_validate(root.model_dump(exclude={"archive_path"})) for root in manifest.roots],
        databases=[WorkspaceBackupDatabase.model_validate(db.model_dump(exclude={"archive_path", "manifest"})) for db in manifest.databases])
    if (manifest.operation == "restore") != (manifest.source_backup_digest is not None):
        raise BackupIOError("archive provenance does not match its operation")
    files = {entry.path: entry for entry in manifest.files}
    directories = set(manifest.directories)
    names = [*files, *directories]
    if len(files) != len(manifest.files) or len(directories) != len(manifest.directories):
        raise BackupIOError("archive manifest contains duplicate members")
    if len({name.casefold() for name in names}) != len(names):
        raise BackupIOError("archive paths collide across files or case-insensitive filesystems")
    if any(str(parent) in files for name in names for parent in Path(name).parents):
        raise BackupIOError("archive file is also a directory ancestor")
    _validate_root_members(manifest, files, directories)
    _validate_database_members(manifest, spec, files)


def _root_contains(root: WorkspaceArchiveRoot, name: str) -> bool:
    return name == root.archive_path or root.kind == "directory" and Path(root.archive_path) in Path(name).parents


def _required_directories(names: list[str]) -> set[str]:
    return {str(parent) for name in names for parent in Path(name).parents if str(parent) != "."}


def _validate_root_members(manifest: WorkspaceBackupManifest, files: dict[str, WorkspaceBackupMember], directories: set[str]) -> None:
    roots = {root.archive_path: root for root in manifest.roots}
    for root in manifest.roots:
        if root.archive_path != archive_root_path(root):
            raise BackupIOError("archive root layout is not canonical")
        if root.archive_path not in (files if root.kind == "file" else directories):
            raise BackupIOError("declared archive root is absent")
    for name in files:
        if not any(_root_contains(root, name) for root in roots.values()):
            raise BackupIOError("archive file is outside the explicit roots")
    required = _required_directories([*files, *roots])
    allowed = required | _contained_directories(manifest.roots, directories)
    if not required.issubset(directories) or directories != allowed:
        raise BackupIOError("archive directory layout is incomplete or contains unrelated paths")


def _contained_directories(roots: list[WorkspaceArchiveRoot], directories: set[str]) -> set[str]:
    return {name for name in directories if any(root.kind == "directory" and _root_contains(root, name) for root in roots)}


def _validate_database_members(manifest: WorkspaceBackupManifest, spec: WorkspaceBackupSpec, files: dict[str, WorkspaceBackupMember]) -> None:
    roots = {root.name: root for root in spec.roots}
    for database in manifest.databases:
        root = roots[database.root]
        expected = archive_root_path(root)
        if root.kind == "directory":
            expected += "/" + database.relative_path
        member = files.get(expected)
        if database.archive_path != expected or member is None:
            raise BackupIOError("declared SQLite snapshot is absent or misplaced")
        if (database.manifest.sha256, database.manifest.database_size_bytes) != (member.sha256, member.size_bytes):
            raise BackupIOError("SQLite snapshot and member metadata disagree")
        if database.manifest.database_file != Path(expected).name:
            raise BackupIOError("SQLite manifest filename differs from its payload")


def _member_signature(path: Path, is_directory: bool) -> tuple[bool, tuple[int, ...]]:
    facts = path.stat(follow_symlinks=False)
    return is_directory, (facts.st_dev, facts.st_ino, facts.st_mode, facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns)


def workspace_archive_signature(directory: Path, limits: WorkspaceBackupLimits,
                                deadline: float) -> dict[str, tuple[bool, tuple[int, ...]]]:
    budget = archive_budget(limits, deadline)
    signature = {".": _member_signature(directory, True)}
    for relative, path, is_directory in walk_regular(directory, budget):
        signature[relative.as_posix()] = _member_signature(path, is_directory)
    return signature


def verify_workspace_members(directory: Path, manifest: WorkspaceBackupManifest, limits: WorkspaceBackupLimits,
                             deadline: float, *, published: bool = True) -> dict[str, tuple[bool, tuple[int, ...]]]:
    signature = workspace_archive_signature(directory, limits, deadline)
    if not published and len(signature) + 1 > limits.max_files:
        raise BackupIOError("archive file count budget must include its completion manifest")
    actual_files = {name for name, (is_directory, _facts) in signature.items() if not is_directory}
    actual_directories = {name for name, (is_directory, _facts) in signature.items() if is_directory and name != "."}
    expected_files = {item.path for item in manifest.files}
    if published:
        expected_files.add(MANIFEST_FILENAME)
    if actual_files != expected_files or actual_directories != set(manifest.directories):
        raise BackupIOError("archive has missing or unlisted members")
    contents = archive_budget(limits, deadline)
    for member in manifest.files:
        backup_checkpoint("archive_members")
        facts = hash_regular(directory / member.path, contents)
        if (facts["size_bytes"], facts["sha256"], facts["mode"]) != (member.size_bytes, member.sha256, member.mode):
            raise BackupIOError("archive payload metadata or digest mismatch")
    return signature


__all__ = [
    "MANIFEST_FILENAME", "MANIFEST_MAX_BYTES", "archive_budget", "archive_root_path", "load_workspace_manifest",
    "remaining_seconds", "validate_workspace_layout", "verify_workspace_members", "workspace_archive_signature",
]
