"""Create, verify and restore explicit offline archives without claiming runnable recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.artifacts.io import canonical_json_bytes, exclusive_atomic_publish, sha256_hex
from app.models.local_data import RuntimeBackupManifest
from app.models.workspace_backup import (
    WorkspaceArchiveDatabase, WorkspaceBackupLimits, WorkspaceBackupManifest, WorkspaceBackupMember, WorkspaceBackupSpec,
)
from app.services.workspace_backup_domains import validate_archive_domains
from app.services.workspace_backup_io import (
    BackupBudget, BackupIOError, copy_regular, create_relative_directories, hash_regular, owned_destination, safe_absolute_path,
)
from app.services.workspace_backup_manifest import (
    MANIFEST_FILENAME, MANIFEST_MAX_BYTES, archive_budget, load_workspace_manifest, remaining_seconds,
    validate_workspace_layout, verify_workspace_members, workspace_archive_signature,
)
from app.services.workspace_backup_progress import BackupProgress, backup_checkpoint, backup_observation
from app.services.workspace_backup_scope import (
    ArchiveSelection, database_source, prepare_workspace_scope, require_declared_sqlite,
    scan_workspace_scope, workspace_file_leases,
)
from app.services.workspace_backup_sqlite import (
    create_database_snapshot, restore_database_snapshot, verify_database_snapshot, workspace_database_leases,
)
from app.utils.audit_time import audit_now_text
from app.utils.clock import monotonic_now


def _domain_validation(root: Path, manifest: WorkspaceBackupManifest, deadline: float) -> dict[str, object]:
    backup_checkpoint("archive_domains")
    roots = [{"name": item.name, "category": item.category, "source_path": str(item.path),
              "archive_path": str(root / item.archive_path)} for item in manifest.roots]
    definitions = {item.name: item for item in manifest.roots}
    databases = []
    for database in manifest.databases:
        source = definitions[database.root].path
        if database.relative_path != ".":
            source /= database.relative_path
        databases.append({"source_path": str(source), "archive_path": str(root / database.archive_path), "role": database.role})
    return validate_archive_domains(roots, databases, timeout_seconds=remaining_seconds(deadline))


def _directories(paths: list[str], explicit: list[str]) -> list[str]:
    result = set(explicit)
    for path in [*paths, *explicit]:
        result.update(str(parent) for parent in Path(path).parents if str(parent) != ".")
    return sorted(result, key=lambda name: (len(Path(name).parts), name))


def _seal_manifest(payload: dict[str, Any]) -> WorkspaceBackupManifest:
    value = {**payload, "digest": sha256_hex(canonical_json_bytes(payload))}
    manifest = WorkspaceBackupManifest.model_validate(value)
    validate_workspace_layout(manifest)
    return manifest


def _manifest_payload(selection: ArchiveSelection, files: list[WorkspaceBackupMember],
                      databases: list[WorkspaceArchiveDatabase]) -> dict[str, Any]:
    return {
        "schema_version": "workspace-backup-manifest-v1", "operation": "backup", "created_at": audit_now_text(),
        "coverage": "explicit_roots_with_reported_exclusions", "consistency": "offline_operator_attested_with_sqlite_write_leases",
        "cross_domain_consistency_verified": False, "resume_eligible": False, "source_backup_digest": None,
        "roots": [root.model_dump(mode="json") for root in selection.roots],
        "databases": [database.model_dump(mode="json") for database in databases],
        "directories": _directories([file.path for file in files], [item.archive_path for item in selection.entries if item.is_directory]),
        "files": [file.model_dump(mode="json") for file in files],
        "exclusions": [item.model_dump(mode="json") for item in selection.exclusions], "domain_validation": {},
    }


def _database_byte_budget(budget: BackupBudget) -> int:
    budget.check()
    if budget.files >= budget.max_files:
        raise BackupIOError("archive file count budget exhausted before SQLite copy")
    remaining = min(budget.max_file_bytes, budget.max_bytes - budget.bytes)
    if remaining <= 0:
        raise BackupIOError("archive byte budget exhausted before SQLite copy")
    return remaining


def _copy_sources(spec: WorkspaceBackupSpec, selection: ArchiveSelection, target: Path,
                  limits: WorkspaceBackupLimits, deadline: float) -> tuple[list[WorkspaceBackupMember], list[WorkspaceArchiveDatabase]]:
    definitions = {database_source(spec, db): db for db in spec.databases}
    files, databases = [], []
    budget = archive_budget(limits, deadline)
    names = [item.archive_path for item in selection.entries]
    create_relative_directories(target, _directories(names, [item.archive_path for item in selection.entries if item.is_directory]))
    for entry in selection.entries:
        if entry.is_directory:
            continue
        backup_checkpoint("source_copy")
        destination = target / entry.archive_path
        database = definitions.get(entry.path)
        if database is None:
            facts = copy_regular(entry.path, destination, budget)
            require_declared_sqlite(destination)
        else:
            value = create_database_snapshot(entry.path, destination, timeout_seconds=remaining_seconds(deadline),
                                             max_bytes=_database_byte_budget(budget))
            facts = hash_regular(destination, budget)
            databases.append(WorkspaceArchiveDatabase(**database.model_dump(), archive_path=entry.archive_path,
                                                      manifest=RuntimeBackupManifest.model_validate(value)))
        files.append(WorkspaceBackupMember(path=entry.archive_path, **facts))
    return files, databases


def _publish_manifest(target: Path, manifest: WorkspaceBackupManifest) -> None:
    if not exclusive_atomic_publish(target / MANIFEST_FILENAME, canonical_json_bytes(manifest.model_dump(mode="json")),
                                    max_bytes=MANIFEST_MAX_BYTES):
        raise BackupIOError("archive completion manifest already exists")


def _verify_archive(target: Path, limits: WorkspaceBackupLimits, deadline: float,
                    expected_digest: str | None = None, *,
                    candidate: WorkspaceBackupManifest | None = None) -> tuple[WorkspaceBackupManifest, dict[str, object]]:
    backup_checkpoint("archive_manifest")
    manifest = candidate if candidate is not None else load_workspace_manifest(target, expected_digest)
    backup_checkpoint("archive_members")
    signature = verify_workspace_members(target, manifest, limits, deadline, published=candidate is None)
    declared = {database.archive_path for database in manifest.databases}
    for member in manifest.files:
        if member.path not in declared:
            require_declared_sqlite(target / member.path)
    for database in manifest.databases:
        verify_database_snapshot(target / database.archive_path, database.manifest.model_dump(mode="json"),
                                 timeout_seconds=remaining_seconds(deadline))
    domains = _domain_validation(target, manifest, deadline)
    backup_checkpoint("archive_recheck")
    if candidate is None and load_workspace_manifest(target, manifest.digest) != manifest:
        raise BackupIOError("archive manifest changed during verification")
    if workspace_archive_signature(target, limits, deadline) != signature:
        raise BackupIOError("archive members changed during verification")
    return manifest, domains


def _finish_archive(target: Path, payload: dict[str, Any], limits: WorkspaceBackupLimits,
                    deadline: float) -> tuple[WorkspaceBackupManifest, dict[str, object]]:
    backup_checkpoint("archive_members")
    signature = workspace_archive_signature(target, limits, deadline)
    _candidate, domains = _verify_archive(target, limits, deadline, candidate=_seal_manifest(payload))
    payload["domain_validation"] = domains
    manifest = _seal_manifest(payload)
    backup_checkpoint("archive_publish")
    if workspace_archive_signature(target, limits, deadline) != signature:
        raise BackupIOError("archive members changed before completion publication")
    remaining_seconds(deadline)
    _publish_manifest(target, manifest)
    if load_workspace_manifest(target, manifest.digest) != manifest:
        raise BackupIOError("archive completion manifest changed during publication")
    return manifest, domains


def _result(operation: str, target: Path, manifest: WorkspaceBackupManifest, domains: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "workspace-backup-result-v1", "operation": operation, "archive_path": str(target),
        "archive_verified": True, "digest": manifest.digest, "file_count": len(manifest.files),
        "size_bytes": sum(file.size_bytes for file in manifest.files), "excluded_count": len(manifest.exclusions),
        "consistency": manifest.consistency, "cross_domain_consistency_verified": False, "resume_eligible": False,
        "roots": [{"name": root.name, "restored_path": str(target / root.archive_path)} for root in manifest.roots],
        "domain_validation": domains,
        "verified_at": audit_now_text(),
    }


def create_workspace_backup(spec: WorkspaceBackupSpec, destination: Path, *,
                            limits: WorkspaceBackupLimits | None = None, progress: BackupProgress | None = None) -> dict[str, object]:
    with backup_observation(progress, "create"):
        result = _create_workspace_backup(spec, destination, limits or WorkspaceBackupLimits())
        backup_checkpoint("completed")
        return result


def _create_workspace_backup(spec: WorkspaceBackupSpec, destination: Path, limits: WorkspaceBackupLimits) -> dict[str, object]:
    deadline = monotonic_now() + limits.max_seconds
    spec = prepare_workspace_scope(spec, destination)
    paths = [database_source(spec, db) for db in spec.databases]
    primary = database_source(spec, next(db for db in spec.databases if db.role == "primary"))
    with owned_destination(destination) as target:
        backup_checkpoint("source_leases")
        with workspace_database_leases(paths, primary) as lease:
            backup_checkpoint("source_scan")
            initial = scan_workspace_scope(spec, limits, deadline)
            with workspace_file_leases(initial.lock_files):
                selection = scan_workspace_scope(spec, limits, deadline)
                files, databases = _copy_sources(spec, selection, target, limits, deadline)
                payload = _manifest_payload(selection, files, databases)
                backup_checkpoint("source_recheck")
                if scan_workspace_scope(spec, limits, deadline).signatures != selection.signatures:
                    raise BackupIOError("source files changed while creating the archive")
                lease.validate()
        backup_checkpoint("source_leases_released")
        manifest, domains = _finish_archive(target, payload, limits, deadline)
    return _result("create", target, manifest, domains)


def verify_workspace_backup(directory: Path, *, expected_digest: str | None = None,
                            limits: WorkspaceBackupLimits | None = None, progress: BackupProgress | None = None) -> dict[str, object]:
    with backup_observation(progress, "verify"):
        limits = limits or WorkspaceBackupLimits()
        target = safe_absolute_path(directory)
        manifest, domains = _verify_archive(target, limits, monotonic_now() + limits.max_seconds, expected_digest)
        backup_checkpoint("completed")
        return _result("verify", target, manifest, domains)


def _restore_destination(source: Path, destination: Path, manifest: WorkspaceBackupManifest) -> Path:
    target = safe_absolute_path(destination)
    protected = [source, *(root.path for root in manifest.roots)]
    if any(target == path or path in target.parents or target in path.parents for path in protected):
        raise BackupIOError("restore destination overlaps the archive or an original source root")
    return target


def _restore_payload(source: Path, target: Path, manifest: WorkspaceBackupManifest,
                     limits: WorkspaceBackupLimits, deadline: float) -> tuple[list[WorkspaceBackupMember], list[WorkspaceArchiveDatabase]]:
    create_relative_directories(target, manifest.directories)
    definitions = {database.archive_path: database for database in manifest.databases}
    files, databases = [], []
    budget = archive_budget(limits, deadline)
    for member in manifest.files:
        backup_checkpoint("source_copy")
        database = definitions.get(member.path)
        if database is None:
            facts = copy_regular(source / member.path, target / member.path, budget)
            if (facts["sha256"], facts["size_bytes"], facts["mode"]) != (member.sha256, member.size_bytes, member.mode):
                raise BackupIOError("archive member changed between verification and copying")
        else:
            value = restore_database_snapshot(source / member.path, target / member.path,
                database.manifest.model_dump(mode="json"), primary=database.role == "primary",
                timeout_seconds=remaining_seconds(deadline), max_bytes=_database_byte_budget(budget))
            facts = hash_regular(target / member.path, budget)
            databases.append(database.model_copy(update={"manifest": RuntimeBackupManifest.model_validate(value)}))
        files.append(WorkspaceBackupMember(path=member.path, **facts))
    return files, databases


def restore_workspace_backup(directory: Path, destination: Path, *, expected_digest: str | None = None,
                             limits: WorkspaceBackupLimits | None = None, progress: BackupProgress | None = None) -> dict[str, object]:
    with backup_observation(progress, "restore"):
        result = _restore_workspace_backup(directory, destination, expected_digest, limits or WorkspaceBackupLimits())
        backup_checkpoint("completed")
        return result


def _restore_workspace_backup(directory: Path, destination: Path, expected_digest: str | None,
                              limits: WorkspaceBackupLimits) -> dict[str, object]:
    deadline = monotonic_now() + limits.max_seconds
    source = safe_absolute_path(directory)
    original, _domains = _verify_archive(source, limits, deadline, expected_digest)
    target = _restore_destination(source, destination, original)
    with owned_destination(target):
        files, databases = _restore_payload(source, target, original, limits, deadline)
        payload = original.model_dump(mode="json", exclude={"digest"})
        payload.update(operation="restore", created_at=audit_now_text(), source_backup_digest=original.digest,
                       files=[file.model_dump(mode="json") for file in files],
                       databases=[database.model_dump(mode="json") for database in databases])
        load_workspace_manifest(source, original.digest)
        manifest, domains = _finish_archive(target, payload, limits, deadline)
    return _result("restore", target, manifest, domains)


__all__ = ["create_workspace_backup", "restore_workspace_backup", "verify_workspace_backup"]
