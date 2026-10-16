"""Observe real temporary archives without weakening their publication boundaries."""

from __future__ import annotations

from contextlib import closing, contextmanager
import fcntl
import json
import os
import shutil
import sqlite3
import time

import pytest

from app.models.workspace_backup import WorkspaceBackupLimits
from app.services import workspace_backup as archive
from app.services import workspace_backup_progress as telemetry
from app.services import workspace_backup_sqlite as database
from app.services.workspace_backup_io import BackupIOError
from app.services.workspace_backup_manifest import MANIFEST_FILENAME, load_workspace_manifest, verify_workspace_members
from tests import test_workspace_backup as backup_tests


archive_sources = backup_tests.archive_sources


def test_source_release_signal_allows_writers_before_target_verification(archive_sources, tmp_path):
    source, spec = archive_sources
    lock_path = source / ".writer.lock"
    lock_path.touch()
    target, events = tmp_path / "backup", []

    def observe(event):
        events.append(event)
        if event["stage"] == "source_recheck":
            with closing(sqlite3.connect(source / "primary.sqlite3", timeout=0)) as connection:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    connection.execute("BEGIN IMMEDIATE")
        if event["stage"] != "source_leases_released":
            return
        assert not (target / MANIFEST_FILENAME).exists()
        with closing(sqlite3.connect(source / "primary.sqlite3", timeout=0)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE evidence SET value='after capture'")
            connection.commit()
        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        (source / "report.txt").unlink()

    result = archive.create_workspace_backup(spec, target, progress=telemetry.BackupProgress(observe))
    assert result["archive_verified"]
    assert (target / "payload/data/report.txt").read_text() == "reproducible research\n"
    with closing(sqlite3.connect(target / "payload/data/primary.sqlite3")) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == ("preserved",)
    stages = [event["stage"] for event in events]
    assert stages.index("source_recheck") < stages.index("source_leases_released") < stages.index("archive_domains")
    assert stages.index("archive_recheck") < stages.index("archive_publish") < stages.index("completed")


def test_failed_source_lease_exit_never_announces_release_or_publishes(archive_sources, tmp_path, monkeypatch):
    _source, spec = archive_sources
    original, events = archive.workspace_database_leases, []

    @contextmanager
    def failed_exit(*args):
        with original(*args) as lease:
            yield lease
        raise OSError("injected release failure")

    monkeypatch.setattr(archive, "workspace_database_leases", failed_exit)
    target = tmp_path / "backup"
    with pytest.raises(BackupIOError):
        archive.create_workspace_backup(spec, target, progress=telemetry.BackupProgress(events.append))
    assert not target.exists()
    assert all(not event["source_leases_released"] for event in events)


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_final_domain_failure_leaves_no_completion_marker(archive_sources, tmp_path, monkeypatch, operation):
    _source, spec = archive_sources
    source, target = tmp_path / "backup", tmp_path / "output"
    archive.create_workspace_backup(spec, source)
    original = archive._domain_validation
    seen = []

    def reject(directory, manifest, deadline):
        if directory == target:
            seen.append(directory)
            assert not (target / MANIFEST_FILENAME).exists()
            raise BackupIOError("PRIVATE failure details")
        return original(directory, manifest, deadline)

    monkeypatch.setattr(archive, "_domain_validation", reject)
    events = []
    progress = telemetry.BackupProgress(events.append)
    with pytest.raises(BackupIOError):
        if operation == "create":
            archive.create_workspace_backup(spec, target, progress=progress)
        else:
            archive.restore_workspace_backup(source, target, progress=progress)
    assert seen == [target] and not target.exists()
    assert all(event["stage"] != "completed" for event in events)
    assert "PRIVATE" not in json.dumps(events)


def test_cancellation_after_releasing_sources_cleans_output_and_resets_observer(archive_sources, tmp_path):
    source, spec = archive_sources
    events = []

    def cancel(event):
        events.append(event)
        if event["stage"] == "source_leases_released":
            raise KeyboardInterrupt()

    target = tmp_path / "cancelled"
    with pytest.raises(KeyboardInterrupt):
        archive.create_workspace_backup(spec, target, progress=telemetry.BackupProgress(cancel))
    assert not target.exists()
    with closing(sqlite3.connect(source / "primary.sqlite3", timeout=0)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
    observed = list(events)
    archive.create_workspace_backup(spec, tmp_path / "retry")
    assert events == observed


def test_progress_throttles_unknown_work_without_inventing_a_percentage(monkeypatch):
    clock, events = [10.0], []
    monkeypatch.setattr(telemetry, "monotonic_now", lambda: clock[0])
    progress = telemetry.BackupProgress(events.append, interval_seconds=2)
    with telemetry.backup_observation(progress, "verify"):
        telemetry.backup_checkpoint("sqlite_integrity")
        for value in (10.1, 10.5, 11.9):
            clock[0] = value
            telemetry.backup_checkpoint()
        assert len(events) == 2
        clock[0] = 12.0
        telemetry.backup_checkpoint()
        assert events[-1]["event"] == "heartbeat"
        assert events[-1]["stage_elapsed_seconds"] == 2.0
        assert "total" not in events[-1] and "percent" not in events[-1]
        telemetry.backup_checkpoint("sqlite_copy", current=10, total=20, unit="bytes")
        assert events[-1]["current"] == 10 and events[-1]["total"] == 20
    before = list(events)
    telemetry.backup_checkpoint("completed")
    assert events == before


@pytest.mark.parametrize("primary", [False, True])
def test_restored_database_has_only_the_required_pre_and_post_rotation_checks(tmp_path, monkeypatch, primary):
    source, snapshot, target = (tmp_path / name for name in ("source.sqlite3", "snapshot.sqlite3", "target.sqlite3"))
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE evidence(value TEXT)")
        connection.execute("INSERT INTO evidence VALUES('preserved')")
        connection.commit()
    manifest = database.create_database_snapshot(source, snapshot)
    calls, original = [], database.verify_database_snapshot

    def track(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(database, "verify_database_snapshot", track)
    result = database.restore_database_snapshot(snapshot, target, manifest, primary=primary)
    assert calls == ([snapshot, target, target] if primary else [snapshot, target])
    assert result["table_row_counts"]["evidence"] == 1
    if not primary:
        assert target.read_bytes() == snapshot.read_bytes()


def test_verified_timestamp_is_receipt_time_and_completion_manifest_counts_toward_budget(archive_sources, tmp_path, monkeypatch):
    _source, spec = archive_sources
    target = tmp_path / "backup"
    created = archive.create_workspace_backup(spec, target)
    fixed = "2026-10-16T05:00:00Z"
    monkeypatch.setattr(archive, "audit_now_text", lambda: fixed)
    verified = archive.verify_workspace_backup(target)
    assert verified["verified_at"] == fixed
    assert verified["digest"] == created["digest"]
    # Staging lacks the final marker, but admission must reserve its walk entry.
    members_with_root = 1 + len(list(target.rglob("*")))
    stage = tmp_path / "unpublished"
    shutil.copytree(target, stage, ignore=shutil.ignore_patterns(MANIFEST_FILENAME))
    with pytest.raises(BackupIOError, match="completion manifest"):
        verify_workspace_members(stage, load_workspace_manifest(target), WorkspaceBackupLimits(max_files=members_with_root - 1),
                                 time.monotonic() + 30, published=False)


def test_progress_observer_cannot_hide_a_change_immediately_before_publication(archive_sources, tmp_path):
    _source, spec = archive_sources
    target = tmp_path / "backup"

    def mutate(event):
        if event["stage"] == "archive_publish":
            assert not (target / MANIFEST_FILENAME).exists()
            (target / "payload/data/report.txt").write_text("changed after full validation")

    with pytest.raises(BackupIOError, match="before completion publication"):
        archive.create_workspace_backup(spec, target, progress=telemetry.BackupProgress(mutate))
    assert not target.exists()
