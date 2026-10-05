"""Date-balanced prediction diagnostics with no model-selection authority."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import re
from statistics import mean
from typing import Any

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.market_scan_probability_metrics import (
    auc, date_block_bootstrap_ci, finite_number, log_loss, require_probability,
)


PREDICTION_DIAGNOSTICS_SCHEMA = "date-balanced-prediction-diagnostics-v1"
_SELECTION_THRESHOLDS = (0.6, 0.7)
_SCORE_NAMES = ("brier_score", "log_loss")


@dataclass(frozen=True)
class DiagnosticPrediction:
    sample_id: str
    session_date: str
    outcome: int
    probability: float


@dataclass(frozen=True)
class FrozenProbabilityBenchmark:
    probabilities: Mapping[str, float]
    definition: str
    provenance_digest: str
    fitted_through: str | None = None


def build_prediction_diagnostics(
    predictions: Sequence[DiagnosticPrediction], *, planned_test_dates: Sequence[str],
    benchmarks: Mapping[str, FrozenProbabilityBenchmark], target_offset_sessions: int,
    bootstrap_samples: int = 1_000,
) -> dict[str, Any]:
    """Compare every supplied frozen benchmark without choosing a winner."""
    dates = _validate_calendar(planned_test_dates, target_offset_sessions, bootstrap_samples)
    rows = _validate_predictions(predictions, dates)
    references = _validate_benchmarks(benchmarks, rows, dates)
    if len(dates) * bootstrap_samples * len(references) * len(_SCORE_NAMES) > 8_000_000:
        raise ValueError("diagnostic planned-calendar bootstrap work exceeds its budget")
    grouped = _group_by_date(rows)
    missing = [day for day in dates if day not in grouped]
    interval_reason = _interval_limitation(dates, missing, target_offset_sessions)
    return {
        "schema_version": PREDICTION_DIAGNOSTICS_SCHEMA,
        "status": "evaluated" if rows else "no_scored_observations",
        "planned_test_dates": list(dates), "missing_test_dates": missing,
        "planned_session_count": len(dates), "scored_session_count": len(grouped),
        "observation_count": len(rows), "weighting": "one_equal_weight_per_scored_signal_date",
        "pooled_auc": _auc(rows), "cross_sectional_auc": _cross_sectional_auc(grouped, dates),
        "date_balanced_model_scores": _date_balanced_scores(rows),
        "benchmarks": _benchmark_reports(rows, references, dates, target_offset_sessions, bootstrap_samples, interval_reason),
        "paired_interval_limitation": interval_reason,
        "fixed_selection_subsets": [_selection_report(rows, references, threshold, dates) for threshold in _SELECTION_THRESHOLDS],
        "input_digest": sha256_hex(canonical_json_bytes([
            [row.sample_id, row.session_date, row.outcome, row.probability] for row in rows
        ])),
        "calendar_digest": sha256_hex(canonical_json_bytes(list(dates))),
        "candidate_selected": False, "promotion_eligible": False,
        "limitations": ["descriptive_evaluation_not_predictive_accuracy_guarantee",
                        "benchmark_provenance_digest_is_a_binding_not_proof_of_prior_freeze",
                        "threshold_subsets_do_not_establish_post_selection_calibration",
                        "moving_block_intervals_do_not_prove_independent_dates_or_regime_stability"],
    }


def _validate_calendar(values: Sequence[str], offset: int, samples: int) -> tuple[str, ...]:
    dates = tuple(values)
    if not dates or len(dates) > 10_000 or dates != tuple(sorted(set(dates))):
        raise ValueError("diagnostics require a nonempty ordered unique planned calendar")
    if any(date.fromisoformat(day).isoformat() != day for day in dates):
        raise ValueError("diagnostic dates must be canonical ISO dates")
    if type(offset) is not int or not 1 <= offset <= 251 or type(samples) is not int or not 1 <= samples <= 2_000:
        raise ValueError("diagnostic offset or bootstrap budget is invalid")
    return dates


def _validate_predictions(values: Sequence[DiagnosticPrediction], dates: tuple[str, ...]) -> tuple[DiagnosticPrediction, ...]:
    if len(values) > 100_000:
        raise ValueError("diagnostics exceed the 100000 observation budget")
    identifiers: set[str] = set()
    calendar = set(dates)
    for row in values:
        if not row.sample_id.strip() or row.sample_id in identifiers or row.session_date not in calendar:
            raise ValueError("prediction identity or planned date is invalid")
        identifiers.add(row.sample_id)
        if type(row.outcome) is not int or row.outcome not in (0, 1):
            raise ValueError("diagnostic outcomes must be exact 0/1 labels")
        require_probability(finite_number(row.probability, "prediction probability"), "prediction probability")
    return tuple(sorted(values, key=lambda row: (row.session_date, row.sample_id)))


def _validate_benchmarks(
    values: Mapping[str, FrozenProbabilityBenchmark], rows: Sequence[DiagnosticPrediction], dates: tuple[str, ...],
) -> dict[str, FrozenProbabilityBenchmark]:
    if not 1 <= len(values) <= 8:
        raise ValueError("diagnostics require one to eight frozen benchmarks")
    identifiers = {row.sample_id for row in rows}
    references = {}
    for name, benchmark in sorted(values.items()):
        if not name.strip() or not benchmark.definition.strip() or re.fullmatch(r"[0-9a-f]{64}", benchmark.provenance_digest) is None:
            raise ValueError("benchmark definition or provenance binding is missing")
        if benchmark.fitted_through is not None and (
            date.fromisoformat(benchmark.fitted_through).isoformat() != benchmark.fitted_through or benchmark.fitted_through >= dates[0]
        ):
            raise ValueError("benchmark fitting must end before the planned test calendar")
        if set(benchmark.probabilities) != identifiers:
            raise ValueError("benchmark probabilities must match the exact scored sample IDs")
        probabilities = {key: finite_number(value, "benchmark probability") for key, value in benchmark.probabilities.items()}
        for probability in probabilities.values():
            require_probability(probability, "benchmark probability")
        references[name] = FrozenProbabilityBenchmark(probabilities, benchmark.definition, benchmark.provenance_digest, benchmark.fitted_through)
    return references


def _group_by_date(rows: Sequence[DiagnosticPrediction]) -> dict[str, list[DiagnosticPrediction]]:
    grouped: dict[str, list[DiagnosticPrediction]] = defaultdict(list)
    for row in rows:
        grouped[row.session_date].append(row)
    return grouped


def _auc(rows: Sequence[DiagnosticPrediction]) -> float | None:
    return auc([(row.probability, row.outcome, row.session_date) for row in rows]) if rows else None


def _cross_sectional_auc(grouped: Mapping[str, list[DiagnosticPrediction]], dates: tuple[str, ...]) -> dict[str, Any]:
    daily: list[dict[str, Any]] = [{"session_date": day, "observation_count": len(grouped.get(day, [])), "auc": _auc(grouped.get(day, []))} for day in dates]
    valid = [row["auc"] for row in daily if row["auc"] is not None]
    return {
        "date_equal_weight_auc": mean(valid) if valid else None,
        "eligible_session_count": len(valid),
        "single_class_session_count": sum(row["observation_count"] > 0 and row["auc"] is None for row in daily),
        "missing_session_count": sum(row["observation_count"] == 0 for row in daily),
        "definition": "same_signal_date_positive_negative_pairs_only_equal_weight_over_two_class_dates",
        "daily": daily,
    }


def _scores(rows: Sequence[DiagnosticPrediction], probabilities: Mapping[str, float] | None = None) -> dict[str, float]:
    values = [(probabilities[row.sample_id] if probabilities is not None else row.probability, row.outcome, row.session_date) for row in rows]
    return {"brier_score": mean((probability - outcome) ** 2 for probability, outcome, _day in values), "log_loss": log_loss(values)}


def _date_balanced_scores(
    rows: Sequence[DiagnosticPrediction], probabilities: Mapping[str, float] | None = None,
) -> dict[str, float] | None:
    daily = [_scores(values, probabilities) for values in _group_by_date(rows).values()]
    return {name: mean(day[name] for day in daily) for name in _SCORE_NAMES} if daily else None


def _paired_daily_scores(rows: Sequence[DiagnosticPrediction], benchmark: FrozenProbabilityBenchmark) -> dict[str, list[tuple[str, float]]]:
    paired: dict[str, list[tuple[str, float]]] = {name: [] for name in _SCORE_NAMES}
    for day, values in sorted(_group_by_date(rows).items()):
        model, reference = _scores(values), _scores(values, benchmark.probabilities)
        for name in _SCORE_NAMES:
            paired[name].append((day, reference[name] - model[name]))
    return paired


def _interval_limitation(dates: tuple[str, ...], missing: Sequence[str], offset: int) -> str | None:
    if missing:
        return "missing_planned_test_dates_not_compressed_into_adjacent_blocks"
    if len(dates) < 2 * offset:
        return "fewer_than_two_target_offset_blocks_no_interval"
    return None


def _benchmark_reports(
    rows: Sequence[DiagnosticPrediction], benchmarks: Mapping[str, FrozenProbabilityBenchmark], dates: tuple[str, ...],
    offset: int, samples: int, interval_reason: str | None,
) -> dict[str, Any]:
    reports = {}
    for name, benchmark in benchmarks.items():
        paired = _paired_daily_scores(rows, benchmark)
        intervals = _paired_intervals(paired, name, benchmark, dates, offset, samples) if interval_reason is None else None
        reports[name] = {
            "definition": benchmark.definition, "provenance_digest": benchmark.provenance_digest,
            "fitted_through": benchmark.fitted_through,
            "probabilities_digest": sha256_hex(canonical_json_bytes(dict(sorted(benchmark.probabilities.items())))),
            "date_balanced_scores": _date_balanced_scores(rows, benchmark.probabilities),
            "paired_improvement": {metric: mean(value for _day, value in values) if values else None for metric, values in paired.items()},
            "paired_improvement_ci95": intervals,
            "improvement_definition": "benchmark_loss_minus_candidate_loss_same_observations_then_equal_date_weight",
        }
    return reports


def _paired_intervals(
    paired: Mapping[str, list[tuple[str, float]]], name: str, benchmark: FrozenProbabilityBenchmark,
    dates: tuple[str, ...], offset: int, samples: int,
) -> dict[str, Any]:
    seed = f"{PREDICTION_DIAGNOSTICS_SCHEMA}:{name}:{benchmark.provenance_digest}:{dates}:d{offset}"
    return {
        "method": "circular_moving_planned_date_blocks", "block_length_sessions": offset, "samples": samples,
        **{metric: date_block_bootstrap_ci(values, f"{seed}:{metric}", samples, block_length_sessions=offset) for metric, values in paired.items()},
    }


def _selection_report(
    rows: Sequence[DiagnosticPrediction], benchmarks: Mapping[str, FrozenProbabilityBenchmark], threshold: float, dates: tuple[str, ...],
) -> dict[str, Any]:
    selected = [row for row in rows if row.probability >= threshold]
    grouped = _group_by_date(selected)
    return {
        "threshold": threshold, "operator": "ge", "selection_rule": "fixed_before_evaluation_not_optimized",
        "selected_observation_count": len(selected), "all_scored_observation_count": len(rows),
        "selected_observation_share": len(selected) / len(rows) if rows else None,
        "selected_session_count": len(grouped), "planned_session_count": len(dates),
        "observed_positive_rate": mean(row.outcome for row in selected) if selected else None,
        "mean_probability": mean(row.probability for row in selected) if selected else None,
        "date_balanced_positive_rate": mean(mean(row.outcome for row in values) for values in grouped.values()) if grouped else None,
        "date_balanced_model_scores": _date_balanced_scores(selected),
        "same_subset_benchmarks": {name: _selected_benchmark(selected, benchmark) for name, benchmark in benchmarks.items()},
        "post_selection_calibration_established": False, "promotion_eligible": False,
    }


def _selected_benchmark(rows: Sequence[DiagnosticPrediction], benchmark: FrozenProbabilityBenchmark) -> dict[str, Any]:
    paired = _paired_daily_scores(rows, benchmark)
    return {
        "selected_observation_count": len(rows), "date_balanced_scores": _date_balanced_scores(rows, benchmark.probabilities),
        "paired_improvement": {metric: mean(value for _day, value in values) if values else None for metric, values in paired.items()},
    }
