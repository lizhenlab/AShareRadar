from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, sha256_hex
from app.services import strategy_prospective_health as health


@pytest.fixture
def heartbeat(tmp_path, monkeypatch):
    identity = health.prospective_runner_identity(tmp_path / "market.sqlite3", tmp_path / "plans", "future-study",
        tmp_path / "strategy_prospective_control", "a" * 64, tmp_path / "sealed", "b" * 64)
    clock = {"now": datetime(2026, 10, 15, 12, tzinfo=timezone.utc)}
    monkeypatch.setattr(health, "utc_now", lambda: clock["now"])
    return identity, clock


def _result(action="idle", fault=None, status="waiting"):
    return {"action": action, "fault": fault, "state": {"status": status}}


def _rewrite(binding, **values):
    path = health.runner_health_directory(binding) / "health.json"
    payload = json.loads(path.read_bytes())
    payload.pop("digest")
    payload.update(values)
    path.write_bytes(canonical_json_bytes({**payload, "digest": sha256_hex(canonical_json_bytes(payload))}))


def test_long_wait_is_healthy_until_its_schedule_plus_grace_and_never_updates_observation(heartbeat):
    identity, clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        first = health.inspect_runner_health(identity)
        assert first["status"] == "healthy" and first["health"]["last_completed_at"] is None
        session.completed(_result(), 3600)
        completed = health.read_runner_health(identity)
        observation = session.directory / "observation.json"
        observation.write_bytes(b"existing observation")
        clock["now"] += timedelta(seconds=3600 + health.HEALTH_GRACE_SECONDS)
        assert health.inspect_runner_health(identity)["status"] == "healthy"
        clock["now"] += timedelta(seconds=1)
        assert health.inspect_runner_health(identity)["fault_codes"] == ["heartbeat_expired"]
        assert health.read_runner_health(identity) == completed
        assert observation.read_bytes() == b"existing observation"


def test_failure_then_recovery_records_real_completion_and_only_safe_fault_codes(heartbeat):
    identity, clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        session.completed(_result("service_failed", "private URL and credentials"), 60)
        failed = health.inspect_runner_health(identity)
        assert failed["fault_codes"] == ["service_unavailable"]
        assert "private URL and credentials" not in json.dumps(failed)
        first_completion = failed["health"]["last_completed_at"]
        clock["now"] += timedelta(seconds=60)
        session.checking()
        assert health.read_runner_health(identity)["last_completed_at"] == first_completion
        assert health.inspect_runner_health(identity)["fault_codes"] == ["service_unavailable"]
        session.completed(_result(), 60)
        recovered = health.inspect_runner_health(identity)
        assert recovered["status"] == "healthy" and recovered["health"]["last_completed_at"] != first_completion


def test_initialization_failure_and_clock_rollback_never_claim_a_successful_cycle(heartbeat):
    identity, clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        session.failed("runtime_validation_failed")
        assert health.read_runner_health(identity)["last_completed_at"] is None
        assert health.inspect_runner_health(identity)["fault_codes"] == ["runtime_validation_failed"]
        before = (session.directory / "health.json").read_bytes()
        clock["now"] -= timedelta(seconds=1)
        with pytest.raises(ValueError, match="backwards"):
            session.checking()
        assert (session.directory / "health.json").read_bytes() == before
        assert "clock_regressed" in health.inspect_runner_health(identity)["fault_codes"]


def test_competing_session_cannot_overwrite_owner_or_hide_its_health(heartbeat):
    identity, _clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        before = health.read_runner_health(identity)
        with pytest.raises(ValueError, match="already active"):
            with health.prospective_runner_health_session(identity):
                pytest.fail("second runner admitted")
        assert health.read_runner_health(identity) == before
        assert health.inspect_runner_health(identity)["status"] == "healthy"
    assert "runner_not_active" in health.inspect_runner_health(identity)["fault_codes"]


def test_atomic_write_failure_preserves_last_complete_record_and_removes_staging(heartbeat, monkeypatch):
    identity, clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        session.completed(_result(), 60)
        before = (session.directory / "health.json").read_bytes()
        clock["now"] += timedelta(seconds=60)
        def fail(*_args, **_kwargs):
            raise OSError("disk failure")
        monkeypatch.setattr(health.os, "replace", fail)
        with pytest.raises(OSError):
            session.checking()
        assert (session.directory / "health.json").read_bytes() == before
        assert not list(session.directory.glob(".health-*"))


@pytest.mark.parametrize("values", [
    {"status": "waiting", "next_check_at": None},
    {"status": "complete", "next_check_at": "2026-10-15T12:01:00+00:00"},
    {"status": "waiting", "last_completed_at": None},
    {"last_completed_at": "2026-10-15T12:01:00+00:00"},
    {"next_check_at": "2026-10-16T12:00:00+00:00"},
    {"next_check_at": "2026-10-15T11:00:00+00:00"},
    {"fault_code": "secret error payload"}, {"status": "waiting", "fault_code": "scan_fault"}, {"status": "fault"},
    {"status": "invented"}, {"pid": True}, {"session_id": "../unsafe"}, {"identity": {}},
    {"schema_version": "altered"},
])
def test_malformed_or_unbounded_health_is_rejected(heartbeat, values):
    identity, _clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        _rewrite(identity, **values)
        with pytest.raises(ValueError):
            health.read_runner_health(identity)


def test_oversized_tampered_and_symlink_records_are_rejected(heartbeat):
    identity, _clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        path = session.directory / "health.json"
        original = path.read_bytes()
        path.write_bytes(b" " * (health.HEALTH_MAX_BYTES + 1))
        with pytest.raises(ArtifactIOError):
            health.read_runner_health(identity)
        path.write_bytes(original.replace(b'"checking"', b'"stopped"'))
        with pytest.raises(ValueError, match="digest"):
            health.read_runner_health(identity)
        path.unlink()
        target = session.directory / "other.json"
        target.write_bytes(original)
        path.symlink_to(target)
        with pytest.raises(ArtifactIOError):
            health.read_runner_health(identity)


def test_read_race_reports_unknown_instead_of_a_false_health_result(heartbeat, monkeypatch):
    identity, _clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        original = health.read_runner_health
        reads = []
        def read(value):
            result = original(value)
            reads.append(result)
            if len(reads) == 2:
                result["session_id"] = "changed-session"
            return result
        monkeypatch.setattr(health, "read_runner_health", read)
        assert health.inspect_runner_health(identity) == {
            "status": "unknown", "fault_codes": ["health_changed_during_inspection"],
        }


def test_terminated_fixture_process_is_detected_before_heartbeat_expires(heartbeat):
    identity, clock = heartbeat
    script = """import json, sys, time
from app.services.strategy_prospective_health import prospective_runner_health_session
with prospective_runner_health_session(json.loads(sys.argv[1])) as health:
    health.checking()
    print('ready', flush=True)
    time.sleep(30)
"""
    process = subprocess.Popen([sys.executable, "-B", "-c", script, json.dumps(identity)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={"PATH": os.defpath})
    try:
        assert process.stdout.readline().strip() == "ready"
        assert health.runner_health_lease_active(identity)
        clock["now"] = datetime.fromisoformat(health.read_runner_health(identity)["updated_at"])
        process.kill()
        process.wait(timeout=5)
        assert health.inspect_runner_health(identity)["fault_codes"] == ["runner_not_active"]
        clock["now"] += timedelta(seconds=health.HEALTH_CHECK_SECONDS + health.HEALTH_GRACE_SECONDS + 1)
        assert health.inspect_runner_health(identity)["fault_codes"] == ["heartbeat_expired", "runner_not_active"]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("status,expected", [("waiting", "runner_not_watching"), ("collection_complete", None)])
def test_stopped_and_complete_are_distinguished_from_a_lost_watcher(heartbeat, status, expected):
    identity, _clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        session.completed(_result(status=status), None)
    result = health.inspect_runner_health(identity)
    assert (expected in result["fault_codes"]) if expected else result["status"] == "healthy"


def test_identity_rejects_relative_redirected_or_alternate_control_paths(tmp_path):
    root = tmp_path / "plans"
    control = tmp_path / "strategy_prospective_control"
    for changed in (Path("relative"), tmp_path / "other"):
        with pytest.raises(ValueError):
            health.prospective_runner_identity(tmp_path / "db", root, "study", changed, "a" * 64, tmp_path / "sealed", "b" * 64)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "sealed")
    with pytest.raises(ValueError):
        health.prospective_runner_identity(tmp_path / "db", root, "study", control, "a" * 64, alias, "b" * 64)


def test_health_write_is_bounded_and_rechecks_open_directory_identity(heartbeat, monkeypatch):
    identity, _clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        with pytest.raises(ValueError, match="oversized"):
            health._replace_health(session.directory, {"large": "a" * health.HEALTH_MAX_BYTES})
        original = health.path_has_only_trusted_aliases
        checks = []
        def trusted(path):
            checks.append(path)
            return len(checks) == 1 and original(path)
        monkeypatch.setattr(health, "path_has_only_trusted_aliases", trusted)
        with pytest.raises(ArtifactIOError):
            session.checking()
        assert not list(session.directory.glob(".health-*"))
        assert not (session.directory / "health.json").exists()


def test_unsafe_or_changed_health_lease_never_claims_a_live_runner(heartbeat):
    identity, _clock = heartbeat
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        path = session.directory / "runner-health/.executor.lock"
        path.write_bytes(b"not an empty lease")
        with pytest.raises(ArtifactIOError):
            health.runner_health_lease_active(identity)
        path.unlink()
        path.symlink_to(session.directory / "health.json")
        with pytest.raises(ValueError, match="unsafe"):
            health.runner_health_lease_active(identity)


def test_dangling_health_symlink_is_not_replaced_at_startup(heartbeat):
    identity, _clock = heartbeat
    directory = health.runner_health_directory(identity)
    directory.mkdir(parents=True)
    path = directory / "health.json"
    path.symlink_to(directory / "missing.json")
    with pytest.raises(ArtifactIOError):
        with health.prospective_runner_health_session(identity):
            pytest.fail("unsafe record admitted")
    assert path.is_symlink()
