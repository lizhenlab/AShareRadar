from __future__ import annotations

from copy import deepcopy

import pytest

from app.services import market_scan_research_runner as runner
from app.services import market_scan_research_experiment as experiment
from app.services.market_scan_trial_registry import create_trial_registry, load_trial_registry
from app.services.market_scan_trial_registry_contract import TRIAL_PRIMARY_OBJECTIVE, TrialRegistryError, validate_trial_contract
from tests.test_market_scan_trial_registry import contract as contract_fixture, frozen_clock as clock_fixture
from tests.test_market_scan_research_runner import research_fixture
from tests.test_market_scan_research_incremental_comparison import _daily_account


contract = contract_fixture
frozen_clock = clock_fixture
INCREMENTAL_OBJECTIVE = {
    "schema_version": "market-scan-primary-incremental-objective-v1",
    "top_n": 100, "horizon": 5, "target": "mean_daily_net_return_improvement", "reference": "production_v5-top100",
}
INCREMENTAL_BENCHMARK = {"allocation": "same-capital-production-v5-top100"}


def test_new_bundle_freezes_incremental_primary_objective_and_reference(monkeypatch) -> None:
    _, _, _, frozen = research_fixture(monkeypatch)
    assert frozen["primary_objective"] == INCREMENTAL_OBJECTIVE
    assert frozen["benchmark_policy"] == INCREMENTAL_BENCHMARK
    protocol = experiment.research_optimization_test_contract()
    assert protocol["target"] == frozen["primary_objective"]["target"]
    assert protocol["reference"] == frozen["primary_objective"]["reference"]


@pytest.mark.parametrize("incremental", [False, True])
def test_registry_reads_exact_old_and_new_objectives_without_rewriting(tmp_path, contract, frozen_clock, incremental) -> None:
    if incremental:
        contract["primary_objective"] = dict(INCREMENTAL_OBJECTIVE)
        contract["benchmark_policy"] = dict(INCREMENTAL_BENCHMARK)
    else:
        assert contract["primary_objective"] == TRIAL_PRIMARY_OBJECTIVE
    create_trial_registry(tmp_path, "objective-version", contract)
    path = tmp_path / "objective-version" / "registration.json"
    original = path.read_bytes()
    loaded = load_trial_registry(tmp_path, "objective-version")
    assert loaded.registration["contract"] == contract
    assert path.read_bytes() == original


@pytest.mark.parametrize("mutation", [
    {"target": "mean_daily_net_excess_return"}, {"reference": "cash"}, {"schema_version": "unknown"},
    {"top_n": True}, {"horizon": 20}, {"extra": "unregistered objective"},
])
def test_registry_does_not_accept_arbitrary_or_relabelled_new_objectives(contract, mutation) -> None:
    contract["primary_objective"] = {**INCREMENTAL_OBJECTIVE, **mutation}
    contract["benchmark_policy"] = dict(INCREMENTAL_BENCHMARK)
    with pytest.raises(TrialRegistryError, match="primary_objective"):
        validate_trial_contract(contract)


def test_incremental_objective_cannot_keep_the_market_as_primary_benchmark(contract) -> None:
    contract["primary_objective"] = dict(INCREMENTAL_OBJECTIVE)
    with pytest.raises(TrialRegistryError, match="benchmark_policy"):
        validate_trial_contract(contract)


def test_current_runner_cannot_reinterpret_a_legacy_primary_objective(tmp_path, monkeypatch) -> None:
    _, dataset, rows, frozen = research_fixture(monkeypatch)
    frozen["primary_objective"] = dict(TRIAL_PRIMARY_OBJECTIVE)
    frozen["benchmark_policy"] = {"allocation": "same-capital-equal-weight-frozen-universe"}
    create_trial_registry(tmp_path, "legacy-objective", frozen)
    with pytest.raises(ValueError, match="mismatch"):
        runner.run_registered_research(tmp_path, "legacy-objective", dataset, synthetic_rows=rows)
    assert load_trial_registry(tmp_path, "legacy-objective").events == ()


def test_bootstrap_parameters_and_report_share_the_frozen_protocol_source(monkeypatch) -> None:
    protocol = deepcopy(experiment.research_optimization_test_contract())
    protocol.update(bootstrap_samples=137, block_length_sessions=7, minimum_session_count=42)
    observed = []

    def interval(values, **settings):
        observed.append(settings)
        return [0., 0.]

    def p_value(values, **settings):
        observed.append(settings)
        return 1.

    monkeypatch.setattr(runner, "research_optimization_test_contract", lambda: protocol)
    monkeypatch.setattr(runner, "moving_block_bootstrap_confidence_interval", interval)
    monkeypatch.setattr(runner, "moving_block_bootstrap_p_value", p_value)
    account = _daily_account([0.] * 45)
    result = runner.compare_research_accounts(account, account, seed="contract-source")
    expected = {"samples": 137, "block_length": 7, "minimum_count": 42, "seed_text": "contract-source"}
    assert observed == [expected, expected]
    assert result["block_length"] == 7 and result["minimum_sessions"] == 42
    assert result["bootstrap_samples"] == 137


def test_family_adjustment_uses_the_same_frozen_alpha_and_family(monkeypatch) -> None:
    protocol = deepcopy(experiment.research_optimization_test_contract())
    protocol["alpha"] = .02
    observed = []

    def adjustment(values, *, alpha):
        observed.append((values, alpha))
        return (None,) * len(values), (None,) * len(values)

    monkeypatch.setattr(runner, "research_optimization_test_contract", lambda: protocol)
    monkeypatch.setattr(runner, "benjamini_yekutieli", adjustment)
    results = {variant: {"p_value": value} for variant, value in zip(protocol["family"], [.01, None, .03], strict=True)}
    family = runner._family_inference(results)
    assert observed == [([.01, None, .03], .02)]
    assert family["alpha"] == .02 and family["declared_family_size"] == 3
