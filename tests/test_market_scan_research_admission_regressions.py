"""Independent regression cases for prepared-input binding and decision timing."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from app.services import market_scan_research_experiment as experiment
from app.services import market_scan_research_runner as runner
from app.services.market_scan_trial_registry import create_trial_registry, load_trial_registry
from tests.test_market_scan_research_runner import research_fixture


@pytest.mark.parametrize("field,value", [
    ("production_raw_score", -999.), ("trend_score", 0), ("quality_penalty", -100.),
    ("continuous_adjustment", 999.), ("impact_without_turnover", 999.), ("turnover_rate", 999.),
    ("original_rank", 2), ("source_digest", "e" * 64), ("symbol", "600000.SH"),
])
def test_every_prepared_score_field_is_bound_before_any_attempt(tmp_path, monkeypatch, field, value):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "independent", contract)
    original = dataset.batches[0]
    altered_score = replace(original.scores[0], **{field: value})
    altered = replace(dataset, batches=(replace(original, scores=(altered_score,)),))
    assert altered.universe_digest == dataset.universe_digest
    assert altered.run_manifest_digest == dataset.run_manifest_digest
    with pytest.raises(ValueError, match="input manifest mismatch"):
        runner.run_registered_research(tmp_path, "independent", altered, synthetic_rows=rows)
    assert load_trial_registry(tmp_path, "independent").events == ()


@pytest.mark.parametrize("timestamp", [
    "2026-07-20T09:30:00", "2026-07-20T09:30:00+08:00", "2026-07-20T01:30:00+00:00",
    "2026-08-25T16:00:00+08:00",
])
def test_publication_at_or_after_entry_open_is_not_a_historical_signal(monkeypatch, timestamp):
    snapshots, _, _, _ = research_fixture(monkeypatch)
    late = deepcopy(snapshots)
    for field in ("finished_at", "updated_at", "snapshot_sealed_at"):
        late[0]["run"][field] = timestamp
    with pytest.raises(ValueError, match=r"before D\+1 opening"):
        experiment.prepare_research_dataset(late)


@pytest.mark.parametrize("field", ["as_of", "quote_observed_at", "item_updated_at"])
def test_late_inputs_are_rejected_even_with_an_earlier_publication_claim(monkeypatch, field):
    snapshots, _, _, _ = research_fixture(monkeypatch)
    late = deepcopy(snapshots)
    timestamp = "2026-08-25T16:00:00+08:00"
    if field == "as_of":
        late[0]["run"][field] = timestamp
    else:
        late[0]["items"][0]["updated_at" if field == "item_updated_at" else field] = timestamp
    with pytest.raises(ValueError, match=r"before D\+1 opening"):
        experiment.prepare_research_dataset(late)


def test_entire_late_capture_cannot_be_backdated_through_the_quote_event(monkeypatch):
    snapshots, _, _, _ = research_fixture(monkeypatch)
    late = deepcopy(snapshots)
    for field in ("as_of", "created_at", "updated_at", "quote_capture_started_at", "quote_capture_finished_at",
                  "finished_at", "snapshot_sealed_at"):
        late[0]["run"][field] = "2026-08-25T16:00:00+08:00"
    late[0]["items"][0]["quote_observed_at"] = "2026-08-25T15:00:00+08:00"
    late[0]["items"][0]["updated_at"] = "2026-08-25T16:00:00+08:00"
    with pytest.raises(ValueError, match=r"before D\+1 opening"):
        experiment.prepare_research_dataset(late)


def test_publication_immediately_before_entry_open_remains_admissible(monkeypatch):
    snapshots, original, _, _ = research_fixture(monkeypatch)
    before = deepcopy(snapshots)
    for field in ("as_of", "finished_at", "updated_at", "snapshot_sealed_at"):
        before[0]["run"][field] = "2026-07-20T09:29:59+08:00"
    accepted = experiment.prepare_research_dataset(before)
    assert accepted.batches[0].signal_date == original.batches[0].signal_date
    assert accepted.batches[0].scores == original.batches[0].scores
    assert accepted.run_manifest_digest != original.run_manifest_digest


@pytest.mark.parametrize("state", ["running", "legacy_backfill"])
def test_unpublished_and_backfilled_rankings_are_not_admitted(monkeypatch, state):
    snapshots, _, _, _ = research_fixture(monkeypatch)
    altered = deepcopy(snapshots)
    if state == "running":
        altered[0]["run"]["status"] = "running"
        for field in ("snapshot_digest", "snapshot_seal_origin", "snapshot_sealed_at"):
            altered[0]["run"][field] = None
    else:
        altered[0]["run"]["snapshot_seal_origin"] = "legacy_backfill"
    with pytest.raises(ValueError, match="completed publication"):
        experiment.prepare_research_dataset(altered)


def test_failure_replay_rejects_a_different_failure_without_mutating_receipts(tmp_path, monkeypatch):
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "failure", contract)
    evaluate = runner._evaluate_trial

    def fail_one(dataset, variant, *args):
        if variant == "smooth_turnover":
            raise ValueError("first stable failure")
        return evaluate(dataset, variant, *args)

    monkeypatch.setattr(runner, "_evaluate_trial", fail_one)
    report = runner.run_registered_research(tmp_path, "failure", dataset, synthetic_rows=rows)
    state = load_trial_registry(tmp_path, "failure")
    assert runner.run_registered_research(tmp_path, "failure", dataset, synthetic_rows=rows, replay_only=True) == report

    def changed_failure(dataset, variant, *args):
        if variant == "smooth_turnover":
            raise ValueError("different failure")
        return evaluate(dataset, variant, *args)

    monkeypatch.setattr(runner, "_evaluate_trial", changed_failure)
    with pytest.raises(ValueError, match="failure replay disagrees"):
        runner.run_registered_research(tmp_path, "failure", dataset, synthetic_rows=rows, replay_only=True)
    assert load_trial_registry(tmp_path, "failure") == state
