from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, sha256_hex
from app.services import market_scan_research_sensitivity as service
from app.services.indicator_trend import trend_score_from_impact
from app.services.market_scan_research_challengers import FrozenResearchScore
from app.services.market_scan_research_experiment import FrozenResearchBatch, FrozenResearchDataset
from app.services.market_scan_research_portfolio import replay_research_portfolio
from app.services.market_scan_research_portfolio_models import ResearchPortfolioConfig
from app.services.market_scan_research_sensitivity_contract import admit_sensitivity_plan
from app.services.market_scan_research_sensitivity_statistics import sensitivity_account_comparison
from app.services.trading_calendar import trading_dates_between
from tests.test_market_scan_research_portfolio import row


DAYS = tuple(day.isoformat() for day in trading_dates_between(date(2026, 8, 24), date(2026, 8, 31)))


def plan(**changes):
    return {"schema_version": "market-scan-execution-sensitivity-plan-v1", "signal_dates": [DAYS[0]],
            "variants": ["production_v5", "smooth_turnover"], "initial_cash_values": [3000.0, 6000.0],
            "cost_profiles": ["base", "stress"], "participation_rates": [0.01], "top_n": 1, "horizon": 1, **changes}


def dataset():
    trend = trend_score_from_impact(8)
    score = FrozenResearchScore("600519.SH", 1, float(trend), trend, 0., 0., 0., 4., "a" * 64)
    return FrozenResearchDataset((FrozenResearchBatch(1, DAYS[0], "b" * 64, (score,)),), "c" * 64, "d" * 64)


def run(value=None, rows=None):
    value = plan() if value is None else value
    rows = tuple(row(day) for day in DAYS) if rows is None else rows
    return service.run_research_sensitivity(dataset(), DAYS, value, expected_plan_digest=sha256_hex(canonical_json_bytes(value)), synthetic_rows=rows)


def test_every_declared_cell_replays_and_matches_a_separate_account():
    result = run()
    assert result["declared_cell_count"] == 8 and result["status"] == "complete"
    assert len({cell["cell_id"] for cell in result["cells"]}) == 8
    batches = service.research_signal_batches(dataset(), "production_v5")
    for cell in result["cells"]:
        direct = replay_research_portfolio(batches, DAYS, config=ResearchPortfolioConfig(**cell["config"]),
                                          synthetic_rows=tuple(row(day) for day in DAYS))
        assert cell["account"]["result_digest"] == direct.result_digest
        assert cell["production_comparison"]["mean_daily_net_return_improvement"] == 0
        assert cell["promotion_eligible"] is False
    assert result == run() and result["scenario_selection"] == "none"


def test_fee_change_can_change_lots_and_invert_net_return_order_without_signal_improvement():
    # Per-sleeve 1005.60 buys one 100-share lot under base but cannot under stress.
    result = run(plan(initial_cash_values=[2011.2], variants=["production_v5"]))
    base, stress = [cell["account"] for cell in result["cells"]]
    assert base["buy_count"] == 1 and stress["buy_count"] == 0
    assert base["total_return"] < stress["total_return"] == 0
    assert base["same_executed_path_fee_addback_return"] == pytest.approx(0, abs=1e-12)
    assert result["promotion_eligible"] is False


def test_participation_changes_actual_lots_instead_of_subtracting_a_scalar_cost():
    rows = tuple(row(day, amount=100000) for day in DAYS)
    result = run(plan(initial_cash_values=[20000.0], cost_profiles=["base"], participation_rates=[.001, .1], variants=["production_v5"]), rows)
    small, large = [cell["account"] for cell in result["cells"]]
    assert small["buy_count"] == 0 and large["buy_count"] == 1
    assert small["event_reason_counts"]["cash_or_prior_capacity_below_minimum_lot"] == 1


def test_failure_stays_in_every_affected_cell_and_does_not_shrink_the_grid(monkeypatch):
    prepare = service.research_signal_batches
    def fail(ds, variant):
        if variant == "smooth_turnover":
            raise ValueError("explicit failed candidate")
        return prepare(ds, variant)
    monkeypatch.setattr(service, "research_signal_batches", fail)
    result = run()
    assert result["declared_cell_count"] == 8 and result["status"] == "incomplete"
    failures = [cell for cell in result["cells"] if cell["status"] == "failed"]
    assert len(failures) == 4 and all(cell["account"] is None for cell in failures)


def test_failed_reference_keeps_successful_candidate_but_withholds_comparison(monkeypatch):
    replay = service.replay_research_portfolio
    calls = 0
    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls % 2:
            raise RuntimeError("reference unavailable")
        return replay(*args, **kwargs)
    monkeypatch.setattr(service, "replay_research_portfolio", fail)
    result = run()
    assert result["status"] == "incomplete"
    assert all(cell["production_comparison"]["status"] == "reference_failed" for cell in result["cells"] if cell["status"] == "succeeded")


def test_missing_valuation_keeps_full_dates_and_withholds_means():
    result = run(rows=tuple(row(day) for day in DAYS if day != DAYS[2]))
    assert result["status"] == "incomplete" and result["comparable_cell_count"] == 0
    assert result["replayed_cell_count"] == result["declared_cell_count"] == 8
    for cell in result["cells"]:
        account = cell["account"]
        assert len(account["daily"]) == len(DAYS)
        assert account["mean_invested_nav_fraction"] is None
        assert account["observed_invested_fraction_days"] < len(DAYS)
        assert cell["production_comparison"]["mean_daily_net_return_improvement"] is None


def test_no_execution_evidence_cannot_be_a_complete_report():
    result = run(rows=())
    assert result["status"] == "incomplete" and result["comparable_cell_count"] == 0
    assert result["replayed_cell_count"] == 8
    assert all(cell["account"]["total_return"] is None for cell in result["cells"])


@pytest.mark.parametrize("changes", [
    {"variants": ["smooth_turnover"]}, {"variants": ["production_v5", "production_v5"]},
    {"initial_cash_values": [True]}, {"initial_cash_values": [100.001]},
    {"initial_cash_values": [0.]}, {"initial_cash_values": [1e13]}, {"initial_cash_values": [100., 100.]},
    {"participation_rates": [0.]}, {"participation_rates": [1.1]}, {"participation_rates": [True]},
    {"cost_profiles": ["fake"]}, {"top_n": True}, {"horizon": 0}, {"horizon": 21},
    {"signal_dates": [DAYS[0], DAYS[0]]}, {"signal_dates": ["2026-8-24"]},
    {"initial_cash_values": [float(x) for x in range(1, 9)], "participation_rates": [.01, .02, .03], "cost_profiles": ["base", "stress", "conservative"]},
    {"surprise": 1},
])
def test_invalid_or_excessive_grids_rejected(changes):
    value = plan(**changes)
    with pytest.raises((ValueError, TypeError)):
        admit_sensitivity_plan(value, sha256_hex(canonical_json_bytes(value)))


def test_pin_dates_and_calendar_identity_cannot_drift():
    value = plan()
    with pytest.raises(ValueError, match="digest mismatch"):
        service.run_research_sensitivity(dataset(), DAYS, value, expected_plan_digest="f" * 64)
    value["signal_dates"] = [DAYS[1]]
    with pytest.raises(ValueError, match="signal date"):
        run(value)
    value = plan()
    with pytest.raises(ValueError, match="calendar"):
        service.run_research_sensitivity(dataset(), DAYS[1:], value, expected_plan_digest=sha256_hex(canonical_json_bytes(value)))
    before = deepcopy(value)
    run(value)
    assert value == before


def test_comparison_rejects_different_account_configuration_or_date_axis():
    rows = tuple(row(day) for day in DAYS)
    account = replay_research_portfolio(service.research_signal_batches(dataset(), "production_v5"), DAYS,
                                       config=ResearchPortfolioConfig(top_n=1, horizon=1), synthetic_rows=rows)
    for changed in (replace(account, config=ResearchPortfolioConfig(top_n=2, horizon=1)), replace(account, days=account.days[1:])):
        with pytest.raises(ValueError, match="identical"):
            sensitivity_account_comparison(account, changed)


def test_nonfinite_input_is_rejected_before_it_can_be_hashed_or_evaluated():
    with pytest.raises(ArtifactIOError):
        admit_sensitivity_plan(plan(initial_cash_values=[float("nan")]), "a" * 64)
