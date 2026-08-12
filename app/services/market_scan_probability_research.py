"""Read-only orchestration for full-market Shadow probability research."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import math
from statistics import fmean
from typing import Literal, cast

from app.services.market_scan_probability import (
    PROBABILITY_CALIBRATOR_VERSION,
    PROBABILITY_COST_MODEL_VERSION,
    PROBABILITY_FEATURE_VERSION,
    PROBABILITY_MODEL_VERSION,
    ProbabilityConfig,
    ProbabilitySample,
    evaluate_probability_predictions,
    fit_shadow_probability,
    predict_shadow_probability,
    stable_probability_hash,
    verify_shadow_probability_evidence,
)
from app.services.market_scan_probability_artifact import PROBABILITY_RESULT_CONTRACT_VERSION
from app.services.market_scan_probability_labels import (
    PROBABILITY_EXECUTION_MODEL,
    PROBABILITY_LABEL_VERSION,
    ProbabilityLabelOutcome,
    probability_label_contract,
)


PROBABILITY_RESEARCH_SCHEMA_VERSION = "market-scan-probability-research-v3"
PROBABILITY_BENCHMARK_VERSION = "same-run-equal-weight-executable-market-v1"
PROBABILITY_PRIMARY_TARGET = "net_excess_positive"
PROBABILITY_ABSOLUTE_TARGET = "absolute_net_positive"
PROBABILITY_TARGETS = (PROBABILITY_PRIMARY_TARGET, PROBABILITY_ABSOLUTE_TARGET)
PROBABILITY_MAJOR_STRATUM_MINIMUM_SHARE = 0.10
PROBABILITY_STABILITY_MINIMUM_SLICE_SESSIONS = 20
PROBABILITY_MAXIMUM_REVIEW_ECE = 0.08
ProbabilityPublicTarget = Literal["net_excess_positive", "absolute_net_positive"]
_ArtifactCohortEvidence = dict[
    tuple[str, int, str],
    tuple[Mapping[str, object], Mapping[str, object]],
]


@dataclass(frozen=True)
class ProbabilityResearchRow:
    run_id: int
    symbol: str
    session_date: str
    features: Mapping[str, float]
    labels: Mapping[int, ProbabilityLabelOutcome]
    mature_horizons: frozenset[int]
    dimensions: Mapping[str, str]
    source_evidence_digest: str | None = None
    mode: str = "unspecified"
    scope: str = "unspecified"
    rule_version: str = "unspecified"


def probability_feature_vector(
    values: Mapping[str, float],
    *,
    market: str,
    board: str,
    liquidity: str,
    regime: str,
    industry: str = "unknown",
    segment: str = "regular",
    market_strength: float = 50.0,
    board_relative_strength: float = 0.0,
    industry_relative_strength: float = 0.0,
) -> dict[str, float]:
    """Build the registered finite feature vector from scan-time-only values."""
    features = {
        "production_score": _feature(values, "raw_score", 50.0),
        "trend_score": _feature(values, "trend_score", 50.0),
        "leader_base": _feature(values, "leader_base", 50.0),
        "leader_trend_delta": _feature(values, "leader_trend_delta", 0.0),
        "leader_unclamped": _feature(values, "leader_unclamped", 50.0),
        "leader_score": _feature(values, "leader_score", 50.0),
        "final_base": _feature(values, "final_base", 50.0),
        "quality_penalty": _feature(values, "final_quality_penalty", 0.0),
        "final_rank_discount": _feature(values, "final_rank_discount", 0.0),
        "final_raw": _feature(values, "final_raw", 50.0),
        "final_rounded": _feature(values, "final_rounded", 50.0),
        "final_score": _feature(values, "final_score", 50.0),
        "rank_refinement": _feature(values, "rank_refinement", 0.5),
        "change_pct": _feature(values, "change_pct", 0.0),
        "data_quality_score": _feature(values, "data_quality_score", 0.0),
        "log_amount": math.log1p(max(0.0, _feature(values, "amount", 0.0))),
        "turnover_rate": _feature(values, "turnover_rate", 0.0),
        "volume_ratio": _feature(values, "volume_ratio", 1.0),
        "alpha_1d": _feature(values, "alpha_1d", 0.0),
        "alpha_5d": _feature(values, "alpha_5d", 0.0),
        "alpha_20d": _feature(values, "alpha_20d", 0.0),
        "confidence": _feature(values, "confidence", 0.0),
        "risk": _feature(values, "risk", 100.0),
        "tradability": _feature(values, "tradability", 0.0),
        **_registered_raw_features(values),
        **_context_strength_features(
            market_strength, board_relative_strength, industry_relative_strength,
        ),
        **_status_and_limit_features(values, board, segment),
    }
    features.update(_categorical_features(market, board, liquidity, regime, industry))
    return features


def build_probability_research(
    rows: Sequence[ProbabilityResearchRow],
    *,
    generated_at: str,
    bootstrap_samples: int = 1_000,
    label_contract: Mapping[str, object] | None = None,
    include_records: bool = True,
) -> dict[str, object]:
    """Fit isolated contract cohorts and emit replayable per-row records."""
    values = tuple(rows)
    contract = dict(label_contract or probability_label_contract())
    cohort_payloads: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    for cohort_contract, cohort_rows in _isolated_cohort_rows(values):
        horizons, cohort_records = _build_cohort_research(
            cohort_rows,
            generated_at=generated_at,
            bootstrap_samples=bootstrap_samples,
            label_contract=contract,
            include_records=include_records,
        )
        cohort = _cohort_payload(cohort_contract, cohort_rows, horizons)
        records.extend(_attach_cohort_evidence(cohort_records, cohort))
        cohort_payloads.append(cohort)
    horizons = _top_level_horizons(cohort_payloads)
    payload = _research_payload(
        values,
        generated_at,
        horizons,
        records,
        contract,
        cohorts=cohort_payloads,
    )
    payload["research_digest"] = stable_probability_hash(payload)
    return payload


def _build_cohort_research(
    rows: Sequence[ProbabilityResearchRow],
    *,
    generated_at: str,
    bootstrap_samples: int,
    label_contract: Mapping[str, object],
    include_records: bool,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    benchmarks = _market_benchmarks(rows)
    horizons: dict[str, object] = {}
    records: list[dict[str, object]] = []
    for horizon in (1, 5, 20):
        target_evidence: dict[str, object] = {}
        for target in cast(tuple[ProbabilityPublicTarget, ...], PROBABILITY_TARGETS):
            evidence = _fit_target(
                rows,
                benchmarks,
                horizon,
                target,
                generated_at,
                bootstrap_samples,
                label_contract,
            )
            target_evidence[target] = _evidence_summary(
                evidence,
                target,
                rows,
                horizon,
                label_contract,
            )
            if include_records:
                records.extend(_probability_records(rows, benchmarks, horizon, target, evidence))
        horizons[str(horizon)] = target_evidence
    return horizons, records


def _isolated_cohort_rows(
    rows: Sequence[ProbabilityResearchRow],
) -> list[tuple[dict[str, str], tuple[ProbabilityResearchRow, ...]]]:
    grouped: dict[tuple[str, str, str], list[ProbabilityResearchRow]] = defaultdict(list)
    run_contracts: dict[int, tuple[str, str, str]] = {}
    cohort_sessions: dict[tuple[tuple[str, str, str], str], int] = {}
    for row in rows:
        key = _cohort_key(row)
        previous_contract = run_contracts.setdefault(row.run_id, key)
        if previous_contract != key:
            raise ValueError(f"probability run {row.run_id} has conflicting cohort contracts")
        session_key = key, row.session_date
        previous_run = cohort_sessions.setdefault(session_key, row.run_id)
        if previous_run != row.run_id:
            raise ValueError("probability cohort contains multiple runs for one session date")
        grouped[key].append(row)
    return [
        (_cohort_contract(key), tuple(grouped[key]))
        for key in sorted(grouped)
    ]


def _cohort_key(row: ProbabilityResearchRow) -> tuple[str, str, str]:
    return tuple(
        _nonempty_cohort_value(value, name)
        for value, name in (
            (row.mode, "mode"),
            (row.scope, "scope"),
            (row.rule_version, "rule_version"),
        )
    )  # type: ignore[return-value]


def _nonempty_cohort_value(value: str, name: str) -> str:
    normalized = " ".join(str(value).split())
    if not normalized:
        raise ValueError(f"probability cohort {name} is empty")
    return normalized


def _cohort_contract(key: tuple[str, str, str]) -> dict[str, str]:
    return dict(zip(("mode", "scope", "rule_version"), key, strict=True))


def _cohort_payload(
    contract: Mapping[str, str],
    rows: Sequence[ProbabilityResearchRow],
    horizons: Mapping[str, object],
) -> dict[str, object]:
    identity = {
        "cohort_contract": dict(contract),
        "run_ids": sorted({row.run_id for row in rows}),
        "session_dates": sorted({row.session_date for row in rows}),
        "horizon_evidence_digests": _cohort_evidence_digests(horizons),
    }
    return {
        **identity,
        "cohort_digest": stable_probability_hash(identity),
        "status": "calibrated_shadow" if _any_calibrated(horizons) else "insufficient_data",
        "observation_count": len(rows),
        "horizons": dict(horizons),
    }


def _cohort_evidence_digests(horizons: Mapping[str, object]) -> dict[str, object]:
    return {
        f"{horizon}/{target}": evidence.get("evidence_digest")
        for horizon, targets in sorted(horizons.items())
        if isinstance(targets, Mapping)
        for target, evidence in sorted(targets.items())
        if isinstance(evidence, Mapping)
    }


def _attach_cohort_evidence(
    records: Sequence[Mapping[str, object]],
    cohort: Mapping[str, object],
) -> list[dict[str, object]]:
    contract = dict(cast(Mapping[str, object], cohort["cohort_contract"]))
    digest = str(cohort["cohort_digest"])
    return [
        {**dict(record), "cohort_contract": contract, "cohort_digest": digest}
        for record in records
    ]


def _top_level_horizons(cohorts: Sequence[Mapping[str, object]]) -> dict[str, object]:
    if len(cohorts) == 1:
        return dict(cast(Mapping[str, object], cohorts[0]["horizons"]))
    return {
        str(horizon): {
            target: _cohort_aggregate_evidence(cohorts, horizon, target)
            for target in PROBABILITY_TARGETS
        }
        for horizon in (1, 5, 20)
    }


def _cohort_aggregate_evidence(
    cohorts: Sequence[Mapping[str, object]],
    horizon: int,
    target: str,
) -> dict[str, object]:
    summaries = [_cohort_target_summary(cohort, horizon, target) for cohort in cohorts]
    calibrated = [item for item in summaries if item["status"] == "calibrated_shadow"]
    reviewable = [item for item in summaries if item["eligible_for_human_review"] is True]
    return {
        "status": "calibrated_shadow" if calibrated else "insufficient_data",
        "probability": None,
        "cohort_count": len(cohorts),
        "calibrated_cohort_count": len(calibrated),
        "counts": {"cohort_count": len(cohorts)},
        "training_cutoff": None,
        "cohort_summaries": summaries,
        "promotion_gates": {"passed": bool(reviewable), "gates": {}},
        "limitations": ["cohort_isolated_summary_has_no_pooled_probability"],
    }


def _cohort_target_summary(
    cohort: Mapping[str, object],
    horizon: int,
    target: str,
) -> dict[str, object]:
    horizons = cast(Mapping[str, object], cohort["horizons"])
    targets = cast(Mapping[str, object], horizons[str(horizon)])
    evidence = cast(Mapping[str, object], targets[target])
    promotion = evidence.get("promotion_gates")
    return {
        "cohort_contract": cohort["cohort_contract"],
        "cohort_digest": cohort["cohort_digest"],
        "status": evidence.get("status") or "insufficient_data",
        "counts": evidence.get("counts") or {},
        "training_cutoff": evidence.get("training_cutoff"),
        "eligible_for_human_review": (
            isinstance(promotion, Mapping) and promotion.get("passed") is True
        ),
    }


def probability_artifact_payload(research: Mapping[str, object]) -> dict[str, object]:
    """Convert a verified research report into the strict immutable artifact payload."""
    raw_records = research.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("probability research payload is incomplete")
    values = [_artifact_mapping(value) for value in raw_records]
    artifact_set_run_ids = sorted({_artifact_integer(value["run_id"]) for value in values})
    cohort_evidence = _artifact_cohort_evidence(research)
    feature_evidence = _artifact_feature_evidence(values, cohort_evidence)
    grouped: dict[tuple[int, str, int], list[Mapping[str, object]]] = defaultdict(list)
    for value in values:
        key = (_artifact_integer(value["run_id"]), str(value["target"]), _artifact_integer(value["horizon"]))
        grouped[key].append(value)
    studies = [
        _artifact_study(key, grouped_values, cohort_evidence, artifact_set_run_ids)
        for key, grouped_values in sorted(grouped.items())
    ]
    study_by_key = {_artifact_study_key(value): value for value in studies}
    records = [
        _artifact_record(
            value,
            study_by_key[key],
            feature_evidence[(_artifact_integer(value["run_id"]), str(value["symbol"]))],
        )
        for key, grouped_values in grouped.items()
        for value in grouped_values
    ]
    return {
        "record_contract_version": PROBABILITY_RESULT_CONTRACT_VERSION,
        "feature_evidence": [value for _key, value in sorted(feature_evidence.items())],
        "studies": studies,
        "records": records,
    }


def _artifact_cohort_evidence(research: Mapping[str, object]) -> _ArtifactCohortEvidence:
    raw_cohorts = research.get("cohorts")
    if not isinstance(raw_cohorts, list):
        raise ValueError("probability research cohort evidence is missing")
    output: _ArtifactCohortEvidence = {}
    for raw_cohort in raw_cohorts:
        cohort = _artifact_mapping(raw_cohort)
        digest = str(cohort.get("cohort_digest") or "")
        contract = cohort.get("cohort_contract")
        horizons = cohort.get("horizons")
        if len(digest) != 64 or not isinstance(contract, Mapping) or not isinstance(horizons, Mapping):
            raise ValueError("probability research cohort identity is invalid")
        for horizon, targets in horizons.items():
            if not isinstance(targets, Mapping):
                raise ValueError("probability research cohort horizon is invalid")
            for target, evidence in targets.items():
                if not isinstance(evidence, Mapping):
                    raise ValueError("probability research cohort evidence is invalid")
                output[(digest, int(horizon), str(target))] = evidence, contract
    return output


def _record_cohort_evidence(
    record: Mapping[str, object],
    cohort_evidence: _ArtifactCohortEvidence,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    key = (
        str(record.get("cohort_digest") or ""),
        _artifact_integer(record["horizon"]),
        str(record["target"]),
    )
    selected = cohort_evidence.get(key)
    if selected is None or record.get("cohort_contract") != selected[1]:
        raise ValueError("probability record cohort evidence is missing or conflicting")
    return selected


def _artifact_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("probability research record must be an object")
    return value


def _artifact_study_key(value: Mapping[str, object]) -> tuple[int, str, int]:
    return _artifact_integer(value["run_id"]), str(value["target"]), _artifact_integer(value["horizon"])


def _artifact_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("probability research identity must be an integer")
    return value


def _artifact_feature_evidence(
    records: Sequence[Mapping[str, object]],
    cohort_evidence: _ArtifactCohortEvidence,
) -> dict[tuple[int, str], dict[str, object]]:
    output: dict[tuple[int, str], dict[str, object]] = {}
    for value in records:
        vector = value.get("feature_values")
        dimensions = value.get("dimensions")
        if not isinstance(vector, list) or not isinstance(dimensions, Mapping):
            continue
        evidence, _contract = _record_cohort_evidence(value, cohort_evidence)
        names = evidence.get("feature_names")
        if not isinstance(names, list) or len(names) != len(vector):
            raise ValueError("probability canonical feature evidence is incomplete")
        features = {str(name): float(item) for name, item in zip(names, vector, strict=True)}
        if stable_probability_hash(features) != value.get("feature_vector_digest"):
            raise ValueError("probability canonical feature digest mismatch")
        key = _artifact_integer(value["run_id"]), str(value["symbol"])
        payload = {
            "run_id": key[0],
            "symbol": key[1],
            "quote_date": value["quote_date"],
            "features": features,
            "feature_names": sorted(features),
            "feature_vector_digest": value["feature_vector_digest"],
            "dimensions": dict(dimensions),
            "source_evidence_digest": _verified_artifact_digest(value.get("source_evidence_digest")),
        }
        if key in output and output[key] != payload:
            raise ValueError("probability canonical feature evidence conflicts")
        output[key] = payload
    expected = {(_artifact_integer(value["run_id"]), str(value["symbol"])) for value in records}
    if expected - output.keys():
        raise ValueError("probability canonical feature evidence is missing")
    return output


def _artifact_study(
    key: tuple[int, str, int],
    records: Sequence[Mapping[str, object]],
    cohort_evidence: _ArtifactCohortEvidence,
    artifact_set_run_ids: Sequence[int],
) -> dict[str, object]:
    run_id, target, horizon = key
    if not records:
        raise ValueError("probability artifact study has no records")
    evidence, cohort_contract = _record_cohort_evidence(records[0], cohort_evidence)
    if any(_record_cohort_evidence(value, cohort_evidence)[0] is not evidence for value in records[1:]):
        raise ValueError("probability artifact study mixes cohort evidence")
    cohort_digest = str(records[0]["cohort_digest"])
    calibrated = any(item.get("status") == "calibrated_shadow" for item in records)
    status = "calibrated_shadow" if calibrated else "insufficient_data"
    limitations = [str(value) for value in cast(Sequence[object], evidence.get("limitations") or ())]
    if not calibrated:
        limitations.append("run_has_no_out_of_sample_calibrated_prediction")
    return {
        "run_id": run_id,
        "target": target,
        "horizon": horizon,
        "status": status,
        "versions": _artifact_versions(evidence),
        "digests": _artifact_digests(evidence),
        "limitations": list(dict.fromkeys(limitations)),
        "metadata": {
            **dict(evidence),
            "cohort_contract": dict(cohort_contract),
            "cohort_digest": cohort_digest,
            "artifact_set_run_ids": list(artifact_set_run_ids),
            "run_record_count": len(records),
            "run_calibrated_record_count": sum(item.get("status") == "calibrated_shadow" for item in records),
        },
    }


def _artifact_record(
    value: Mapping[str, object],
    study: Mapping[str, object],
    feature_evidence: Mapping[str, object],
) -> dict[str, object]:
    interval = value.get("confidence_interval")
    bounds = None
    if isinstance(interval, Mapping):
        bounds = [interval.get("lower"), interval.get("upper")]
    identity = {"run_id", "symbol", "target", "horizon", "status", "probability", "confidence_interval"}
    details = {key: item for key, item in value.items() if key not in identity}
    for compact_name in ("feature_values", "feature_evidence_role", "feature_evidence_reference", "dimensions"):
        details.pop(compact_name, None)
    details.update(_self_contained_record_evidence(value, study, feature_evidence))
    return {
        "run_id": value["run_id"],
        "symbol": value["symbol"],
        "target": value["target"],
        "horizon": value["horizon"],
        "status": value["status"],
        "probability": value.get("probability"),
        "confidence_interval": bounds,
        "details": details,
    }


def _self_contained_record_evidence(
    value: Mapping[str, object],
    study: Mapping[str, object],
    feature_evidence: Mapping[str, object],
) -> dict[str, object]:
    metadata = cast(Mapping[str, object], study["metadata"])
    features = cast(Mapping[str, object], feature_evidence["features"])
    local_limitations = [str(item) for item in cast(Sequence[object], value.get("limitations") or ())]
    shared_limitations = [str(item) for item in cast(Sequence[object], study["limitations"])]
    mature = "label_not_matured" not in local_limitations
    source_digest = _verified_artifact_digest(value.get("source_evidence_digest"))
    executable = (
        mature
        and value.get("label_status") == "modelled"
        and value.get("label_rule_profile_verified") is True
        and source_digest is not None
    )
    return {
        "record_contract_version": PROBABILITY_RESULT_CONTRACT_VERSION,
        "sample_id": f'{value["run_id"]}:{value["symbol"]}:{value["horizon"]}:{value["target"]}',
        "feature_evidence_key": f'{value["run_id"]}:{value["symbol"]}',
        "dimensions": dict(cast(Mapping[str, object], feature_evidence["dimensions"])),
        "feature_vector_digest": stable_probability_hash(features),
        "source_evidence_digest": source_digest,
        "mature_horizon": mature,
        "executable": executable,
        "model_target": value.get("observed_label") if executable else None,
        "versions": study["versions"],
        "digests": _artifact_result_digests(value, study),
        "base_rate": metadata.get("base_rate"),
        "training_cutoff": metadata.get("training_cutoff"),
        "target_definition": metadata.get("target_definition"),
        "counts": metadata.get("counts"),
        "contract": metadata.get("contract"),
        "calibration_summary": _record_calibration_summary(metadata),
        "calibration_offset_ci_95": _calibration_offset(metadata),
        "limitations": list(dict.fromkeys([*shared_limitations, *local_limitations])),
        "generated_at": metadata.get("generated_at"),
        "automatic_promotion": False,
    }


def _artifact_result_digests(
    value: Mapping[str, object], study: Mapping[str, object],
) -> dict[str, object]:
    digests = dict(cast(Mapping[str, object], study["digests"]))
    fold_id = value.get("fold_id")
    if fold_id is None:
        return digests
    if isinstance(fold_id, bool) or not isinstance(fold_id, int) or fold_id <= 0:
        raise ValueError("probability record fold_id must be a positive integer or null")
    metadata = cast(Mapping[str, object], study["metadata"])
    folds = metadata.get("folds")
    rows = [item for item in folds if isinstance(item, Mapping)] if isinstance(folds, list) else []
    fold = next((item for item in rows if item.get("fold_id") == fold_id), None)
    if fold is None:
        raise ValueError("probability record fold evidence is missing")
    for digest_name in ("model", "calibrator", "isotonic_calibrator", "baseline"):
        digests[digest_name] = fold.get(f"{digest_name}_digest")
    return digests


def _verified_artifact_digest(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    return value if all(character in "0123456789abcdef" for character in value) else None


def _calibration_offset(evidence: Mapping[str, object]) -> object:
    metrics = evidence.get("calibration_metrics")
    calibrated = metrics.get("calibrated") if isinstance(metrics, Mapping) else None
    return calibrated.get("calibration_offset_ci_95") if isinstance(calibrated, Mapping) else None


def _record_calibration_summary(evidence: Mapping[str, object]) -> dict[str, object]:
    metrics = evidence.get("calibration_metrics")
    calibrated = metrics.get("calibrated") if isinstance(metrics, Mapping) else None
    values: Mapping[str, object] = calibrated if isinstance(calibrated, Mapping) else {}
    names = (
        "observation_count",
        "independent_session_count",
        "brier_score",
        "brier_skill_score",
        "reference_base_rate_mean",
        "reference_brier_score",
        "reference_definition",
        "log_loss",
        "ece",
        "auc",
        "bin_monotonic",
        "highest_bin_above_base_rate",
        "brier_score_ci_95",
        "actual_positive_rate_ci_95",
        "calibration_offset_ci_95",
        "bootstrap_samples",
    )
    bins = values.get("calibration_bins")
    bin_rows = [item for item in bins if isinstance(item, Mapping)] if isinstance(bins, list) else []
    sessions = [int(item.get("independent_session_count") or 0) for item in bin_rows]
    return {
        **{name: values.get(name) for name in names},
        "calibration_bin_count": len(bin_rows) if calibrated is not None else None,
        "minimum_bin_independent_session_count": min(sessions) if sessions else None,
    }


def _target_evidence(
    horizons: Mapping[str, object],
    horizon: int,
    target: str,
) -> Mapping[str, object]:
    targets = horizons.get(str(horizon))
    evidence = targets.get(target) if isinstance(targets, Mapping) else None
    if not isinstance(evidence, Mapping):
        raise ValueError(f"probability research evidence missing: {horizon}/{target}")
    return evidence


def _artifact_versions(evidence: Mapping[str, object]) -> dict[str, object]:
    return {
        "model": str(evidence.get("model_version") or PROBABILITY_MODEL_VERSION),
        "calibrator": PROBABILITY_CALIBRATOR_VERSION,
        "feature": str(evidence.get("feature_version") or PROBABILITY_FEATURE_VERSION),
        "label": str(evidence.get("label_version") or PROBABILITY_LABEL_VERSION),
        "cost_model": str(evidence.get("cost_model_version") or PROBABILITY_COST_MODEL_VERSION),
        "benchmark": PROBABILITY_BENCHMARK_VERSION,
    }


def _artifact_digests(evidence: Mapping[str, object]) -> dict[str, object]:
    fitted = evidence.get("model") is not None
    digests = {
        "input": evidence["input_digest"],
        "model": evidence.get("model_digest") if fitted else None,
        "calibrator": evidence.get("calibrator_digest") if fitted else None,
        "isotonic_calibrator": evidence.get("isotonic_calibrator_digest") if fitted else None,
        "baseline": evidence.get("baseline_digest") if fitted else None,
        "evidence": evidence.get("evidence_digest"),
    }
    if evidence.get("label_contract_digest") is not None:
        digests["label_contract"] = evidence["label_contract_digest"]
    return digests


def _fit_target(
    rows: Sequence[ProbabilityResearchRow],
    benchmarks: Mapping[tuple[int, int], float],
    horizon: int,
    target: ProbabilityPublicTarget,
    generated_at: str,
    bootstrap_samples: int,
    label_contract: Mapping[str, object],
) -> dict[str, object]:
    samples = [
        _probability_sample(row, benchmarks, horizon, target)
        for row in rows
        if horizon in row.mature_horizons
    ]
    config = ProbabilityConfig(
        horizon=horizon,
        target="net_excess_positive" if target == PROBABILITY_PRIMARY_TARGET else "net_return_positive",
        cost_model_version=_label_cost_model_version(label_contract),
        label_contract=dict(label_contract),
        bootstrap_samples=bootstrap_samples,
    )
    return fit_shadow_probability(samples, config=config, generated_at=generated_at)


def _label_cost_model_version(label_contract: Mapping[str, object]) -> str:
    value = label_contract.get("cost_model_version")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("probability label contract cost_model_version is missing")
    return value


def _probability_sample(
    row: ProbabilityResearchRow,
    benchmarks: Mapping[tuple[int, int], float],
    horizon: int,
    target: ProbabilityPublicTarget,
) -> ProbabilitySample:
    outcome = row.labels.get(horizon)
    verified_outcome = (
        outcome
        if outcome is not None
        and outcome.status == "modelled"
        and outcome.rule_profile_verified
        else None
    )
    net_return = verified_outcome.net_return if verified_outcome is not None else None
    benchmark = benchmarks.get((row.run_id, horizon))
    net_excess = net_return - benchmark if net_return is not None and benchmark is not None else None
    point_in_time_verified = _has_verified_source_evidence(row)
    target_value = _target_value(target, net_return, net_excess) if point_in_time_verified else None
    return ProbabilitySample(
        sample_id=_sample_id(row, horizon, target),
        session_date=row.session_date,
        features=row.features,
        target=target_value,
        executable=point_in_time_verified and verified_outcome is not None,
        net_return=net_return,
        net_excess_return=net_excess,
    )


def _target_value(
    target: ProbabilityPublicTarget,
    net_return: float | None,
    net_excess: float | None,
) -> int | None:
    value = net_excess if target == PROBABILITY_PRIMARY_TARGET else net_return
    return int(value > 0) if value is not None else None


def _market_benchmarks(rows: Sequence[ProbabilityResearchRow]) -> dict[tuple[int, int], float]:
    grouped: dict[tuple[int, int], list[float]] = defaultdict(list)
    for row in rows:
        for horizon, outcome in row.labels.items():
            if (
                horizon in row.mature_horizons
                and outcome.status == "modelled"
                and outcome.rule_profile_verified
                and outcome.net_return is not None
            ):
                grouped[(row.run_id, horizon)].append(outcome.net_return)
    return {key: fmean(values) for key, values in grouped.items() if values}


def _probability_records(
    rows: Sequence[ProbabilityResearchRow],
    benchmarks: Mapping[tuple[int, int], float],
    horizon: int,
    target: ProbabilityPublicTarget,
    evidence: Mapping[str, object],
) -> list[dict[str, object]]:
    test_end = _test_end_date(evidence)
    persisted_predictions = {
        str(item.get("sample_id") or ""): item
        for item in _prediction_rows(evidence)
    }
    return [
        _probability_record(
            row, benchmarks, horizon, target, evidence, test_end,
            persisted_predictions,
        )
        for row in rows
    ]


def _probability_record(
    row: ProbabilityResearchRow,
    benchmarks: Mapping[tuple[int, int], float],
    horizon: int,
    target: ProbabilityPublicTarget,
    evidence: Mapping[str, object],
    test_end: str | None,
    persisted_predictions: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    sample_id = _sample_id(row, horizon, target)
    estimate = _row_estimate(
        row, sample_id, evidence, test_end, persisted_predictions,
    )
    outcome = row.labels.get(horizon)
    benchmark = benchmarks.get((row.run_id, horizon))
    verified_outcome = (
        outcome
        if outcome is not None
        and outcome.status == "modelled"
        and outcome.rule_profile_verified
        else None
    )
    net_return = verified_outcome.net_return if verified_outcome is not None else None
    net_excess = net_return - benchmark if net_return is not None and benchmark is not None else None
    limitations = _record_limitations(row, horizon, evidence, estimate, outcome)
    return {
        "run_id": row.run_id,
        "symbol": row.symbol,
        "quote_date": row.session_date,
        "horizon": horizon,
        "target": target,
        "status": estimate["status"],
        "fold_id": estimate.get("fold_id"),
        "probability": estimate.get("probability"),
        "raw_probability": estimate.get("raw_probability"),
        "empirical_bayes_probability": estimate.get("empirical_bayes_probability"),
        "confidence_interval": _confidence_interval(estimate),
        "confidence_interval_definition": estimate.get("confidence_interval_definition"),
        "feature_vector_digest": stable_probability_hash(row.features),
        "source_evidence_digest": row.source_evidence_digest,
        "observed_label": _target_value(target, net_return, net_excess),
        "label_status": outcome.status if outcome is not None else "data_unavailable",
        "label_reason": outcome.reason if outcome is not None else "label_missing",
        "label_rule_profile_verified": (
            outcome.rule_profile_verified if outcome is not None else False
        ),
        "label_daily_bar_model_limited": (
            outcome.daily_bar_model_limited if outcome is not None else False
        ),
        "net_return": net_return,
        "market_benchmark_net_return": benchmark,
        "net_excess_return": net_excess,
        "entry_date": outcome.entry_date if outcome is not None else None,
        "exit_date": outcome.exit_date if outcome is not None else None,
        **_record_feature_evidence(row, horizon, target),
        **_record_common_evidence(evidence, limitations),
    }


def _record_feature_evidence(
    row: ProbabilityResearchRow,
    horizon: int,
    target: ProbabilityPublicTarget,
) -> dict[str, object]:
    if horizon == 1 and target == PROBABILITY_PRIMARY_TARGET:
        return {
            "feature_values": [row.features[name] for name in sorted(row.features)],
            "dimensions": dict(sorted(row.dimensions.items())),
            "feature_evidence_role": "canonical_for_run_symbol",
        }
    return {"feature_evidence_reference": "1/net_excess_positive"}


def _row_estimate(
    row: ProbabilityResearchRow,
    sample_id: str,
    evidence: Mapping[str, object],
    test_end: str | None,
    persisted_predictions: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    if evidence.get("status") != "calibrated_shadow":
        return {"status": "insufficient_data", "probability": None, "confidence_interval": None}
    persisted = persisted_predictions.get(sample_id)
    if persisted is not None:
        return _persisted_oos_estimate(evidence, persisted)
    if test_end is None or row.session_date <= test_end:
        return {"status": "insufficient_data", "probability": None, "confidence_interval": None}
    return predict_shadow_probability(evidence, row.features, sample_id=sample_id)


def _persisted_oos_estimate(
    evidence: Mapping[str, object], prediction: Mapping[str, object],
) -> dict[str, object]:
    probability = _mapping_float(prediction, "probability")
    offset_value = _calibration_offset(evidence)
    offset = [float(value) for value in offset_value] if isinstance(offset_value, list) and len(offset_value) == 2 else [0.0, 0.0]
    return {
        "status": "calibrated_shadow",
        "fold_id": prediction.get("fold_id"),
        "probability": probability,
        "raw_probability": prediction.get("raw_probability"),
        "empirical_bayes_probability": prediction.get("baseline_probability"),
        "confidence_interval": [
            min(1.0, max(0.0, probability + offset[0])),
            min(1.0, max(0.0, probability + offset[1])),
        ],
        "confidence_interval_definition": "oos_session_block_bootstrap_calibration_offset_95pct",
    }


def _record_common_evidence(
    evidence: Mapping[str, object],
    limitations: list[str],
) -> dict[str, object]:
    return {
        "study_evidence_digest": evidence.get("evidence_digest"),
        "label_contract_digest": evidence.get("label_contract_digest"),
        "label_contract_binding": evidence.get("label_contract_binding"),
        "selection_qualified": evidence.get("selection_qualified") is True,
        "selection_qualification": evidence.get("selection_qualification"),
        "limitations": limitations,
        "generated_at": evidence.get("generated_at"),
        "automatic_promotion": False,
    }


def _record_limitations(
    row: ProbabilityResearchRow,
    horizon: int,
    evidence: Mapping[str, object],
    estimate: Mapping[str, object],
    outcome: ProbabilityLabelOutcome | None,
) -> list[str]:
    values: list[str] = []
    if horizon not in row.mature_horizons:
        values.append("label_not_matured")
    elif outcome is None or outcome.status != "modelled":
        values.append(outcome.reason if outcome is not None else "label_missing")
    elif not outcome.rule_profile_verified:
        values.append("label_rule_profile_unverified")
    if outcome is not None and outcome.daily_bar_model_limited:
        values.append("daily_bar_execution_model_limited")
    if estimate.get("status") != "calibrated_shadow" and evidence.get("status") == "calibrated_shadow":
        values.append("not_out_of_sample_prediction")
    if not _has_verified_source_evidence(row):
        values.append("point_in_time_source_digest_unavailable")
    return list(dict.fromkeys(values))


def _evidence_summary(
    evidence: Mapping[str, object],
    target: ProbabilityPublicTarget,
    rows: Sequence[ProbabilityResearchRow],
    horizon: int,
    label_contract: Mapping[str, object],
) -> dict[str, object]:
    summary = dict(evidence)
    outcome_metrics = _portfolio_outcome_metrics(evidence)
    point_in_time = _point_in_time_evidence_summary(rows, horizon)
    stratified = _stratified_metrics(evidence, rows, horizon, target)
    probability_bins = _probability_bin_outcome_metrics(evidence)
    stability = _temporal_stability_metrics(evidence)
    major_strata = _major_strata_calibration_summary(stratified)
    replay_verified = _deterministic_replay_verified(evidence, rows, horizon, target)
    expected_label_digest = stable_probability_hash(label_contract)
    if (
        evidence.get("cost_model_version") != label_contract.get("cost_model_version")
        or evidence.get("label_contract_digest") != expected_label_digest
    ):
        raise ValueError("probability study and complete label contract differ")
    summary.pop("predictions", None)
    summary["target"] = target
    summary["label_version"] = label_contract.get("label_version") or evidence.get("label_version")
    summary["cost_model_version"] = evidence.get("cost_model_version")
    summary["label_contract_digest"] = expected_label_digest
    summary["label_contract_binding"] = evidence.get("label_contract_binding")
    summary["feature_names"] = sorted(rows[0].features) if rows else []
    summary["benchmark_definition"] = PROBABILITY_BENCHMARK_VERSION
    summary["automatic_promotion"] = False
    summary["point_in_time_evidence"] = point_in_time
    summary["outcome_metrics"] = outcome_metrics
    summary["probability_bin_outcomes"] = probability_bins
    summary["stratified_metrics"] = stratified
    summary["stability_metrics"] = stability
    summary["major_strata_calibration"] = major_strata
    summary["deterministic_replay_verified"] = replay_verified
    summary["promotion_gates"] = _promotion_gates(
        evidence, outcome_metrics, point_in_time, probability_bins, major_strata, stability,
        replay_verified,
    )
    return summary


def _deterministic_replay_verified(
    evidence: Mapping[str, object],
    rows: Sequence[ProbabilityResearchRow],
    horizon: int,
    target: ProbabilityPublicTarget,
) -> bool:
    benchmarks = _market_benchmarks(rows)
    samples = [
        _probability_sample(row, benchmarks, horizon, target)
        for row in rows
        if horizon in row.mature_horizons
    ]
    return verify_shadow_probability_evidence(evidence, samples)


def _promotion_gates(
    evidence: Mapping[str, object],
    outcomes: Mapping[str, object],
    point_in_time: Mapping[str, object],
    probability_bins: Sequence[Mapping[str, object]],
    major_strata: Mapping[str, object],
    stability: Mapping[str, object],
    replay_verified: bool,
) -> dict[str, object]:
    calibrated = _calibrated_metrics(evidence)
    raw_counts = evidence.get("counts")
    counts: Mapping[str, object] = raw_counts if isinstance(raw_counts, Mapping) else {}
    gates = {
        "calibrated_shadow": evidence.get("status") == "calibrated_shadow",
        "selection_qualified": evidence.get("selection_qualified") is True,
        "label_coverage_at_least_95pct": _mapping_float(counts, "label_coverage") >= 0.95,
        "point_in_time_evidence_at_least_95pct": _mapping_float(point_in_time, "coverage") >= 0.95,
        "brier_skill_positive": _mapping_float(calibrated, "brier_skill_score") > 0,
        "ece_at_most_5pct": _mapping_float(calibrated, "ece", default=math.inf) <= 0.05,
        "auc_at_least_0_52": _mapping_float(calibrated, "auc") >= 0.52,
        "probability_bins_monotonic": calibrated.get("bin_monotonic") is True,
        "highest_bin_above_base_rate": calibrated.get("highest_bin_above_base_rate") is True,
        "probability_bin_outcomes_complete": bool(probability_bins) and all(
            _mapping_float(item, "independent_session_count") >= 20 for item in probability_bins
        ),
        "top100_independent_sessions_at_least_60": _mapping_float(
            outcomes, "independent_session_count",
        ) >= 60,
        "top100_mean_net_excess_return_positive": _mapping_float(outcomes, "mean_net_excess_return") > 0,
        "maximum_drawdown_at_least_minus_25pct": _mapping_float(
            outcomes, "maximum_drawdown", default=-math.inf,
        ) >= -0.25,
        "mean_turnover_at_most_80pct": _mapping_float(
            outcomes, "mean_top100_turnover", default=math.inf,
        ) <= 0.80,
        "temporal_stability_acceptable": stability.get("passed") is True,
        "major_market_calibration_acceptable": _major_dimension_passes(major_strata, "market"),
        "major_board_calibration_acceptable": _major_dimension_passes(major_strata, "board"),
        "deterministic_replay_verified": replay_verified,
    }
    return {"version": "market-scan-probability-shadow-gates-v2", "passed": all(gates.values()), "gates": gates}


def _point_in_time_evidence_summary(
    rows: Sequence[ProbabilityResearchRow],
    horizon: int,
) -> dict[str, object]:
    matured = [row for row in rows if horizon in row.mature_horizons]
    verified = sum(_has_verified_source_evidence(row) for row in matured)
    return {
        "matured_observation_count": len(matured),
        "verified_observation_count": verified,
        "coverage": verified / len(matured) if matured else 0.0,
        "requirement": "verified-persisted-at-scan-time payload digest",
    }


def _portfolio_outcome_metrics(evidence: Mapping[str, object]) -> dict[str, object]:
    predictions = _prediction_rows(evidence)
    if not predictions:
        return {
            "observation_count": 0,
            "independent_session_count": 0,
            "mean_net_return": None,
            "mean_net_excess_return": None,
            "maximum_drawdown": None,
            "mean_top100_turnover": None,
        }
    selected = _top_probability_rows(predictions, limit=100)
    daily_returns = _daily_prediction_returns(selected)
    excess_path = [item[2] for item in daily_returns]
    return {
        "observation_count": len(selected),
        "independent_session_count": len({item[0] for item in daily_returns}),
        "mean_net_return": fmean(item[1] for item in daily_returns),
        "mean_net_excess_return": fmean(excess_path),
        "maximum_drawdown": _maximum_drawdown(excess_path),
        "mean_top100_turnover": _selection_turnover(selected),
        "portfolio_definition": "per-run top100 calibrated probability, equal-weight, no production rerank",
    }


def _probability_bin_outcome_metrics(
    evidence: Mapping[str, object],
) -> list[dict[str, object]]:
    predictions = _prediction_rows(evidence)
    calibrated = _calibrated_metrics(evidence)
    raw_bins = calibrated.get("calibration_bins")
    bins = [item for item in raw_bins if isinstance(item, Mapping)] if isinstance(raw_bins, list) else []
    return [
        _probability_bin_outcome_record(predictions, item, index == len(bins) - 1)
        for index, item in enumerate(bins)
    ]


def _probability_bin_outcome_record(
    predictions: Sequence[Mapping[str, object]],
    calibration_bin: Mapping[str, object],
    is_last: bool,
) -> dict[str, object]:
    lower = _mapping_float(calibration_bin, "lower")
    upper = _mapping_float(calibration_bin, "upper", default=1.0)
    rows = [
        item for item in predictions
        if _probability_in_interval(_mapping_float(item, "probability"), lower, upper, is_last)
    ]
    daily = _daily_prediction_returns(rows)
    net_path, excess_path = [item[1] for item in daily], [item[2] for item in daily]
    independent_sessions = len({item[0] for item in daily})
    return {
        "lower": lower,
        "upper": upper,
        "observation_count": len(rows),
        "independent_session_count": independent_sessions,
        "status": "ok" if independent_sessions >= 20 else "insufficient_data",
        "mean_net_return": fmean(net_path) if net_path else None,
        "mean_net_excess_return": fmean(excess_path) if excess_path else None,
        "mean_turnover": _selection_turnover(rows),
        "maximum_net_return_drawdown": _maximum_drawdown(net_path),
        "maximum_net_excess_drawdown": _maximum_drawdown(excess_path),
    }


def _probability_in_interval(value: float, lower: float, upper: float, is_last: bool) -> bool:
    return lower <= value <= upper if is_last else lower <= value < upper


def _daily_prediction_returns(
    predictions: Sequence[Mapping[str, object]],
) -> list[tuple[str, float, float]]:
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for item in predictions:
        net_return = _optional_number(item.get("net_return"))
        net_excess = _optional_number(item.get("net_excess_return"))
        if net_return is not None and net_excess is not None:
            grouped[str(item["session_date"])].append((net_return, net_excess))
    return [
        (day, fmean(value[0] for value in values), fmean(value[1] for value in values))
        for day, values in sorted(grouped.items())
        if values
    ]


def _top_probability_rows(
    predictions: Sequence[Mapping[str, object]], *, limit: int,
) -> list[Mapping[str, object]]:
    grouped: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    for item in predictions:
        grouped[_prediction_portfolio_key(item)].append(item)
    return [
        item
        for _key, values in sorted(grouped.items())
        for item in sorted(
            values,
            key=lambda row: (-_mapping_float(row, "probability", default=-math.inf), str(row.get("sample_id") or "")),
        )[:limit]
    ]


def _prediction_portfolio_key(item: Mapping[str, object]) -> tuple[str, str]:
    sample_id = str(item.get("sample_id") or "")
    return str(item.get("session_date") or ""), sample_id.split(":", 1)[0]


def _maximum_drawdown(returns: Sequence[float]) -> float | None:
    if not returns:
        return None
    wealth = peak = 1.0
    drawdown = 0.0
    for value in returns:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1.0)
    return drawdown


def _selection_turnover(predictions: Sequence[Mapping[str, object]]) -> float | None:
    grouped: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    for item in predictions:
        grouped[_prediction_portfolio_key(item)].append(item)
    selections = [
        {
            str(item["sample_id"]).split(":", 2)[1]
            for item in values
        }
        for _key, values in sorted(grouped.items())
    ]
    turnovers = [
        1.0 - len(left & right) / max(1, len(left), len(right))
        for left, right in zip(selections, selections[1:], strict=False)
    ]
    return fmean(turnovers) if turnovers else 0.0


def _stratified_metrics(
    evidence: Mapping[str, object],
    rows: Sequence[ProbabilityResearchRow],
    horizon: int,
    target: ProbabilityPublicTarget,
) -> dict[str, object]:
    predictions = _prediction_rows(evidence)
    rows_by_id = {_sample_id(row, horizon, target): row for row in rows}
    output: dict[str, object] = {}
    for dimension in ("market", "board", "industry", "liquidity", "regime"):
        grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
        for prediction in predictions:
            row = rows_by_id.get(str(prediction.get("sample_id") or ""))
            if row is not None:
                grouped[str(row.dimensions.get(dimension) or "unknown")].append(prediction)
        output[dimension] = [
            _stratum_record(value, items)
            for value, items in sorted(grouped.items())
        ]
    return output


def _stratum_record(value: str, rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    probabilities = [_mapping_float(item, "probability") for item in rows]
    outcomes = [int(_mapping_float(item, "outcome")) for item in rows]
    dates = [str(item["session_date"]) for item in rows]
    references = _reference_probabilities(rows)
    base_rate = fmean(references) if references is not None else sum(outcomes) / len(outcomes)
    metrics = evaluate_probability_predictions(
        probabilities,
        outcomes,
        dates,
        base_rate=base_rate,
        reference_probabilities=references,
    )
    return {
        "value": value,
        "status": "ok" if _mapping_float(metrics, "independent_session_count") >= 20 else "insufficient_data",
        **metrics,
        "mean_net_return": _optional_mean(rows, "net_return"),
        "mean_net_excess_return": _optional_mean(rows, "net_excess_return"),
    }


def _temporal_stability_metrics(evidence: Mapping[str, object]) -> dict[str, object]:
    predictions = _prediction_rows(evidence)
    dates = sorted({str(item.get("session_date") or "") for item in predictions})
    midpoint = len(dates) // 2
    slices = [
        _stability_slice(predictions, frozenset(selected), evidence.get("base_rate"), name)
        for name, selected in (("early", dates[:midpoint]), ("late", dates[midpoint:]))
    ]
    sufficient = all(
        _mapping_float(item, "independent_session_count") >= PROBABILITY_STABILITY_MINIMUM_SLICE_SESSIONS
        for item in slices
    )
    max_ece = max((_mapping_float(item, "ece", default=math.inf) for item in slices), default=math.inf)
    brier_gap = _slice_gap(slices, "brier_score")
    offset_gap = _slice_gap(slices, "calibration_offset")
    passed = (
        sufficient
        and max_ece <= PROBABILITY_MAXIMUM_REVIEW_ECE
        and brier_gap is not None and brier_gap <= 0.05
        and offset_gap is not None and offset_gap <= 0.05
    )
    return {
        "status": "ok" if sufficient else "insufficient_data",
        "passed": passed,
        "slices": slices,
        "maximum_ece": None if not math.isfinite(max_ece) else max_ece,
        "brier_score_gap": brier_gap,
        "calibration_offset_gap": offset_gap,
        "thresholds": {
            "minimum_slice_sessions": PROBABILITY_STABILITY_MINIMUM_SLICE_SESSIONS,
            "maximum_ece": PROBABILITY_MAXIMUM_REVIEW_ECE,
            "maximum_brier_score_gap": 0.05,
            "maximum_calibration_offset_gap": 0.05,
        },
    }


def _stability_slice(
    predictions: Sequence[Mapping[str, object]],
    dates: frozenset[str],
    base_rate_value: object,
    name: str,
) -> dict[str, object]:
    rows = [item for item in predictions if str(item.get("session_date") or "") in dates]
    if not rows:
        return {"name": name, "observation_count": 0, "independent_session_count": 0}
    probabilities = [_mapping_float(item, "probability") for item in rows]
    outcomes = [int(_mapping_float(item, "outcome")) for item in rows]
    session_dates = [str(item["session_date"]) for item in rows]
    references = _reference_probabilities(rows)
    base_rate = fmean(references) if references is not None else _optional_number(base_rate_value)
    metrics = evaluate_probability_predictions(
        probabilities, outcomes, session_dates,
        base_rate=base_rate if base_rate is not None else sum(outcomes) / len(outcomes),
        reference_probabilities=references,
    )
    return {
        "name": name,
        **metrics,
        "calibration_offset": fmean(probabilities) - fmean(outcomes),
    }


def _reference_probabilities(
    rows: Sequence[Mapping[str, object]],
) -> list[float] | None:
    values = [_optional_number(item.get("reference_base_rate")) for item in rows]
    if any(value is None for value in values):
        return None
    return [cast(float, value) for value in values]


def _slice_gap(slices: Sequence[Mapping[str, object]], name: str) -> float | None:
    values = [_optional_number(item.get(name)) for item in slices]
    if len(values) != 2 or values[0] is None or values[1] is None:
        return None
    return abs(values[0] - values[1])


def _major_strata_calibration_summary(
    stratified: Mapping[str, object],
) -> dict[str, object]:
    return {
        dimension: _major_dimension_summary(stratified.get(dimension))
        for dimension in ("market", "board")
    }


def _major_dimension_summary(value: object) -> dict[str, object]:
    rows = [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []
    total = sum(_mapping_float(item, "observation_count") for item in rows)
    major = [
        item for item in rows
        if total > 0 and _mapping_float(item, "observation_count") / total >= PROBABILITY_MAJOR_STRATUM_MINIMUM_SHARE
    ]
    assessments = [
        {
            "value": item.get("value"),
            "share": _mapping_float(item, "observation_count") / total,
            "status": item.get("status"),
            "brier_skill_score": item.get("brier_skill_score"),
            "ece": item.get("ece"),
            "passed": (
                item.get("status") == "ok"
                and _mapping_float(item, "brier_skill_score", default=-math.inf) > 0
                and _mapping_float(item, "ece", default=math.inf) <= PROBABILITY_MAXIMUM_REVIEW_ECE
            ),
        }
        for item in major
    ]
    return {
        "status": "ok" if assessments else "insufficient_data",
        "passed": bool(assessments) and all(item["passed"] is True for item in assessments),
        "minimum_share": PROBABILITY_MAJOR_STRATUM_MINIMUM_SHARE,
        "maximum_ece": PROBABILITY_MAXIMUM_REVIEW_ECE,
        "strata": assessments,
    }


def _major_dimension_passes(values: Mapping[str, object], dimension: str) -> bool:
    summary = values.get(dimension)
    return isinstance(summary, Mapping) and summary.get("passed") is True


def _prediction_rows(evidence: Mapping[str, object]) -> list[Mapping[str, object]]:
    values = evidence.get("predictions")
    return [item for item in values if isinstance(item, Mapping)] if isinstance(values, list) else []


def _optional_mean(rows: Sequence[Mapping[str, object]], name: str) -> float | None:
    values = [value for item in rows if (value := _optional_number(item.get(name))) is not None]
    return fmean(values) if values else None


def _optional_number(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return None


def _research_payload(
    rows: Sequence[ProbabilityResearchRow],
    generated_at: str,
    horizons: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    label_contract: Mapping[str, object],
    *,
    cohorts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": PROBABILITY_RESEARCH_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "calibrated_shadow" if _any_calibrated(horizons) else "insufficient_data",
        "default_horizon": 5,
        "primary_target": PROBABILITY_PRIMARY_TARGET,
        "horizons": dict(horizons),
        "cohorts": [dict(cohort) for cohort in cohorts],
        "cohort_count": len(cohorts),
        "records": list(records),
        "record_count": len(records),
        "run_count": len({row.run_id for row in rows}),
        "symbol_count": len({row.symbol for row in rows}),
        "label_contract": dict(label_contract),
        "model_version": PROBABILITY_MODEL_VERSION,
        "feature_version": PROBABILITY_FEATURE_VERSION,
        "label_version": PROBABILITY_LABEL_VERSION,
        "benchmark_version": PROBABILITY_BENCHMARK_VERSION,
        "execution_model": PROBABILITY_EXECUTION_MODEL,
        "production_ranking_effect": "none",
        "automatic_promotion": False,
        "limitations": [
            "Shadow 研究结果不参与 full-market-score-v4 排名、分数或回放。",
            "日K无法复原盘口排队和盘中成交先后；不可成交与模型受限状态不会被假定为成交。",
            "缺少可验证扫描时点证据的特征不会进入模型训练、校准或测试。",
        ],
    }


def _sample_id(row: ProbabilityResearchRow, horizon: int, target: str) -> str:
    return f"{row.run_id}:{row.symbol}:{horizon}:{target}"


def _test_end_date(evidence: Mapping[str, object]) -> str | None:
    split = evidence.get("split")
    dates = split.get("test_dates") if isinstance(split, Mapping) else None
    return str(dates[-1]) if isinstance(dates, list) and dates else None


def _confidence_interval(estimate: Mapping[str, object]) -> dict[str, object] | None:
    values = estimate.get("confidence_interval")
    if not isinstance(values, list) or len(values) != 2:
        return None
    return {
        "level": 0.95,
        "lower": values[0],
        "upper": values[1],
        "method": estimate.get("confidence_interval_definition") or "date_block_bootstrap",
    }


def _calibrated_metrics(evidence: Mapping[str, object]) -> Mapping[str, object]:
    metrics = evidence.get("calibration_metrics")
    calibrated = metrics.get("calibrated") if isinstance(metrics, Mapping) else None
    return calibrated if isinstance(calibrated, Mapping) else {}


def _mapping_float(values: Mapping[str, object], name: str, *, default: float = 0.0) -> float:
    value = values.get(name)
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return default


def _has_verified_source_evidence(row: ProbabilityResearchRow) -> bool:
    digest = row.source_evidence_digest
    return isinstance(digest, str) and len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _feature(values: Mapping[str, float], name: str, default: float) -> float:
    value = values.get(name)
    return float(value) if value is not None and math.isfinite(float(value)) else default


def _registered_raw_features(values: Mapping[str, float]) -> dict[str, float]:
    aliases = {
        "return_1d_pct": ("feature_return_1d_pct", "return_1d_pct"),
        "return_5d_pct": ("feature_return_5d_pct", "return_5d_pct"),
        "return_20d_pct": ("feature_return_20d_pct", "return_20d_pct"),
        "return_60d_pct": ("feature_return_60d_pct", "return_60d_pct"),
        "skip5_return_20d_pct": ("feature_skip5_return_20d_pct", "skip5_return_20d_pct"),
        "skip5_return_55d_pct": ("feature_skip5_return_55d_pct", "skip5_return_55d_pct"),
        "ma20_slope_10d_pct": ("feature_ma20_slope_10d_pct", "ma20_slope_10d_pct"),
        "ma_alignment": ("feature_ma_alignment", "ma_alignment"),
        "atr20_pct": ("feature_atr20_pct", "atr20_pct"),
        "downside_volatility_20d_pct": ("feature_downside_volatility_20d_pct", "downside_volatility_20d_pct"),
        "max_drawdown_60d_pct": ("feature_max_drawdown_60d_pct", "max_drawdown_60d_pct"),
        "range_position_20d": ("feature_range_position_20d", "range_position_20d"),
        "volume_lifecycle_delta": ("feature_volume_lifecycle_delta", "volume_lifecycle_delta"),
    }
    return {
        name: _first_feature(values, candidates, 0.5 if name == "range_position_20d" else 0.0)
        for name, candidates in aliases.items()
    }


def _first_feature(values: Mapping[str, float], names: Sequence[str], default: float) -> float:
    for name in names:
        value = values.get(name)
        if value is not None and math.isfinite(float(value)):
            return float(value)
    return default


def _context_strength_features(
    market_strength: float,
    board_relative_strength: float,
    industry_relative_strength: float,
) -> dict[str, float]:
    return {
        "market_strength": _finite_or(market_strength, 50.0),
        "board_relative_strength": _finite_or(board_relative_strength, 0.0),
        "industry_relative_strength": _finite_or(industry_relative_strength, 0.0),
    }


def _finite_or(value: float, default: float) -> float:
    return float(value) if math.isfinite(float(value)) else default


def _status_and_limit_features(
    values: Mapping[str, float], board: str, segment: str,
) -> dict[str, float]:
    is_st = bool(_first_feature(values, ("is_st",), float(segment == "st")))
    is_new = bool(_first_feature(values, ("is_new",), float(segment == "new")))
    fallback_limit = 5.0 if is_st else 30.0 if board == "BSE" else 20.0 if board in {"STAR", "CHINEXT"} else 10.0
    limit_pct = max(0.0, _first_feature(values, ("price_limit_pct",), fallback_limit))
    verified = _first_feature(values, ("price_limit_profile_verified",), float(not is_new))
    uncertain = _first_feature(values, ("price_limit_profile_uncertain",), float(is_new))
    absent = _first_feature(values, ("price_limit_absent",), float(limit_pct <= 0))
    new_no_limit_phase = _first_feature(values, ("new_stock_no_limit_phase",), 0.0)
    change = _first_feature(values, ("change_pct", "feature_return_1d_pct", "return_1d_pct"), 0.0)
    denominator = limit_pct if limit_pct > 0 and absent < 0.5 else math.inf
    return {
        "is_st": float(is_st),
        "is_new": float(is_new),
        "price_limit_pct": limit_pct,
        "upper_price_limit_proximity": min(2.0, max(0.0, change) / denominator),
        "lower_price_limit_proximity": min(2.0, max(0.0, -change) / denominator),
        "price_limit_profile_verified": verified,
        "price_limit_profile_uncertain": uncertain,
        "price_limit_absent": absent,
        "new_stock_no_limit_phase": new_no_limit_phase,
    }


def _categorical_features(
    market: str, board: str, liquidity: str, regime: str, industry: str,
) -> dict[str, float]:
    features = {
        "market_sh": float(market == "SH"),
        "market_sz": float(market == "SZ"),
        "market_bj": float(market == "BJ"),
        "board_sh_main": float(board == "SH_MAIN"),
        "board_star": float(board == "STAR"),
        "board_sz_main": float(board == "SZ_MAIN"),
        "board_chinext": float(board == "CHINEXT"),
        "board_bse": float(board == "BSE"),
        "liquidity_high": float(liquidity == "high"),
        "liquidity_mid": float(liquidity == "mid"),
        "liquidity_low": float(liquidity == "low"),
        "regime_strong": float(regime == "strong"),
        "regime_neutral": float(regime == "neutral"),
        "regime_weak": float(regime == "weak"),
    }
    bucket = _industry_bucket(industry)
    features.update({f"industry_bucket_{index:02d}": float(index == bucket) for index in range(16)})
    return features


def _industry_bucket(industry: str) -> int:
    normalized = " ".join(str(industry or "unknown").strip().lower().split()) or "unknown"
    return int.from_bytes(hashlib.sha256(normalized.encode("utf-8")).digest()[:2], "big") % 16


def _any_calibrated(horizons: Mapping[str, object]) -> bool:
    return any(
        evidence.get("status") == "calibrated_shadow"
        for targets in horizons.values()
        if isinstance(targets, Mapping)
        for evidence in targets.values()
        if isinstance(evidence, Mapping)
    )


__all__ = [
    "PROBABILITY_ABSOLUTE_TARGET",
    "PROBABILITY_BENCHMARK_VERSION",
    "PROBABILITY_PRIMARY_TARGET",
    "PROBABILITY_RESEARCH_SCHEMA_VERSION",
    "PROBABILITY_TARGETS",
    "ProbabilityResearchRow",
    "build_probability_research",
    "probability_feature_vector",
]
