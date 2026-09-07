from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest
from app.models.market_scan import MarketScanMarketProgress

from app.services import market_scan_research_experiment as experiment
from app.services import market_scan_research_runner as runner
from app.services.market_scan_trial_registry import create_trial_registry, load_trial_registry
from app.services.market_scan_trial_registry_contract import trial_registry_digest
from app.services.trading_calendar import trading_dates_between
from tests.test_market_scan_raw_score_replay import _snapshot
from tests.test_market_scan_research_portfolio import replay, row


def research_fixture(monkeypatch):
    item, run = _snapshot(monkeypatch, mutation="none", mode="official")
    run = run.model_copy(update={"status": "success", "finished_at": run.as_of,
        "snapshot_digest": "d"*64, "snapshot_seal_origin": "publication", "snapshot_sealed_at": run.as_of,
        "market_progress": [MarketScanMarketProgress(market=market, total_count=int(market == item.market),
            processed_count=int(market == item.market), success_count=int(market == item.market),
            coverage_pct=100 if market == item.market else 0)
            for market in ("SH", "SZ", "BJ")]})
    item = item.model_copy(update={"rank": 1})
    snapshots = [{"run": run.model_dump(mode="json"), "items": [item.model_dump(mode="json")]}]
    dataset = experiment.prepare_research_dataset(snapshots)
    dates = [day.isoformat() for day in trading_dates_between(date(2026, 6, 15), date(2026, 7, 27))]
    calendar = {"trading_dates": dates, "train_signal_dates": [dates[0]],
                "calibration_signal_dates": [dates[8]], "test_signal_dates": [run.data_date]}
    rows = tuple(row(day, symbol=item.symbol) for day in dates if day >= run.data_date)
    monkeypatch.setattr(experiment, "research_implementation_digest", lambda: "a"*64)
    contract = experiment.build_research_trial_contract(dataset, calendar,
        execution_manifest_digest=experiment.research_execution_manifest(synthetic_rows=rows), exploration_cutoff="2026-07-16")
    return snapshots, dataset, rows, contract


def test_registered_family_runs_and_replays_every_receipt(tmp_path, monkeypatch):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    report = runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows)
    assert report["registry"]["declared_family_complete"] is True
    assert report["promotion_eligible"] is False
    assert report["family_inference"]["declared_family_size"] == 3
    assert report["family_inference"]["declared_attempt_count"] == 4
    assert len(report["results"]) == 4
    for result in report["results"].values():
        assert result["status"] == "succeeded"
        assert result["candidate"]["provenance_status"] == "synthetic"
        assert result["comparison"]["p_value"] is None
    assert runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, replay_only=True) == report
    state = load_trial_registry(tmp_path, "experiment")
    assert len(state.events) == 8
    assert set(state.statuses.values()) == {"succeeded"}


@pytest.mark.parametrize("change", ["code", "rows", "spec", "policy", "family"])
def test_changes_cannot_reuse_a_frozen_registration(tmp_path, monkeypatch, change):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    if change == "spec":
        spec = contract["trials"][0]["score_specification"]
        spec["turnover_knots"][1][1] = 100
        contract["trials"][0]["score_spec_hash"] = trial_registry_digest(spec)
    if change == "policy":
        contract["shared_account_policy"]["entry"] = "D+1-close"
    if change == "family":
        contract["trials"].pop()
    create_trial_registry(tmp_path, "experiment", contract)
    if change == "code":
        monkeypatch.setattr(experiment, "research_implementation_digest", lambda: "b"*64)
    if change == "rows":
        rows = rows[:-1]
    with pytest.raises(ValueError, match="mismatch"):
        runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows)
    assert load_trial_registry(tmp_path, "experiment").events == ()


def test_failed_trial_is_retained_and_does_not_shrink_family(tmp_path, monkeypatch):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    evaluate = runner._evaluate_trial
    def fail_one(dataset, variant, *args):
        if variant == "smooth_turnover":
            raise ValueError("fixture execution failure")
        return evaluate(dataset, variant, *args)
    monkeypatch.setattr(runner, "_evaluate_trial", fail_one)
    report = runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows)
    assert report["results"]["smooth_turnover"]["status"] == "failed"
    assert report["registry"]["statuses"]["smooth_turnover"] == "failed"
    assert report["family_inference"]["declared_family_size"] == 3
    assert report["family_inference"]["declared_attempt_count"] == 4
    assert report["registry"]["declared_family_complete"] is True


def test_interrupt_is_recorded_before_propagating(tmp_path, monkeypatch):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    def interrupt(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(runner, "_evaluate_trial", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows)
    state = load_trial_registry(tmp_path, "experiment")
    assert state.statuses == {"production_v5": "cancelled"}
    assert state.seal is None


def test_replay_is_independent_of_recorded_numerical_claims(tmp_path, monkeypatch):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows)
    evaluate = runner._evaluate_trial
    def changed(*args):
        result = evaluate(*args)
        result["comparison"]["mean_daily_net_excess_return"] = 999
        return result
    monkeypatch.setattr(runner, "_evaluate_trial", changed)
    with pytest.raises(ValueError, match="replay disagrees"):
        runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, replay_only=True)


def test_replay_requires_seal_and_execution_requires_fresh_registry(tmp_path, monkeypatch):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    with pytest.raises(ValueError, match="sealed"):
        runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, replay_only=True)
    runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows)
    with pytest.raises(ValueError, match="fresh"):
        runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows)


def test_complete_aligned_nav_has_zero_excess_against_itself():
    account = replay()
    days = tuple(replace(account.days[0], session_date=str(i), daily_return=.001) for i in range(45))
    account = replace(account, days=days)
    result = runner.compare_research_accounts(account, account, seed="fixed")
    assert result["mean_daily_net_excess_return"] == 0
    assert result["confidence_interval_95"] == [0, 0]
    assert result["p_value"] == 1
    assert result["inference_status"] == "available"


def test_missing_nav_is_not_removed_from_paired_time_axis():
    account = replay()
    days = list(account.days)
    days[2] = replace(days[2], daily_return=None, nav=None)
    damaged = replace(account, days=tuple(days), total_return=None, maximum_drawdown=None)
    result = runner.compare_research_accounts(damaged, account, seed="fixed")
    assert len(result["daily_net_excess"]) == len(days)-1
    assert result["daily_net_excess"][1] is None
    assert result["mean_daily_net_excess_return"] is None
    assert result["confidence_interval_95"] is None
    with pytest.raises(ValueError, match="calendars"):
        runner.compare_research_accounts(replace(account, days=account.days[:-1]), account, seed="fixed")


def test_signal_dataset_hash_is_stable_and_covers_entire_snapshot(monkeypatch):
    snapshots, dataset, _, _ = research_fixture(monkeypatch)
    assert experiment.prepare_research_dataset(deepcopy(snapshots)) == dataset
    altered = deepcopy(snapshots)
    altered[0]["run"]["trigger"] = "scheduled"
    assert experiment.prepare_research_dataset(altered).run_manifest_digest != dataset.run_manifest_digest
    with pytest.raises(ValueError, match="unique"):
        experiment.prepare_research_dataset(snapshots+snapshots)
    with pytest.raises(ValueError, match="exactly"):
        experiment.prepare_research_dataset([{"run": {}, "items": [], "extra": 1}])


def test_contract_rejects_wrong_test_dates_and_fake_exchange_sessions(monkeypatch):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    kwargs = {"execution_manifest_digest": experiment.research_execution_manifest(synthetic_rows=rows),
              "exploration_cutoff": "2026-07-16"}
    calendar = deepcopy(contract["calendar"])
    calendar["test_signal_dates"] = ["2026-07-16"]
    with pytest.raises(ValueError):
        experiment.build_research_trial_contract(dataset, calendar, **kwargs)
    calendar = deepcopy(contract["calendar"])
    calendar["trading_dates"].append("2026-07-28")
    calendar["trading_dates"].insert(5, "2026-06-20")
    calendar["trading_dates"].sort()
    with pytest.raises(ValueError, match="trusted trading"):
        experiment.build_research_trial_contract(dataset, calendar, **kwargs)


def test_execution_manifest_does_not_accept_self_asserted_official_tokens():
    with pytest.raises(ValueError, match="strict-loader"):
        experiment.research_execution_manifest(official_sessions=[{"verified": True}])
    with pytest.raises(ValueError, match="mix"):
        experiment.research_execution_manifest(official_sessions=[{}], synthetic_rows=[row("2026-07-17")])
    assert len(experiment.research_execution_manifest()) == 64


def test_implementation_digest_binds_application_sources():
    assert len(experiment.research_implementation_digest()) == 64
    assert experiment.research_implementation_digest() == experiment.research_implementation_digest()


def test_prepared_values_cannot_bypass_manifest_by_reusing_digest_attributes(tmp_path, monkeypatch):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    batch = dataset.batches[0]
    altered = replace(dataset, batches=(replace(batch, scores=(replace(batch.scores[0], production_raw_score=-999),)),))
    assert altered.run_manifest_digest == dataset.run_manifest_digest
    with pytest.raises(ValueError, match="mismatch"):
        runner.run_registered_research(tmp_path, "experiment", altered, synthetic_rows=rows)
    assert load_trial_registry(tmp_path, "experiment").events == ()


def test_failed_terminal_can_replay_without_dropping_other_hypotheses(tmp_path, monkeypatch):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    evaluate = runner._evaluate_trial
    def fail_one(dataset, variant, *args):
        if variant == "smooth_turnover":
            raise ValueError("reproducible execution failure")
        return evaluate(dataset, variant, *args)
    monkeypatch.setattr(runner, "_evaluate_trial", fail_one)
    report = runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows)
    assert runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, replay_only=True) == report
    monkeypatch.setattr(runner, "_evaluate_trial", evaluate)
    with pytest.raises(ValueError, match="replay disagrees"):
        runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, replay_only=True)


@pytest.mark.parametrize("terminal", [False, True])
def test_resume_preserves_interrupted_attempt_and_finishes_remaining_family(tmp_path, monkeypatch, terminal):
    from app.services.market_scan_trial_registry import start_trial, finish_trial
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    start_trial(tmp_path, "experiment", "production_v5")
    if terminal:
        finish_trial(tmp_path, "experiment", "production_v5", status="cancelled", reason="user cancellation")
    report = runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, resume=True)
    assert report["results"]["production_v5"]["status"] == "cancelled"
    assert report["registry"]["declared_family_complete"] is True
    assert len(load_trial_registry(tmp_path, "experiment").events) == 8
    assert runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, replay_only=True) == report


def test_replay_reports_failure_if_a_previous_success_no_longer_executes(tmp_path, monkeypatch):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows)
    def fail(*args):
        raise ValueError("different failure")
    monkeypatch.setattr(runner, "_evaluate_trial", fail)
    with pytest.raises(ValueError, match="failure replay"):
        runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, replay_only=True)


@pytest.mark.parametrize("mutation", ["missing", "member_removed", "wrong_run"])
def test_incomplete_market_membership_cannot_be_dropped_before_research(monkeypatch, mutation):
    from app.models.market_scan import MarketScanResultItem, MarketScanRun
    snapshots, _, _, _ = research_fixture(monkeypatch)
    run = MarketScanRun.model_validate(snapshots[0]["run"])
    item = MarketScanResultItem.model_validate(snapshots[0]["items"][0])
    if mutation == "missing":
        run = run.model_copy(update={"missing_count": 1})
    if mutation == "wrong_run":
        item = item.model_copy(update={"run_id": 999})
    with pytest.raises(ValueError):
        experiment._validate_research_membership(run, [] if mutation == "member_removed" else [item])
