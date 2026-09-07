from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.artifacts.io import ArtifactContentConflictError, ArtifactIOError, canonical_json_bytes
import app.services.market_scan_trial_registry as registry
from app.services.market_scan_trial_registry_contract import (
    TRIAL_PRIMARY_OBJECTIVE,
    TrialRegistryError,
    trial_registry_digest,
    validate_trial_contract,
)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    instant = datetime(2030, 2, 1, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(registry, "utc_now", lambda: instant)
    return instant


@pytest.fixture
def contract() -> dict:
    days = [(date(2030, 1, 1) + timedelta(days=index)).isoformat() for index in range(70)]
    specification = {"version": "v5", "weights": {"trend": 1.0}}
    return {
        "registration_kind": "prospective",
        "primary_objective": dict(TRIAL_PRIMARY_OBJECTIVE),
        "trials": [
            {"trial_id": name, "candidate_id": name, "score_specification": specification,
             "score_spec_hash": trial_registry_digest(specification), "parameters": {"variant": name}}
            for name in ("baseline", "smooth", "combined")
        ],
        "input_manifest": {name: "a" * 64 for name in ("universe_digest", "run_manifest_digest", "implementation_digest")},
        "cost_policy": {"profile": "base", "max_participation_rate": 0.01},
        "capital_policy": {"initial_cash": 1000000, "currency": "CNY"},
        "shared_account_policy": {
            "allocation": "rotating-horizon-plus-one-sleeves", "reinvest": "within-sleeve", "entry": "D+1-open",
            "scheduled_exit": "D+H+1-close", "blocked_exit": "retain-and-retry-next-session",
            "duplicate_symbol": "keep-existing-position-and-cash-slot", "unfilled_entry": "cash-no-replacement",
        },
        "benchmark_policy": {"allocation": "same-capital-equal-weight-frozen-universe"},
        "calendar": {"trading_dates": days, "train_signal_dates": days[:10],
                     "calibration_signal_dates": days[17:24], "test_signal_dates": days[40:50]},
        "exploration_cutoff": days[30],
    }


def _complete(root: Path, contract: dict) -> tuple[dict, dict]:
    registration = registry.create_trial_registry(root, "research", contract)
    for trial, status in (("baseline", "succeeded"), ("smooth", "failed"), ("combined", "cancelled")):
        registry.start_trial(root, "research", trial)
        registry.finish_trial(root, "research", trial, status=status,
                              result={"net_excess_return": 0.02} if status == "succeeded" else None,
                              reason="retained test outcome" if status != "succeeded" else "")
    return registration, registry.seal_trial_registry(root, "research")


def _rewrite(path: Path, transform) -> dict:
    record = json.loads(path.read_text())
    transform(record)
    record["digest"] = trial_registry_digest({key: value for key, value in record.items() if key != "digest"})
    path.write_bytes(canonical_json_bytes(record))
    return record


def test_complete_chain_preserves_failed_cancelled_and_binds_external_anchors(tmp_path, contract, frozen_clock) -> None:
    registration, seal = _complete(tmp_path, contract)
    report = registry.verify_trial_registry(tmp_path, "research", expected_registry_digest=registration["digest"],
                                           expected_seal_digest=seal["digest"])
    assert report["local_integrity"] == "verified"
    assert report["declared_family_complete"] is True
    assert report["prospective_time_order"] == "locally_consistent"
    assert report["statuses"] == {"baseline": "succeeded", "smooth": "failed", "combined": "cancelled"}
    assert report["expected_trial_count"] == report["attempted_trial_count"] == 3
    assert report["trusted_timestamp_attestation"] == "unavailable"
    assert report["external_attempt_completeness"] == "unproven"
    assert report["machine_promotion_eligible"] is False
    assert report["result_evaluation_verified"] is False
    assert len(registry.load_trial_registry(tmp_path, "research").events) == 6


def test_unsealed_family_is_explicitly_incomplete(tmp_path, contract, frozen_clock) -> None:
    registry.create_trial_registry(tmp_path, "research", contract)
    registry.start_trial(tmp_path, "research", "baseline")
    report = registry.verify_trial_registry(tmp_path, "research")
    assert report["declared_family_complete"] is False
    assert report["attempted_trial_count"] == 1
    with pytest.raises(TrialRegistryError, match="incomplete"):
        registry.seal_trial_registry(tmp_path, "research")


@pytest.mark.parametrize("mutation, message", [
    (lambda c: c["trials"].append(deepcopy(c["trials"][0])), "duplicate"),
    (lambda c: c["trials"][0].update(score_spec_hash="b" * 64), "score_spec_hash"),
    (lambda c: c["trials"][1].update(candidate_id="baseline"), "different parameters"),
    (lambda c: c["primary_objective"].update(horizon=20), "primary_objective"),
    (lambda c: c["primary_objective"].update(top_n=True), "primary_objective"),
    (lambda c: c["calendar"]["test_signal_dates"].append(c["calendar"]["test_signal_dates"][0]), "unique and ordered"),
    (lambda c: c["calendar"].update(test_signal_dates=c["calendar"]["train_signal_dates"]), "cross partitions"),
    (lambda c: c["calendar"].update(calibration_signal_dates=c["calendar"]["trading_dates"][15:24]), "purge"),
    (lambda c: c["calendar"].update(test_signal_dates=c["calendar"]["trading_dates"][-2:]), "label target"),
    (lambda c: c.update(exploration_cutoff="2030-01-29"), "exploration_cutoff"),
    (lambda c: c["input_manifest"].pop("implementation_digest"), "missing fields"),
    (lambda c: c.update(capital_policy={}), "full policy"),
    (lambda c: c["capital_policy"].update(initial_cash=True), "positive finite"),
    (lambda c: c["capital_policy"].update(initial_cash=-1), "positive finite"),
    (lambda c: c["capital_policy"].update(currency="USD"), "positive finite"),
    (lambda c: c["cost_policy"].update(max_participation_rate=1.1), "participation rate"),
    (lambda c: c["cost_policy"].update(profile=" "), "named cost"),
    (lambda c: c["shared_account_policy"].pop("blocked_exit"), "missing fields"),
    (lambda c: c["shared_account_policy"].update(blocked_exit=""), "explicit nonempty"),
    (lambda c: c.update(verified=True), "unexpected or missing fields"),
    (lambda c: c["trials"][0].update(trial_id="../escape"), "invalid identifier"),
])
def test_contract_rejects_relabelled_incomplete_or_leaky_families(contract, mutation, message) -> None:
    mutation(contract)
    with pytest.raises(TrialRegistryError, match=message):
        validate_trial_contract(contract)


def test_contract_is_copied_and_all_policies_change_its_digest(contract) -> None:
    checked = validate_trial_contract(contract)
    original_digest = trial_registry_digest(checked)
    contract["capital_policy"]["initial_cash"] = 1
    assert checked["capital_policy"]["initial_cash"] == 1000000
    assert trial_registry_digest(validate_trial_contract(contract)) != original_digest


def test_nonfinite_or_duplicate_json_cannot_enter_registry(tmp_path, contract, frozen_clock) -> None:
    invalid = deepcopy(contract)
    invalid["cost_policy"]["fee"] = float("nan")
    with pytest.raises(ArtifactIOError):
        registry.create_trial_registry(tmp_path, "nan", invalid)
    registry.create_trial_registry(tmp_path, "research", contract)
    path = tmp_path / "research" / "registration.json"
    path.write_bytes(path.read_bytes().replace(b'{"contract":', b'{"schema_version":"forged","contract":', 1))
    with pytest.raises(ArtifactIOError):
        registry.load_trial_registry(tmp_path, "research")


def test_retrospective_data_can_be_audited_but_never_relabelled_prospective(tmp_path, contract, monkeypatch) -> None:
    monkeypatch.setattr(registry, "utc_now", lambda: datetime(2031, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(TrialRegistryError, match="precede"):
        registry.create_trial_registry(tmp_path, "late", contract)
    contract["registration_kind"] = "retrospective"
    _complete(tmp_path, contract)
    report = registry.verify_trial_registry(tmp_path, "research")
    assert report["local_integrity"] == "verified"
    assert report["prospective_time_order"] == "not_prospective"
    assert report["machine_promotion_eligible"] is False


def test_shanghai_signal_day_boundary_and_late_start_are_rejected(tmp_path, contract, monkeypatch, frozen_clock) -> None:
    registry.create_trial_registry(tmp_path, "research", contract)
    first_test = date.fromisoformat(contract["calendar"]["test_signal_dates"][0])
    instant = datetime.combine(first_test, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=8)
    monkeypatch.setattr(registry, "utc_now", lambda: instant)
    with pytest.raises(TrialRegistryError, match="precede"):
        registry.start_trial(tmp_path, "research", "baseline")


def test_registry_cannot_be_overwritten_or_mutated_after_seal(tmp_path, contract, frozen_clock) -> None:
    _complete(tmp_path, contract)
    changed = deepcopy(contract)
    changed["capital_policy"]["initial_cash"] = 2
    with pytest.raises(ArtifactContentConflictError):
        registry.create_trial_registry(tmp_path, "research", changed)
    with pytest.raises(TrialRegistryError, match="sealed"):
        registry.start_trial(tmp_path, "research", "baseline")
    with pytest.raises(TrialRegistryError, match="sealed"):
        registry.finish_trial(tmp_path, "research", "baseline", status="cancelled", reason="remove loser")


def test_trials_require_declared_unique_starts_and_single_terminal_receipt(tmp_path, contract, frozen_clock) -> None:
    registry.create_trial_registry(tmp_path, "research", contract)
    with pytest.raises(TrialRegistryError, match="absent"):
        registry.start_trial(tmp_path, "research", "undeclared")
    with pytest.raises(TrialRegistryError, match="unmatched start"):
        registry.finish_trial(tmp_path, "research", "baseline", status="succeeded", result={"score": 100})
    registry.start_trial(tmp_path, "research", "baseline")
    with pytest.raises(TrialRegistryError, match="already started"):
        registry.start_trial(tmp_path, "research", "baseline")
    registry.finish_trial(tmp_path, "research", "baseline", status="failed", reason="numerical failure")
    with pytest.raises(TrialRegistryError, match="unmatched start"):
        registry.finish_trial(tmp_path, "research", "baseline", status="succeeded", result={"score": 100})


@pytest.mark.parametrize("status,result,reason", [("succeeded", None, ""), ("failed", None, ""),
                                                ("cancelled", None, "  "), ("removed", None, "bad"),
                                                ("succeeded", {"score": 1}, 1)])
def test_attempts_cannot_hide_failure_reasons_or_empty_success(tmp_path, contract, frozen_clock, status, result, reason) -> None:
    registry.create_trial_registry(tmp_path, "research", contract)
    registry.start_trial(tmp_path, "research", "baseline")
    with pytest.raises(TrialRegistryError):
        registry.finish_trial(tmp_path, "research", "baseline", status=status, result=result, reason=reason)


@pytest.mark.parametrize("filename", ["event-00000003.json", "event-00000006.json"])
def test_deleted_failure_or_tail_is_detected_by_chain_or_seal(tmp_path, contract, frozen_clock, filename) -> None:
    _complete(tmp_path, contract)
    (tmp_path / "research" / filename).unlink()
    with pytest.raises(TrialRegistryError):
        registry.load_trial_registry(tmp_path, "research")


def test_changing_candidate_parameters_breaks_frozen_registration_anchor(tmp_path, contract, frozen_clock) -> None:
    registration = registry.create_trial_registry(tmp_path, "research", contract)
    path = tmp_path / "research" / "registration.json"
    _rewrite(path, lambda r: r["contract"]["trials"][0]["parameters"].update(variant="new-choice"))
    with pytest.raises(TrialRegistryError, match="independently retained anchor"):
        registry.verify_trial_registry(tmp_path, "research", expected_registry_digest=registration["digest"])


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(trial_digest="b" * 64),
    lambda e: e.update(previous_digest="b" * 64),
    lambda e: e.update(sequence=True),
    lambda e: e.update(recorded_at="2029-01-01T00:00:00+00:00"),
    lambda e: e.update(status="succeeded"),
])
def test_redigested_receipt_still_must_match_frozen_lifecycle(tmp_path, contract, frozen_clock, mutate) -> None:
    registry.create_trial_registry(tmp_path, "research", contract)
    registry.start_trial(tmp_path, "research", "baseline")
    _rewrite(tmp_path / "research" / "event-00000001.json", mutate)
    with pytest.raises(TrialRegistryError):
        registry.load_trial_registry(tmp_path, "research")


def test_result_claims_cannot_self_certify_evaluation_or_promotion(tmp_path, contract, frozen_clock) -> None:
    registry.create_trial_registry(tmp_path, "research", contract)
    registry.start_trial(tmp_path, "research", "baseline")
    registry.finish_trial(tmp_path, "research", "baseline", status="succeeded",
                          result={"machine_promotion_eligible": True, "verified": True, "sharpe": 900})
    report = registry.verify_trial_registry(tmp_path, "research")
    assert report["result_evaluation_verified"] is False
    assert report["machine_promotion_eligible"] is False


@pytest.mark.parametrize("registration_id", ["../escape", "/absolute", "a/b", "a\\b", ".", ""])
def test_registry_identifier_cannot_escape_managed_root(tmp_path, contract, frozen_clock, registration_id) -> None:
    with pytest.raises(TrialRegistryError, match="invalid identifier"):
        registry.create_trial_registry(tmp_path, registration_id, contract)


def test_registry_rejects_symlink_ancestors_receipts_and_unexpected_files(tmp_path, contract, frozen_clock) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(TrialRegistryError, match="symlinks"):
        registry.create_trial_registry(linked, "research", contract)
    assert list(outside.iterdir()) == []
    registry.create_trial_registry(tmp_path, "research", contract)
    unexpected = tmp_path / "research" / "unregistered.json"
    unexpected.write_text("{}")
    with pytest.raises(TrialRegistryError, match="unexpected file"):
        registry.load_trial_registry(tmp_path, "research")
    unexpected.unlink()
    (tmp_path / "research" / "event-00000001.json").symlink_to(tmp_path / "research" / "registration.json")
    with pytest.raises(TrialRegistryError, match="regular immutable"):
        registry.load_trial_registry(tmp_path, "research")


def test_concurrent_trials_serialize_into_one_gapless_chain(tmp_path, contract, frozen_clock) -> None:
    registry.create_trial_registry(tmp_path, "research", contract)
    trials = ["baseline", "smooth", "combined"]
    with ThreadPoolExecutor(max_workers=3) as workers:
        starts = list(workers.map(lambda trial: registry.start_trial(tmp_path, "research", trial), trials))
        finishes = list(workers.map(lambda trial: registry.finish_trial(
            tmp_path, "research", trial, status="cancelled", reason="concurrent test",
        ), trials))
    assert {event["sequence"] for event in starts} == {1, 2, 3}
    assert {event["sequence"] for event in finishes} == {4, 5, 6}
    registry.seal_trial_registry(tmp_path, "research")
    assert registry.verify_trial_registry(tmp_path, "research")["declared_family_complete"] is True


def test_forged_seal_does_not_hide_deleted_expected_trial(tmp_path, contract, frozen_clock) -> None:
    _complete(tmp_path, contract)
    path = tmp_path / "research" / "seal.json"
    _rewrite(path, lambda seal: seal["trials"].pop("smooth"))
    with pytest.raises(TrialRegistryError, match="complete expected"):
        registry.load_trial_registry(tmp_path, "research")


def test_result_tamper_is_detected_even_when_outer_event_is_redigested(tmp_path, contract, frozen_clock) -> None:
    registry.create_trial_registry(tmp_path, "research", contract)
    registry.start_trial(tmp_path, "research", "baseline")
    registry.finish_trial(tmp_path, "research", "baseline", status="succeeded", result={"score": 1})
    path = tmp_path / "research" / "event-00000002.json"
    _rewrite(path, lambda event: event["result"].update(score=999))
    with pytest.raises(TrialRegistryError, match="result digest"):
        registry.load_trial_registry(tmp_path, "research")


def test_wrong_seal_anchor_and_truncated_unsealed_family_are_not_verified(tmp_path, contract, frozen_clock) -> None:
    _, seal = _complete(tmp_path, contract)
    with pytest.raises(TrialRegistryError, match="independently retained anchor"):
        registry.verify_trial_registry(tmp_path, "research", expected_seal_digest="c" * 64)
    (tmp_path / "research" / "seal.json").unlink()
    (tmp_path / "research" / "event-00000006.json").unlink()
    report = registry.verify_trial_registry(tmp_path, "research")
    assert report["declared_family_complete"] is False
    with pytest.raises(TrialRegistryError, match="independently retained anchor"):
        registry.verify_trial_registry(tmp_path, "research", expected_seal_digest=seal["digest"])
