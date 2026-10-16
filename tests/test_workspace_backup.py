"""Real temporary databases and archive trees exercise the complete offline workflow."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.db.alert_stream import ensure_alert_stream_state, read_alert_stream_state
from app.models.workspace_backup import WorkspaceBackupLimits, WorkspaceBackupSpec
from app.services import workspace_backup as archive
from app.services.workspace_backup_io import BackupIOError
from app.services.workspace_backup_manifest import MANIFEST_FILENAME
from app.services.runtime_backup import RuntimeBackupError


@pytest.fixture
def archive_sources(tmp_path):
    source = tmp_path / "sources"
    source.mkdir()
    for name in ("primary.sqlite3", "research.sqlite3"):
        connection = sqlite3.connect(source / name)
        try:
            with connection:
                connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO evidence VALUES(1, 'preserved')")
                if name == "primary.sqlite3":
                    ensure_alert_stream_state(connection)
        finally:
            connection.close()
    (source / "report.txt").write_text("reproducible research\n")
    (source / "empty").mkdir()
    (source / ".env").write_text("PRIVATE_DO_NOT_ARCHIVE=secret")
    spec = WorkspaceBackupSpec.model_validate({
        "schema_version": "workspace-backup-scope-v1", "offline_writers_stopped": True,
        "roots": [{"name": "data", "category": "research", "path": str(source), "kind": "directory"}],
        "databases": [{"root": "data", "relative_path": "primary.sqlite3", "role": "primary"},
                      {"root": "data", "relative_path": "research.sqlite3", "role": "auxiliary"}],
    })
    return source, spec


def _tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _reseal(directory, change):
    path = directory / MANIFEST_FILENAME
    manifest = json.loads(path.read_bytes())
    change(manifest)
    manifest.pop("digest")
    manifest["digest"] = sha256_hex(canonical_json_bytes(manifest))
    path.write_bytes(canonical_json_bytes(manifest))
    return manifest


def test_backup_is_independently_restorable_after_all_original_sources_are_removed(archive_sources, tmp_path):
    source, spec = archive_sources
    connection = sqlite3.connect(source / "primary.sqlite3")
    old_stream = read_alert_stream_state(connection)
    connection.close()
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    created = archive.create_workspace_backup(spec, backup)
    assert created["archive_verified"] and not created["cross_domain_consistency_verified"]
    assert not created["resume_eligible"] and created["excluded_count"] >= 1
    assert not (backup / "payload/data/.env").exists()
    before = _tree_bytes(backup)
    shutil.rmtree(source)
    verified = archive.verify_workspace_backup(backup, expected_digest=created["digest"])
    result = archive.restore_workspace_backup(backup, restored, expected_digest=verified["digest"])
    assert result["operation"] == "restore" and result["archive_verified"] and not result["resume_eligible"]
    assert _tree_bytes(backup) == before
    assert (restored / "payload/data/report.txt").read_text() == "reproducible research\n"
    assert (restored / "payload/data/empty").is_dir()
    assert (restored / "payload/data/research.sqlite3").read_bytes() == before["payload/data/research.sqlite3"]
    connection = sqlite3.connect(restored / "payload/data/primary.sqlite3")
    try:
        assert read_alert_stream_state(connection) != old_stream
        assert connection.execute("SELECT * FROM evidence").fetchall() == [(1, "preserved")]
    finally:
        connection.close()
    assert archive.verify_workspace_backup(restored)["digest"] == result["digest"]


def test_verify_is_read_only_and_does_not_create_sqlite_sidecars(archive_sources, tmp_path):
    _source, spec = archive_sources
    target = tmp_path / "backup"
    archive.create_workspace_backup(spec, target)
    before = _tree_bytes(target)
    archive.verify_workspace_backup(target)
    assert _tree_bytes(target) == before
    assert not any(path.name.endswith(("-wal", "-shm", "-journal", ".lock")) for path in target.rglob("*"))


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_existing_empty_destinations_are_not_overwritten(archive_sources, tmp_path, operation):
    _source, spec = archive_sources
    backup, target = tmp_path / "backup", tmp_path / "occupied"
    archive.create_workspace_backup(spec, backup)
    target.mkdir()
    identity = target.stat().st_ino
    with pytest.raises(BackupIOError):
        if operation == "create":
            archive.create_workspace_backup(spec, target)
        else:
            archive.restore_workspace_backup(backup, target)
    assert target.stat().st_ino == identity and list(target.iterdir()) == []


@pytest.mark.parametrize("change", ["corrupt", "missing", "extra", "symlink", "mode"])
def test_payload_damage_or_unlisted_content_is_rejected(archive_sources, tmp_path, change):
    _source, spec = archive_sources
    target = tmp_path / "backup"
    archive.create_workspace_backup(spec, target)
    member = target / "payload/data/report.txt"
    if change == "corrupt":
        member.write_text("tampered")
    elif change == "missing":
        member.unlink()
    elif change == "extra":
        (target / "unlisted").write_text("not declared")
    elif change == "symlink":
        member.unlink()
        member.symlink_to(tmp_path / "outside")
    else:
        member.chmod(0o700)
    with pytest.raises((BackupIOError, OSError)):
        archive.verify_workspace_backup(target)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "payload//bad", "payload/data/../bad", "payload\\bad"])
def test_resealed_manifest_cannot_escape_destination(archive_sources, tmp_path, path):
    _source, spec = archive_sources
    target, restore = tmp_path / "backup", tmp_path / "restore"
    archive.create_workspace_backup(spec, target)
    _reseal(target, lambda value: value["files"][0].update(path=path))
    with pytest.raises(ValueError):
        archive.restore_workspace_backup(target, restore)
    assert not restore.exists()


def test_resealed_manifest_cannot_remove_a_sqlite_declaration(archive_sources, tmp_path):
    _source, spec = archive_sources
    target = tmp_path / "backup"
    archive.create_workspace_backup(spec, target)
    _reseal(target, lambda value: value.update(databases=[row for row in value["databases"] if row["role"] == "primary"]))
    with pytest.raises(BackupIOError, match="explicitly declared"):
        archive.verify_workspace_backup(target)


def test_verify_reports_recomputed_domain_results_instead_of_self_asserted_success(archive_sources, tmp_path):
    _source, spec = archive_sources
    target = tmp_path / "backup"
    archive.create_workspace_backup(spec, target)
    _reseal(target, lambda value: value.update(domain_validation={"resume_eligible": True, "runtime_execution_verified": True}))
    report = archive.verify_workspace_backup(target)
    assert report["domain_validation"]["resume_eligible"] is False
    assert report["domain_validation"]["runtime_execution_verified"] is False


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_scope_requires_explicit_boolean_offline_attestation(archive_sources, value):
    _source, spec = archive_sources
    with pytest.raises(ValueError):
        WorkspaceBackupSpec.model_validate({**spec.model_dump(), "offline_writers_stopped": value})


def test_undeclared_sqlite_with_unusual_extension_cannot_be_copied_raw(archive_sources, tmp_path):
    source, spec = archive_sources
    shutil.copyfile(source / "research.sqlite3", source / "undisclosed.bin")
    target = tmp_path / "backup"
    with pytest.raises(BackupIOError, match="explicitly declared"):
        archive.create_workspace_backup(spec, target)
    assert not target.exists()


def test_active_existing_domain_lease_rejects_create_and_releases_database_guards(archive_sources, tmp_path):
    source, spec = archive_sources
    descriptor = os.open(source / ".writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(BackupIOError):
            archive.create_workspace_backup(spec, tmp_path / "backup")
    finally:
        os.close(descriptor)
    assert not (tmp_path / "backup").exists()
    assert archive.create_workspace_backup(spec, tmp_path / "after-release")["archive_verified"]


@pytest.mark.parametrize("failure", [OSError("disk full"), KeyboardInterrupt()])
def test_failure_before_completion_marker_cleans_owned_output(archive_sources, tmp_path, monkeypatch, failure):
    _source, spec = archive_sources
    def failed(*_args):
        raise failure
    monkeypatch.setattr(archive, "_publish_manifest", failed)
    target = tmp_path / "backup"
    with pytest.raises(BackupIOError if isinstance(failure, OSError) else type(failure)):
        archive.create_workspace_backup(spec, target)
    assert not target.exists()


def test_final_verification_failure_never_publishes_completion_marker(archive_sources, tmp_path, monkeypatch):
    _source, spec = archive_sources
    def failed(*_args, **_kwargs):
        assert not (target / MANIFEST_FILENAME).exists()
        raise BackupIOError("injected final failure")
    monkeypatch.setattr(archive, "_verify_archive", failed)
    target = tmp_path / "backup"
    with pytest.raises(BackupIOError):
        archive.create_workspace_backup(spec, target)
    assert not target.exists()


def test_late_source_change_during_backup_is_rejected(archive_sources, tmp_path, monkeypatch):
    source, spec = archive_sources
    original = archive._copy_sources
    def changed(*args):
        result = original(*args)
        (source / "report.txt").write_text("changed after copying")
        return result
    monkeypatch.setattr(archive, "_copy_sources", changed)
    with pytest.raises(BackupIOError, match="source files changed"):
        archive.create_workspace_backup(spec, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_source_replaced_after_restore_preverification_cannot_install_wrong_bytes(archive_sources, tmp_path, monkeypatch):
    _source, spec = archive_sources
    target, restored = tmp_path / "backup", tmp_path / "restored"
    archive.create_workspace_backup(spec, target)
    original = archive._restore_payload
    def changed(*args):
        (target / "payload/data/report.txt").write_text("raced after verification")
        return original(*args)
    monkeypatch.setattr(archive, "_restore_payload", changed)
    with pytest.raises(BackupIOError, match="changed between"):
        archive.restore_workspace_backup(target, restored)
    assert not restored.exists()


def test_external_pin_rejects_resealed_archive_before_restore_output(archive_sources, tmp_path):
    _source, spec = archive_sources
    target = tmp_path / "backup"
    result = archive.create_workspace_backup(spec, target)
    _reseal(target, lambda value: value.update(created_at="2026-10-15T00:00:00+00:00"))
    with pytest.raises(BackupIOError, match="expected digest"):
        archive.restore_workspace_backup(target, tmp_path / "restored", expected_digest=result["digest"])
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("limit", [{"max_bytes": 1}, {"max_file_bytes": 1}, {"max_files": 1}, {"max_depth": 0}])
def test_insufficient_budget_never_publishes_a_complete_archive(archive_sources, tmp_path, limit):
    _source, spec = archive_sources
    with pytest.raises((BackupIOError, RuntimeBackupError)):
        archive.create_workspace_backup(spec, tmp_path / "backup", limits=WorkspaceBackupLimits(**limit))
    assert not (tmp_path / "backup").exists()


def test_destination_inside_source_is_rejected_before_writing(archive_sources):
    source, spec = archive_sources
    with pytest.raises(BackupIOError, match="disjoint"):
        archive.create_workspace_backup(spec, source / "nested-backup")
    assert not (source / "nested-backup").exists()


def test_explicit_file_roots_restore_their_original_names(archive_sources, tmp_path):
    source, _spec = archive_sources
    spec = WorkspaceBackupSpec.model_validate({
        "schema_version": "workspace-backup-scope-v1", "offline_writers_stopped": True,
        "roots": [{"name": "primary", "category": "database", "path": str(source / "primary.sqlite3"), "kind": "file"},
                  {"name": "evidence", "category": "research", "path": str(source / "report.txt"), "kind": "file"}],
        "databases": [{"root": "primary", "relative_path": ".", "role": "primary"}],
    })
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    archive.create_workspace_backup(spec, backup)
    shutil.rmtree(source)
    archive.restore_workspace_backup(backup, restored)
    assert (restored / "payload/primary/primary.sqlite3").is_file()
    assert (restored / "payload/evidence/report.txt").read_text() == "reproducible research\n"


@pytest.mark.parametrize("change", ["replace", "extra", "remove"])
def test_verification_rejects_payload_change_after_initial_hashes(archive_sources, tmp_path, monkeypatch, change):
    _source, spec = archive_sources
    target = tmp_path / "backup"
    archive.create_workspace_backup(spec, target)
    original = archive._domain_validation
    def changed(*args):
        result = original(*args)
        member = target / "payload/data/report.txt"
        if change == "replace":
            replacement = target / "replacement"
            replacement.write_bytes(member.read_bytes())
            replacement.replace(member)
        elif change == "extra":
            (target / "new-member").write_text("late member")
        else:
            member.unlink()
        return result
    monkeypatch.setattr(archive, "_domain_validation", changed)
    with pytest.raises(BackupIOError, match="changed during verification"):
        archive.verify_workspace_backup(target)


@pytest.mark.parametrize("change", [
    lambda value: value["files"].append(value["files"][0]),
    lambda value: value["directories"].append(value["directories"][0]),
    lambda value: value["directories"].remove("payload"),
    lambda value: value["directories"].append("unrelated"),
    lambda value: value["directories"].append("payload/data/report.txt/child"),
    lambda value: value["files"].append({**value["files"][-1], "path": value["files"][-1]["path"].upper()}),
    lambda value: value["roots"][0].update(archive_path="payload/wrong"),
    lambda value: value["databases"][0].update(archive_path="payload/data/report.txt"),
    lambda value: value["databases"][0]["manifest"].update(sha256="0" * 64),
    lambda value: value["databases"][0]["manifest"].update(database_file="incorrect.sqlite3"),
    lambda value: value.update(source_backup_digest="0" * 64),
    lambda value: value.update(created_at="2026-10-15T12:00:00"),
])
def test_resealed_inconsistent_layout_is_rejected_before_restore_output(archive_sources, tmp_path, change):
    _source, spec = archive_sources
    target, restored = tmp_path / "backup", tmp_path / "restored"
    archive.create_workspace_backup(spec, target)
    _reseal(target, change)
    with pytest.raises(ValueError):
        archive.restore_workspace_backup(target, restored)
    assert not restored.exists()


def test_exhausted_file_budget_rejects_before_starting_sqlite_copy(archive_sources, tmp_path, monkeypatch):
    _source, spec = archive_sources
    def unexpected(*_args, **_kwargs):
        pytest.fail("SQLite snapshot must not start with an exhausted scope budget")
    monkeypatch.setattr(archive, "create_database_snapshot", unexpected)
    with pytest.raises(BackupIOError):
        archive.create_workspace_backup(spec, tmp_path / "backup", limits=WorkspaceBackupLimits(max_files=1))
    assert not (tmp_path / "backup").exists()
