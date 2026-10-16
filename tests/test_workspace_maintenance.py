from datetime import timedelta
import json
import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_datahub
from app.api.routes import workspace_maintenance as routes
from app.artifacts.io import canonical_json_bytes
from app.services import strategy_prospective_health as health
from app.services import workspace_maintenance as maintenance
from app.services.workspace_backup import create_workspace_backup
from app.utils.audit_time import audit_now_text
from app.utils.clock import utc_now
from tests import test_workspace_backup as backup_tests


archive_sources = backup_tests.archive_sources


def _scope(database, **values):
    path = maintenance.maintenance_scope_path(database)
    path.write_text(json.dumps({"schema_version": "workspace-maintenance-scope-v1", **values}))
    return path


@pytest.fixture
def recorded_backup(tmp_path, archive_sources):
    _source, spec = archive_sources
    database, archive = tmp_path / "app.sqlite3", tmp_path / "archive"
    result = create_workspace_backup(spec, archive)
    receipt = tmp_path / "receipt.json"
    result["verified_at"] = audit_now_text()
    receipt.write_bytes(canonical_json_bytes(result))
    config = {"name": "离线备份", "archive": str(archive), "expected_digest": result["digest"],
              "verification_receipt": str(receipt)}
    _scope(database, backups=[config])
    return database, archive, receipt, result, config


def test_absent_configuration_does_not_create_files_or_claim_full_workspace(tmp_path):
    before = list(tmp_path.iterdir())
    report = maintenance.workspace_maintenance_status(tmp_path / "app.sqlite3")
    assert report.status == "not_configured" and report.scope_digest is None
    assert report.storage_root_count == 0 and not report.backups and not report.runners
    assert list(tmp_path.iterdir()) == before


@pytest.mark.parametrize("payload", [b'{"private-secret":', b'{"schema_version":"unknown"}', b'null', b'x' * 65537])
def test_invalid_scope_is_redacted_and_never_partially_used(tmp_path, payload):
    database = tmp_path / "PRIVATE.sqlite3"
    maintenance.maintenance_scope_path(database).write_bytes(payload)
    report = maintenance.workspace_maintenance_status(database)
    assert report.status == "unavailable" and report.fault_code == "scope_invalid_or_unreadable"
    assert "PRIVATE" not in report.model_dump_json() and "private-secret" not in report.model_dump_json()


def test_backup_history_is_bound_and_never_claims_current_payload_integrity(recorded_backup):
    database, archive, _receipt, _result, _config = recorded_backup
    report = maintenance.workspace_maintenance_status(database)
    backup = report.backups[0]
    assert backup.status == "manifest_checked" and backup.verification_record_status == "recorded"
    assert backup.last_verified_at and backup.root_names == ["data"]
    assert backup.current_payload_verified is False and backup.resume_eligible is False
    (archive / "payload/data/report.txt").write_text("changed after recorded verification")
    observed = maintenance.workspace_maintenance_status(database).backups[0]
    assert observed.current_payload_verified is False and observed.last_verified_at == backup.last_verified_at
    assert str(archive) not in report.model_dump_json()


@pytest.mark.parametrize("change", [
    {"digest": "f" * 64}, {"archive_path": "/another/archive"}, {"archive_verified": False},
    {"verified_at": None}, {"verified_at": "not-a-time"}, {"verified_at": "2099-01-01T00:00:00Z"},
    {"verified_at": "2000-01-01T00:00:00Z"}, {"operation": "preview"},
])
def test_bad_or_unrelated_receipt_cannot_claim_verification(recorded_backup, change):
    database, _archive, receipt, result, _config = recorded_backup
    receipt.write_bytes(canonical_json_bytes({**result, **change}))
    status = maintenance.workspace_maintenance_status(database).backups[0]
    assert status.last_verified_at is None and status.verification_record_status == "invalid"


def test_manifest_must_match_independently_configured_identity(recorded_backup):
    database, _archive, _receipt, _result, config = recorded_backup
    _scope(database, backups=[{**config, "expected_digest": "e" * 64}])
    backup = maintenance.workspace_maintenance_status(database).backups[0]
    assert backup.status == "unavailable" and backup.last_verified_at is None


def test_legacy_receipt_does_not_invent_a_verification_time(recorded_backup):
    database, _archive, receipt, result, _config = recorded_backup
    result.pop("verified_at")
    receipt.write_bytes(canonical_json_bytes(result))
    backup = maintenance.workspace_maintenance_status(database).backups[0]
    assert backup.verification_record_status == "undated" and backup.last_verified_at is None


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin trusted system aliases")
def test_backup_receipt_accepts_trusted_system_alias_but_not_an_arbitrary_symlink(recorded_backup, tmp_path):
    database, archive, _receipt, _result, config = recorded_backup
    assert str(archive).startswith("/private/")
    _scope(database, backups=[{**config, "archive": str(archive).removeprefix("/private")}])
    assert maintenance.workspace_maintenance_status(database).backups[0].verification_record_status == "recorded"
    alias = tmp_path / "alias"
    alias.symlink_to(archive, target_is_directory=True)
    assert not maintenance._same_archive_path(str(alias), archive)


def test_status_does_not_traverse_storage_and_explicit_inspection_reports_partial(tmp_path, monkeypatch):
    database, directory = tmp_path / "app.sqlite3", tmp_path / "data"
    directory.mkdir()
    (directory / "one.txt").write_bytes(b"12345")
    (directory / "outside-link").symlink_to(tmp_path / "PRIVATE")
    _scope(database, storage_roots=[{"name": "业务资料", "category": "research", "path": str(directory)}])
    real_inspect = maintenance.inspect_workspace_storage
    monkeypatch.setattr(maintenance, "inspect_workspace_storage", lambda *_a, **_k: pytest.fail("implicit scan"))
    status = maintenance.workspace_maintenance_status(database)
    assert status.storage_root_count == 1
    monkeypatch.setattr(maintenance, "inspect_workspace_storage", real_inspect)
    report = maintenance.workspace_maintenance_storage(database, status.scope_digest)
    assert report.status == "partial" and report.totals.unique_bytes == 5
    assert "PRIVATE" not in report.model_dump_json() and str(directory) not in report.model_dump_json()


def test_storage_rejects_scope_changed_before_or_during_inspection(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    _scope(database, storage_roots=[{"name": "data", "category": "research", "path": str(tmp_path)}])
    status = maintenance.workspace_maintenance_status(database)
    with pytest.raises(ValueError, match="范围已变化"):
        maintenance.workspace_maintenance_storage(database, "f" * 64)
    real_inspect = maintenance.inspect_workspace_storage

    def changed(*args, **kwargs):
        report = real_inspect(*args, **kwargs)
        _scope(database)
        return report

    monkeypatch.setattr(maintenance, "inspect_workspace_storage", changed)
    with pytest.raises(ValueError, match="期间变化"):
        maintenance.workspace_maintenance_storage(database, status.scope_digest)


def test_runner_health_preserves_faults_and_separates_liveness_from_admission(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    identity = health.prospective_runner_identity(database, tmp_path / "plans", "future-study",
        tmp_path / "strategy_prospective_control", "a" * 64, tmp_path / "sealed", "b" * 64)
    _scope(database, runners=[{"name": "前瞻采集", **{key: value for key, value in identity.items() if key != "database"}}])
    clock = utc_now()
    monkeypatch.setattr(health, "utc_now", lambda: clock)
    with health.prospective_runner_health_session(identity) as session:
        session.completed({"action": "idle", "state": {"status": "waiting"}}, 60)
        status = maintenance.workspace_maintenance_status(database).runners[0]
        assert status.status == "healthy" and status.last_completed_at and status.next_check_at
        assert status.runtime_execution_verified is False and status.sample_admission_verified is False
        before = (session.directory / "health.json").read_bytes()
        clock += timedelta(seconds=181)
        stale = maintenance.workspace_maintenance_status(database).runners[0]
        assert stale.status == "fault" and "heartbeat_expired" in stale.fault_codes
        assert (session.directory / "health.json").read_bytes() == before
    stopped = maintenance.workspace_maintenance_status(database).runners[0]
    assert "runner_not_active" in stopped.fault_codes


def test_api_uses_server_scope_and_rejects_parallel_storage_inspections(tmp_path):
    database = tmp_path / "app.sqlite3"
    _scope(database)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_datahub] = lambda: SimpleNamespace(cache=SimpleNamespace(path=database))
    with TestClient(app) as client:
        status = client.get("/api/system/workspace-maintenance")
        assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
        url = "/api/system/workspace-maintenance/storage?scope_digest=" + status.json()["scope_digest"]
        assert client.post(url).json()["status"] == "not_configured"
        with routes._STORAGE_INSPECTION:
            busy = client.post(url)
        assert busy.status_code == 503 and busy.headers["retry-after"] == "2"
        assert busy.headers["cache-control"] == "no-store"
        assert client.post(url).status_code == 200
        changed = client.post("/api/system/workspace-maintenance/storage?scope_digest=" + "f" * 64)
        assert changed.status_code == 400 and changed.headers["cache-control"] == "no-store"
        assert client.post("/api/system/workspace-maintenance/storage?scope_digest=invalid").status_code == 422
