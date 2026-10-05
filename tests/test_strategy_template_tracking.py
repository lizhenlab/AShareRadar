"""Frozen strategy selection is independent of mutable forward labels."""

from contextlib import closing
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from app.db.market_scan_integrity import market_scan_snapshot_digest
from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market import DAILY_KLINE_CONTRACT_VERSION
from app.models.strategy_lab import StrategySpecInput
from app.services import strategy_template_tracking as tracking
from app.services.trading_calendar import ASHARE_TIMEZONE, next_trade_dates
from app.services.market_scan_score_contract import stable_score_spec_hash
from app.services.market_scan_scoring import market_scan_score_spec
from tests.test_market_scan_scoring import _as_v4_score_details
from tests.market_scan_test_support import _daily_rows
from tests.test_strategy_automation_atomic_completion import _isolated_environment
from tests.test_strategy_execution import _disable_market_scan_immutability


AS_OF = datetime(2026, 8, 20, 16, tzinfo=ASHARE_TIMEZONE)


def _narrow_rows(*args, **kwargs):
    return [row.model_copy(update={"open": row.close, "high": row.close + .06, "low": row.close - .06})
            for row in _daily_rows(*args, **kwargs)]


def _seed_database(directory: Path) -> tuple[Path, int]:
    with patch("tests.test_strategy_execution._daily_rows", side_effect=_narrow_rows):
        with _isolated_environment(directory) as (cache, _service, _strategy_id, run_id):
            path = cache.path
            with cache._connect() as conn:
                _disable_market_scan_immutability(conn)
                conn.execute("UPDATE market_scan_result SET updated_at = '2026-07-17T08:31:00Z' WHERE run_id=?", (run_id,))
                conn.execute("""UPDATE market_scan_run SET created_at='2026-07-17T08:30:00Z',
                    started_at='2026-07-17T08:30:00Z',finished_at='2026-07-17T08:31:00Z',
                    updated_at='2026-07-17T08:31:00Z',snapshot_sealed_at='2026-07-17T08:31:00Z',
                    quote_capture_started_at='2026-07-17T08:30:00Z',quote_capture_finished_at='2026-07-17T08:30:00Z'
                    WHERE id=?""", (run_id,))
                rows = conn.execute("SELECT symbol,metrics_json FROM market_scan_result WHERE run_id=?", (run_id,)).fetchall()
                details = json.loads(rows[0]["metrics_json"])["score_details"]
                conn.execute("INSERT INTO market_scan_rule_contract VALUES (?,?,?,?,?)", (
                    "full-market-score-v4-test", "{}", details["score_spec"]["rule_version"],
                    details["score_spec_hash"], "2026-07-17T08:30:00Z",
                ))
                _reseal(conn, run_id)
                for row in rows:
                    detail = json.loads(row["metrics_json"])["score_details"]
                    signal = detail["components"]["score_dimensions"]["point_in_time_evidence"]["payload"]["bar_contract_61"][-1]
                    _insert_prices(conn, row["symbol"], signal)
            return Path(path), run_id


def _insert_prices(conn, symbol, signal):
    days = [date.fromisoformat(signal[0]), *next_trade_dates(date.fromisoformat(signal[0]), 20)]
    for index, day in enumerate(days):
        price = signal[2] * (1 + .01 * index)
        prices = signal[1:5] if index == 0 else (price, price, price + .06, price - .06)
        conn.execute("""INSERT INTO kline_daily
            (symbol,adjustment_mode,date,open,close,high,low,volume,source,data_version,
             contract_version,corporate_action_status,as_of,fetched_at)
            VALUES (?,'qfq',?,?,?,?,?,1000000,'合成前复权','tracking-vintage',?,'none',?,?)""",
            (symbol, day.isoformat(), *prices, DAILY_KLINE_CONTRACT_VERSION,
             f"{day.isoformat()}T15:30:00+08:00", f"{day.isoformat()}T07:30:00Z"))


def _reseal(conn, run_id):
    conn.execute("UPDATE market_scan_run SET snapshot_digest=? WHERE id=?", (market_scan_snapshot_digest(conn, run_id), run_id))


@pytest.fixture
def database(tmp_path):
    return _seed_database(tmp_path)


def _report(database, **kwargs):
    path, _run_id = database
    return tracking.evaluate_strategy_template_tracking(path, as_of=kwargs.pop("as_of", AS_OF), **kwargs)


def _selections(report):
    return report["cohorts"][0]["sessions"][0]["selections"]


def _sql(database, statement, parameters=()):
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(statement, parameters).fetchall()


def _freeze_session(database, *, templates=None, authorizer=None, **kwargs):
    metadata = templates if templates is not None else tracking.strategy_template_tracking_contracts()[1]
    with closing(sqlite3.connect(f"{database[0].as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        if authorizer is not None:
            conn.set_authorizer(authorizer)
        row = conn.execute("""SELECT r.*,c.production_score_spec_hash AS declared_score_spec_hash
            FROM market_scan_run r LEFT JOIN market_scan_rule_contract c ON c.rule_version=r.rule_version
            WHERE r.id=?""", (database[1],)).fetchone()
        arguments = {"as_of": AS_OF, "horizon": 10, "notional_cash_cny": 1_000_000, "templates": metadata}
        arguments.update(kwargs)
        return tracking.freeze_strategy_template_session(conn, row, **arguments)


def _clone_run(database, *, rule=None, score_spec=None, finished="2026-07-17T08:32:00Z"):
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.row_factory = sqlite3.Row
        original = dict(conn.execute("SELECT * FROM market_scan_run WHERE id=?", (database[1],)).fetchone())
        old_id = original.pop("id")
        original.update(finished_at=finished, updated_at=finished, snapshot_sealed_at=finished)
        if rule:
            original["rule_version"] = rule
        columns = ",".join(original)
        new_id = conn.execute(f"INSERT INTO market_scan_run ({columns}) VALUES ({','.join('?' for _ in original)})", tuple(original.values())).lastrowid
        for source in conn.execute("SELECT * FROM market_scan_result WHERE run_id=?", (old_id,)).fetchall():
            row = dict(source)
            row["run_id"] = new_id
            metrics = json.loads(row["metrics_json"])
            details = metrics["score_details"]
            if rule:
                details["run_rule_version"] = rule
            if score_spec is not None:
                details["score_spec"] = score_spec
                details["score_spec_hash"] = stable_score_spec_hash(score_spec)
            row["metrics_json"] = json.dumps(metrics)
            conn.execute(f"INSERT INTO market_scan_result ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        if score_spec is not None:
            conn.execute("INSERT INTO market_scan_rule_contract VALUES (?,?,?,?,?)", (
                rule, "{}", score_spec["rule_version"], stable_score_spec_hash(score_spec), "2026-07-17T08:30:00Z",
            ))
        _reseal(conn, new_id)
    return new_id


def test_three_templates_use_real_sealed_evidence_and_gross_frozen_weights(database):
    report = _report(database)
    assert report["source_session_count"] == 1 and report["run_failures"] == []
    assert report["source"]["provider_calls"] == 0
    assert report["source"]["strategy_or_execution_rows_written"] == 0
    assert report["costs_deducted"] is False and report["promotion_eligible"] is False
    assert {row["template_id"] for row in report["templates"]} == set(tracking.STRATEGY_TEMPLATE_TRACKING_IDS)
    assert all(row["strategy_spec"] and len(row["strategy_fingerprint"]) == 64 for row in report["templates"])
    for selection in _selections(report):
        assert selection["outcome_status"] == "available"
        assert len(selection["positions"]) == 4
        assert selection["target_invested_weight"] < .5
        assert selection["weighted_gross_return"] == pytest.approx(selection["target_invested_weight"] * .1)
        assert selection["estimated_round_trip_cost_cny"] > 0


def test_report_is_read_only_repeatable_and_preserves_database_bytes(database):
    path, _run_id = database
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    counts = _sql(database, "SELECT (SELECT COUNT(*) FROM strategy_spec),(SELECT COUNT(*) FROM strategy_execution)")
    first = _report(database)
    assert _report(database) == first
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert [tuple(row) for row in _sql(database, "SELECT (SELECT COUNT(*) FROM strategy_spec),(SELECT COUNT(*) FROM strategy_execution)")] == [tuple(row) for row in counts]


def test_all_three_selections_finish_before_first_forward_query(database, monkeypatch):
    observed = []
    build, read = tracking.build_portfolio_draft, tracking._forward_bars
    def counted(*args, **kwargs):
        observed.append("selection")
        return build(*args, **kwargs)
    def assert_ready(*args, **kwargs):
        assert observed == ["selection"] * 3
        observed.append("forward")
        return read(*args, **kwargs)
    monkeypatch.setattr(tracking, "build_portfolio_draft", counted)
    monkeypatch.setattr(tracking, "_forward_bars", assert_ready)
    _report(database)
    assert observed == ["selection"] * 3 + ["forward"]


def test_concurrent_future_price_refresh_cannot_split_the_read_snapshot(database, monkeypatch):
    build = tracking.build_portfolio_draft
    count = 0
    def refresh_during_selection(*args, **kwargs):
        nonlocal count
        count += 1
        result = build(*args, **kwargs)
        if count == 3:
            _sql(database, "UPDATE kline_daily SET open=open*1.2,close=close*1.2,high=high*1.2,low=low*1.2 WHERE date='2026-07-31'")
        return result
    monkeypatch.setattr(tracking, "build_portfolio_draft", refresh_during_selection)
    original_snapshot = _selections(_report(database))
    next_snapshot = _selections(_report(database))
    for old, new in zip(original_snapshot, next_snapshot, strict=True):
        assert old["result_digest"] == new["result_digest"]
        assert old["weighted_gross_return"] == pytest.approx(old["target_invested_weight"] * .1)
        assert new["weighted_gross_return"] == pytest.approx(new["target_invested_weight"] * .32)


def test_future_mutation_changes_outcomes_without_changing_any_selection(database):
    first = _selections(_report(database))
    _sql(database, "UPDATE kline_daily SET open=open*1.2,close=close*1.2,high=high*1.2,low=low*1.2 WHERE date='2026-07-31'")
    second = _selections(_report(database))
    for old, new in zip(first, second, strict=True):
        assert old["execution_fingerprint"] == new["execution_fingerprint"]
        assert old["result_digest"] == new["result_digest"]
        assert [(row["symbol"], row["target_weight"]) for row in old["positions"]] == [(row["symbol"], row["target_weight"]) for row in new["positions"]]
        assert old["weighted_gross_return"] != new["weighted_gross_return"]


@pytest.mark.parametrize("statement,reason", [
    ("DELETE FROM kline_daily WHERE symbol='600001.SH' AND date='2026-07-20'", "holding_path_bar_missing_or_duplicate"),
    ("UPDATE kline_daily SET corporate_action_status='effective_event' WHERE symbol='600001.SH' AND date='2026-07-20'", "corporate_action_effective_event"),
    ("UPDATE kline_daily SET corporate_action_status='unknown' WHERE symbol='600001.SH' AND date='2026-07-20'", "corporate_action_status_unknown"),
    ("UPDATE kline_daily SET data_version='other' WHERE symbol='600001.SH' AND date='2026-07-20'", "mixed_forward_price_vintages"),
    ("UPDATE kline_daily SET fetched_at='2027-01-01T00:00:00Z' WHERE symbol='600001.SH' AND date='2026-07-20'", "forward_observation_after_as_of_or_unknown"),
    ("UPDATE kline_daily SET as_of='2027-01-01T00:00:00Z' WHERE symbol='600001.SH' AND date='2026-07-20'", "forward_snapshot_time_invalid"),
    ("UPDATE kline_daily SET as_of='2026-07-20T13:00:00+08:00' WHERE symbol='600001.SH' AND date='2026-07-20'", "forward_snapshot_time_invalid"),
    ("UPDATE kline_daily SET close=close+0.01 WHERE symbol='600001.SH' AND date='2026-07-17'", "target_adjustment_rebase_conflict"),
])
def test_one_missing_or_incomparable_selected_stock_invalidates_whole_basket(database, statement, reason):
    before = _selections(_report(database))
    _sql(database, statement)
    for old, new in zip(before, _selections(_report(database)), strict=True):
        assert new["outcome_status"] == "missing"
        assert new["weighted_gross_return"] is None
        assert new["available_outcome_count"] == 3
        assert new["missing_reason_counts"] == {reason: 1}
        assert old["result_digest"] == new["result_digest"]


def test_future_cache_does_not_turn_unmatured_signal_into_known_return(database):
    report = _report(database, as_of=datetime(2026, 7, 20, 15, 14, 59, tzinfo=ASHARE_TIMEZONE), horizon=1)
    assert all(row["outcome_status"] == "pending" and row["available_outcome_count"] == 0 for row in _selections(report))
    complete = _report(database, as_of=datetime(2026, 7, 20, 16, tzinfo=ASHARE_TIMEZONE), horizon=1)
    assert all(row["outcome_status"] == "available" for row in _selections(complete))


@pytest.mark.parametrize("horizon", [1, 5, 10, 20])
def test_supported_horizons_bind_exact_target_session(database, horizon):
    report = _report(database, horizon=horizon)
    session = report["cohorts"][0]["sessions"][0]
    assert session["target_date"] == next_trade_dates(date(2026, 7, 17), horizon)[-1].isoformat()
    assert all(row["weighted_gross_return"] == pytest.approx(row["target_invested_weight"] * horizon / 100) for row in session["selections"])


@pytest.mark.parametrize("kwargs", [{"horizon": True}, {"horizon": 2}, {"notional_cash_cny": float("inf")},
    {"notional_cash_cny": True}, {"run_ids": [True]}, {"run_ids": [-1]}, {"run_ids": [1.5]},
    {"as_of": datetime(2100, 1, 1, tzinfo=ASHARE_TIMEZONE)}])
def test_invalid_inputs_fail_without_touching_database(tmp_path, kwargs):
    with pytest.raises(ValueError):
        tracking.evaluate_strategy_template_tracking(tmp_path / "nonexistent.db", **kwargs)


def test_empty_explicit_run_list_has_no_sessions(database):
    report = _report(database, run_ids=[])
    assert report["source_session_count"] == 0 and report["cohorts"] == []


def test_no_candidate_is_explicit_no_selection_not_zero_return(database):
    report = _report(database, notional_cash_cny=10_000.0)
    for selection in _selections(report):
        assert selection["status"] == "no_trade"
        assert selection["outcome_status"] == "no_selection"
        assert selection["weighted_gross_return"] is None
        assert not selection["positions"] and selection["reasons"]


def test_duplicate_scans_choose_earliest_before_outcomes_and_do_not_fallback(database):
    second = _clone_run(database)
    report = _report(database)
    assert report["source_session_count"] == 1
    assert report["cohorts"][0]["sessions"][0]["run_id"] == database[1]
    assert report["excluded_runs"] == [{"run_id": second, "reason": "same_contract_session_rescan"}]
    _sql(database, "UPDATE market_scan_run SET snapshot_digest=? WHERE id=?", ("0" * 64, database[1]))
    failed = _report(database)
    assert failed["source_session_count"] == 0
    assert failed["run_failures"][0]["run_id"] == database[1]
    assert failed["excluded_runs"] == report["excluded_runs"]


def test_score_contracts_are_separate_even_on_the_same_signal_date(database):
    second = _clone_run(database, rule="tracking-quality60", score_spec=market_scan_score_spec(min_data_quality_score=60))
    report = _report(database)
    assert report["run_failures"] == []
    assert len(report["cohorts"]) == 2 and report["source_session_count"] == 2
    assert len({row["score_spec_hash"] for row in report["cohorts"]}) == 2
    assert {row["sessions"][0]["run_id"] for row in report["cohorts"]} == {database[1], second}


def test_published_after_cutoff_is_excluded_before_selection(database):
    later = _clone_run(database, finished="2026-09-01T08:00:00Z")
    report = _report(database)
    assert report["excluded_runs"] == [{"run_id": later, "reason": "published_after_as_of"}]


def test_session_limit_is_explicit_instead_of_silent_truncation(database, monkeypatch):
    _clone_run(database, rule="tracking-quality60", score_spec=market_scan_score_spec(min_data_quality_score=60))
    monkeypatch.setattr(tracking, "MAX_STRATEGY_TRACKING_SESSIONS", 1)
    with pytest.raises(ValueError, match="最多接受200"):
        _report(database)


def test_explicit_id_subset_is_recorded_and_missing_file_is_not_created(tmp_path, database):
    second = _clone_run(database)
    report = _report(database, run_ids=[second, second])
    assert report["source"]["run_id_filter_applied"] is True
    assert report["source_session_count"] == 0
    assert report["excluded_runs"] == [
        {"run_id": second, "reason": "same_contract_session_rescan"},
        {"run_id": database[1], "reason": "canonical_run_not_requested"},
    ]
    canonical = _report(database, run_ids=[database[1], second])
    assert canonical["cohorts"][0]["sessions"][0]["run_id"] == database[1]
    missing = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        tracking.evaluate_strategy_template_tracking(missing, as_of=AS_OF)
    assert not missing.exists()


@pytest.mark.parametrize("mutation,reason", [
    ("late_publication", "signal_availability_invalid"), ("legacy_seal", "snapshot_seal_invalid"),
    ("invalid_digest", "snapshot_seal_invalid"), ("unknown_contract", "score_contract_unregistered"),
    ("unbound_raw", "pit_feature_binding_invalid"),
])
def test_unavailable_frozen_snapshot_is_reported_without_repair(database, mutation, reason):
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.row_factory = sqlite3.Row
        if mutation == "late_publication":
            conn.execute("UPDATE market_scan_run SET finished_at='2026-07-20T02:00:00Z',updated_at='2026-07-20T02:00:00Z',snapshot_sealed_at='2026-07-20T02:00:00Z'")
        elif mutation == "legacy_seal":
            conn.execute("UPDATE market_scan_run SET snapshot_seal_origin='legacy_backfill'")
        elif mutation == "invalid_digest":
            conn.execute("UPDATE market_scan_run SET snapshot_digest=?", ("f" * 64,))
        elif mutation == "unknown_contract":
            conn.execute("DROP TRIGGER trg_market_scan_rule_contract_immutable_delete")
            conn.execute("DELETE FROM market_scan_rule_contract")
        else:
            row = conn.execute("SELECT symbol,metrics_json FROM market_scan_result LIMIT 1").fetchone()
            metrics = json.loads(row["metrics_json"])
            metrics["score_details"]["components"]["score_dimensions"]["raw_features"]["atr20_pct"] = 0.0
            conn.execute("UPDATE market_scan_result SET metrics_json=? WHERE symbol=?", (json.dumps(metrics), row["symbol"]))
        if mutation != "invalid_digest":
            _reseal(conn, database[1])
    report = _report(database)
    assert report["source_session_count"] == 0
    assert report["run_failures"][0]["run_id"] == database[1]
    assert report["run_failures"][0]["reason"] == reason
    assert report["status"] == "insufficient_data"


def test_missing_frozen_member_does_not_become_an_implicit_filtered_survivor(database, monkeypatch):
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("""UPDATE market_scan_result SET status='missing',rank=NULL,score=NULL,raw_score=NULL,
            trend_score=NULL,leader_score=NULL,data_quality_score=NULL WHERE rank=4""")
        conn.execute("UPDATE market_scan_run SET status='degraded',success_count=3,missing_count=1")
        progress = json.loads(conn.execute("SELECT market_progress_json FROM market_scan_run").fetchone()[0])
        for market in progress:
            if market["market"] == "SH":
                market.update(success_count=1, missing_count=1, coverage_pct=50.0)
        conn.execute("UPDATE market_scan_run SET market_progress_json=?", (json.dumps(progress),))
        _reseal(conn, database[1])
    def unexpected(*args, **kwargs):
        pytest.fail("must reject missing members before portfolio selection")
    monkeypatch.setattr(tracking, "build_portfolio_draft", unexpected)
    report = _report(database)
    assert report["source_session_count"] == 0 and len(report["run_failures"]) == 1
    assert report["run_failures"][0]["reason"] == "missing_frozen_universe"


@pytest.mark.parametrize("statement,reason", [
    ("UPDATE market_scan_result SET quote_observed_at='2026-07-20T02:00:00Z'", "decision_time_invalid"),
    ("UPDATE market_scan_run SET quote_capture_finished_at=NULL", "decision_time_invalid"),
    ("UPDATE market_scan_run SET created_at='2026-07-20T02:00:00Z'", "frozen_snapshot_invalid"),
])
def test_backfilled_decision_timestamps_cannot_hide_behind_early_publication(database, statement, reason):
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute(statement)
        _reseal(conn, database[1])
    report = _report(database)
    assert report["source_session_count"] == 0 and len(report["run_failures"]) == 1
    assert report["run_failures"][0]["reason"] == reason


def test_legacy_v4_remains_auditable_but_its_downside_definition_cannot_select_low_volatility(database):
    with closing(sqlite3.connect(database[0])) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DROP TRIGGER trg_market_scan_rule_contract_immutable_update")
        for row in conn.execute("SELECT symbol,metrics_json FROM market_scan_result").fetchall():
            metrics = json.loads(row["metrics_json"])
            details = _as_v4_score_details(metrics["score_details"])
            metrics["score_details"] = details
            final = details["components"]["final_score"]
            conn.execute("UPDATE market_scan_result SET metrics_json=?,score=?,raw_score=? WHERE symbol=?",
                         (json.dumps(metrics), final["score"], final["raw"], row["symbol"]))
        conn.execute("UPDATE market_scan_rule_contract SET production_score_rule_version=?,production_score_spec_hash=?",
                     (details["score_spec"]["rule_version"], details["score_spec_hash"]))
        _reseal(conn, database[1])
    report = _report(database)
    assert report["run_failures"] == []
    selections = {row["template_id"]: row for row in _selections(report)}
    assert selections["medium_momentum"]["outcome_status"] == "available"
    assert selections["low_volatility_trend"]["outcome_status"] == "no_selection"
    assert selections["low_volatility_trend"]["rejection_counts"]


def test_scan_header_limit_is_explicit(database, monkeypatch):
    _clone_run(database)
    monkeypatch.setattr(tracking, "_MAX_SCAN_HEADERS", 1)
    with pytest.raises(ValueError, match="批次头超过"):
        _report(database)


def test_safe_admission_reason_never_exposes_internal_exception_text(database, monkeypatch):
    private_detail = "private-fixture-path-and-internal-SQL"
    def invalid(*args, **kwargs):
        raise ValueError(private_detail)
    monkeypatch.setattr(tracking, "_require_registered_contract", invalid)
    report = _report(database)
    failure = report["run_failures"][0]
    assert failure["reason"] == "score_contract_unregistered"
    assert failure["error_type"] == "ValueError"
    assert private_detail not in json.dumps(report)


def test_net_evidence_is_separate_from_available_gross_returns(database):
    report = _report(database)
    assert report['schema_version'] == 'strategy-template-tracking-v2'
    assert all(row['outcome_status'] == 'available' for row in _selections(report))
    net = report['net_comparison']
    assert net['entry_policy'] == 'D+1-open' and net['exit_policy'] == 'D+H+1-close'
    assert net['execution_evidence']['provenance_status'] == 'unavailable'
    assert all(row['net_return'] is None for row in net['cohorts'][0]['sessions'][0]['selections'])
    assert report['strategy_selection']['adoptable_template_id'] is None
    assert 'official_execution_evidence_unavailable' in report['strategy_selection']['adoption_blockers']


def test_complete_empty_report_still_explains_why_no_strategy_can_be_adopted(database):
    report = _report(database, run_ids=[])
    assert report['net_comparison']['cohorts'] == []
    assert report['strategy_selection']['status'] == 'insufficient_data'
    assert report['strategy_selection']['adoptable_template_id'] is None


def test_fixed_nonoverlapping_schedule_keeps_missing_calendar_anchor(tmp_path):
    first = date(2026, 7, 17)
    days = [first, *next_trade_dates(first, 24)]
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE runs (id INTEGER, data_date TEXT, rule_version TEXT, declared_score_spec_hash TEXT)')
        conn.executemany('INSERT INTO runs VALUES (?,?,?,?)',
                         [(index + 1, day.isoformat(), 'rule', 'a' * 64) for index, day in enumerate(days) if index != 11])
        rows = conn.execute('SELECT * FROM runs ORDER BY id').fetchall()
    excluded = []
    selected = tracking._scheduled_tracking_runs(rows, 11, excluded)
    assert [row['data_date'] for row in selected] == [days[0].isoformat(), days[22].isoformat()]
    assert all(item['reason'] == 'non_overlapping_signal_schedule' for item in excluded)
    assert len(excluded) == len(rows) - 2


def test_nonoverlapping_schedule_keeps_failed_first_batch_without_fallback(database):
    _sql(database, 'UPDATE market_scan_run SET snapshot_digest=? WHERE id=?', ('f' * 64, database[1]))
    _clone_run(database)
    report = _report(database, non_overlapping_signals=True)
    assert report['non_overlapping_signals'] is True
    assert report['source_session_count'] == 0
    assert report['run_failures'][0]['run_id'] == database[1]
    assert report['strategy_selection']['adoptable_template_id'] is None


@pytest.mark.parametrize('kwargs', [{'non_overlapping_signals': 1}, {'non_overlapping_signals': True, 'run_ids': [1]}])
def test_signal_schedule_cannot_be_shifted_with_manual_ids(database, kwargs):
    with pytest.raises(ValueError, match='non-overlapping'):
        _report(database, **kwargs)


def test_offline_report_never_starts_calendar_refresh(database, monkeypatch):
    from app.services import trading_calendar
    monkeypatch.setattr(trading_calendar, '_should_auto_refresh', lambda *_args: True)
    monkeypatch.setattr(trading_calendar, '_trigger_auto_refresh', lambda: pytest.fail('offline provider request'))
    report = _report(database)
    assert report['source']['calendar_auto_refresh'] is False
    assert report['source']['provider_calls'] == 0


def test_public_freeze_never_reads_forward_prices_or_the_current_catalog(database, monkeypatch):
    templates = tracking.strategy_template_tracking_contracts()[1]
    before = deepcopy(templates)
    reads = set()
    def reject_prices(action, table, *_args):
        if action == sqlite3.SQLITE_READ:
            reads.add(table)
            return sqlite3.SQLITE_DENY if table == "kline_daily" else sqlite3.SQLITE_OK
        return sqlite3.SQLITE_OK
    def unexpected(*args, **kwargs):
        pytest.fail("freezing must use the supplied template bytes and never read future outcomes")
    monkeypatch.setattr(tracking, "market_strategy_template_catalog", unexpected)
    monkeypatch.setattr(tracking, "_forward_bars", unexpected)
    frozen = _freeze_session(database, templates=templates, authorizer=reject_prices)
    assert "market_scan_result" in reads and "kline_daily" not in reads
    assert templates == before
    assert frozen.run_id == database[1]
    assert len(frozen.selections) == 3
    for selected in frozen.selections:
        assert selected.positions and selected.outcome_status == "missing"
        assert selected.weighted_gross_return is None and selected.available_outcome_count == 0
        assert all(item.forward_return is None and item.outcome_reason == "forward_outcome_not_read" for item in selected.positions)


def test_public_freeze_preserves_the_exact_basket_and_labels_do_not_mutate_it(database):
    frozen = _freeze_session(database)
    report = _report(database)
    for captured, labelled in zip(frozen.selections, _selections(report), strict=True):
        assert captured.execution_fingerprint == labelled["execution_fingerprint"]
        assert captured.result_digest == labelled["result_digest"]
        assert captured.target_invested_weight == labelled["target_invested_weight"]
        assert captured.residual_cash_cny == labelled["residual_cash_cny"]
        assert captured.estimated_round_trip_cost_cny == labelled["estimated_round_trip_cost_cny"]
        assert [(item.symbol, item.target_weight) for item in captured.positions] == [
            (item["symbol"], item["target_weight"]) for item in labelled["positions"]]
        assert captured.weighted_gross_return is None and labelled["weighted_gross_return"] is not None
    _sql(database, "DELETE FROM kline_daily")
    assert _freeze_session(database) == frozen


def test_public_freeze_uses_a_valid_frozen_specification_instead_of_a_new_catalog_version(database):
    templates = tracking.strategy_template_tracking_contracts()[1]
    original = _freeze_session(database, templates=templates)
    templates[0]["strategy_spec"]["execution_policy"]["minimum_commission_cny"] = 100.0
    templates[0]["strategy_fingerprint"] = tracking.strategy_spec_fingerprint(StrategySpecInput.model_validate(templates[0]["strategy_spec"]))
    template_bytes = {key: value for key, value in templates[0].items() if key not in {"template_digest", "strategy_fingerprint"}}
    templates[0]["template_digest"] = sha256_hex(canonical_json_bytes(template_bytes))
    changed = _freeze_session(database, templates=templates)
    assert changed.selections[0].execution_fingerprint != original.selections[0].execution_fingerprint
    assert changed.selections[0].estimated_round_trip_cost_cny > original.selections[0].estimated_round_trip_cost_cny
    assert changed.selections[1:] == original.selections[1:]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "fingerprint", "no_fingerprint", "digest", "changed_spec"])
def test_public_freeze_rejects_unverified_template_identity_before_selection(database, monkeypatch, mutation):
    templates = tracking.strategy_template_tracking_contracts()[1]
    if mutation == "missing":
        templates.pop()
    elif mutation == "duplicate":
        templates[1] = deepcopy(templates[0])
    elif mutation == "fingerprint":
        templates[0]["strategy_fingerprint"] = "f" * 64
    elif mutation == "no_fingerprint":
        del templates[0]["strategy_fingerprint"]
    elif mutation == "digest":
        templates[0]["template_digest"] = "f" * 64
    else:
        templates[0]["strategy_spec"]["execution_policy"]["minimum_commission_cny"] = 100
    monkeypatch.setattr(tracking, "build_portfolio_draft", lambda *_args, **_kwargs: pytest.fail("invalid template must not generate a basket"))
    with pytest.raises(ValueError):
        _freeze_session(database, templates=templates)


def test_public_freeze_keeps_strict_original_publication_admission(database, monkeypatch):
    templates = tracking.strategy_template_tracking_contracts()[1]
    _sql(database, "UPDATE market_scan_run SET snapshot_digest=?", ("0" * 64,))
    monkeypatch.setattr(tracking, "build_portfolio_draft", lambda *_args, **_kwargs: pytest.fail("invalid scan must not generate a basket"))
    with pytest.raises(ValueError, match="snapshot_seal_invalid"):
        _freeze_session(database, templates=templates)


def test_public_contract_getter_returns_defensive_complete_values():
    strategies, templates, contracts = tracking.strategy_template_tracking_contracts()
    assert set(strategies) == {item["template_id"] for item in templates} == set(tracking.STRATEGY_TEMPLATE_TRACKING_IDS)
    assert contracts
    for item in templates:
        assert strategies[item["template_id"]].fingerprint == item["strategy_fingerprint"]
        assert len(item["template_digest"]) == 64
    templates[0]["strategy_spec"]["execution_policy"]["minimum_commission_cny"] = 777
    assert tracking.strategy_template_tracking_contracts()[1][0]["strategy_spec"]["execution_policy"]["minimum_commission_cny"] != 777


def test_public_freeze_requires_one_read_snapshot_for_seal_and_membership(database):
    templates = tracking.strategy_template_tracking_contracts()[1]
    with closing(sqlite3.connect(f"{database[0].as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM market_scan_run WHERE id=?", (database[1],)).fetchone()
        with pytest.raises(ValueError, match="read transaction"):
            tracking.freeze_strategy_template_session(conn, row, as_of=AS_OF, horizon=10,
                                                      notional_cash_cny=1_000_000, templates=templates)
