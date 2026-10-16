from datetime import timedelta
import base64
import json
from pathlib import Path
import sqlite3

import pytest

from app.artifacts.io import ArtifactChangedError
from app.services import strategy_prospective_health as health
from app.services import strategy_prospective_plan as plans
from app.services import strategy_prospective_runner as runner
from tests import test_strategy_prospective_collection as collection_fixtures
from tests import test_strategy_prospective_runtime as runtime_fixtures
from tools import inspect_strategy_prospective_runtime as inspector

study = collection_fixtures.study
prepared_runtime = runtime_fixtures.prepared_runtime
sealed = runtime_fixtures.sealed


def _binding(identity):
    return {"schema_version": "strategy-prospective-runner-binding-v1", "plan_id": identity["plan_id"],
        "plan_digest": identity["plan_digest"], "root": identity["root"], "database": identity["database"]}


@pytest.fixture
def inspectable(study, monkeypatch, tmp_path):
    database, root, clock = study
    plan = plans.read_strategy_prospective_plan(root, "study")
    identity = health.prospective_runner_identity(database[0], root, "study", root.parent / "strategy_prospective_control",
        plan["digest"], tmp_path / "sealed", "b" * 64)
    manifest = {"digest": identity["runtime_digest"], "code_files": {
        **plan["specification"]["source_manifest"], "app/resources/trading_calendar.json": plan["calendar_sha256"],
    }}
    def inspected(_directory, *, expected_digest=None):
        if expected_digest != manifest["digest"]:
            raise ValueError("runtime does not match expected digest")
        return manifest
    monkeypatch.setattr(inspector, "inspect_runtime", inspected)
    monkeypatch.setattr(health, "utc_now", lambda: clock["now"])
    runner._publish(health.runner_health_directory(identity) / "binding.json", _binding(identity))
    arguments = ["--directory", identity["runtime_directory"], "--expected-runtime-digest", identity["runtime_digest"],
        "--root", identity["root"], "--database", identity["database"], "--control-root", identity["control_root"],
        "--plan-id", identity["plan_id"], "--expected-plan-digest", identity["plan_digest"]]
    return identity, manifest, clock, arguments


def test_inspection_is_read_only_and_uses_frozen_source_instead_of_current_workspace(inspectable, monkeypatch, capsys):
    identity, _manifest, _clock, arguments = inspectable
    def forbidden(*_args, **_kwargs):
        pytest.fail("offline inspection performed collection, HTTP, SQLite access or current-source comparison")
    monkeypatch.setattr(plans, "_source_manifest", forbidden)
    monkeypatch.setattr(plans, "_validate_frozen_contracts", forbidden)
    monkeypatch.setattr(runner, "collect_strategy_prospective_inputs", forbidden)
    monkeypatch.setattr(runner, "local_service_request", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with health.prospective_runner_health_session(identity) as session:
        session.completed({"action": "idle", "fault": None, "state": {"status": "waiting"}}, 3600)
        directory = Path(identity["root"]).parent
        before = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        assert inspector.main(arguments) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "healthy" and result["identity"] == identity
        assert before == {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


@pytest.mark.parametrize("part", ["plan", "runtime", "source", "calendar", "control", "health"])
def test_mismatched_identity_is_a_nonzero_sanitized_fault(inspectable, part, capsys):
    identity, manifest, _clock, arguments = inspectable
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        if part in {"plan", "runtime"}:
            option = "--expected-plan-digest" if part == "plan" else "--expected-runtime-digest"
            arguments[arguments.index(option) + 1] = "c" * 64
        elif part == "source":
            manifest["code_files"]["app/example.py"] = "c" * 64
        elif part == "calendar":
            manifest["code_files"]["app/resources/trading_calendar.json"] = "c" * 64
        elif part == "control":
            (session.directory / "binding.json").write_text('{"unsafe":"private payload"}')
        else:
            (session.directory / "health.json").write_text('{"unsafe":"private payload"}')
        assert inspector.main(arguments) == 3
        output = capsys.readouterr().out
        assert "private payload" not in output and json.loads(output)["status"] == "fault"


def test_missing_or_concurrently_replaced_heartbeat_reports_unknown(inspectable, monkeypatch, capsys):
    identity, _manifest, _clock, arguments = inspectable
    assert inspector.main(arguments) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
    def changed(_identity):
        raise ArtifactChangedError(Path("health.json"), stage="read")
    monkeypatch.setattr(inspector, "inspect_runner_health", changed)
    assert inspector.main(arguments) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
    monkeypatch.setattr(inspector, "inspect_runner_health", lambda _identity: {
        "status": "unknown", "fault_codes": ["health_changed_during_inspection"],
    })
    assert inspector.main(arguments) == 2
    assert json.loads(capsys.readouterr().out)["fault_codes"] == ["health_changed_during_inspection"]


def test_fault_expiration_and_clock_regression_have_explanatory_codes(inspectable, capsys):
    identity, _manifest, clock, arguments = inspectable
    with health.prospective_runner_health_session(identity) as session:
        session.completed({"action": "service_failed", "fault": "OSError", "state": {"status": "waiting"}}, 60)
        assert inspector.main(arguments) == 3
        assert json.loads(capsys.readouterr().out)["fault_codes"] == ["service_unavailable"]
        clock["now"] -= timedelta(seconds=1)
        assert inspector.main(arguments) == 3
        assert json.loads(capsys.readouterr().out)["fault_codes"] == ["clock_regressed", "service_unavailable"]
        clock["now"] += timedelta(seconds=182)
        assert inspector.main(arguments) == 3
        assert json.loads(capsys.readouterr().out)["fault_codes"] == ["heartbeat_expired", "service_unavailable"]


def test_real_sealed_runtime_is_verified_and_drift_is_rejected_without_execution(sealed, tmp_path, monkeypatch):
    from tools import strategy_prospective_runtime as runtime
    manifest = runtime.inspect_runtime(sealed)
    source = {name: digest for name, digest in manifest["code_files"].items() if name.endswith(".py")}
    plan = {"schema_version": "strategy-template-prospective-plan-v1", "plan_id": "study", "recorded_at": "2026-01-01T00:00:00Z",
        "specification": {"source_manifest": source},
        "calendar_sha256": manifest["code_files"]["app/resources/trading_calendar.json"],
        "calendar_base64": base64.b64encode((sealed / "code/app/resources/trading_calendar.json").read_bytes()).decode("ascii")}
    path = tmp_path / "plans/study/plan.json"
    runner._publish(path, plan)
    digest = json.loads(path.read_bytes())["digest"]
    identity = health.prospective_runner_identity(tmp_path / "db", tmp_path / "plans", "study",
        tmp_path / "strategy_prospective_control", digest, sealed, manifest["digest"])
    runner._publish(health.runner_health_directory(identity) / "binding.json", _binding(identity))
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        assert inspector.inspect_strategy_prospective_runtime(identity)["status"] == "healthy"
        (sealed / "code/app/calculation.py").write_text("changed source")
        with pytest.raises(ValueError, match="drift"):
            inspector.inspect_strategy_prospective_runtime(identity)


def test_inspection_passes_expected_pin_before_any_target_python_probe(sealed, tmp_path, monkeypatch):
    from tools import strategy_prospective_runtime as runtime
    identity = health.prospective_runner_identity(tmp_path / "db", tmp_path / "plans", "study",
        tmp_path / "strategy_prospective_control", "a" * 64, sealed, "f" * 64)
    monkeypatch.setattr(runtime, "_probe", lambda _python: pytest.fail("wrong runtime executable was probed"))
    with pytest.raises(ValueError, match="admitted digest"):
        inspector.inspect_strategy_prospective_runtime(identity)
