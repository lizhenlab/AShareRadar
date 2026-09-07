"""Date-preserving inference for descriptive frozen-score factor diagnostics."""

from __future__ import annotations

from collections.abc import Sequence
import math
from statistics import fmean

from app.services.market_scan_evaluation_config import EvaluationConfig
from app.services.market_scan_evaluation_statistics import inference_contract, moving_block_bootstrap_p_value


def factor_inference_metrics(
    daily_ic: Sequence[float], daily_partial_ic: Sequence[float], *,
    horizon: int, config: EvaluationConfig, seed_text: str,
) -> dict[str, object]:
    valid = [value for value in daily_ic if math.isfinite(value)]
    partial = [value for value in daily_partial_ic if math.isfinite(value)]
    minimum = max(config.minimum_session_count, 20, 2 * horizon)
    missing = len(daily_ic) - len(valid)
    raw_p = moving_block_bootstrap_p_value(
        daily_ic, samples=config.bootstrap_samples, block_length=horizon,
        seed_text=seed_text, minimum_count=minimum,
    )
    return {
        "schema_version": "market-scan-factor-diagnostics-v2",
        "status": "ok" if len(valid) >= minimum and not missing else "insufficient_data",
        "independent_session_count": len(valid),
        "expected_session_count": len(daily_ic),
        "missing_session_count": missing,
        "mean_rank_ic": fmean(valid) if valid else None,
        "mean_partial_rank_ic_controlling_raw_score": fmean(partial) if partial else None,
        "partial_ic_session_count": len(partial),
        "partial_ic_missing_session_count": len(daily_partial_ic) - len(partial),
        "descriptive_mean_scope": "finite-session-values-only; missing dates block inference",
        "rank_ic_target": "signal-close-to-D+H-close-gross-return",
        "minimum_outcome_coverage": config.complete_day_coverage,
        "inference": {**inference_contract(horizon, minimum), "missing_dates": "retained; no time compression"},
        "insufficient_reasons": (["minimum_session_count"] if len(valid) < minimum else [])
        + (["incomplete_factor_target_dates"] if missing else []),
        "hypothesis": "H0: mean session rank IC <= 0",
        "raw_p_value_one_sided": raw_p,
        "multiple_testing": {
            "method": "benjamini-hochberg-fdr",
            "family": "same-contract-and-horizon-factor-diagnostics",
            "dependence_assumption": "independence-or-positive-regression-dependence; not-verified",
            "alpha": 0.05,
            "status": "pending_family_adjustment" if raw_p is not None else "insufficient_data",
            "adjusted_p_value": None,
            "rejected": None,
            "minimum_independent_session_count": minimum,
        },
    }
