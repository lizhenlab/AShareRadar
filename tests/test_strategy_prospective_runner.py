from contextlib import closing
from datetime import datetime
import json
import sqlite3

import pytest

from app.artifacts.io import ArtifactJsonDecodeError
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE
from app.services import strategy_prospective_runner as runner
from app.services import strategy_prospective_plan as plans
from app.services.market_scan_trial_registry_lock import trial_registry_execution_lock
from app.utils.database_identity import database_identity
from app.api.routes.health import _readiness_checks
from types import SimpleNamespace
from tests import test_strategy_prospective_collection as fixtures

study = fixtures.study


@pytest.fixture
def loop(study, monkeypatch):
    database, root, clock = study
    monkeypatch.setattr(runner, "utc_now", lambda: clock["now"])
    digest = plans.strategy_prospective_status(root, "study")["plan_digest"]
    calls = []
    def request(method, path, payload):
        calls.append((method, path, payload))
        if path.endswith("/ready"):
            return {"status": "ready", "checks": {"database_identity": database_identity(database[0])}}
        if path.endswith("/status"):
            return {"tasks": []}
        return {"accepted": True, "deduplicated": False, "run": {
            "id": database[1], "mode": "official", "data_date": clock["now"].date().isoformat(),
            "scope": MARKET_SCAN_FULL_MARKET_SCOPE,
        }}
    def cycle(request_override=None, control_override=None):
        return runner.run_strategy_prospective_cycle(database[0], root, "study",
            control_override or root.parent / "strategy_prospective_control", digest,
            request=request_override or request)
    return cycle, calls, database, root, clock, digest


def _no_publication(database):
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.execute("UPDATE market_scan_run SET status='failed'")


def test_published_first_batch_captured_without_http_and_retries_are_quiet(loop):
    cycle, calls, _database, root, _clock, _digest = loop
    first = cycle()
    assert first["receipts_added"][0]["status"] == "captured" and first["meaningful_change"]
    before = {p.name: p.read_bytes() for p in (root / "study").iterdir() if p.is_file()}
    second = cycle()
    assert second["receipts_added"] == [] and not second["meaningful_change"] and calls == []
    assert before == {p.name: p.read_bytes() for p in (root / "study").iterdir() if p.is_file()}


def test_source_drift_blocks_all_writes_to_study_but_new_overdue_notifies(loop, monkeypatch):
    cycle, calls, _database, root, clock, _digest = loop
    before = {p.name: p.read_bytes() for p in (root / "study").iterdir() if p.is_file()}
    monkeypatch.setattr(plans, "_source_manifest", lambda: {"app/example.py": "b" * 64})
    assert cycle()["action"] == "blocked"
    assert not cycle()["meaningful_change"]
    clock["now"] = datetime(2026, 7, 17, 21, tzinfo=fixtures.ASHARE_TIMEZONE)
    result = cycle()
    assert result["meaningful_change"] and result["state"]["overdue_count"] == 1
    assert calls == [] and before == {p.name: p.read_bytes() for p in (root / "study").iterdir() if p.is_file()}


@pytest.mark.parametrize("hour", [8, 15])
def test_pre_window_is_idle_with_zero_http(loop, hour):
    cycle, calls, _database, _root, clock, _digest = loop
    clock["now"] = datetime(2026, 7, 17, hour, tzinfo=fixtures.ASHARE_TIMEZONE)
    assert cycle()["action"] == "idle" and calls == []


def test_post_deadline_records_missing_without_http_or_database(loop):
    cycle, calls, database, _root, clock, _digest = loop
    clock["now"] = datetime(2026, 7, 17, 21, tzinfo=fixtures.ASHARE_TIMEZONE)
    database[0].unlink()
    result = cycle()
    assert result["receipts_added"][0]["status"] == "missing" and calls == []


def test_one_submission_survives_repeated_cycles_and_terminal_failure(loop):
    cycle, calls, database, _root, _clock, _digest = loop
    _no_publication(database)
    assert cycle()["action"] == "scan_submitted"
    result = cycle()
    assert result["fault"] == "scan_failed" and result["meaningful_change"]
    assert not cycle()["meaningful_change"]
    assert sum(method == "POST" for method, _path, _payload in calls) == 1


@pytest.mark.parametrize("exception", [TimeoutError("secret response"), ArtifactJsonDecodeError()])
def test_unknown_http_outcome_is_persisted_and_never_retried(loop, exception):
    cycle, calls, database, _root, _clock, _digest = loop
    _no_publication(database)
    def timeout(method, path, payload):
        calls.append((method, path, payload))
        if method == "POST":
            raise exception
        if path.endswith("/ready"):
            return {"status": "ready", "checks": {"database_identity": database_identity(database[0])}}
        return {"tasks": []}
    result = cycle(timeout)
    assert result["action"] == "submission_uncertain" and "secret" not in json.dumps(result)
    assert not cycle(timeout)["meaningful_change"]
    assert len([call for call in calls if call[0] == "POST"]) == 1


def test_crashed_reservation_keeps_gap_without_posting_again(loop):
    cycle, calls, database, root, _clock, _digest = loop
    _no_publication(database)
    control = root.parent / "strategy_prospective_control/study"
    runner._publish(control / "attempt-2026-07-17.json", {"trade_date": "2026-07-17"})
    assert cycle()["fault"] == "reserved_without_acknowledgement" and calls == []


def test_wrong_database_service_never_consumes_attempt_or_submits(loop):
    cycle, calls, database, root, _clock, _digest = loop
    _no_publication(database)
    def wrong(*_args):
        return {"status": "ready", "checks": {"database_identity": "another database"}}
    assert cycle(wrong)["action"] == "service_failed"
    assert not list((root.parent / "strategy_prospective_control").rglob("attempt-*.json")) and calls == []


def test_user_active_scan_is_left_running_without_http(loop):
    cycle, calls, database, _root, _clock, _digest = loop
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.execute("UPDATE market_scan_run SET status='running',mode='intraday'")
    assert cycle()["action"] == "waiting_for_scan" and calls == []


def test_first_bad_published_batch_is_retained_without_rescan(loop):
    cycle, calls, database, root, _clock, _digest = loop
    fixtures._clone_run(database)
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.execute("UPDATE market_scan_run SET snapshot_digest=? WHERE id=?", ("f" * 64, database[1]))
    result = cycle()
    assert result["receipts_added"][0]["status"] == "source_incomplete" and calls == []
    receipt = plans.read_strategy_prospective_receipts(root, "study")[0]
    assert receipt["payload"]["run_id"] == database[1]


def test_attempt_cannot_be_bypassed_by_changing_control_directory(loop):
    cycle, _calls, database, root, _clock, _digest = loop
    _no_publication(database)
    cycle()
    with pytest.raises(ValueError, match="canonical"):
        cycle(control_override=root.parent / "alternate-control")


def test_parallel_cycle_fails_without_http_or_study_changes(loop):
    cycle, calls, _database, root, _clock, _digest = loop
    with trial_registry_execution_lock(root.parent / "strategy_prospective_control/study"):
        with pytest.raises(ValueError, match="already active"):
            cycle()
    assert calls == [] and plans.strategy_prospective_status(root, "study")["receipt_count"] == 0


def test_durable_attempt_crossing_cutoff_never_posts(loop, monkeypatch):
    cycle, calls, database, _root, clock, _digest = loop
    _no_publication(database)
    original = runner._publish
    def delayed(path, value):
        original(path, value)
        if path.name.startswith("attempt-"):
            clock["now"] = datetime(2026, 7, 17, 20, tzinfo=fixtures.ASHARE_TIMEZONE)
    monkeypatch.setattr(runner, "_publish", delayed)
    assert cycle()["fault"] == "cutoff_elapsed_without_submission"
    assert not any(method == "POST" for method, _path, _payload in calls)


def test_capture_fault_after_committing_missing_still_observes_new_head(loop, monkeypatch):
    cycle, calls, _database, root, clock, _digest = loop
    clock["now"] = datetime(2026, 7, 17, 21, tzinfo=fixtures.ASHARE_TIMEZONE)
    def partial(*_args):
        plans.append_strategy_prospective_receipt(root, "study", "2026-07-17", {
            "status": "missing", "session": None, "available_at": None, "reason": "not_captured_before_deadline",
        })
        raise ValueError("unsafe-payload")
    monkeypatch.setattr(runner, "collect_strategy_prospective_inputs", partial)
    result = cycle()
    assert result["action"] == "capture_failed" and result["state"]["receipt_count"] == 1 and calls == []


@pytest.mark.parametrize("url", ["https://127.0.0.1:8010", "http://example.com:8010", "http://user:pass@127.0.0.1:8010", "http://127.0.0.1:8010/evil"])
def test_remote_credentials_and_non_loopback_service_are_rejected(url):
    with pytest.raises(ValueError, match="loopback"):
        runner.local_service_request(url)


def test_database_identity_is_opaque_and_based_on_real_path(tmp_path):
    alias = tmp_path / "alias"
    database = tmp_path / "database.sqlite3"
    alias.symlink_to(database)
    assert database_identity(alias) == database_identity(database)
    assert str(tmp_path) not in database_identity(database)


def test_health_binding_uses_actual_cache_path_without_creating_a_database(tmp_path):
    path = tmp_path / "not-created.sqlite3"
    checks = _readiness_checks(SimpleNamespace(cache=SimpleNamespace(path=path)), SimpleNamespace(runtime_coordinator=None))
    assert checks["database_identity"] == database_identity(path) and not path.exists()
