from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from app.services.indicator_trend import trend_score_from_impact
from app.services import market_scan_research_runner as runner
from app.services import market_scan_research_experiment as experiment
from app.services.market_scan_research_challengers import FrozenResearchScore, RESEARCH_CHALLENGERS, score_research_challenger
from app.services.market_scan_research_experiment import FrozenResearchBatch, FrozenResearchDataset
from app.services.market_scan_research_portfolio_models import ResearchPortfolioConfig
from app.services.trading_calendar import trading_dates_between
from tests.test_market_scan_research_portfolio import replay, row
from tests.test_market_scan_research_runner import research_fixture
from app.services.market_scan_trial_registry import create_trial_registry, load_trial_registry
from tools import run_market_scan_research as cli


@pytest.fixture(scope="module")
def identical_score_results() -> dict:
    days = tuple(day.isoformat() for day in trading_dates_between(date(2026, 1, 5), date(2026, 4, 15)))[:55]
    symbols = [f"{600000 + index:06d}.SH" for index in range(200)]
    trend = trend_score_from_impact(8)
    scores = tuple(FrozenResearchScore(symbol, index + 1, float(trend), trend, 0., 0., 0., 4., "a" * 64)
                   for index, symbol in enumerate(symbols))
    rankings = [tuple(item.symbol for item in score_research_challenger(scores, variant)) for variant in RESEARCH_CHALLENGERS]
    assert len(set(rankings)) == 1  # Every stock and every candidate has the same economic return.
    batches = tuple(FrozenResearchBatch(index + 1, day, "b" * 64, scores) for index, day in enumerate(days[:-6]))
    dataset = FrozenResearchDataset(batches, "c" * 64, "d" * 64)
    prices = [round(10 * 1.005 ** index, 4) for index in range(len(days))]
    rows = tuple(row(day, symbol=symbol, price=prices[index], previous=prices[max(0, index - 1)])
                 for index, day in enumerate(days) for symbol in symbols)
    return {variant: runner._evaluate_trial(dataset, variant, days, ResearchPortfolioConfig(), (), rows)
            for variant in RESEARCH_CHALLENGERS}


def test_cash_market_benchmark_cannot_make_identical_scores_an_improvement(identical_score_results) -> None:
    for variant, result in identical_score_results.items():
        assert result["benchmark"]["filled_entry_slots"] == 0
        assert result["candidate"]["total_return"] > 0
        assert result["comparison"]["p_value"] < .01  # Preserve the market-relative diagnostic honestly.
        assert result["comparison"]["eligible_for_optimization_test"] is False
        paired = result["production_comparison"]
        assert result["production_reference"]["config"] == result["candidate"]["config"]
        if variant == "production_v5":
            assert result["p_value"] is None
            assert paired["inference_status"] == "baseline_reference"
        else:
            assert paired["mean_daily_net_return_improvement"] == 0
            assert paired["p_value"] == result["p_value"] == 1
            assert paired["confidence_interval_95"] == [0, 0]
    family = runner._family_inference(identical_score_results)
    assert family["declared_family_size"] == 3
    assert family["declared_attempt_count"] == 4
    assert "production_v5" not in family["adjusted_p_values"]
    assert set(family["rejections"].values()) == {False}


def _daily_account(returns):
    account = replay()
    days = tuple(replace(account.days[0], session_date=str(index), daily_return=value) for index, value in enumerate(returns))
    return replace(account, days=days)


def test_production_comparison_tests_increment_beyond_an_already_profitable_model() -> None:
    candidate = _daily_account([0.] + [.002] * 44)
    production = _daily_account([0.] + [.003] * 44)
    comparison = runner._production_comparison(candidate, production, "smooth_turnover")
    assert comparison["reference"] == "production_v5-top100"
    assert comparison["mean_daily_net_return_improvement"] == pytest.approx(-.001)
    assert comparison["p_value"] == 1
    assert comparison["confidence_interval_95"][1] < 0


def test_production_comparison_retains_missing_daily_return_in_fixed_time_axis() -> None:
    candidate = _daily_account([0.] + [.002] * 44)
    production = _daily_account([0.] + [.001] * 44)
    days = list(production.days)
    days[10] = replace(days[10], daily_return=None)
    result = runner._production_comparison(candidate, replace(production, days=tuple(days)), "combined")
    assert len(result["daily_net_return_improvement"]) == 44
    assert result["daily_net_return_improvement"][9] is None
    assert result["mean_daily_net_return_improvement"] is None
    assert result["confidence_interval_95"] is None and result["p_value"] is None


def test_failed_candidate_stays_in_declared_optimization_family() -> None:
    results = {variant: {"status": "succeeded", "p_value": .01} for variant in RESEARCH_CHALLENGERS}
    results["production_v5"]["p_value"] = None
    results["combined"] = {"status": "failed", "p_value": None}
    family = runner._family_inference(results)
    assert family["declared_family_size"] == 3
    assert family["adjusted_p_values"]["combined"] is None
    assert family["method"] == "benjamini-yekutieli-fdr"
    assert family["adjusted_p_values"]["smooth_turnover"] == pytest.approx(.0275)
    assert family["adjusted_p_values"]["without_continuous_trend"] == pytest.approx(.0275)


def test_incremental_hypothesis_is_frozen_in_every_trial_before_execution(tmp_path, monkeypatch) -> None:
    _, dataset, rows, contract = research_fixture(monkeypatch)
    expected = experiment.research_optimization_test_contract()
    assert expected["family"] == ["without_continuous_trend", "smooth_turnover", "combined"]
    assert expected["block_length_sessions"] == 6 and expected["minimum_session_count"] == 40
    assert expected["method"] == "benjamini-yekutieli-fdr" and expected["alternative"] == "greater"
    assert all(trial["parameters"]["optimization_test"] == expected for trial in contract["trials"])
    contract["trials"][1]["parameters"]["optimization_test"]["block_length_sessions"] = 1
    create_trial_registry(tmp_path, "altered-test", contract)
    with pytest.raises(ValueError, match="mismatch"):
        runner.run_registered_research(tmp_path, "altered-test", dataset, synthetic_rows=rows)
    assert load_trial_registry(tmp_path, "altered-test").events == ()


def test_prepared_future_results_cannot_claim_a_prospective_registration(monkeypatch) -> None:
    _, dataset, rows, contract = research_fixture(monkeypatch)
    with pytest.raises(ValueError, match="retrospective registration only"):
        experiment.build_research_trial_contract(
            dataset, contract["calendar"], execution_manifest_digest=experiment.research_execution_manifest(synthetic_rows=rows),
            exploration_cutoff=contract["exploration_cutoff"], registration_kind="prospective",
        )


def test_cli_rejects_unsupported_prospective_mode_before_reading_any_inputs(tmp_path, capsys) -> None:
    root = tmp_path / "registries"
    code = cli.main([
        "register", "--bundle", str(tmp_path / "future-input-not-yet-available.json"),
        "--registry-root", str(root), "--registration-id", "future-plan", "--registration-kind", "prospective",
        "--output", str(tmp_path / "receipt.json"),
    ])
    assert code == 1 and "prospective" in capsys.readouterr().err
    assert not root.exists() and not (tmp_path / "receipt.json").exists()
