from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

import pytest

from app.db.market_scan_integrity import seal_market_scan_snapshot
from app.services import market_scan_research_availability as availability
from app.services.market_scan_research_availability_contract import AVAILABILITY_PLAN_VERSION
from tests.test_market_scan_evaluation import _disable_market_scan_immutability, _initialize
from tests.test_market_scan_research_availability import DATES, plan
from tests.test_market_scan_research_inputs import _seed_run
from tools import audit_market_scan_availability as cli


def seed(path: Path, day= DATES[0], *, missing=False) -> int:
    run_id = _seed_run(path, mode="official", rule_version="test-v5", quote_date=day, ranks=("600001.SH",))
    with sqlite3.connect(path) as conn:
        _disable_market_scan_immutability(conn)
        conn.execute("UPDATE market_scan_run SET snapshot_digest=NULL, snapshot_seal_origin=NULL, snapshot_sealed_at=NULL WHERE id=?", (run_id,))
        if missing:
            conn.execute("UPDATE market_scan_run SET status='degraded', total_count=2, processed_count=2, missing_count=1 WHERE id=?", (run_id,))
            conn.execute("INSERT INTO market_scan_result(run_id,symbol,code,market,name,status,updated_at,error) VALUES(?,?,?,?,?,?,?,?)",
                         (run_id, "600002.SH", "600002", "SH", "missing", "missing", day + "T08:00:00Z", "provider timeout"))
        seal_market_scan_snapshot(conn, run_id, sealed_at=day + "T08:00:00Z")
    return run_id


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "runtime" / "research.sqlite3"
    path.parent.mkdir()
    _initialize(path)
    return path


def test_database_verified_missing_members_remain_outside_strict_research_admission(database):
    run_id = seed(database, missing=True)
    before = database.read_bytes()
    report = availability.audit_market_scan_availability(database, plan())
    day = report["days"][0]
    assert day["run_id"] == run_id and day["source_status"] == "database_snapshot_verified"
    assert day["status_counts"] == {"success": 1, "missing": 1, "pending": 0, "skipped": 0}
    assert day["strict_member_admission"] == "blocked"
    assert day["predecision_differences"]["observed_minus_unobserved"] is None
    assert report["returns_inspected"] is False
    assert database.read_bytes() == before


def test_tampered_selected_batch_is_blocked_without_falling_back(database):
    seed(database)
    selected = seed(database)
    with sqlite3.connect(database) as conn:
        _disable_market_scan_immutability(conn)
        conn.execute("UPDATE market_scan_result SET name='tampered' WHERE run_id=?", (selected,))
    report = availability.audit_market_scan_availability(database, plan())
    assert report["status"] == "blocked"
    assert report["days"][0]["run_id"] == selected
    assert report["days"][0]["member_count"] is None
    assert report["days"][0]["source_status"] == "unverified"


def test_sealed_missing_member_with_stale_kline_date_remains_auditable(database):
    run_id = seed(database, DATES[1], missing=True)
    with sqlite3.connect(database) as conn:
        _disable_market_scan_immutability(conn)
        conn.execute("UPDATE market_scan_run SET snapshot_digest=NULL, snapshot_seal_origin=NULL, snapshot_sealed_at=NULL WHERE id=?", (run_id,))
        conn.execute("UPDATE market_scan_result SET data_date=? WHERE run_id=? AND status='missing'", (DATES[0], run_id))
        seal_market_scan_snapshot(conn, run_id, sealed_at=DATES[1] + "T08:00:00Z")
    before = database.read_bytes()
    day = availability.audit_market_scan_availability(database, plan())["days"][1]
    assert day["source_status"] == "database_snapshot_verified"
    assert day["status_counts"]["missing"] == 1 and day["member_count"] == 2
    assert day["strict_member_admission"] == "blocked"
    assert database.read_bytes() == before


def test_no_database_is_created_and_all_calendar_dates_remain(tmp_path):
    absent = tmp_path / "absent.sqlite3"
    report = availability.audit_market_scan_availability(absent, plan())
    assert report["status"] == "blocked" and not absent.exists()
    assert len(report["days"]) == 5
    assert all(day["reason"] == "database_read_unavailable" for day in report["days"])


def test_only_selected_snapshots_are_verified_and_no_forward_tables_are_read(database, monkeypatch):
    selected = seed(database)
    seed(database, "2026-08-10")
    original_connect = availability.readonly_research_connection
    original_read = availability.read_research_session
    seen, reads = [], []

    @contextmanager
    def constrained_connect(path):
        with original_connect(path) as conn:
            def authorize(action, table, _column, _database, _trigger):
                if action == sqlite3.SQLITE_READ:
                    reads.append(table)
                    return sqlite3.SQLITE_OK if table in {"market_scan_run", "market_scan_result"} else sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            assert conn.in_transaction and conn.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("DELETE FROM market_scan_result")
            yield conn

    def tracked_read(conn, run_id):
        seen.append(run_id)
        return original_read(conn, run_id)

    monkeypatch.setattr(availability, "readonly_research_connection", constrained_connect)
    monkeypatch.setattr(availability, "read_research_session", tracked_read)
    assert availability.audit_market_scan_availability(database, plan())["days"][0]["source_status"] == "database_snapshot_verified"
    assert seen == [selected]
    assert set(reads) == {"market_scan_run", "market_scan_result"}


def test_snapshot_verification_and_member_reads_share_pinned_transaction(database, monkeypatch):
    selected = seed(database, missing=True)
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    original_read = availability.read_research_session

    def rotate_after_selection(conn, run_id):
        with sqlite3.connect(database) as writer:
            _disable_market_scan_immutability(writer)
            writer.execute("DELETE FROM market_scan_result WHERE run_id=?", (selected,))
            writer.execute("DELETE FROM market_scan_run WHERE id=?", (selected,))
        return original_read(conn, run_id)

    monkeypatch.setattr(availability, "read_research_session", rotate_after_selection)
    day = availability.audit_market_scan_availability(database, plan())["days"][0]
    assert day["source_status"] == "database_snapshot_verified" and day["member_count"] == 2
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 0


def test_cli_uses_declared_plan_and_creates_only_independent_report(database, tmp_path, monkeypatch):
    seed(database)
    source = tmp_path / "plan.json"
    source.write_text(json.dumps({"schema_version": AVAILABILITY_PLAN_VERSION, **asdict(plan(trading_dates=(DATES[0],)))}))
    output = tmp_path / "availability.json"
    monkeypatch.setattr("sys.argv", ["audit", "--database", str(database), "--plan", str(source), "--output", str(output)])
    before = database.read_bytes()
    assert cli.main() == 0
    assert json.loads(output.read_text())["days"][0]["source_status"] == "database_snapshot_verified"
    rendered = output.read_bytes()
    assert cli.main() == 2
    assert output.read_bytes() == rendered and database.read_bytes() == before


@pytest.mark.parametrize("target", ["database", "sidecar", "directory_alias", "plan", "existing", "output_alias"])
def test_cli_rejects_unsafe_output_before_database_audit(database, tmp_path, monkeypatch, target):
    source = tmp_path / "plan.json"
    source.write_text(json.dumps({"schema_version": AVAILABILITY_PLAN_VERSION, **asdict(plan())}))
    alias = tmp_path / "alias"
    alias.symlink_to(database.parent, target_is_directory=True)
    existing = tmp_path / "existing.json"
    existing.write_text("do not replace")
    link = tmp_path / "output-link.json"
    link.symlink_to(existing)
    output = {"database": database, "sidecar": Path(str(database) + "-wal"), "directory_alias": alias / "new.json",
              "plan": source, "existing": existing, "output_alias": link}[target]
    monkeypatch.setattr(cli, "audit_market_scan_availability", lambda *_args: pytest.fail("must reject output before reading database"))
    monkeypatch.setattr("sys.argv", ["audit", "--database", str(database), "--plan", str(source), "--output", str(output)])
    assert cli.main() == 2
    assert existing.read_text() == "do not replace"


def test_cli_rejects_sql_shaped_plan_without_touching_database(database, tmp_path, monkeypatch):
    source = tmp_path / "plan.json"
    source.write_text(json.dumps({"schema_version": AVAILABILITY_PLAN_VERSION, **asdict(plan()), "run_ids": ["1); DROP TABLE market_scan_run;--"],
                                  "selection_policy": "explicit-run-ids"}))
    output = tmp_path / "out.json"
    before = database.read_bytes()
    monkeypatch.setattr("sys.argv", ["audit", "--database", str(database), "--plan", str(source), "--output", str(output)])
    assert cli.main() == 2
    assert not output.exists() and database.read_bytes() == before
