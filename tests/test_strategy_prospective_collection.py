from contextlib import closing
from datetime import datetime
import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from app.services import strategy_prospective_collection as collection
from app.services import strategy_prospective_outcomes as outcomes
from app.services import strategy_prospective_plan as plans
from app.services import strategy_prospective_snapshot as snapshots
from app.services.trading_calendar import ASHARE_TIMEZONE
from tests.test_strategy_template_tracking import _clone_run, _seed_database, _sql


@pytest.fixture
def study(tmp_path, monkeypatch):
    database = _seed_database(tmp_path)
    root = tmp_path / "plans"
    clock = {"now": datetime(2026, 7, 16, 16, tzinfo=ASHARE_TIMEZONE)}
    for module in (plans, collection, outcomes):
        monkeypatch.setattr(module, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(plans, "_source_manifest", lambda: {"app/example.py": "a" * 64})
    row = _sql(database, "SELECT metrics_json FROM market_scan_result WHERE run_id=? LIMIT 1", (database[1],))[0]
    score = json.loads(row["metrics_json"])["score_details"]["score_spec"]
    monkeypatch.setattr(plans, "market_scan_score_spec", lambda **_kwargs: score)
    plans.create_strategy_prospective_plan(root, "study", start_date="2026-07-17", end_date="2026-08-20")
    clock["now"] = datetime(2026, 7, 17, 17, tzinfo=ASHARE_TIMEZONE)
    return database, root, clock


def _collect(study):
    database, root, _clock = study
    return collection.collect_strategy_prospective_inputs(database[0], root, "study")


def _receipts(study):
    return plans.read_strategy_prospective_receipts(study[1], "study")


def test_capture_freezes_all_input_rows_without_reading_future_prices(study, monkeypatch):
    connect = sqlite3.connect
    reads = []
    def opened(*args, **kwargs):
        conn = connect(*args, **kwargs)
        def authorize(action, table, *_rest):
            if action == sqlite3.SQLITE_READ:
                reads.append(table)
                if table == "kline_daily":
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn
    monkeypatch.setattr(collection.sqlite3, "connect", opened)
    result = _collect(study)
    assert result["receipts_added"][0]["status"] == "captured"
    assert "kline_daily" not in reads
    receipt = _receipts(study)[0]
    payload = receipt["payload"]
    assert datetime.fromisoformat(payload["available_at"]) < datetime.fromisoformat(payload["observed_at"])
    assert payload["available_at"] == payload["source_available_at"]
    archive = study[1] / "study" / payload["snapshot_path"]
    rows = [json.loads(line) for line in gzip.decompress(archive.read_bytes()).splitlines()]
    assert rows[0]["forward_prices_read"] is False
    assert len(rows) - 1 == payload["snapshot_result_count"] == payload["source_result_counts"]["total_count"]
    assert all(p["forward_return"] is None for s in payload["session"]["selections"] for p in s["positions"])
    snapshots.verify_strategy_scan_archive(archive.parent, payload)
    before = {p.name: p.read_bytes() for p in archive.parent.iterdir() if p.is_file()}
    assert _collect(study)["receipts_added"] == []
    assert before == {p.name: p.read_bytes() for p in archive.parent.iterdir() if p.is_file()}


def test_bad_first_original_is_retained_without_using_a_good_later_scan(study):
    database, root, _clock = study
    later = _clone_run(database)
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.execute("UPDATE market_scan_run SET snapshot_digest=? WHERE id=?", ("f" * 64, database[1]))
    assert later != database[1]
    assert _collect(study)["receipts_added"][0]["status"] == "source_incomplete"
    receipt = _receipts(study)[0]
    assert receipt["payload"]["run_id"] == database[1]
    assert receipt["payload"]["session"] is None
    assert receipt["payload"]["reason"] == "snapshot_seal_invalid"
    assert outcomes.prospective_captured_sessions(root, "study") == ()


def test_wrong_registered_score_is_an_explicit_source_gap(study):
    database, _root, _clock = study
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.execute("UPDATE market_scan_run SET rule_version='unregistered-rule' WHERE id=?", (database[1],))
    _collect(study)
    assert _receipts(study)[0]["payload"]["reason"] == "registered_score_contract_mismatch"


@pytest.mark.parametrize("both_invalid", [False, True])
def test_invalid_first_publication_time_cannot_select_a_later_good_scan(study, both_invalid):
    database, _root, _clock = study
    _clone_run(database)
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.execute("UPDATE market_scan_run SET finished_at='invalid' WHERE id=?", (database[1],))
        if both_invalid:
            conn.execute("UPDATE market_scan_run SET snapshot_sealed_at='invalid' WHERE id=?", (database[1],))
    result = _collect(study)
    assert result["receipts_added"][0]["status"] == "source_incomplete"
    assert _receipts(study)[0]["payload"]["run_id"] == database[1]


@pytest.mark.parametrize("hour", [8, 15])
def test_before_close_does_not_read_or_record_inputs(study, hour):
    database, root, clock = study
    clock["now"] = datetime(2026, 7, 17, hour, 0, tzinfo=ASHARE_TIMEZONE)
    assert collection.collect_strategy_prospective_inputs(Path("absent.sqlite3"), root, "study")["receipts_added"] == []
    assert not _receipts(study)


def test_future_phase_does_not_require_database(study):
    _database, root, clock = study
    clock["now"] = datetime(2026, 7, 16, 18, tzinfo=ASHARE_TIMEZONE)
    assert collection.collect_strategy_prospective_inputs(Path("absent.sqlite3"), root, "study")["receipts_added"] == []


def test_missed_deadline_is_permanent_even_if_database_contains_published_input(study):
    _database, root, clock = study
    clock["now"] = datetime(2026, 7, 17, 21, tzinfo=ASHARE_TIMEZONE)
    result = collection.collect_strategy_prospective_inputs(Path("absent.sqlite3"), root, "study")
    assert result["receipts_added"][0]["status"] == "missing"
    _collect(study)
    assert len(_receipts(study)) == 1 and _receipts(study)[0]["payload"]["session"] is None


def test_capture_crossing_deadline_remains_late(study, monkeypatch):
    _database, root, clock = study
    capture = collection._capture_source
    def delayed(*args, **kwargs):
        value = capture(*args, **kwargs)
        clock["now"] = datetime(2026, 7, 17, 21, tzinfo=ASHARE_TIMEZONE)
        return value
    monkeypatch.setattr(collection, "_capture_source", delayed)
    assert _collect(study)["receipts_added"][0]["status"] == "late"
    assert outcomes.prospective_captured_sessions(root, "study") == ()


def test_no_published_source_waits_before_deadline(study, monkeypatch):
    monkeypatch.setattr(collection, "_first_published_scan", lambda *_args: None)
    assert _collect(study)["receipts_added"] == []
    assert not _receipts(study)


def test_captured_inputs_survive_operational_database_removal(study):
    database, root, clock = study
    _collect(study)
    database[0].unlink()
    sessions = outcomes.prospective_captured_sessions(root, "study")
    assert len(sessions) == 1 and sessions[0].run_id == database[1]
    clock["now"] = datetime(2026, 8, 20, 17, tzinfo=ASHARE_TIMEZONE)
    result = outcomes.evaluate_prospective_execution(root, "study")
    report = json.loads(Path(result["report_path"]).read_text())
    assert report["adoptable_template_id"] is None and report["promotion_eligible"] is False
    selections = report["net_comparison"]["cohorts"][0]["sessions"][0]["selections"]
    assert all(s["net_return"] is None for s in selections)


def test_corrupt_retained_archive_rejects_future_evaluation(study):
    _collect(study)
    path = study[1] / "study" / _receipts(study)[0]["payload"]["snapshot_path"]
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="digest"):
        outcomes.prospective_captured_sessions(study[1], "study")


def test_research_collection_preserves_requirements_without_official_upgrade(study):
    _collect(study)
    result = outcomes.collect_prospective_execution_evidence(study[1], "study", None)
    report = json.loads(Path(result["report_path"]).read_text())
    assert report["official_execution_admitted"] is False
    assert report["requirements"]["pair_count"] > 0
    assert report["research"]["official_execution_admitted"] is False
    assert plans.strategy_prospective_status(study[1], "study")["receipt_count"] == 1


def test_archive_limits_fail_before_receipt_commit(study, monkeypatch):
    monkeypatch.setattr(snapshots, "MAX_SNAPSHOT_RAW_BYTES", 10)
    with pytest.raises(ValueError, match="uncompressed"):
        _collect(study)
    assert _receipts(study) == ()
