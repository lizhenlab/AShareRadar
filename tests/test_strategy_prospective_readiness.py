"""Readiness never turns a diagnosis into a receipt or execution evidence."""

from contextlib import closing
from datetime import UTC, date, datetime
import json
from pathlib import Path
import sqlite3

import pytest

from app.services import strategy_prospective_collection as collection
from app.services import strategy_prospective_plan as plans
from app.services import strategy_prospective_readiness as readiness
from app.services import trading_calendar
from app.services.trading_calendar import ASHARE_TIMEZONE
from tests import test_strategy_prospective_collection as collection_fixtures
from tests.test_strategy_template_tracking import _clone_run, _reseal

study = collection_fixtures.study


@pytest.fixture
def diagnostic_study(study, monkeypatch):
    monkeypatch.setattr(readiness, "utc_now", lambda: study[2]["now"])
    return study


def _diagnose(study_state):
    database, root, _clock = study_state
    return readiness.strategy_prospective_readiness(database[0], root, "study")


def _files(root: Path):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_readiness_preserves_database_and_archive_bytes_without_forward_reads(diagnostic_study, monkeypatch):
    database, root, _clock = diagnostic_study
    before = _files(root)
    database_before = database[0].read_bytes()
    connect = sqlite3.connect
    reads = set()
    def opened(*args, **kwargs):
        assert "mode=ro" in args[0] and kwargs["uri"] is True
        conn = connect(*args, **kwargs)
        def authorize(action, table, *_rest):
            if action == sqlite3.SQLITE_READ:
                reads.add(table)
                if table == "kline_daily":
                    return sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                          sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn
    monkeypatch.setattr(readiness.sqlite3, "connect", opened)
    report = _diagnose(diagnostic_study)
    assert report["read_only"] is True
    assert report["schema_version"] == "strategy-template-prospective-readiness-v2"
    assert report["framework"]["status"] == "matched"
    assert report["basket_input"]["status"] == "admissible"
    assert all(check["status"] == "passed" for check in report["basket_input"]["checks"].values())
    assert report["basket_input"]["creates_prospective_sample"] is False
    assert report["basket_input"]["completeness_status"] == "admissible"
    assert report["basket_input"]["current_readiness"]["status"] == "ready"
    assert report["basket_input"]["current_readiness"]["pending_conditions"] == []
    assert report["sample_maturity"]["captured_anchor_count"] == 0
    assert "kline_daily" not in reads
    assert _files(root) == before
    assert database[0].read_bytes() == database_before
    assert report["formal_execution"]["status"] == "not_assessed"
    assert len(report["formal_execution"]["missing_or_unverified_materials"]) == 4
    assert report["formal_execution"]["official_execution_admitted"] is False
    assert report["formal_execution"]["net_returns_available"] is False


def test_source_drift_keeps_overdue_counts_and_still_diagnoses_frozen_contract(diagnostic_study, monkeypatch):
    _database, _root, clock = diagnostic_study
    clock["now"] = datetime(2026, 7, 17, 21, tzinfo=ASHARE_TIMEZONE)
    monkeypatch.setattr(plans, "_source_manifest", lambda: {"app/example.py": "b" * 64})
    monkeypatch.setattr(plans, "market_scan_score_spec", lambda **_kwargs: pytest.fail("must not rebuild live score parameters"))
    report = _diagnose(diagnostic_study)
    assert report["framework"]["plan_status"] == "source_drift"
    assert report["framework"]["capture_blockers"] == ["source_drift"]
    assert report["framework"]["overdue_count"] == 1
    assert report["framework"]["receipt_count"] == 0
    assert report["basket_input"]["status"] == "admissible"
    assert report["basket_input"]["capture_blockers"] == ["source_drift"]
    assert report["basket_input"]["current_readiness"]["status"] == "blocked"
    assert report["basket_input"]["current_readiness"]["pending_conditions"] == ["source_drift"]
    assert report["sample_maturity"]["captured_anchor_count"] == 0


def test_bad_first_scan_is_diagnosed_without_selecting_good_rescan(diagnostic_study):
    database, _root, _clock = diagnostic_study
    later = _clone_run(database)
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.execute("UPDATE market_scan_run SET snapshot_digest=? WHERE id=?", ("f" * 64, database[1]))
    report = _diagnose(diagnostic_study)
    source = report["basket_input"]
    assert source["run_id"] == database[1] != later
    assert source["status"] == "source_incomplete"
    assert source["admission_reason"] == "snapshot_seal_invalid"
    assert source["checks"]["original_publication_seal"]["status"] == "failed"
    assert source["checks"]["point_in_time_features"]["status"] == "passed"


@pytest.mark.parametrize("reason,category", [
    ("日K停留在 2026-07-16，早于应有交易日 2026-07-17，可能停牌", "daily_bar_behind_required_session"),
    ("当日全天单一价格，无法确认开盘可成交性", "single_price_entry_execution_unknown"),
    ("供应商返回了不能验证的内容", "unclassified_recorded_failure"),
])
def test_missing_classification_never_authorizes_exclusion_or_skips_pit(diagnostic_study, reason, category):
    database, _root, _clock = diagnostic_study
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("""UPDATE market_scan_result SET status='missing',rank=NULL,score=NULL,raw_score=NULL,
            trend_score=NULL,leader_score=NULL,data_quality_score=NULL,reason=NULL,error=? WHERE rank=4""", (reason,))
        conn.execute("UPDATE market_scan_run SET status='degraded',success_count=3,missing_count=1")
        progress = json.loads(conn.execute("SELECT market_progress_json FROM market_scan_run").fetchone()[0])
        for market in progress:
            if market["market"] == "SH":
                market.update(success_count=1, missing_count=1, coverage_pct=50.0)
        conn.execute("UPDATE market_scan_run SET market_progress_json=?", (json.dumps(progress),))
        _reseal(conn, database[1])
    source = _diagnose(diagnostic_study)["basket_input"]
    assert source["admission_reason"] == "missing_frozen_universe"
    assert source["missing_reason_counts"] == {category: 1}
    missing = source["missing_classifications"][0]
    assert missing["category"] == category and missing["status"] == "missing"
    assert missing["exchange_session_state"] == missing["entry_execution_state"] == "unknown"
    assert missing["exclusion_authorized"] is False
    assert source["checks"]["no_missing_members"]["status"] == "failed"
    assert source["checks"]["point_in_time_features"]["status"] == "passed"


def test_frozen_contract_mismatch_is_explicit(diagnostic_study):
    database, _root, _clock = diagnostic_study
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.execute("UPDATE market_scan_run SET rule_version='unregistered-rule' WHERE id=?", (database[1],))
    source = _diagnose(diagnostic_study)["basket_input"]
    assert source["status"] == "source_incomplete"
    assert source["admission_reason"] == "registered_score_contract_mismatch"
    assert source["checks"]["frozen_score_contract"]["status"] == "failed"


def test_captured_maturity_is_distinct_from_formal_net_results(diagnostic_study):
    database, root, clock = diagnostic_study
    collection.collect_strategy_prospective_inputs(database[0], root, "study")
    before = _files(root)
    pending = _diagnose(diagnostic_study)["sample_maturity"]
    assert pending["captured_anchor_count"] == 1 and pending["captured_template_basket_count"] == 3
    assert pending["pending_captured_anchor_count"] == 1
    clock["now"] = datetime(2026, 8, 20, 17, tzinfo=ASHARE_TIMEZONE)
    report = _diagnose(diagnostic_study)
    assert report["sample_maturity"]["mature_captured_anchor_count"] == 1
    assert report["sample_maturity"]["formal_net_outcome_count"] is None
    assert report["formal_execution"]["net_returns_available"] is False
    assert _files(root) == before


def test_unknown_exit_and_day_publication_cutoff_remain_distinct():
    spec = {"signal_dates": ["2026-12-15", "2026-12-30"],
            "exit_dates": {"2026-12-15": "2026-12-30", "2026-12-30": None},
            "templates": [1, 2, 3], "inference_contract": {"minimum_complete_anchors": 20}}
    receipts = [{"trade_date": day, "status": "captured"} for day in spec["signal_dates"]]
    early = readiness._sample_maturity(spec, receipts, datetime(2026, 12, 30, 15, 14, tzinfo=ASHARE_TIMEZONE))
    late = readiness._sample_maturity(spec, receipts, datetime(2026, 12, 30, 15, 15, tzinfo=ASHARE_TIMEZONE))
    assert early["pending_captured_anchor_count"] == 1 and early["mature_captured_anchor_count"] == 0
    assert late["mature_captured_anchor_count"] == 1
    assert late["calendar_unavailable_captured_anchor_count"] == 1
    assert late["anchors"][-1]["exit_date"] is None


def test_missing_database_is_not_created_and_plan_remains_readable(diagnostic_study):
    _database, root, _clock = diagnostic_study
    absent = root.parent / "absent.sqlite3"
    report = readiness.strategy_prospective_readiness(absent, root, "study")
    assert report["framework"]["plan_receipt_integrity"] == "verified"
    assert report["basket_input"]["status"] == "unavailable"
    assert not absent.exists()


def test_invalid_plan_cannot_produce_green_layers(diagnostic_study):
    _database, root, _clock = diagnostic_study
    path = root / "study" / "plan.json"
    contents = json.loads(path.read_text())
    contents["digest"] = "0" * 64
    path.write_text(json.dumps(contents))
    report = _diagnose(diagnostic_study)
    assert report["framework"]["status"] == "unavailable"
    assert report["basket_input"]["status"] == report["sample_maturity"]["status"] == "not_assessed"
    assert report["formal_execution"]["official_execution_admitted"] is False
    assert report["basket_input"]["current_readiness"]["status"] == "unknown"


@pytest.mark.parametrize("observed,expected,status", [
    ("2026-07-17T17:00:00+08:00", "2026-07-17", "admissible"),
    ("2026-07-18T17:00:00+08:00", "2026-07-17", "admissible"),
    ("2026-07-19T23:00:00+08:00", "2026-07-17", "admissible"),
    ("2026-07-20T08:00:00+08:00", "2026-07-17", "admissible"),
    ("2026-07-20T15:14:59+08:00", "2026-07-17", "admissible"),
    ("2026-07-20T15:15:00+08:00", "2026-07-20", "source_stale"),
    ("2026-07-20T07:15:00+00:00", "2026-07-20", "source_stale"),
])
def test_freshness_uses_publication_cutoff_and_local_sessions_not_elapsed_days(diagnostic_study, observed, expected, status):
    database, _root, clock = diagnostic_study
    clock["now"] = datetime.fromisoformat(observed)
    source = _diagnose(diagnostic_study)["basket_input"]
    assert source["run_id"] == database[1]
    assert source["signal_date"] == source["freshness"]["source_date"] == "2026-07-17"
    assert source["freshness"]["expected_published_trade_date"] == expected
    assert source["freshness"]["publication_cutoff_local"] == "15:15:00"
    assert source["status"] == status and source["completeness_status"] == "admissible"
    current = status == "admissible"
    assert source["freshness"]["status"] == ("current" if current else "stale")
    assert source["current_readiness"]["status"] == ("ready" if current else "blocked")
    assert source["current_readiness"]["pending_conditions"] == ([] if current else ["expected_published_session_source_required"])
    assert source["creates_prospective_sample"] is False


def test_complete_but_old_source_and_identity_drift_are_both_reported(diagnostic_study, monkeypatch):
    _database, root, clock = diagnostic_study
    before = _files(root)
    clock["now"] = datetime(2026, 7, 20, 17, tzinfo=ASHARE_TIMEZONE)
    monkeypatch.setattr(plans, "_source_manifest", lambda: {"app/example.py": "b" * 64})
    monkeypatch.setattr(plans, "_calendar_matches", lambda _plan: False)
    report = _diagnose(diagnostic_study)
    source = report["basket_input"]
    assert source["status"] == "source_stale" and source["completeness_status"] == "admissible"
    assert source["current_readiness"]["status"] == "blocked"
    assert source["current_readiness"]["pending_conditions"] == [
        "source_drift", "calendar_drift", "expected_published_session_source_required",
    ]
    assert all(check["status"] == "passed" for check in source["checks"].values())
    assert report["framework"]["overdue_count"] == 1 and report["framework"]["receipt_count"] == 0
    assert _files(root) == before


def test_old_incomplete_first_source_stays_incomplete_without_rescan_fallback(diagnostic_study):
    database, _root, clock = diagnostic_study
    later = _clone_run(database)
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.execute("UPDATE market_scan_run SET snapshot_digest=? WHERE id=?", ("f" * 64, database[1]))
    clock["now"] = datetime(2026, 7, 20, 17, tzinfo=ASHARE_TIMEZONE)
    source = _diagnose(diagnostic_study)["basket_input"]
    assert source["run_id"] == database[1] != later
    assert source["status"] == source["completeness_status"] == "source_incomplete"
    assert source["freshness"]["status"] == "stale"
    assert source["admission_reason"] == "snapshot_seal_invalid"
    assert source["checks"]["point_in_time_features"]["status"] == "passed"
    assert source["current_readiness"]["pending_conditions"] == [
        "first_published_source_must_pass_frozen_input_contract", "expected_published_session_source_required",
    ]


@pytest.mark.parametrize("year,coverage", [(2026, "covered"), (2030, "unknown")])
def test_readiness_calendar_queries_are_offline_even_when_refresh_is_enabled(diagnostic_study, monkeypatch, year, coverage):
    _database, _root, clock = diagnostic_study
    clock["now"] = datetime(year, 7, 20, 17, tzinfo=ASHARE_TIMEZONE)
    monkeypatch.setattr(trading_calendar, "_should_auto_refresh", lambda *_args: True)
    monkeypatch.setattr(trading_calendar, "_trigger_auto_refresh", lambda: pytest.fail("readiness must not refresh calendar"))
    source = _diagnose(diagnostic_study)["basket_input"]
    assert source["freshness"]["calendar_policy"] == "local_only_no_auto_refresh"
    assert source["freshness"]["calendar_coverage"] == coverage
    assert source["completeness_status"] == ("admissible" if coverage == "covered" else "unknown")
    if coverage == "unknown":
        assert source["status"] == "freshness_unknown"
        assert source["freshness"]["status"] == "unknown"
        assert source["freshness"]["expected_published_trade_date"] is None
        assert source["current_readiness"]["status"] == "unknown"
        assert source["current_readiness"]["pending_conditions"] == [
            "source_integrity_assessment_required", "trusted_calendar_coverage_required",
        ]


def test_no_source_still_reports_expected_published_session(diagnostic_study):
    database, _root, clock = diagnostic_study
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.execute("UPDATE market_scan_run SET status='running'")
    clock["now"] = datetime(2026, 7, 20, 7, 15, tzinfo=UTC)
    source = _diagnose(diagnostic_study)["basket_input"]
    assert source["status"] == source["completeness_status"] == "no_source"
    assert source["freshness"]["source_date"] is None
    assert source["freshness"]["expected_published_trade_date"] == "2026-07-20"
    assert source["current_readiness"]["status"] == "blocked"
    assert source["current_readiness"]["pending_conditions"] == ["published_source_required"]


def test_source_ahead_of_publication_window_is_not_current():
    source = readiness.prospective_source_freshness("2026-07-20", datetime(2026, 7, 20, 8, tzinfo=ASHARE_TIMEZONE))
    assert source["status"] == "ahead_of_expected" and source["expected_published_trade_date"] == "2026-07-17"
    diagnosed = readiness._current_input_readiness({"status": "admissible", "capture_blockers": [], "freshness": source})
    assert diagnosed["status"] == "source_ahead_of_expected"
    assert diagnosed["completeness_status"] == "admissible"
    assert diagnosed["current_readiness"]["status"] == "blocked"


def test_covered_holiday_keeps_previous_published_source_current(monkeypatch):
    monkeypatch.setattr(trading_calendar, "_trade_days", lambda: frozenset({date(2026, 7, 17), date(2026, 7, 21)}))
    source = readiness.prospective_source_freshness("2026-07-17", datetime(2026, 7, 20, 17, tzinfo=ASHARE_TIMEZONE))
    assert source["status"] == "current" and source["expected_published_trade_date"] == "2026-07-17"
    assert source["calendar_days_since_source"] == 3


def test_empty_calendar_does_not_guess_weekdays(monkeypatch):
    monkeypatch.setattr(trading_calendar, "_trade_days", lambda: frozenset())
    source = readiness.prospective_source_freshness("2026-07-17", datetime(2026, 7, 20, 17, tzinfo=ASHARE_TIMEZONE))
    assert source["status"] == "unknown" and source["expected_published_trade_date"] is None


@pytest.mark.parametrize("day,freshness,state", [(17, "current", "unknown"), (20, "stale", "blocked")])
def test_source_calendar_validation_unknown_does_not_relabel_known_date(diagnostic_study, monkeypatch, day, freshness, state):
    def unavailable(*_args, **_kwargs):
        raise trading_calendar.TradingCalendarCoverageError("source next-session coverage missing")
    monkeypatch.setattr(readiness, "strategy_template_input_checks", unavailable)
    diagnostic_study[2]["now"] = datetime(2026, 7, day, 17, tzinfo=ASHARE_TIMEZONE)
    source = _diagnose(diagnostic_study)["basket_input"]
    assert source["status"] == "source_integrity_unknown" and source["completeness_status"] == "unknown"
    assert source["freshness"]["status"] == freshness
    assert source["current_readiness"]["status"] == state
    assert source["current_readiness"]["pending_conditions"] == [
        "source_integrity_assessment_required",
        *(["expected_published_session_source_required"] if freshness == "stale" else []),
        "source_validation_calendar_coverage_required",
    ]


@pytest.mark.parametrize("error", [RuntimeError("unexpected implementation failure"), KeyboardInterrupt()])
def test_calendar_diagnostic_does_not_own_unexpected_or_fatal_failures(diagnostic_study, monkeypatch, error):
    def fail(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(trading_calendar, "_calendar_resolution", fail)
    with pytest.raises(type(error)):
        _diagnose(diagnostic_study)
