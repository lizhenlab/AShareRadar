"""Re-run every predeclared execution scenario with its own actual account path."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from itertools import product
from typing import TypeAlias

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.market_scan_official_execution import OfficialExecutionSessionRow, VerifiedOfficialExecutionSession
from app.services.market_scan_research_challengers import ResearchChallenger, research_challenger_spec
from app.services.market_scan_research_experiment import (
    FrozenResearchDataset, frozen_research_dataset_digest, research_execution_manifest, research_signal_batches,
)
from app.services.market_scan_research_portfolio import replay_research_portfolio
from app.services.market_scan_research_portfolio_models import ResearchPortfolioConfig, ResearchPortfolioResult, ResearchSignalBatch
from app.services.market_scan_research_sensitivity_contract import admit_sensitivity_plan
from app.services.market_scan_research_sensitivity_statistics import sensitivity_account_comparison, sensitivity_account_summary


Failure: TypeAlias = dict[str, object]
Outcome: TypeAlias = ResearchPortfolioResult | Failure


def run_research_sensitivity(
    dataset: FrozenResearchDataset, sessions: Sequence[str], plan: object, *, expected_plan_digest: str,
    official_sessions: Sequence[VerifiedOfficialExecutionSession] = (), synthetic_rows: Sequence[OfficialExecutionSessionRow] = (),
) -> dict[str, object]:
    settings, raw_plan, plan_digest = admit_sensitivity_plan(plan, expected_plan_digest)
    if [batch.signal_date for batch in dataset.batches] != settings.signal_dates:
        raise ValueError("frozen batches must match every predeclared signal date exactly")
    if not sessions or sessions[0] != settings.signal_dates[0] or len(sessions) > 2048:
        raise ValueError("execution calendar must begin on the first signal and contain at most 2048 sessions")
    execution_digest = research_execution_manifest(official_sessions, synthetic_rows)
    prepared = {variant: _prepare_variant(dataset, variant) for variant in settings.variants}
    cells = []
    for cash, profile, participation in product(settings.initial_cash_values, settings.cost_profiles, settings.participation_rates):
        config = ResearchPortfolioConfig(initial_cash=cash, top_n=settings.top_n, horizon=settings.horizon,
                                        cost_profile=profile, max_participation_rate=participation)
        outcomes = {variant: _run_account(prepared[variant], sessions, config, official_sessions, synthetic_rows)
                    for variant in settings.variants}
        for variant in settings.variants:
            cells.append(_cell(variant, config, outcomes[variant], outcomes["production_v5"]))
    report: dict[str, object] = {
        "schema_version": "market-scan-execution-sensitivity-report-v1", "plan": raw_plan, "plan_digest": plan_digest,
        "input_digest": frozen_research_dataset_digest(dataset), "execution_manifest_digest": execution_digest,
        "sessions": list(sessions), "variant_specifications": {variant: research_challenger_spec(variant) for variant in settings.variants},
        "cells": cells, "declared_cell_count": len(cells),
        **_completion(cells),
        "scenario_selection": "none", "promotion_eligible": False,
        "limitations": ["descriptive execution-assumption sensitivity, not identified market or crowding capacity",
                        "each cell replays fees, integer lots, cash, blocked trades and delayed exits; returns need not be monotonic in costs",
                        "fee addback retains the executed path; it is not a zero-cost counterfactual or gross-signal alpha",
                        "all cash and unresolved dates remain in each account; never average only successful cells",
                        "fixed score ranks are not expected-return forecasts; no strategy or model is promoted",
                        "a locally pinned grid is not external preregistration and cannot reveal undeclared searches"],
    }
    report["digest"] = sha256_hex(canonical_json_bytes(report))
    return report


def _completion(cells: list[dict[str, object]]) -> dict[str, object]:
    replayed = sum(cell["status"] == "succeeded" for cell in cells)
    comparable = sum(isinstance(comparison := cell["production_comparison"], dict) and comparison["status"] == "complete"
                     for cell in cells)
    return {"status": "complete" if replayed == comparable == len(cells) else "incomplete",
            "replayed_cell_count": replayed, "comparable_cell_count": comparable}


def _prepare_variant(dataset: FrozenResearchDataset, variant: ResearchChallenger) -> tuple[ResearchSignalBatch, ...] | Failure:
    try:
        return research_signal_batches(dataset, variant)
    except (ValueError, RuntimeError, ArithmeticError) as exc:
        return _failure(exc)


def _run_account(
    batches: tuple[ResearchSignalBatch, ...] | Failure, sessions: Sequence[str], config: ResearchPortfolioConfig,
    official: Sequence[VerifiedOfficialExecutionSession], synthetic: Sequence[OfficialExecutionSessionRow],
) -> Outcome:
    if isinstance(batches, dict):
        return batches
    try:
        return replay_research_portfolio(batches, sessions, config=config, official_sessions=official, synthetic_rows=synthetic)
    except (ValueError, RuntimeError, ArithmeticError) as exc:
        return _failure(exc)


def _failure(exc: Exception) -> Failure:
    return {"status": "failed", "error_type": type(exc).__name__, "reason": str(exc)[:500]}


def _cell(variant: ResearchChallenger, config: ResearchPortfolioConfig, account: Outcome, reference: Outcome) -> dict[str, object]:
    identity = {"variant": variant, "config": asdict(config)}
    cell: dict[str, object] = {**identity, "cell_id": sha256_hex(canonical_json_bytes(identity)), "promotion_eligible": False}
    if isinstance(account, dict):
        return {**cell, **account, "account": None, "production_comparison": None}
    cell.update(status="succeeded", account=sensitivity_account_summary(account))
    cell["production_comparison"] = sensitivity_account_comparison(account, reference) if isinstance(reference, ResearchPortfolioResult) else {
        "status": "reference_failed", "reason": reference, "mean_daily_net_return_improvement": None}
    return cell
