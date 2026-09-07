from dataclasses import replace

import pytest

from app.services.market_scan_research_execution_audit import ExecutionAuditDecision, audit_research_execution, capture_execution_audit_decision
from app.services.market_scan_research_portfolio_models import ResearchPortfolioConfig
from tests.test_market_scan_research_portfolio import row
from tests.test_market_scan_research_runner import research_fixture


def inputs(monkeypatch):
    snapshots, dataset, rows, contract = research_fixture(monkeypatch)
    decisions = [capture_execution_audit_decision(snapshots[0])]
    sessions = tuple(day for day in contract["calendar"]["trading_dates"] if day >= dataset.batches[0].signal_date)
    return dataset, decisions, rows, sessions


def audit(dataset, decisions, rows, sessions, **kwargs):
    return audit_research_execution(dataset, decisions, sessions, synthetic_rows=rows,
                                    config=ResearchPortfolioConfig(initial_cash=20_000., top_n=1, horizon=1), **kwargs)


def test_overnight_alpha_disappears_after_entry_and_fees(monkeypatch):
    dataset, decisions, rows, sessions = inputs(monkeypatch)
    rows = tuple(row(day, symbol=rows[0].symbol, price=10. if i == 0 else 11., previous=10. if i < 2 else 11.)
                 for i, day in enumerate(sessions))
    report = audit(dataset, decisions, rows, sessions)
    item = report["records"][0]
    assert item["original_signal_close_label"]["return"] == pytest.approx(.1)
    assert item["same_exit_signal_close_label"]["return"] == pytest.approx(.1)
    assert item["same_exit_entry_open_label"]["return"] == 0
    assert item["same_exit_entry_attenuation"] == pytest.approx(.1)
    assert item["actual_trade_net_return"] < 0
    assert item["signal_label_starts_before_available"] is True
    assert item["original_signal_close_label"]["end_date"] != item["same_exit_entry_open_label"]["end_date"]
    assert report["source_provenance"] == "synthetic" and report["promotion_eligible"] is False
    assert set(report["daily_net_production_comparison"]["daily_net_excess"]) == {0.}


def test_blocked_exit_keeps_actual_endpoint_and_unfilled_cash(monkeypatch):
    dataset, decisions, rows, sessions = inputs(monkeypatch)
    changed = tuple(row(day, symbol=rows[0].symbol, exit_state="locked_limit" if day in sessions[2:4] else "executable") for day in sessions)
    item = audit(dataset, decisions, changed, sessions)["records"][0]
    assert item["exit_date"] == sessions[4] and item["exit_delay_sessions"] == 2
    assert item["same_exit_signal_close_label"]["end_date"] == item["same_exit_entry_open_label"]["end_date"]
    blocked = tuple(row(day, symbol=rows[0].symbol, entry_state="locked_limit") for day in sessions)
    report = audit(dataset, decisions, blocked, sessions)
    item = report["records"][0]
    assert item["entered"] is False and item["actual_trade_net_return"] is None
    assert item["same_exit_entry_open_label"]["return"] is None
    assert report["candidate_account"]["total_return"] == 0
    assert len(report["records"]) == 1  # no dropped frozen slot


@pytest.mark.parametrize("mode", ["missing", "action"])
def test_unknown_path_has_null_labels_and_unresolved_position(monkeypatch, mode):
    dataset, decisions, rows, sessions = inputs(monkeypatch)
    rows = tuple(row(day, symbol=rows[0].symbol, action="effective_event" if mode == "action" and day == sessions[2] else "none")
                 for day in sessions if mode != "missing" or day != sessions[2])
    report = audit(dataset, decisions, rows, sessions)
    item = report["records"][0]
    assert item["entered"] is True and item["closed"] is False
    assert item["actual_trade_net_return"] is None
    assert report["candidate_account"]["total_return"] is None


def test_decision_identity_cannot_be_swapped(monkeypatch):
    dataset, decisions, rows, sessions = inputs(monkeypatch)
    for bad in ([], decisions + decisions, [replace(decisions[0], source_digest="f" * 64)],
                [replace(decisions[0], available_at="2026-07-17T16:00:00")]):
        with pytest.raises(ValueError, match="decision"):
            audit(dataset, bad, rows, sessions)


def test_replacing_only_available_time_cannot_erase_execution_lookahead(monkeypatch):
    dataset, decisions, rows, sessions = inputs(monkeypatch)
    fake = replace(decisions[0], available_at=decisions[0].signal_date + "T15:00:00+08:00")
    with pytest.raises(ValueError, match="captured.*unchanged"):
        audit(dataset, [fake], rows, sessions)
    fake = ExecutionAuditDecision(decisions[0].run_id, decisions[0].signal_date, decisions[0].available_at,
                                  decisions[0].source_digest, decisions[0].naive_timestamp_count)
    with pytest.raises(ValueError, match="captured"):
        audit(dataset, [fake], rows, sessions)


def test_late_publication_rejected_before_execution(monkeypatch):
    snapshots, dataset, rows, contract = research_fixture(monkeypatch)
    snapshots[0]["run"]["snapshot_sealed_at"] = "2026-07-21T12:00:00+08:00"
    with pytest.raises(ValueError, match="before D\\+1"):
        capture_execution_audit_decision(snapshots[0])


def test_new_variant_does_not_modify_frozen_v5(monkeypatch):
    dataset, decisions, rows, sessions = inputs(monkeypatch)
    report = audit(dataset, decisions, rows, sessions, variant="smooth_turnover")
    baseline = audit(dataset, decisions, rows, sessions)
    assert report["production_account_digest"] == baseline["candidate_account"]["result_digest"]
    assert dataset.batches[0].source_digest == decisions[0].source_digest
