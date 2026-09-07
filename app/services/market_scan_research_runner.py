"""Run every frozen challenger, retaining failures and independently replaying receipts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from statistics import fmean
from typing import cast

from app.models.paper_trading import CostProfileName
from app.services.market_scan_evaluation_statistics import (
    benjamini_yekutieli, moving_block_bootstrap_confidence_interval, moving_block_bootstrap_p_value,
)
from app.services.market_scan_official_execution import OfficialExecutionSessionRow, VerifiedOfficialExecutionSession
from app.services.market_scan_research_challengers import RESEARCH_CHALLENGERS, ResearchChallenger
from app.services.market_scan_research_experiment import (
    FrozenResearchDataset, build_research_trial_contract,
    research_execution_manifest, research_optimization_test_contract, research_signal_batches,
)
from app.services.market_scan_research_portfolio import replay_research_portfolio, research_portfolio_payload
from app.services.market_scan_research_portfolio_models import ResearchPortfolioConfig, ResearchPortfolioResult
from app.services.market_scan_trial_registry import (
    finish_trial, load_trial_registry, seal_trial_registry, start_trial, trial_registry_execution_lease, verify_trial_registry,
)
from app.services.market_scan_trial_registry_contract import registry_object, trial_registry_digest


RESEARCH_REPORT_VERSION = "market-scan-shared-account-research-v2"


def run_registered_research(
    root: Path, registration_id: str, dataset: FrozenResearchDataset, *,
    official_sessions: Sequence[VerifiedOfficialExecutionSession] = (),
    synthetic_rows: Sequence[OfficialExecutionSessionRow] = (), replay_only: bool = False, resume: bool = False,
) -> dict[str, object]:
    with trial_registry_execution_lease(root, registration_id):
        return _execute_registered_research(
            root, registration_id, dataset, official_sessions=official_sessions, synthetic_rows=synthetic_rows,
            replay_only=replay_only, resume=resume,
        )


def _execute_registered_research(
    root: Path, registration_id: str, dataset: FrozenResearchDataset, *,
    official_sessions: Sequence[VerifiedOfficialExecutionSession],
    synthetic_rows: Sequence[OfficialExecutionSessionRow], replay_only: bool, resume: bool,
) -> dict[str, object]:
    state = load_trial_registry(root, registration_id)
    contract = registry_object(state.registration["contract"], "contract")
    config, sessions = _validate_run_contract(contract, dataset, official_sessions, synthetic_rows)
    if (replay_only and state.seal is None) or (not replay_only and state.statuses and not resume) or (resume and state.seal):
        raise ValueError("replay requires a sealed registry; execution requires a fresh registry")
    results: dict[str, object] = {}
    for variant in RESEARCH_CHALLENGERS:
        if terminal := _resume_terminal(root, registration_id, variant, state.statuses.get(variant), resume):
            results[variant] = terminal
            continue
        replay_trial = replay_only or state.statuses.get(variant) in {"succeeded", "failed"}
        results[variant] = _run_trial(root, registration_id, variant, dataset, sessions, config,
                                      official_sessions, synthetic_rows, replay_trial)
    if not replay_only:
        seal_trial_registry(root, registration_id)
    report: dict[str, object] = {
        "schema_version": RESEARCH_REPORT_VERSION, "registry": verify_trial_registry(root, registration_id),
        "input_manifest": contract["input_manifest"], "results": results,
        "primary_objective": contract["primary_objective"], "benchmark_policy": contract["benchmark_policy"],
        "optimization_test": research_optimization_test_contract(),
        "family_inference": _family_inference(results), "promotion_eligible": False,
        "conclusion": "research_diagnostics_only; external preregistration and prospective performance remain unproven",
    }
    report["digest"] = trial_registry_digest(report)
    return report


def _resume_terminal(root: Path, registration_id: str, variant: str, status: str | None, resume: bool) -> dict[str, object] | None:
    if status == "running" and resume:
        finish_trial(root, registration_id, variant, status="cancelled", reason="interrupted_without_terminal_receipt")
        status = "cancelled"
    if status != "cancelled":
        return None
    state = load_trial_registry(root, registration_id)
    event = next(event for event in state.events if event["trial_id"] == variant and event["event"] == "finished")
    return {"status": "cancelled", "reason": event["reason"], "p_value": None, "numerical_replay": "not_available"}


def _validate_run_contract(
    contract: Mapping[str, object], dataset: FrozenResearchDataset,
    official: Sequence[VerifiedOfficialExecutionSession], synthetic: Sequence[OfficialExecutionSessionRow],
) -> tuple[ResearchPortfolioConfig, tuple[str, ...]]:
    cost = registry_object(contract["cost_policy"], "cost_policy")
    capital = registry_object(contract["capital_policy"], "capital_policy")
    calendar = registry_object(contract["calendar"], "calendar")
    config = ResearchPortfolioConfig(
        top_n=100, horizon=5,
        initial_cash=cast(float, capital["initial_cash"]), cost_profile=cast(CostProfileName, cost["profile"]),
        max_participation_rate=cast(float, cost["max_participation_rate"]),
    )
    expected = build_research_trial_contract(
        dataset, calendar, execution_manifest_digest=research_execution_manifest(official, synthetic),
        exploration_cutoff=cast(str, contract["exploration_cutoff"]),
        registration_kind=cast(str, contract["registration_kind"]), initial_cash=config.initial_cash,
        cost_profile=config.cost_profile, max_participation_rate=config.max_participation_rate,
    )
    if trial_registry_digest(dict(contract)) != trial_registry_digest(expected):
        raise ValueError("frozen family, policy, implementation or input manifest mismatch")
    dates = cast(list[str], calendar["trading_dates"])
    # Start at first test signal, preserving all subsequent dates, including blocked exits.
    sessions = tuple(day for day in dates if day >= dataset.batches[0].signal_date)
    return config, sessions


def _run_trial(
    root: Path, registration_id: str, variant: ResearchChallenger, dataset: FrozenResearchDataset,
    sessions: Sequence[str], config: ResearchPortfolioConfig,
    official: Sequence[VerifiedOfficialExecutionSession], synthetic: Sequence[OfficialExecutionSessionRow], replay_only: bool,
) -> dict[str, object]:
    if not replay_only:
        start_trial(root, registration_id, variant)
    try:
        result = _evaluate_trial(dataset, variant, sessions, config, official, synthetic)
    except KeyboardInterrupt:
        if not replay_only:
            finish_trial(root, registration_id, variant, status="cancelled", reason="execution interrupted")
        raise
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if replay_only:
            _verify_failure(root, registration_id, variant, reason)
        else:
            finish_trial(root, registration_id, variant, status="failed", reason=reason)
        return {"status": "failed", "reason": reason, "p_value": None}
    receipt = {"evaluation_digest": trial_registry_digest(result), "comparison": result["comparison"],
               "production_comparison": result["production_comparison"]}
    if replay_only:
        _verify_receipt(root, registration_id, variant, receipt)
    else:
        finish_trial(root, registration_id, variant, status="succeeded", result=receipt)
    return result


def _verify_receipt(root: Path, registration_id: str, variant: str, receipt: dict[str, object]) -> None:
    state = load_trial_registry(root, registration_id)
    matches = [event for event in state.events if event["trial_id"] == variant and event["event"] == "finished"]
    if len(matches) != 1 or matches[0]["status"] != "succeeded" or matches[0]["result"] != receipt:
        raise ValueError("independent evaluation replay disagrees with recorded trial result")


def _verify_failure(root: Path, registration_id: str, variant: str, reason: str) -> None:
    state = load_trial_registry(root, registration_id)
    matches = [event for event in state.events if event["trial_id"] == variant and event["event"] == "finished"]
    if len(matches) != 1 or matches[0]["status"] != "failed" or matches[0]["reason"] != reason:
        raise ValueError("independent failure replay disagrees with recorded trial result")


def _evaluate_trial(
    dataset: FrozenResearchDataset, variant: ResearchChallenger, sessions: Sequence[str], config: ResearchPortfolioConfig,
    official: Sequence[VerifiedOfficialExecutionSession], synthetic: Sequence[OfficialExecutionSessionRow],
) -> dict[str, object]:
    batches = research_signal_batches(dataset, variant)
    candidate = replay_research_portfolio(batches, sessions, config=config, official_sessions=official, synthetic_rows=synthetic)
    benchmark = replay_research_portfolio(
        research_signal_batches(dataset, "production_v5"), sessions, config=replace(config, allocation="frozen-universe"),
        official_sessions=official, synthetic_rows=synthetic,
    )
    comparison = compare_research_accounts(candidate, benchmark, seed=variant)
    comparison.update({"reference": "same-capital-frozen-universe", "eligible_for_optimization_test": False})
    production = candidate if variant == "production_v5" else replay_research_portfolio(
        research_signal_batches(dataset, "production_v5"), sessions, config=config,
        official_sessions=official, synthetic_rows=synthetic,
    )
    incremental = _production_comparison(candidate, production, variant)
    return {"status": "succeeded", "variant": variant, "candidate": research_portfolio_payload(candidate),
            "benchmark": research_portfolio_payload(benchmark), "comparison": comparison,
            "production_reference": research_portfolio_payload(production), "production_comparison": incremental,
            "p_value": incremental["p_value"]}


def _production_comparison(
    candidate: ResearchPortfolioResult, production: ResearchPortfolioResult, variant: ResearchChallenger,
) -> dict[str, object]:
    protocol = research_optimization_test_contract()
    identity = {"reference": protocol["reference"], "target": protocol["target"]}
    if variant == "production_v5":
        return {**identity, "inference_status": "baseline_reference", "p_value": None}
    comparison = compare_research_accounts(candidate, production, seed="candidate-vs-production:" + variant)
    comparison.update(identity)
    comparison["daily_net_return_improvement"] = comparison.pop("daily_net_excess")
    comparison["mean_daily_net_return_improvement"] = comparison.pop("mean_daily_net_excess_return")
    return comparison


def compare_research_accounts(candidate: ResearchPortfolioResult, benchmark: ResearchPortfolioResult, *, seed: str) -> dict[str, object]:
    pairs = _paired_daily_excess(candidate, benchmark)
    complete = bool(pairs) and all(value is not None for value in pairs)
    complete = complete and candidate.total_return is not None and benchmark.total_return is not None
    series = [cast(float, value) if complete else float("nan") for value in pairs]
    return _account_comparison(candidate, benchmark, pairs, series, complete, seed)


def _paired_daily_excess(candidate: ResearchPortfolioResult, benchmark: ResearchPortfolioResult) -> list[float | None]:
    if [day.session_date for day in candidate.days] != [day.session_date for day in benchmark.days]:
        raise ValueError("candidate and benchmark calendars must match exactly")
    return [None if left.daily_return is None or right.daily_return is None else left.daily_return-right.daily_return
             for left, right in zip(candidate.days[1:], benchmark.days[1:], strict=True)]


def _account_comparison(
    candidate: ResearchPortfolioResult, benchmark: ResearchPortfolioResult,
    pairs: list[float | None], series: list[float], complete: bool, seed: str,
) -> dict[str, object]:
    protocol = research_optimization_test_contract()
    samples = cast(int, protocol["bootstrap_samples"])
    block = cast(int, protocol["block_length_sessions"])
    minimum = cast(int, protocol["minimum_session_count"])
    ci = moving_block_bootstrap_confidence_interval(series, samples=samples, block_length=block, minimum_count=minimum, seed_text=seed)
    p_value = moving_block_bootstrap_p_value(series, samples=samples, block_length=block, minimum_count=minimum, seed_text=seed)
    return {
        "target": "mean_daily_net_excess_return", "daily_net_excess": pairs,
        "mean_daily_net_excess_return": fmean(cast(list[float], pairs)) if complete else None,
        "confidence_interval_95": ci, "p_value": p_value, "block_length": block, "minimum_sessions": minimum,
        "bootstrap_samples": samples,
        "observation_count": len(pairs), "complete": complete,
        "candidate_maximum_drawdown": candidate.maximum_drawdown, "benchmark_maximum_drawdown": benchmark.maximum_drawdown,
        "candidate_entry_fill_coverage": candidate.entry_fill_coverage, "benchmark_entry_fill_coverage": benchmark.entry_fill_coverage,
        "candidate_mean_cash_to_initial_capital": fmean(day.cash/candidate.config.initial_cash for day in candidate.days),
        "benchmark_mean_cash_to_initial_capital": fmean(day.cash/benchmark.config.initial_cash for day in benchmark.days),
        "exposure_warning": "finite capital and lot sizes can leave different cash allocations; this is an investable-account comparison",
        "inference_status": "available" if ci is not None else "insufficient_or_incomplete_sessions",
    }


def _family_inference(results: Mapping[str, object]) -> dict[str, object]:
    protocol = research_optimization_test_contract()
    variants = cast(list[ResearchChallenger], protocol["family"])
    values = [cast(float | None, registry_object(results[variant], variant)["p_value"]) for variant in variants]
    adjusted, rejected = benjamini_yekutieli(values, alpha=cast(float, protocol["alpha"]))
    return {"method": protocol["method"], "alpha": protocol["alpha"], "declared_family_size": len(variants),
            "declared_attempt_count": len(RESEARCH_CHALLENGERS), "reference": protocol["reference"],
            "adjusted_p_values": dict(zip(variants, adjusted, strict=True)),
            "rejections": dict(zip(variants, rejected, strict=True)),
            "scope": "all three declared incremental hypotheses; unavailable tests remain in family; baseline is a reference",
            "limitations": "arbitrary-dependence FDR adjustment requires valid bootstrap p-values; no FWER or external completeness claim"}
