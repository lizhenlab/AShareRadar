from datetime import timedelta
import json

import pytest

from app.services import strategy_prospective_health as health
from app.services import strategy_prospective_plan as plans
from app.services import strategy_prospective_runner as runner
from tests import test_strategy_prospective_runner as runner_fixtures
from tools import run_strategy_prospective as cli

study = runner_fixtures.study
loop = runner_fixtures.loop


@pytest.fixture
def watched(loop, monkeypatch, tmp_path):
    cycle, calls, database, root, clock, digest = loop
    identity = health.prospective_runner_identity(database[0], root, "study", root.parent / "strategy_prospective_control",
        digest, tmp_path / "sealed", "b" * 64)
    monkeypatch.setenv(cli.RUNTIME_DIRECTORY_ENV, identity["runtime_directory"])
    monkeypatch.setenv(cli.RUNTIME_DIGEST_ENV, identity["runtime_digest"])
    monkeypatch.setattr(cli, "inspect_running_runtime", lambda: {"digest": identity["runtime_digest"]})
    monkeypatch.setattr(cli, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(health, "utc_now", lambda: clock["now"])
    arguments = ["--root", str(root), "--plan-id", "study", "--database", str(database[0]),
        "--control-root", identity["control_root"], "--expected-plan-digest", digest]
    return loop, identity, arguments


def test_watch_service_unreachable_then_recovery_submits_once_and_updates_heartbeat(watched, monkeypatch, capsys):
    (_cycle, calls, database, _root, clock, _digest), identity, arguments = watched
    runner_fixtures._no_publication(database)
    iterations = []
    def request(method, path, payload):
        calls.append((method, path, payload))
        if not iterations:
            raise ConnectionRefusedError("secret endpoint")
        if path.endswith("/ready"):
            return {"status": "ready", "checks": {"database_identity": runner_fixtures.database_identity(database[0])}}
        if path.endswith("/status"):
            return {"tasks": []}
        return {"accepted": True, "run": {"id": database[1], "mode": "official",
            "data_date": clock["now"].date().isoformat(), "scope": runner_fixtures.MARKET_SCAN_FULL_MARKET_SCOPE}}
    def wait(delay):
        record = health.read_runner_health(identity)
        iterations.append(record)
        clock["now"] += timedelta(seconds=delay)
        if len(iterations) == 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli, "local_service_request", lambda _url: request)
    monkeypatch.setattr(cli.time, "sleep", wait)
    with pytest.raises(KeyboardInterrupt):
        cli.main([*arguments, "--watch"])
    assert [row["fault_code"] for row in iterations] == ["service_unavailable", None, "scan_fault"]
    assert sum(method == "POST" for method, _path, _payload in calls) == 1
    assert "secret" not in capsys.readouterr().out
    assert not health.runner_health_lease_active(identity)


def test_health_rewrites_during_normal_wait_do_not_produce_notifications(watched, monkeypatch, capsys):
    (_cycle, _calls, _database, _root, clock, _digest), identity, arguments = watched
    clock["now"] = clock["now"].replace(hour=8)
    records = []
    def wait(delay):
        records.append(health.read_runner_health(identity))
        clock["now"] += timedelta(seconds=delay)
        if len(records) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", wait)
    with pytest.raises(KeyboardInterrupt):
        cli.main([*arguments, "--watch"])
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(outputs) == 1 and outputs[0]["action"] == "idle"
    assert records[0]["updated_at"] != records[1]["updated_at"]
    assert (runner_fixtures.datetime.fromisoformat(records[1]["next_check_at"])
        - runner_fixtures.datetime.fromisoformat(records[1]["updated_at"])).total_seconds() == 3600


def test_runtime_failure_is_recorded_before_any_collection_and_returns_failure(watched, monkeypatch, capsys):
    (_cycle, calls, _database, _root, _clock, _digest), identity, arguments = watched
    def invalid():
        raise ValueError("unsafe configuration and secret")
    monkeypatch.setattr(cli, "inspect_running_runtime", invalid)
    monkeypatch.setattr(cli, "run_strategy_prospective_cycle", lambda *_args, **_kwargs: pytest.fail("collection executed"))
    assert cli.main(arguments) == 2
    record = health.read_runner_health(identity)
    assert record["status"] == "fault" and record["fault_code"] == "runtime_validation_failed"
    assert record["last_completed_at"] is None and calls == []
    output = capsys.readouterr()
    assert output.out == "" and "secret" not in output.err and json.loads(output.err)["status"] == "failed"


def test_initial_health_write_failure_prevents_collection(watched, monkeypatch, capsys):
    (_cycle, calls, _database, _root, _clock, _digest), identity, arguments = watched
    def fail(*_args):
        raise OSError("disk full")
    monkeypatch.setattr(health, "_replace_health", fail)
    monkeypatch.setattr(cli, "run_strategy_prospective_cycle", lambda *_args, **_kwargs: pytest.fail("collection executed"))
    assert cli.main(arguments) == 2 and calls == []
    assert not (health.runner_health_directory(identity) / "health.json").exists()
    assert not health.runner_health_lease_active(identity)
    assert json.loads(capsys.readouterr().err)["error_type"] == "OSError"


def test_health_failure_after_reserved_post_does_not_allow_resubmission(watched, monkeypatch, capsys):
    (cycle, calls, database, root, _clock, _digest), identity, arguments = watched
    runner_fixtures._no_publication(database)
    original_replace = health._replace_health
    def fail_completion(directory, payload):
        if payload["last_completed_at"] is not None:
            raise OSError("cannot persist completed cycle")
        original_replace(directory, payload)
    original_cycle = cli.run_strategy_prospective_cycle
    monkeypatch.setattr(health, "_replace_health", fail_completion)
    monkeypatch.setattr(cli, "run_strategy_prospective_cycle", lambda *_args, **_kwargs: cycle())
    assert cli.main(arguments) == 2
    assert health.read_runner_health(identity)["fault_code"] == "health_write_failed"
    monkeypatch.setattr(health, "_replace_health", original_replace)
    assert cli.main(arguments) == 3
    assert sum(method == "POST" for method, _path, _payload in calls) == 1
    assert (root.parent / "strategy_prospective_control/study/attempt-2026-07-17.json").exists()
    assert original_cycle is runner.run_strategy_prospective_cycle
    capsys.readouterr()


def test_blocked_study_and_one_shot_service_failure_have_nonzero_status(watched, monkeypatch, capsys):
    (_cycle, _calls, database, _root, _clock, _digest), identity, arguments = watched
    runner_fixtures._no_publication(database)
    def unavailable(*_args):
        raise ConnectionRefusedError
    monkeypatch.setattr(cli, "local_service_request", lambda _url: unavailable)
    assert cli.main(arguments) == 3
    assert health.read_runner_health(identity)["fault_code"] == "service_unavailable"
    monkeypatch.setattr(plans, "_source_manifest", lambda: {"app/example.py": "c" * 64})
    assert cli.main([*arguments, "--watch"]) == 3
    assert health.read_runner_health(identity)["fault_code"] == "study_blocked"
    capsys.readouterr()


def test_competing_cli_does_not_replace_owner_health_or_run_a_cycle(watched, monkeypatch, capsys):
    (_cycle, calls, _database, _root, _clock, _digest), identity, arguments = watched
    with health.prospective_runner_health_session(identity) as session:
        session.checking()
        before = health.read_runner_health(identity)
        monkeypatch.setattr(cli, "run_strategy_prospective_cycle", lambda *_args, **_kwargs: pytest.fail("competing cycle"))
        assert cli.main(arguments) == 2
        assert health.read_runner_health(identity) == before and calls == []
    capsys.readouterr()


def test_missing_runtime_marker_cannot_reuse_an_old_healthy_appearance(watched, monkeypatch, capsys):
    (_cycle, _calls, _database, _root, _clock, _digest), identity, arguments = watched
    with health.prospective_runner_health_session(identity) as session:
        session.completed({"action": "idle", "fault": None, "state": {"status": "waiting"}}, 60)
    monkeypatch.delenv(cli.RUNTIME_DIGEST_ENV)
    assert cli.main(arguments) == 2
    assert health.inspect_runner_health(identity)["fault_codes"] == ["runner_not_active"]
    capsys.readouterr()
