from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, cast

import pytest

from app.repositories.market_scan_mapping import decode_result_payload, encode_result_payload
from app.services import market_scan_evaluation as evaluation
from app.services import market_scan_score_tracking as tracking
from app.services.market_scan_evaluation import EvaluationConfig, evaluate_market_scan_score_tracking
from app.services.market_scan_score_contract import stable_score_spec_hash
from app.services.market_scan_score_tracking import ScoreTrackingMember, ScoreTrackingSession, build_score_tracking
from app.services.market_scan_scoring import market_scan_score_spec
from app.services.trading_calendar import ASHARE_TIMEZONE, TradingCalendarCoverageError, trading_dates_between
from tests.test_market_scan_evaluation import (
    _disable_market_scan_immutability, _initialize, _reseal_market_scan_snapshot, _seed_forward_prices, _seed_run,
)


def _session(day: str = "2026-01-05", run_id: int = 1, *, count: int = 10) -> ScoreTrackingSession:
    members = tuple(ScoreTrackingMember(f"S{index}", float(index * 100 / count), {1: index / 100, 5: index / 100}, True)
                    for index in range(count))
    return ScoreTrackingSession(run_id, day, "official", "SH/SZ/BJ", "rule-v1", "a" * 64, members,
                                f"{day}T08:00:00Z", "publication", "b" * 64, f"{day}T08:00:01Z")


def _report(sessions: list[ScoreTrackingSession], *, at: str = "2026-03-02T16:00:00+08:00", horizons: tuple[int, ...] = (1,)) -> dict[str, Any]:
    return cast(dict[str, Any], build_score_tracking(sessions, as_of=datetime.fromisoformat(at),
                config=EvaluationConfig(horizons=horizons, minimum_sample_size=1, bootstrap_samples=100)))


def test_frozen_groups_are_selected_before_missing_outcomes_and_share_dates() -> None:
    first = _session()
    # No replacement of missing top names with the best surviving lower score.
    missing = replace(first, members=tuple(replace(item, returns={}) if item.raw_score >= 80 else item for item in first.members))
    second = _session("2026-01-06", 2)
    report = _report([missing, second])
    cohort = report["cohorts"][0]
    assert cohort["paired_session_count"] == 1
    assert cohort["missing_session_count"] == 1
    assert cohort["sessions"][0]["high_frozen_count"] == 2
    assert cohort["sessions"][0]["high_available_count"] == 0
    assert cohort["high_average_return"] == pytest.approx(0.085)
    assert cohort["low_average_return"] == pytest.approx(0.005)
    assert cohort["high_minus_low_return"] == pytest.approx(0.08)
    assert cohort["confidence_interval_95"] is None
    assert report["promotion_eligible"] is False
    assert report["score_semantics"] == "ordinal-not-probability"


def test_dates_have_equal_weight_despite_different_universe_sizes() -> None:
    first, second = _session(), _session("2026-01-06", 2, count=100)
    second = replace(second, members=tuple(replace(item, returns={1: 1.0 if item.raw_score >= 80 else 0.0}) for item in second.members))
    cohort = _report([first, second])["cohorts"][0]
    assert cohort["high_average_return"] == pytest.approx((0.085 + 1) / 2)
    assert cohort["low_average_return"] == pytest.approx(0.005 / 2)


def test_same_day_rescans_use_earliest_and_do_not_add_samples() -> None:
    first = _session()
    second = replace(first, run_id=2, published_at="2026-01-05T09:00:00Z",
                     members=tuple(replace(item, returns={1: 50.0}) for item in first.members))
    report = _report([second, first])
    assert report["selected_session_count"] == 1
    assert report["cohorts"][0]["sessions"][0]["run_id"] == 1
    assert report["excluded_sessions"] == [{"run_id": 2, "reason": "same_contract_session_rescan"}]


def test_score_hash_and_unknown_identity_never_mix() -> None:
    first = _session()
    sessions = [first, replace(first, run_id=2, score_spec_hash="c" * 64),
                replace(first, run_id=3, score_spec_hash=None), replace(first, run_id=4, score_spec_hash=None)]
    report = _report(sessions)
    assert len(report["cohorts"]) == 4
    assert all(row["paired_session_count"] == 1 for row in report["cohorts"])
    assert all("unverified_score_contract" in row["insufficient_reasons"] for row in report["cohorts"] if row["score_spec_hash"] is None)


def test_boundary_ties_are_included_without_symbol_tie_breaks() -> None:
    original = _session()
    scores = (0, 0, 0, 30, 40, 50, 60, 90, 90, 90)
    session = replace(original, members=tuple(replace(item, raw_score=score) for item, score in zip(original.members, scores, strict=True)))
    row = _report([session])["cohorts"][0]["sessions"][0]
    assert row["high_frozen_count"] == row["low_frozen_count"] == 3
    renamed = replace(session, members=tuple(replace(item, symbol=f"reversed-{10-index}") for index, item in enumerate(session.members)))
    assert _report([renamed])["cohorts"][0]["high_minus_low_return"] == _report([session])["cohorts"][0]["high_minus_low_return"]


def test_equal_scores_cannot_manufacture_a_spread() -> None:
    session = _session()
    session = replace(session, members=tuple(replace(item, raw_score=50) for item in session.members))
    row = _report([session])["cohorts"][0]["sessions"][0]
    assert row["status"] == "not_comparable"
    assert row["spread"] is None


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True, -1.0, 101.0, 10**500])
def test_invalid_frozen_score_rejects_grouping(score: float) -> None:
    session = _session()
    session = replace(session, members=(replace(session.members[0], raw_score=score), *session.members[1:]))
    assert _report([session])["cohorts"][0]["sessions"][0]["reason"] == "invalid_frozen_score"


@pytest.mark.parametrize("members", [(), (_session().members[0],), (_session().members[0], _session().members[0])])
def test_empty_single_and_duplicate_members_are_not_comparable(members: tuple[ScoreTrackingMember, ...]) -> None:
    row = _report([replace(_session(), members=members)])["cohorts"][0]["sessions"][0]
    assert row["status"] == "not_comparable"


def test_pending_is_wall_clock_based_even_if_future_returns_are_cached() -> None:
    before = _report([_session()], at="2026-01-06T15:14:59+08:00")["cohorts"][0]
    assert before["pending_session_count"] == 1
    assert before["sessions"][0]["high_available_count"] == 0
    after = _report([_session()], at="2026-01-06T15:15:00+08:00")["cohorts"][0]
    assert after["paired_session_count"] == 1
    missing = replace(_session(), members=tuple(replace(item, returns={}) for item in _session().members))
    assert _report([missing], at="2026-01-06T15:15:00+08:00")["cohorts"][0]["sessions"][0]["status"] == "missing"


def test_twenty_contiguous_dates_support_descriptive_ci_but_never_promotion() -> None:
    days = trading_dates_between(date(2026, 1, 5), date(2026, 2, 10))[:20]
    sessions = [_session(day.isoformat(), index + 1) for index, day in enumerate(days)]
    report = _report(sessions)
    cohort = report["cohorts"][0]
    assert cohort["status"] == "ok"
    assert cohort["confidence_interval_95"] == pytest.approx([0.08, 0.08])
    assert cohort["prediction_accuracy_validated"] is False
    assert cohort["promotion_eligible"] is False
    assert report == _report(sessions)


def test_missing_label_date_and_unscanned_date_both_block_ci() -> None:
    days = trading_dates_between(date(2026, 1, 5), date(2026, 2, 10))[:21]
    sessions = [_session(day.isoformat(), index + 1) for index, day in enumerate(days)]
    without_scan = _report(sessions[:10] + sessions[11:])["cohorts"][0]
    assert without_scan["paired_session_count"] == 20
    assert without_scan["unobserved_session_count"] == 1
    assert without_scan["confidence_interval_95"] is None
    assert "unobserved_signal_session_gaps" in without_scan["insufficient_reasons"]
    sessions[10] = replace(sessions[10], members=tuple(replace(item, returns={}) for item in sessions[10].members))
    without_label = _report(sessions)["cohorts"][0]
    assert without_label["paired_session_count"] == 20
    assert without_label["missing_session_count"] == 1
    assert without_label["confidence_interval_95"] is None


def test_prospective_recording_requires_identity_and_early_original_seal() -> None:
    session = _session()
    assert _report([session])["cohorts"][0]["prospective_recording_session_count"] == 1
    for changed in (replace(session, sealed_at="2026-01-07T08:00:00Z"),
                    replace(session, snapshot_origin="legacy_backfill"), replace(session, score_spec_hash=None),
                    replace(session, members=(replace(session.members[0], point_in_time_verified=False), *session.members[1:]))):
        assert _report([changed])["cohorts"][0]["prospective_recording_session_count"] == 0


def test_future_publication_calendar_errors_and_naive_pure_clock_are_explicit() -> None:
    future = replace(_session(), published_at="2026-04-01T08:00:00Z")
    assert _report([future])["excluded_sessions"][0]["reason"] == "published_after_as_of"
    assert _report([replace(_session(), signal_date="invalid")])["cohorts"][0]["sessions"][0]["status"] == "calendar_unavailable"
    with pytest.raises(ValueError, match="timezone"):
        build_score_tracking([], as_of=datetime(2026, 1, 5))


def _seed_tracking_database(path: Path, *, rescans: int = 1, register: bool = True) -> list[int]:
    _initialize(path)
    ids = [_seed_run(path, mode="official", rule_version="tracking-test", quote_date="2026-01-05",
                    ranks=("600001.SH", "000002.SZ", "600003.SH")) for _index in range(rescans)]
    spec = market_scan_score_spec(min_data_quality_score=60)
    digest = stable_score_spec_hash(spec)
    with closing(sqlite3.connect(path)) as conn, conn:
        _disable_market_scan_immutability(conn)
        for run_id in ids:
            for symbol, payload in conn.execute("SELECT symbol, metrics_json FROM market_scan_result WHERE run_id = ?", (run_id,)).fetchall():
                metrics, details = decode_result_payload(payload)
                details.update(score_spec=spec, score_spec_hash=digest)
                conn.execute("UPDATE market_scan_result SET metrics_json = ? WHERE run_id = ? AND symbol = ?",
                             (encode_result_payload(metrics, details), run_id, symbol))
            _reseal_market_scan_snapshot(conn, run_id)
        if register:
            conn.execute("INSERT INTO market_scan_rule_contract VALUES (?, ?, ?, ?, ?)",
                         ("tracking-test", "{}", spec["rule_version"], digest, "2026-01-05T08:00:00Z"))
    _seed_forward_prices(path, dates=("2026-01-06", "2026-01-07", "2026-01-08"),
                         closes={"600001.SH": (110, 120, 130), "000002.SZ": (105, 110, 115), "600003.SH": (90, 80, 70)})
    return ids


def _database_report(path: Path, **kwargs: Any) -> dict[str, Any]:
    return cast(dict[str, Any], evaluate_market_scan_score_tracking(path, as_of=datetime(2026, 1, 8, 16, tzinfo=ASHARE_TIMEZONE),
                config=EvaluationConfig(horizons=(1, 3), minimum_sample_size=1, bootstrap_samples=100), **kwargs))


def test_lightweight_database_path_is_readonly_bounded_and_does_not_fit_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "with?#fragment.sqlite3"
    ids = _seed_tracking_database(path, rescans=2)
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("heavy model or execution path called")
    monkeypatch.setattr(evaluation, "build_probability_research", forbidden)
    monkeypatch.setattr(evaluation, "_execution_outcomes", forbidden)
    report = _database_report(path)
    assert report["source"]["selected_run_count"] == 1
    assert report["cohorts"][0]["sessions"][0]["run_id"] == ids[0]
    assert report["cohorts"][0]["high_minus_low_return"] == pytest.approx(0.20)
    assert report["cohorts"][1]["high_minus_low_return"] == pytest.approx(0.60)
    assert report["source"]["model_fitting_performed"] is False
    assert original == hashlib.sha256(path.read_bytes()).hexdigest()
    assert _database_report(path, run_ids=[])["cohorts"] == []
    assert _database_report(path, run_ids=[ids[1]], mode="official")["cohorts"][0]["sessions"][0]["run_id"] == ids[1]
    assert _database_report(path, mode="intraday")["cohorts"] == []


def test_corrupt_first_registered_scan_is_not_replaced_with_later_scan(tmp_path: Path) -> None:
    path = tmp_path / "first-corrupt.sqlite3"
    ids = _seed_tracking_database(path, rescans=2)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE market_scan_result SET raw_score = 12 WHERE run_id = ?", (ids[0],))
    report = _database_report(path)
    assert report["cohorts"] == []
    assert report["run_failures"][0]["run_id"] == ids[0]
    assert report["source"]["selected_run_count"] == 1


def test_missing_intermediate_session_blocks_horizon_without_forward_shifting(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    _seed_tracking_database(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM kline_daily WHERE date = '2026-01-07'")
    report = _database_report(path)
    assert report["cohorts"][0]["sessions"][0]["status"] == "paired"
    assert report["cohorts"][1]["sessions"][0]["status"] == "missing"


def test_target_cutoff_ignores_future_cache_and_uses_shanghai_for_naive_as_of(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    _seed_tracking_database(path)
    report = cast(dict[str, Any], evaluate_market_scan_score_tracking(path, as_of=datetime(2026, 1, 6, 15, 14),
                  config=EvaluationConfig(horizons=(1, 3))))
    assert report["as_of_completed_date"] == "2026-01-05"
    assert all(cohort["sessions"][0]["status"] == "pending" for cohort in report["cohorts"])
    with pytest.raises(ValueError, match="future"):
        evaluate_market_scan_score_tracking(path, as_of=datetime.now(UTC) + timedelta(days=2))


@pytest.mark.parametrize("column,value", [("data_version", "another-vintage"), ("corporate_action_status", "effective_event")])
def test_future_suffix_vintage_or_company_action_does_not_rewrite_short_horizon(tmp_path: Path, column: str, value: str) -> None:
    path = tmp_path / "vintage.sqlite3"
    _seed_tracking_database(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"UPDATE kline_daily SET {column} = ? WHERE date = '2026-01-08'", (value,))
    report = _database_report(path)
    assert report["cohorts"][0]["sessions"][0]["status"] == "paired"
    assert report["cohorts"][1]["sessions"][0]["status"] == "missing"


def test_unregistered_runs_remain_per_run_until_their_actual_hash_is_verified(tmp_path: Path) -> None:
    path = tmp_path / "unregistered.sqlite3"
    _seed_tracking_database(path, rescans=2, register=False)
    report = _database_report(path)
    assert report["source"]["selected_run_count"] == 2
    assert report["selected_session_count"] == 1


def test_registered_hash_conflict_is_explicit_failure(tmp_path: Path) -> None:
    path = tmp_path / "hash-conflict.sqlite3"
    ids = _seed_tracking_database(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE market_scan_result SET metrics_json = '{}' WHERE run_id = ?", (ids[0],))
        _reseal_market_scan_snapshot(conn, ids[0])
    report = _database_report(path)
    assert report["cohorts"] == []
    assert report["run_failures"][0]["error_type"] == "ValueError"


def test_read_snapshot_does_not_mix_concurrent_forward_cache_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "snapshot.sqlite3"
    _seed_tracking_database(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
    original = tracking._score_tracking_bars
    def replace_forward_after_scan_read(conn: sqlite3.Connection, run: sqlite3.Row, cutoff: str) -> dict[str, tuple[sqlite3.Row, ...]]:
        with closing(sqlite3.connect(path)) as writer, writer:
            writer.execute("UPDATE kline_daily SET open = 210, close = 210, high = 210, low = 210 "
                           "WHERE symbol = '600001.SH' AND date = '2026-01-06'")
        return original(conn, run, cutoff)
    monkeypatch.setattr(tracking, "_score_tracking_bars", replace_forward_after_scan_read)
    report = _database_report(path)
    assert report["cohorts"][0]["high_minus_low_return"] == pytest.approx(0.20)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT close FROM kline_daily WHERE symbol = '600001.SH' AND date = '2026-01-06'").fetchone()[0] == 210


def test_long_horizon_calendar_gap_does_not_erase_available_short_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "calendar-prefix.sqlite3"
    _seed_tracking_database(path)
    original = tracking.next_trade_dates
    def short_calendar(signal: date, count: int) -> tuple[date, ...]:
        if count > 1:
            raise TradingCalendarCoverageError("long target outside trusted calendar")
        return original(signal, count)
    monkeypatch.setattr(tracking, "next_trade_dates", short_calendar)
    report = _database_report(path)
    assert report["run_failures"] == []
    assert report["cohorts"][0]["sessions"][0]["status"] == "paired"
    assert report["cohorts"][1]["sessions"][0]["status"] == "calendar_unavailable"
    assert report["cohorts"][1]["mature_session_count"] == 0
    assert report["cohorts"][1]["pending_session_count"] == 0
    assert report["cohorts"][1]["missing_session_count"] == 0
    assert report["cohorts"][1]["calendar_unavailable_session_count"] == 1
    assert report["cohorts"][0]["high_minus_low_return"] == pytest.approx(0.20)
    monkeypatch.setattr(tracking, "next_trade_dates", lambda *args: (_ for _ in ()).throw(TradingCalendarCoverageError("no target")))
    assert _database_report(path)["source"]["failed_run_count"] == 0
