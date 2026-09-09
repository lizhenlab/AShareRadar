"""Explicit limits of the v6 ranking shadow comparison.

A fixed challenger comparison does not provide a complete search trial family,
independent Sharpe observations, or a shared-capital portfolio equity curve.
Its descriptive diagnostics cannot authorize production promotion.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Mapping, cast

RANKING_INFERENCE_REGISTERED_AT = "2026-09-10T01:30:18+08:00"
RANKING_SHADOW_CONTRACT_VERSION = "probability-ranking-diagnostic-shadow-v2"


def ranking_inference_contract() -> dict[str, object]:
    """Return the current immutable inference policy, with its actual change time."""
    return {
        "version": "probability-ranking-inference-v2-audit-only",
        "registered_at": RANKING_INFERENCE_REGISTERED_AT,
        "promotion_eligible": False,
        "probability_of_backtest_overfitting": {
            "status": "unavailable",
            "reason": "complete_frozen_search_trial_family_and_aligned_returns_missing",
        },
        "deflated_sharpe_probability": {
            "status": "unavailable",
            "reason": "search_trial_benchmark_and_time_dependence_contract_missing",
        },
        "portfolio_drawdown": {
            "status": "unavailable",
            "reason": "overlapping_horizon_excess_returns_are_not_shared_capital_daily_nav",
        },
        "mean_delta_confidence_interval": {
            "method": "deterministic_circular_moving_target_offset_block_95pct",
            "interpretation": "fixed_pair_mean_comparison_diagnostic_only",
            "promotion_eligible": False,
        },
        "iid_zero_benchmark_probabilistic_sharpe": {
            "interpretation": "iid_psr_with_zero_sharpe_benchmark_not_dsr",
            "promotion_eligible": False,
        },
        "distinct_sessions_are_not_independent_observations": True,
    }


def current_ranking_shadow_analysis(legacy_shape: Mapping[str, object]) -> dict[str, object]:
    """Keep all rows/dates and diagnostics while rejecting unsupported inference."""
    output = deepcopy(dict(legacy_shape))
    metrics = cast(dict[str, object], output["metrics"])
    gates = cast(dict[str, bool], output["gates"])
    metrics["distinct_session_count"] = metrics.pop("independent_session_count")
    metrics["iid_zero_benchmark_probabilistic_sharpe_diagnostic"] = metrics[
        "deflated_sharpe_probability"
    ]
    metrics["synthetic_horizon_excess_return_chain_drawdown_diagnostic"] = {
        "v5": metrics["v5_maximum_drawdown"],
        "v6": metrics["v6_maximum_drawdown"],
        "promotion_eligible": False,
    }
    for name in ("probability_of_backtest_overfitting", "deflated_sharpe_probability",
                 "v5_maximum_drawdown", "v6_maximum_drawdown"):
        metrics[name] = None
    gates["minimum_60_distinct_shadow_sessions"] = gates.pop(
        "minimum_60_independent_shadow_sessions"
    )
    for name in ("pbo_at_most_20pct", "deflated_sharpe_probability_at_least_95pct",
                 "maximum_drawdown_not_materially_worse"):
        gates[name] = False
    gates["inference_contract_predates_every_shadow_session"] = all(
        str(item["signal_session"]) > RANKING_INFERENCE_REGISTERED_AT[:10]
        for item in cast(list[dict[str, object]], output["session_summaries"])
    )
    output["qualified"] = False
    output["failed_gates"] = sorted(name for name, passed in gates.items() if not passed)
    output["inference_contract"] = ranking_inference_contract()
    output["production_effect"] = "audit_only_inference_unavailable"
    return output
