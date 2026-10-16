"""Exercise the shipped backup CLI in isolated subprocesses against temporary SQLite."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from app.db.alert_stream import read_alert_stream_state
from app.services.workspace_backup import create_workspace_backup
from app.services.workspace_backup_manifest import MANIFEST_FILENAME
from tests import test_workspace_backup as backup_tests


archive_sources = backup_tests.archive_sources
CLI = Path(__file__).resolve().parents[1] / "tools/workspace_backup.py"
PRIVATE = "PRIVATE-CLI-MARKER"


def run_cli(tmp_path, *arguments):
    return subprocess.run(
        [sys.executable, "-I", "-B", str(CLI), *map(str, arguments)], cwd=tmp_path,
        env={"PATH": os.defpath}, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("operation", ["create", "verify", "restore"])
def test_real_cli_opt_in_progress_preserves_final_json_and_redacts_paths(cli_archive, tmp_path, operation):
    _source, scope, archive, created = cli_archive
    destination = tmp_path / f"{PRIVATE}-destination"
    inputs = ["--scope", scope] if operation == "create" else ["--archive", archive, "--expected-digest", created["digest"]]
    extra = ["--destination", destination] if operation != "verify" else []
    result = run_cli(tmp_path, operation, *inputs, *extra, "--progress", "--progress-interval", "0.1")
    assert result.returncode == 0
    final = json.loads(result.stdout)
    assert final["operation"] == operation and final["archive_verified"] is True
    assert final["verified_at"] and final["resume_eligible"] is False
    events = [json.loads(line) for line in result.stderr.splitlines()]
    assert events[-1]["stage"] == "completed"
    assert all(event["schema_version"] == "workspace-backup-progress-v1" for event in events)
    assert all(event["operation"] == operation for event in events)
    assert ("source_leases_released" in [event["stage"] for event in events]) == (operation == "create")
    assert PRIVATE not in result.stderr and str(tmp_path) not in result.stderr


def test_real_cli_progress_failure_reports_safe_stage_and_keeps_stdout_empty(cli_archive, tmp_path):
    _source, _scope, archive, created = cli_archive
    (archive / "payload/data/report.txt").write_text(PRIVATE)
    result = run_cli(tmp_path, "verify", "--archive", archive, "--expected-digest", created["digest"], "--progress")
    assert result.returncode == 2 and result.stdout == ""
    events = [json.loads(line) for line in result.stderr.splitlines()]
    final = events[-1]
    assert final["status"] == "failed" and final["stage"] == "archive_members"
    assert final["error_code"] == "backup_archive_members_failed"
    assert not final["archive_verified"] and not final["resume_eligible"]
    assert PRIVATE not in result.stderr and str(tmp_path) not in result.stderr
    assert all(event.get("stage") != "completed" for event in events)


@pytest.mark.parametrize("interval", ["nan", "inf", "0", "61"])
def test_real_cli_rejects_invalid_progress_interval_before_creating_output(cli_scope, tmp_path, interval):
    _source, scope = cli_scope
    target = tmp_path / "not-created"
    failure(run_cli(tmp_path, "create", "--scope", scope, "--destination", target,
                    "--progress", "--progress-interval", interval), "create", scope)
    assert not target.exists()


def success(result, operation):
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    value = json.loads(result.stdout)
    assert value["operation"] == operation and value["archive_verified"] is True
    assert value["resume_eligible"] is False
    assert value["cross_domain_consistency_verified"] is False
    assert len(value["digest"]) == 64
    return value


def failure(result, operation, *private_values):
    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    value = json.loads(result.stderr)
    assert value == {"status": "failed", "operation": operation, "archive_verified": False,
                     "resume_eligible": False, "error_type": value["error_type"]}
    assert value["error_type"] and "Traceback" not in result.stderr
    for text in (PRIVATE, *map(str, private_values)):
        assert text not in result.stderr
    return value


def tree_identity(directory):
    values = {}
    for path in directory.rglob("*"):
        facts = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        values[str(path.relative_to(directory))] = (digest, facts.st_mode, facts.st_mtime_ns)
    return values


@pytest.fixture
def cli_scope(archive_sources, tmp_path):
    source, spec = archive_sources
    scope = tmp_path / f"{PRIVATE}-scope.json"
    scope.write_text(spec.model_dump_json())
    return source, scope


@pytest.fixture
def cli_archive(cli_scope, archive_sources, tmp_path):
    """Prepare an archive bound to this case's real source paths without another CLI process."""
    source, scope = cli_scope
    _source, spec = archive_sources
    archive = tmp_path / "archive"
    created = create_workspace_backup(spec, archive)
    return source, scope, archive, created


@pytest.fixture
def real_cli_archive(cli_scope, tmp_path):
    source, scope = cli_scope
    archive = tmp_path / "archive"
    created = success(run_cli(tmp_path, "create", "--scope", scope, "--destination", archive), "create")
    return source, scope, archive, created


def test_real_cli_create_verify_and_restore_without_original_sources(real_cli_archive, tmp_path):
    source, _scope, archive, created = real_cli_archive
    with closing(sqlite3.connect(f"{(source / 'primary.sqlite3').as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
        old_stream = read_alert_stream_state(connection)
    assert created["excluded_count"] >= 1
    assert not (archive / "payload/data/.env").exists()
    before = tree_identity(archive)
    shutil.rmtree(source)
    verified = success(run_cli(tmp_path, "verify", "--archive", archive, "--expected-digest", created["digest"]), "verify")
    assert verified["digest"] == created["digest"]
    destination = tmp_path / "restored"
    restored = success(run_cli(tmp_path, "restore", "--archive", archive, "--destination", destination,
                               "--expected-digest", verified["digest"]), "restore")
    assert tree_identity(archive) == before
    assert (destination / "payload/data/report.txt").read_text() == "reproducible research\n"
    assert (destination / "payload/data/empty").is_dir()
    assert (destination / "payload/data/research.sqlite3").read_bytes() == (archive / "payload/data/research.sqlite3").read_bytes()
    with closing(sqlite3.connect(f"{(destination / 'payload/data/primary.sqlite3').as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
        assert connection.execute("SELECT * FROM evidence").fetchall() == [(1, "preserved")]
        assert read_alert_stream_state(connection) != old_stream
    restored_manifest = json.loads((destination / MANIFEST_FILENAME).read_bytes())
    assert restored_manifest["source_backup_digest"] == created["digest"]
    assert success(run_cli(tmp_path, "verify", "--archive", destination, "--expected-digest", restored["digest"]), "verify")["digest"] == restored["digest"]


def test_real_cli_verify_is_read_only_and_creates_no_sqlite_sidecars(cli_archive, tmp_path):
    _source, _scope, archive, created = cli_archive
    before = tree_identity(archive)
    success(run_cli(tmp_path, "verify", "--archive", archive, "--expected-digest", created["digest"]), "verify")
    assert tree_identity(archive) == before
    assert not any(path.name.endswith(("-wal", "-shm", "-journal", ".lock")) for path in archive.rglob("*"))


@pytest.mark.parametrize("operation", ["verify", "restore"])
@pytest.mark.parametrize("digest", ["f" * 64, PRIVATE])
def test_real_cli_wrong_expected_digest_fails_without_writes(cli_archive, tmp_path, operation, digest):
    _source, _scope, archive, _created = cli_archive
    before = tree_identity(archive)
    target = tmp_path / "not-created"
    extra = ["--destination", target] if operation == "restore" else []
    result = run_cli(tmp_path, operation, "--archive", archive, "--expected-digest", digest, *extra)
    failure(result, operation, archive)
    assert not target.exists()
    assert tree_identity(archive) == before


@pytest.mark.parametrize("operation", ["verify", "restore"])
@pytest.mark.parametrize("mutation", ["corrupt", "missing-member", "missing-manifest", "duplicate-manifest-key"])
def test_real_cli_rejects_damaged_or_incomplete_archives(cli_archive, tmp_path, operation, mutation):
    _source, _scope, archive, _created = cli_archive
    member = archive / "payload/data/report.txt"
    manifest = archive / MANIFEST_FILENAME
    if mutation == "corrupt":
        member.write_text(PRIVATE)
    elif mutation == "missing-member":
        member.unlink()
    elif mutation == "missing-manifest":
        manifest.unlink()
    else:
        manifest.write_text('{"' + PRIVATE + '":1,"' + PRIVATE + '":2}')
    before = tree_identity(archive)
    target = tmp_path / "not-created"
    extra = ["--destination", target] if operation == "restore" else []
    failure(run_cli(tmp_path, operation, "--archive", archive, *extra), operation, archive)
    assert tree_identity(archive) == before
    assert not target.exists()


@pytest.mark.parametrize("operation", ["create", "restore"])
@pytest.mark.parametrize("occupied", [False, True])
def test_real_cli_refuses_existing_destination_without_replacing_it(cli_archive, tmp_path, operation, occupied):
    _source, scope, archive, _created = cli_archive
    target = tmp_path / f"{PRIVATE}-existing"
    target.mkdir()
    if occupied:
        (target / "retained.txt").write_text("never overwrite")
    before, inode = tree_identity(target), target.stat().st_ino
    inputs = ["--scope", scope] if operation == "create" else ["--archive", archive]
    failure(run_cli(tmp_path, operation, *inputs, "--destination", target), operation, target)
    assert target.stat().st_ino == inode and tree_identity(target) == before


@pytest.mark.parametrize("value", ["missing", False, 1, "true", None])
def test_real_cli_requires_explicit_boolean_offline_attestation(cli_scope, tmp_path, value):
    _source, scope = cli_scope
    content = json.loads(scope.read_bytes())
    if value == "missing":
        content.pop("offline_writers_stopped")
    else:
        content["offline_writers_stopped"] = value
    scope.write_text(json.dumps(content))
    target = tmp_path / "not-created"
    failure(run_cli(tmp_path, "create", "--scope", scope, "--destination", target), "create", scope)
    assert not target.exists()


@pytest.mark.parametrize("mutation", ["duplicate-key", "oversized", "relative-source", "invalid-json", "nonfinite", "missing-scope"])
def test_real_cli_scope_is_strict_bounded_and_failures_are_sanitized(cli_scope, tmp_path, mutation):
    _source, scope = cli_scope
    if mutation == "duplicate-key":
        scope.write_text('{"' + PRIVATE + '":1,"' + PRIVATE + '":2}')
    elif mutation == "oversized":
        scope.write_text('"' + PRIVATE + "x" * (64 * 1024) + '"')
    elif mutation == "relative-source":
        content = json.loads(scope.read_bytes())
        content["roots"][0]["path"] = f"relative/{PRIVATE}"
        scope.write_text(json.dumps(content))
    elif mutation == "invalid-json":
        scope.write_text(PRIVATE)
    elif mutation == "nonfinite":
        scope.write_text('{"' + PRIVATE + '":NaN}')
    else:
        scope.unlink()
    target = tmp_path / "not-created"
    failure(run_cli(tmp_path, "create", "--scope", scope, "--destination", target), "create", scope)
    assert not target.exists()


@pytest.mark.parametrize("operation", ["create", "verify", "restore"])
def test_real_cli_rejects_relative_archive_or_destination(cli_archive, tmp_path, operation):
    _source, scope, archive, _created = cli_archive
    if operation == "create":
        arguments = ["--scope", scope, "--destination", f"relative-{PRIVATE}"]
    else:
        arguments = ["--archive", archive.name]
        if operation == "restore":
            arguments += ["--destination", tmp_path / "not-created"]
    failure(run_cli(tmp_path, operation, *arguments), operation)
    assert not (tmp_path / f"relative-{PRIVATE}").exists()
    assert not (tmp_path / "not-created").exists()


def test_real_cli_restore_requires_an_absolute_destination(cli_archive, tmp_path):
    _source, _scope, archive, _created = cli_archive
    destination = f"relative-{PRIVATE}"
    failure(run_cli(tmp_path, "restore", "--archive", archive, "--destination", destination), "restore")
    assert not (tmp_path / destination).exists()


@pytest.mark.parametrize("arguments", [
    ["--max-files", "0"], ["--max-bytes", "0"], ["--max-file-bytes", "-1"],
    ["--max-depth", "-1"], ["--max-seconds", "nan"], ["--max-seconds", "inf"], ["--max-seconds", "0"],
    ["--max-files", PRIVATE], ["--max-seconds", PRIVATE],
])
def test_real_cli_invalid_budgets_fail_before_creating_destination(cli_scope, tmp_path, arguments):
    _source, scope = cli_scope
    target = tmp_path / "not-created"
    operation = None if arguments[-1] == PRIVATE else "create"
    failure(run_cli(tmp_path, "create", "--scope", scope, "--destination", target, *arguments), operation, scope)
    assert not target.exists()


@pytest.mark.parametrize("operation", ["create", "verify", "restore"])
def test_real_cli_exhausted_budget_never_reports_success_or_leaves_output(cli_archive, tmp_path, operation):
    _source, scope, archive, _created = cli_archive
    before = tree_identity(archive)
    target = tmp_path / "not-created"
    inputs = ["--scope", scope] if operation == "create" else ["--archive", archive]
    extra = ["--destination", target] if operation != "verify" else []
    failure(run_cli(tmp_path, operation, *inputs, *extra, "--max-files", "1"), operation)
    assert not target.exists() and tree_identity(archive) == before


@pytest.mark.parametrize("operation", ["verify", "restore"])
def test_real_cli_missing_archive_has_sanitized_failure(tmp_path, operation):
    target = tmp_path / "not-created"
    missing = tmp_path / f"{PRIVATE}-missing"
    extra = ["--destination", target] if operation == "restore" else []
    failure(run_cli(tmp_path, operation, "--archive", missing, *extra), operation, missing)
    assert not target.exists() and not missing.exists()


@pytest.mark.parametrize("arguments", [
    [], [f"{PRIVATE}-unknown-operation"], ["create"], ["verify"], ["restore"],
    ["create", "--scope", f"/{PRIVATE}-scope.json"],
    ["restore", "--archive", f"/{PRIVATE}-archive"],
    ["verify", "--archive", f"/{PRIVATE}-archive", f"--{PRIVATE}-unknown-flag"],
])
def test_real_cli_parser_errors_never_echo_unvalidated_arguments(tmp_path, arguments):
    before = tree_identity(tmp_path)
    failure(run_cli(tmp_path, *arguments), None)
    assert tree_identity(tmp_path) == before


@pytest.mark.parametrize("arguments", [["--help"], ["create", "--help"], ["verify", "--help"], ["restore", "--help"]])
def test_real_cli_help_remains_available_without_scope_or_writes(tmp_path, arguments):
    before = tree_identity(tmp_path)
    result = run_cli(tmp_path, *arguments)
    assert result.returncode == 0 and not result.stderr
    assert "usage:" in result.stdout and "--help" in result.stdout
    assert tree_identity(tmp_path) == before
