"""Fixed final-period replay for personal D+1/D+2/D+5 direction models.

This is a fresh, historical train/calibration/test replay, not a claim that the
data were unseen by earlier models or research. It never publishes an estimator,
selects a horizon, or produces authority for production filtering or ranking.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import math
import re
from statistics import mean
from typing import Any, cast
from zoneinfo import ZoneInfo

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market import Kline
from app.services.experimental_direction_model import direction_samples
from app.services.experimental_probability_model import (
    MODEL_FEATURE_NAMES, ExperimentalCalibrator, ExperimentalLogit,
    ExperimentalProbabilityUnavailable,
)
from app.services.market_scan_probability import (
    ProbabilityConfig, ProbabilityModelConvergenceError, ProbabilitySample,
    fit_probability_logistic_model, fit_probability_platt_calibrator,
    probability_model_probability, probability_platt_probability,
)
from app.services.market_scan_probability_metrics import (
    date_block_bootstrap_ci, evaluate_probability_predictions,
)


DIRECTION_VALIDATION_SCHEMA = "personal-experimental-direction-validation-v1"
DIRECTION_VALIDATION_MAX_BYTES = 8 * 1024 * 1024
_OFFSETS = (1, 2, 5)
_MARKETS = ("SH", "SZ", "BJ")
_WARMUP_SESSIONS = 60
_TRAIN_MINIMUM = 120
_CALIBRATION_SESSIONS = 40
_TEST_SESSIONS = 60
_BOOTSTRAP_SAMPLES = 1_000
_LIMITATIONS = (
    "historical_isolated_replay_not_prospective",
    "history_may_have_been_seen_by_prior_models_or_research",
    "repeated_holdout_inspection_cannot_be_reclassified_as_independent_evidence",
    "all_horizons_reported_without_test_based_model_or_horizon_selection",
    "evaluates_fresh_preholdout_refits_not_existing_online_estimators",
    "reconstructed_qfq_not_original_provider_vintage_or_forward_pit",
    "close_direction_not_costed_profit_or_execution_probability",
    "selected_history_universe_not_full_market_generalization_proof",
    "missing_targets_and_out_of_distribution_rows_are_excluded_not_imputed",
    "date_counts_are_not_iid_sample_sizes_with_overlapping_forward_labels",
    "descriptive_metrics_and_bootstrap_intervals_do_not_grant_authority",
)


@dataclass(frozen=True)
class _Split:
    train: tuple[str, ...]
    training_gap: tuple[str, ...]
    calibration: tuple[str, ...]
    test_gap: tuple[str, ...]
    test: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {name: list(getattr(self, name)) for name in (
            "train", "training_gap", "calibration", "test_gap", "test",
        )}


@dataclass(frozen=True)
class _Fit:
    model: ExperimentalLogit
    calibrator: ExperimentalCalibrator
    base_rate: float
    receipt: dict[str, Any]


@dataclass(frozen=True)
class _Prediction:
    sample: ProbabilitySample
    probability: float


class _FitUnavailable(ValueError):
    pass


def validate_experimental_direction_history(
    series: Mapping[str, Sequence[Kline]],
    sessions: Sequence[str],
    provenance: Mapping[str, Any],
    *,
    generated_at: str,
) -> dict[str, Any]:
    """Evaluate already verified history without any filesystem or model writes."""
    source = _validate_input(series, sessions, provenance, generated_at)
    horizons = {str(offset): _evaluate_horizon(series, tuple(sessions), offset) for offset in _OFFSETS}
    evaluated = sum(result["status"] == "evaluated" for result in horizons.values())
    payload = {
        "schema_version": DIRECTION_VALIDATION_SCHEMA,
        "status": "completed" if evaluated == len(_OFFSETS) else "partial" if evaluated else "unavailable",
        "generated_at": generated_at,
        "experimental": True, "formal_filter_qualified": False,
        "production_ranking_effect": "none", "online_models_modified": False,
        "evidence_kind": "historical_isolated_replay_not_prospective",
        "existing_online_estimators_validated": False,
        "source": source, "protocol": _protocol(), "horizons": horizons,
        "limitations": list(_LIMITATIONS),
    }
    if len(canonical_json_bytes(payload)) > DIRECTION_VALIDATION_MAX_BYTES:
        raise ExperimentalProbabilityUnavailable("方向验证报告超过大小限制")
    return payload


def _protocol() -> dict[str, Any]:
    return {
        "horizons": list(_OFFSETS), "target": "close_return_positive",
        "reference": "signal_day_qfq_close", "target_rule": "fixed_calendar_D_plus_h_close_gt_D_close",
        "warmup_sessions": _WARMUP_SESSIONS, "minimum_train_sessions": _TRAIN_MINIMUM,
        "calibration_sessions": _CALIBRATION_SESSIONS, "test_signal_sessions": _TEST_SESSIONS,
        "test_period": "last_60_common_signal_sessions_with_D_plus_5_target_in_source",
        "split_calendar_fixed_before_label_eligibility": True,
        "purge_between_each_partition": "horizon_sessions_and_prior_label_strictly_before_next_signal",
        "test_used_for_fitting": False, "test_used_for_selection": False,
        "hyperparameter_search": "none_fixed_existing_logit_l2_and_platt_recipe",
        "probability_rejection": "same_as_runtime_any_feature_more_than_8_training_standard_deviations",
        "classification_threshold": 0.5,
        "baselines": ["calibration_label_rate_only", "constant_half"],
        "primary_weighting": "equal_weight_for_each_scored_signal_date",
        "bootstrap_samples": _BOOTSTRAP_SAMPLES,
        "bootstrap_block_length": "horizon_sessions",
        "bootstrap_requires_complete_scored_test_calendar": True,
        "previous_exposure_excluded": False,
        "repeat_policy": "same_fixed_period_is_replay_not_new_independent_evidence",
    }


def _validate_input(
    series: Mapping[str, Sequence[Kline]], sessions: Sequence[str],
    provenance: Mapping[str, Any], generated_at: str,
) -> dict[str, Any]:
    if not series or not sessions or len(series) * len(sessions) > 100_000:
        raise ExperimentalProbabilityUnavailable("方向验证历史为空或超过100000个股票交易日")
    if list(sessions) != sorted(set(sessions)):
        raise ExperimentalProbabilityUnavailable("方向验证交易日历必须严格递增且不重复")
    if any(date.fromisoformat(day).isoformat() != day for day in sessions):
        raise ExperimentalProbabilityUnavailable("方向验证交易日必须使用规范ISO日期")
    observed = datetime.fromisoformat(generated_at)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ExperimentalProbabilityUnavailable("方向验证生成时间必须有时区")
    if sessions[-1] > observed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat():
        raise ExperimentalProbabilityUnavailable("方向验证历史不能包含未来交易日")
    bound_provenance = _bound_provenance(provenance)
    source_digest = _series_digest(series, set(sessions))
    return {
        "provenance": bound_provenance, "provenance_digest": sha256_hex(canonical_json_bytes(bound_provenance)),
        "series_digest": source_digest, "calendar_digest": sha256_hex(canonical_json_bytes(list(sessions))),
        "first_session": sessions[0], "last_session": sessions[-1], "session_count": len(sessions),
        "symbol_count": len(series), "market_symbol_counts": dict(Counter(symbol[-2:] for symbol in series)),
    }


def _bound_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(
        not isinstance(value.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None
        for key in ("manifest_digest", "source_sha256")
    ):
        raise ExperimentalProbabilityUnavailable("方向验证缺少已校验历史的来源绑定")
    return deepcopy(dict(value))


def _series_digest(series: Mapping[str, Sequence[Kline]], sessions: set[str]) -> str:
    digest = hashlib.sha256()
    for symbol, rows in sorted(series.items()):
        if re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", symbol) is None:
            raise ExperimentalProbabilityUnavailable("方向验证股票代码无效")
        if len(rows) > len(sessions) or len({row.date for row in rows}) != len(rows):
            raise ExperimentalProbabilityUnavailable("方向验证股票日期重复或超出日历")
        digest.update(canonical_json_bytes(symbol))
        for row in sorted(rows, key=lambda item: item.date):
            if row.date not in sessions or row.adjustment_mode != "qfq":
                raise ExperimentalProbabilityUnavailable("方向验证仅接受日历内的已校验qfq历史")
            digest.update(canonical_json_bytes(row.model_dump(mode="json")))
    return digest.hexdigest()


def _split_dates(sessions: tuple[str, ...], offset: int) -> _Split | None:
    test_end = len(sessions) - max(_OFFSETS)
    test_start = test_end - _TEST_SESSIONS
    calibration_end = test_start - offset
    calibration_start = calibration_end - _CALIBRATION_SESSIONS
    train_end = calibration_start - offset
    if train_end - _WARMUP_SESSIONS < _TRAIN_MINIMUM:
        return None
    return _Split(
        train=sessions[_WARMUP_SESSIONS:train_end],
        training_gap=sessions[train_end:calibration_start],
        calibration=sessions[calibration_start:calibration_end],
        test_gap=sessions[calibration_end:test_start], test=sessions[test_start:test_end],
    )


def _evaluate_horizon(series: Mapping[str, Sequence[Kline]], sessions: tuple[str, ...], offset: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "horizon": offset, "target": "close_return_positive", "status": "insufficient_data",
        "minimum_source_sessions": _WARMUP_SESSIONS + _TRAIN_MINIMUM + _CALIBRATION_SESSIONS + _TEST_SESSIONS + max(_OFFSETS) + 2 * offset,
        "available_source_sessions": len(sessions),
    }
    split = _split_dates(sessions, offset)
    if split is None:
        return {**result, "reason": "insufficient_fixed_calendar_sessions", "split": None, "fit": None, "overall": None, "by_market": {}}
    samples, targets, _symbols, sample_receipt = direction_samples(series, sessions, offset=offset)
    train, calibration, test = _partition_samples(samples, targets, split)
    result.update(split=split.payload(), all_history_sample_receipt=sample_receipt)
    predictions: list[_Prediction] = []
    rejected: Counter[str] = Counter()
    fitted = None
    try:
        fitted = _fit_fixed_partitions(train, calibration, targets, split, offset)
        predictions, rejected = _predict_test(fitted, test)
        result.update(status="evaluated" if predictions else "no_evaluable_test_rows", fit=fitted.receipt)
    except (_FitUnavailable, ProbabilityModelConvergenceError) as exc:
        result.update(reason=str(exc), fit=None)
    baseline = fitted.base_rate if fitted is not None else None
    result["overall"] = _group_report(test, predictions, rejected, split, tuple(sorted(series)), baseline, offset)
    result["by_market"] = {
        market: _group_report(test, predictions, rejected, split, tuple(sorted(symbol for symbol in series if symbol.endswith(f".{market}"))), baseline, offset)
        for market in _MARKETS
    }
    result["test_prediction_digest"] = sha256_hex(canonical_json_bytes([
        [row.sample.sample_id, targets[row.sample.sample_id], row.sample.target, row.probability] for row in predictions
    ]))
    result["test_probability_digest"] = sha256_hex(canonical_json_bytes([[row.sample.sample_id, row.probability] for row in predictions]))
    return result


def _partition_samples(
    samples: Sequence[ProbabilitySample], targets: Mapping[str, str], split: _Split,
) -> tuple[list[ProbabilitySample], list[ProbabilitySample], list[ProbabilitySample]]:
    train_dates, calibration_dates, test_dates = set(split.train), set(split.calibration), set(split.test)
    train = [sample for sample in samples if sample.session_date in train_dates]
    calibration = [sample for sample in samples if sample.session_date in calibration_dates]
    test = [sample for sample in samples if sample.session_date in test_dates]
    if any(targets[row.sample_id] >= split.calibration[0] for row in train):
        raise ExperimentalProbabilityUnavailable("方向训练标签越过校准隔离边界")
    if any(targets[row.sample_id] >= split.test[0] for row in calibration):
        raise ExperimentalProbabilityUnavailable("方向校准标签越过最终测试隔离边界")
    return train, calibration, test


def _fit_fixed_partitions(
    train: list[ProbabilitySample], calibration: list[ProbabilitySample],
    targets: Mapping[str, str], split: _Split, offset: int,
) -> _Fit:
    train_dates = {sample.session_date for sample in train}
    calibration_dates = {sample.session_date for sample in calibration}
    if len(train_dates) < _TRAIN_MINIMUM or calibration_dates != set(split.calibration):
        raise _FitUnavailable("insufficient_eligible_train_or_fixed_calibration_sessions")
    if any({row.target for row in group} != {0, 1} for group in (train, calibration)):
        raise _FitUnavailable("train_and_calibration_each_require_both_labels")
    config = ProbabilityConfig(horizon=offset, target="net_return_positive")
    model = ExperimentalLogit.model_validate(fit_probability_logistic_model(train, MODEL_FEATURE_NAMES, config))
    raw = [probability_model_probability(model.model_dump(), row.features) for row in calibration]
    labels = [int(cast(int, row.target)) for row in calibration]
    calibrator = ExperimentalCalibrator.model_validate(fit_probability_platt_calibrator(raw, labels, config))
    base_rate = mean(labels)
    receipt = {
        "train_session_count": len(train_dates), "train_record_count": len(train),
        "calibration_session_count": len(calibration_dates), "calibration_record_count": len(calibration),
        "train_signal_end": max(train_dates), "train_label_end": max(targets[row.sample_id] for row in train),
        "calibration_start": min(calibration_dates), "calibration_signal_end": max(calibration_dates),
        "calibration_label_end": max(targets[row.sample_id] for row in calibration),
        "calibration_base_rate": base_rate,
        "model_digest": sha256_hex(canonical_json_bytes(model.model_dump())),
        "calibrator_digest": sha256_hex(canonical_json_bytes(calibrator.model_dump())),
        "train_sample_digest": _sample_digest(train, targets), "calibration_sample_digest": _sample_digest(calibration, targets),
        "parameters_published": False,
    }
    return _Fit(model, calibrator, base_rate, receipt)


def _sample_digest(samples: Sequence[ProbabilitySample], targets: Mapping[str, str]) -> str:
    return sha256_hex(canonical_json_bytes([
        [row.sample_id, row.session_date, targets[row.sample_id], dict(row.features), row.target] for row in samples
    ]))


def _predict_test(fitted: _Fit, samples: Sequence[ProbabilitySample]) -> tuple[list[_Prediction], Counter[str]]:
    predictions: list[_Prediction] = []
    rejected: Counter[str] = Counter()
    for sample in samples:
        values = [sample.features[name] for name in MODEL_FEATURE_NAMES]
        if any(abs((value - center) / scale) > 8 for value, center, scale in zip(values, fitted.model.means, fitted.model.scales, strict=True)):
            rejected[_symbol(sample)] += 1
            continue
        raw = probability_model_probability(fitted.model.model_dump(), sample.features)
        probability = probability_platt_probability(fitted.calibrator.model_dump(), raw)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ExperimentalProbabilityUnavailable("方向验证预测不在有效概率范围")
        predictions.append(_Prediction(sample, probability))
    return predictions, rejected


def _symbol(sample: ProbabilitySample) -> str:
    return sample.sample_id.rsplit(":", 1)[-1]


def _group_report(
    samples: Sequence[ProbabilitySample], predictions: Sequence[_Prediction], rejected: Counter[str],
    split: _Split, symbols: tuple[str, ...], base_rate: float | None, offset: int,
) -> dict[str, Any]:
    selected = set(symbols)
    eligible = [sample for sample in samples if _symbol(sample) in selected]
    scored = [row for row in predictions if _symbol(row.sample) in selected]
    expected = len(symbols) * len(split.test)
    daily = _daily_reports(eligible, scored, split.test, len(symbols), base_rate)
    metrics = _metrics(scored, base_rate) if scored and base_rate is not None else None
    return {
        "status": "scored" if scored else "no_input_symbols" if not symbols else "no_scored_observations",
        "symbol_count": len(symbols), "planned_signal_session_count": len(split.test),
        "expected_symbol_session_count": expected, "label_eligible_count": len(eligible),
        "predicted_count": len(scored), "label_unavailable_count": expected - len(eligible),
        "out_of_distribution_count": sum(rejected[symbol] for symbol in symbols),
        "label_coverage": len(eligible) / expected if expected else None,
        "prediction_coverage": len(scored) / expected if expected else None,
        "unique_scored_session_count": len({row.sample.session_date for row in scored}),
        "pooled_metrics": metrics, "date_balanced_metrics": _date_balanced_metrics(daily, offset),
        "daily": daily,
    }


def _daily_reports(
    samples: Sequence[ProbabilitySample], predictions: Sequence[_Prediction], test_dates: tuple[str, ...],
    symbol_count: int, base_rate: float | None,
) -> list[dict[str, Any]]:
    eligible = Counter(row.session_date for row in samples)
    by_date: dict[str, list[_Prediction]] = defaultdict(list)
    for row in predictions:
        by_date[row.sample.session_date].append(row)
    return [{
        "session_date": day, "expected_count": symbol_count,
        "label_eligible_count": eligible[day], "predicted_count": len(by_date[day]),
        "prediction_coverage": len(by_date[day]) / symbol_count if symbol_count else None,
        "metrics": _metrics(by_date[day], base_rate) if by_date[day] and base_rate is not None else None,
    } for day in test_dates]


def _metrics(predictions: Sequence[_Prediction], base_rate: float) -> dict[str, Any]:
    probabilities = [row.probability for row in predictions]
    labels = [int(cast(int, row.sample.target)) for row in predictions]
    dates = [row.sample.session_date for row in predictions]
    model = evaluate_probability_predictions(probabilities, labels, dates, base_rate=base_rate)
    calibration_baseline = evaluate_probability_predictions([base_rate] * len(labels), labels, dates, base_rate=base_rate)
    half_baseline = evaluate_probability_predictions([0.5] * len(labels), labels, dates, base_rate=0.5)
    model["classification_accuracy_at_half"] = mean(int((probability >= .5) == bool(label)) for probability, label in zip(probabilities, labels, strict=True))
    calibration_baseline["classification_accuracy_at_half"] = mean(int((base_rate >= .5) == bool(label)) for label in labels)
    half_baseline["classification_accuracy_at_half"] = mean(labels)
    return {"model": model, "calibration_rate_baseline": calibration_baseline, "constant_half_baseline": half_baseline}


def _date_balanced_metrics(daily: Sequence[Mapping[str, Any]], offset: int) -> dict[str, Any] | None:
    scored = [day for day in daily if day["metrics"] is not None]
    if not scored:
        return None
    result: dict[str, Any] = {
        "scored_session_count": len(scored), "planned_session_count": len(daily),
        "coverage_complete_for_time_bootstrap": len(scored) == len(daily),
        "weighting": "one_equal_weight_per_scored_signal_session",
    }
    for name in ("model", "calibration_rate_baseline", "constant_half_baseline"):
        result[name] = {key: mean(day["metrics"][name][key] for day in scored) for key in ("brier_score", "log_loss", "classification_accuracy_at_half")}
    reference = result["calibration_rate_baseline"]["brier_score"]
    result["brier_skill_vs_calibration_rate"] = 1 - result["model"]["brier_score"] / reference if reference > 0 else None
    result["improvement_ci95"] = _improvement_intervals(scored, offset) if len(scored) == len(daily) else None
    result["interval_limitation"] = None if len(scored) == len(daily) else "missing_test_dates_not_compressed_into_adjacent_bootstrap_blocks"
    return result


def _improvement_intervals(daily: Sequence[Mapping[str, Any]], offset: int) -> dict[str, Any]:
    return {
        "method": "circular_moving_calendar_date_blocks", "block_length_sessions": offset,
        "samples": _BOOTSTRAP_SAMPLES,
        **{metric: date_block_bootstrap_ci(
            [(day["session_date"], day["metrics"]["calibration_rate_baseline"][metric] - day["metrics"]["model"][metric]) for day in daily],
            f"{DIRECTION_VALIDATION_SCHEMA}:d{offset}:{metric}", _BOOTSTRAP_SAMPLES, block_length_sessions=offset,
        ) for metric in ("brier_score", "log_loss")},
    }
