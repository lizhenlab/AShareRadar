from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import json
import os
from pathlib import Path

import pytest

import app.services.market_scan_probability_artifact as artifact_module
from app.services.market_scan_probability import (
    PROBABILITY_BASELINE_VERSION,
    PROBABILITY_CALIBRATOR_VERSION,
    PROBABILITY_COST_MODEL_VERSION,
    PROBABILITY_FEATURE_VERSION,
    PROBABILITY_LABEL_VERSION,
    PROBABILITY_MODEL_VERSION,
    ProbabilityConfig,
    ProbabilitySample,
    fit_shadow_probability,
)
from app.services.market_scan_probability_artifact import (
    PROBABILITY_ARTIFACT_SET_REPLAY_SCHEMA_VERSION,
    PROBABILITY_ARTIFACT_INTEGRITY_NOTICE,
    PROBABILITY_ARTIFACT_SCHEMA_VERSION,
    PROBABILITY_RESULT_CONTRACT_VERSION,
    ProbabilityArtifactError,
    build_probability_artifact,
    canonical_probability_artifact_json,
    load_probability_artifact,
    probability_payload_integrity_digest,
    replay_probability_artifact,
    replay_probability_artifact_set,
    verify_probability_artifact,
    write_probability_artifact,
)


def _versions() -> dict[str, object]:
    return {
        "model": "shadow-logit-l2-v1",
        "calibrator": "shadow-platt-v1",
        "feature": "point-in-time-v1",
        "label": "next-open-h-close-v1",
        "cost_model": "ashare-cost-v1",
        "baseline": "empirical-bayes-v1",
    }


def _model() -> dict[str, object]:
    return {
        "version": "shadow-logit-l2-v1",
        "feature_names": ["trend"],
        "means": [0.0],
        "scales": [1.0],
        "intercept": 0.0,
        "coefficients": [1.0],
    }


def _calibrator() -> dict[str, object]:
    return {"version": "shadow-platt-v1", "intercept": 0.0, "slope": 1.0}


def _baseline() -> dict[str, object]:
    return {"version": "empirical-bayes-v1", "boundaries": [], "probabilities": [0.6]}


def _digests(*, calibrated: bool) -> dict[str, object]:
    return {
        "input": "a" * 64,
        "model": probability_payload_integrity_digest(_model()) if calibrated else None,
        "calibrator": probability_payload_integrity_digest(_calibrator()) if calibrated else None,
        "isotonic_calibrator": None,
        "baseline": probability_payload_integrity_digest(_baseline()) if calibrated else None,
        "evidence": None,
    }


def _calibration_metrics(*, calibrated: bool) -> dict[str, object] | None:
    if not calibrated:
        return None
    return {
        "calibrated": {
            "observation_count": 100,
            "independent_session_count": 60,
            "brier_score": 0.2,
            "brier_skill_score": 0.1,
            "log_loss": 0.6,
            "ece": 0.03,
            "auc": 0.62,
            "bin_monotonic": True,
            "highest_bin_above_base_rate": True,
            "brier_score_ci_95": [0.18, 0.22],
            "actual_positive_rate_ci_95": [0.48, 0.58],
            "calibration_offset_ci_95": [-0.1, 0.1],
            "bootstrap_samples": 1000,
            "calibration_bins": [{"independent_session_count": 20}],
        }
    }


def _calibration_summary(*, calibrated: bool) -> dict[str, object]:
    metrics = _calibration_metrics(calibrated=calibrated)
    values = metrics["calibrated"] if metrics is not None else {}
    names = (
        "observation_count", "independent_session_count", "brier_score", "brier_skill_score",
        "reference_base_rate_mean", "reference_brier_score", "reference_definition",
        "log_loss", "ece", "auc", "bin_monotonic", "highest_bin_above_base_rate",
        "brier_score_ci_95", "actual_positive_rate_ci_95", "calibration_offset_ci_95", "bootstrap_samples",
    )
    return {
        **{name: values.get(name) for name in names},
        "calibration_bin_count": 1 if calibrated else None,
        "minimum_bin_independent_session_count": 20 if calibrated else None,
    }


def _study(
    *, status: str = "insufficient_data", generated_at: str = "2026-08-11T10:00:00+08:00",
) -> dict[str, object]:
    calibrated = status == "calibrated_shadow"
    digests = _digests(calibrated=calibrated)
    split = {
        "train_dates": ["2026-07-01"],
        "train_gap_dates": [],
        "calibration_dates": ["2026-07-15"],
        "calibration_gap_dates": [],
        "test_dates": ["2026-07-31"],
    } if calibrated else None
    folds = [{
        "fold_id": 1,
        "split": split,
        "training_cutoff": "2026-07-31",
        "base_rate": 0.53,
        "model": _model(),
        "calibrator": _calibrator(),
        "isotonic_calibrator": None,
        "empirical_bayes_baseline": _baseline(),
        "model_digest": digests["model"],
        "calibrator_digest": digests["calibrator"],
        "isotonic_calibrator_digest": None,
        "baseline_digest": digests["baseline"],
        "prediction_count": 1,
        "test_session_count": 1,
    }] if calibrated else []
    return {
        "run_id": 29,
        "target": "net_excess_positive",
        "horizon": 5,
        "status": status,
        "versions": _versions(),
        "digests": digests,
        "limitations": ["shadow_only_no_production_ranking_effect"],
        "metadata": {
            "training_cutoff": "2026-07-31" if calibrated else None,
            "base_rate": 0.53 if calibrated else None,
            "split": split,
            "target_definition": "future_5d_net_excess_return_gt_0_after_costs",
            "counts": {"training_session_count": 120 if calibrated else 0},
            "contract": {"schema_version": "probability-contract-v1"},
            "generated_at": generated_at,
            "input_digest": digests["input"],
            "evidence_digest": None,
            "model": _model() if calibrated else None,
            "calibrator": _calibrator() if calibrated else None,
            "empirical_bayes_baseline": _baseline() if calibrated else None,
            "model_digest": digests["model"],
            "calibrator_digest": digests["calibrator"],
            "isotonic_calibrator_digest": None,
            "baseline_digest": digests["baseline"],
            "calibration_metrics": _calibration_metrics(calibrated=calibrated),
            "folds": folds,
        },
    }


def _record(
    *, status: str = "insufficient_data", generated_at: str = "2026-08-11T10:00:00+08:00",
) -> dict[str, object]:
    calibrated = status == "calibrated_shadow"
    probability = 0.7310585786300049 if calibrated else None
    digests = _digests(calibrated=calibrated)
    return {
        "run_id": 29,
        "symbol": "600519.SH",
        "target": "net_excess_positive",
        "horizon": 5,
        "status": status,
        "probability": probability,
        "confidence_interval": [probability - 0.1, probability + 0.1] if probability is not None else None,
        "details": {
            "record_contract_version": PROBABILITY_RESULT_CONTRACT_VERSION,
            "sample_id": "29:600519.SH:5:net_excess_positive",
            "quote_date": "2026-07-31",
            "feature_evidence_key": "29:600519.SH",
            "dimensions": {"market": "SH", "board": "SH_MAIN"},
            "feature_vector_digest": probability_payload_integrity_digest({"trend": 1.0}),
            "source_evidence_digest": "e" * 64,
            "mature_horizon": calibrated,
            "executable": calibrated,
            "model_target": 1 if calibrated else None,
            "fold_id": 1 if calibrated else None,
            "observed_label": 1 if calibrated else None,
            "label_status": "modelled" if calibrated else "data_unavailable",
            "label_reason": "target_close" if calibrated else "label_missing",
            "net_return": 0.02 if calibrated else None,
            "market_benchmark_net_return": 0.01 if calibrated else None,
            "net_excess_return": 0.01 if calibrated else None,
            "entry_date": "2026-08-03" if calibrated else None,
            "exit_date": "2026-08-10" if calibrated else None,
            "raw_probability": probability,
            "empirical_bayes_probability": 0.6 if calibrated else None,
            "confidence_interval_definition": "test_session_block_bootstrap_calibration_offset_95pct" if calibrated else None,
            "versions": _versions(),
            "digests": digests,
            "base_rate": 0.53 if calibrated else None,
            "training_cutoff": "2026-07-31" if calibrated else None,
            "target_definition": "future_5d_net_excess_return_gt_0_after_costs",
            "counts": {"training_session_count": 120 if calibrated else 0},
            "contract": {"schema_version": "probability-contract-v1"},
            "calibration_summary": _calibration_summary(calibrated=calibrated),
            "calibration_offset_ci_95": [-0.1, 0.1] if calibrated else None,
            "limitations": ["shadow_only_no_production_ranking_effect"],
            "generated_at": generated_at,
            "automatic_promotion": False,
        },
    }


def _payload(
    *, status: str = "insufficient_data", generated_at: str = "2026-08-11T10:00:00+08:00",
) -> dict[str, object]:
    return {
        "record_contract_version": PROBABILITY_RESULT_CONTRACT_VERSION,
        "feature_evidence": [
            {
                "run_id": 29,
                "symbol": "600519.SH",
                "quote_date": "2026-07-31",
                "features": {"trend": 1.0},
                "feature_names": ["trend"],
                "feature_vector_digest": probability_payload_integrity_digest({"trend": 1.0}),
                "dimensions": {"market": "SH", "board": "SH_MAIN"},
                "source_evidence_digest": "e" * 64,
            }
        ],
        "studies": [_study(status=status, generated_at=generated_at)],
        "records": [_record(status=status, generated_at=generated_at)],
    }


def _artifact(*, status: str = "insufficient_data", generated_at: str = "2026-08-11T10:00:00+08:00") -> dict[str, object]:
    return build_probability_artifact(_payload(status=status, generated_at=generated_at), generated_at=generated_at)


def _replay_set_artifacts(
    *, generated_at: str = "2026-08-11T08:00:00Z",
    run_start: int = 101,
    artifact_set_run_ids: list[int] | None = None,
    cohort_contract: dict[str, object] | None = None,
    feature_scale: float = 1.0,
) -> tuple[list[ProbabilitySample], list[dict[str, object]]]:
    run_ids = list(range(run_start, run_start + 8))
    manifest = artifact_set_run_ids or run_ids
    samples = [
        ProbabilitySample(
            sample_id=f"{run_id}:600{run_id}.SH:1:net_excess_positive",
            session_date=(date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            features={"trend": feature_scale * (1 if index % 2 else -1)},
            target=index % 2,
            executable=True,
            net_return=0.02 if index % 2 else -0.02,
            net_excess_return=0.01 if index % 2 else -0.01,
        )
        for index, run_id in enumerate(run_ids)
    ]
    config = ProbabilityConfig(
        horizon=1,
        minimum_train_sessions=2,
        minimum_calibration_sessions=2,
        minimum_test_sessions=2,
        minimum_label_coverage=1.0,
        minimum_bin_sessions=1,
        gap_sessions=1,
        calibration_bin_count=2,
        minimum_isotonic_calibration_sessions=2,
        empirical_bayes_bin_count=2,
        bootstrap_samples=100,
    )
    evidence = fit_shadow_probability(samples, config=config, generated_at=generated_at)
    assert evidence["status"] == "calibrated_shadow"
    cohort_digest = None
    if cohort_contract is not None:
        cohort_digest = probability_payload_integrity_digest({
            "cohort_contract": cohort_contract,
            "run_ids": sorted(run_ids),
            "session_dates": sorted(sample.session_date for sample in samples),
            "horizon_evidence_digests": {"1/net_excess_positive": evidence["evidence_digest"]},
        })
    return samples, [
        build_probability_artifact(
            _replay_set_payload(sample, evidence, manifest, cohort_contract, cohort_digest),
            generated_at=generated_at,
        )
        for sample in samples
    ]


def _replay_set_payload(
    sample: ProbabilitySample,
    evidence: dict[str, object],
    run_ids: list[int],
    cohort_contract: dict[str, object] | None,
    cohort_digest: str | None,
) -> dict[str, object]:
    run_id, symbol = int(sample.sample_id.split(":", 1)[0]), sample.sample_id.split(":", 2)[1]
    digests = {
        "input": evidence["input_digest"],
        "model": evidence["model_digest"],
        "calibrator": evidence["calibrator_digest"],
        "isotonic_calibrator": evidence["isotonic_calibrator_digest"],
        "baseline": evidence["baseline_digest"],
        "evidence": evidence["evidence_digest"],
    }
    versions = {
        "model": PROBABILITY_MODEL_VERSION,
        "calibrator": PROBABILITY_CALIBRATOR_VERSION,
        "feature": PROBABILITY_FEATURE_VERSION,
        "label": PROBABILITY_LABEL_VERSION,
        "cost_model": PROBABILITY_COST_MODEL_VERSION,
        "baseline": PROBABILITY_BASELINE_VERSION,
    }
    limitations = [*evidence["limitations"], "run_has_no_out_of_sample_calibrated_prediction"]  # type: ignore[misc]
    metadata = {key: deepcopy(value) for key, value in evidence.items() if key != "predictions"}
    metadata.update(artifact_set_run_ids=run_ids, run_record_count=1, run_calibrated_record_count=0)
    if cohort_contract is not None:
        metadata["cohort_contract"] = dict(cohort_contract)
        metadata["cohort_digest"] = cohort_digest
    feature_digest = probability_payload_integrity_digest(sample.features)
    source_digest = "e" * 64
    details = {
        "record_contract_version": PROBABILITY_RESULT_CONTRACT_VERSION,
        "sample_id": sample.sample_id,
        "quote_date": sample.session_date,
        "feature_evidence_key": f"{run_id}:{symbol}",
        "dimensions": {"market": "SH", "board": "SH_MAIN"},
        "feature_vector_digest": feature_digest,
        "source_evidence_digest": source_digest,
        "mature_horizon": True,
        "executable": True,
        "model_target": sample.target,
        "fold_id": None,
        "observed_label": sample.target,
        "label_status": "modelled",
        "label_reason": "target_close",
        "net_return": sample.net_return,
        "market_benchmark_net_return": sample.net_return - sample.net_excess_return,  # type: ignore[operator]
        "net_excess_return": sample.net_excess_return,
        "entry_date": sample.session_date,
        "exit_date": sample.session_date,
        "raw_probability": None,
        "empirical_bayes_probability": None,
        "confidence_interval_definition": None,
        "versions": versions,
        "digests": digests,
        "base_rate": evidence["base_rate"],
        "training_cutoff": evidence["training_cutoff"],
        "target_definition": evidence["target_definition"],
        "counts": evidence["counts"],
        "contract": evidence["contract"],
        "calibration_summary": _replay_set_calibration_summary(evidence),
        "calibration_offset_ci_95": evidence["calibration_metrics"]["calibrated"]["calibration_offset_ci_95"],  # type: ignore[index]
        "limitations": limitations,
        "generated_at": evidence["generated_at"],
        "automatic_promotion": False,
    }
    return {
        "record_contract_version": PROBABILITY_RESULT_CONTRACT_VERSION,
        "feature_evidence": [{
            "run_id": run_id,
            "symbol": symbol,
            "quote_date": sample.session_date,
            "features": dict(sample.features),
            "feature_names": sorted(sample.features),
            "feature_vector_digest": feature_digest,
            "dimensions": details["dimensions"],
            "source_evidence_digest": source_digest,
        }],
        "studies": [{
            "run_id": run_id,
            "target": "net_excess_positive",
            "horizon": 1,
            "status": "insufficient_data",
            "versions": versions,
            "digests": digests,
            "limitations": limitations,
            "metadata": metadata,
        }],
        "records": [{
            "run_id": run_id,
            "symbol": symbol,
            "target": "net_excess_positive",
            "horizon": 1,
            "status": "insufficient_data",
            "probability": None,
            "confidence_interval": None,
            "details": details,
        }],
    }


def _replay_set_calibration_summary(evidence: dict[str, object]) -> dict[str, object]:
    calibrated = evidence["calibration_metrics"]["calibrated"]  # type: ignore[index]
    names = (
        "observation_count", "independent_session_count", "brier_score", "brier_skill_score",
        "reference_base_rate_mean", "reference_brier_score", "reference_definition",
        "log_loss", "ece", "auc", "bin_monotonic", "highest_bin_above_base_rate",
        "brier_score_ci_95", "actual_positive_rate_ci_95", "calibration_offset_ci_95", "bootstrap_samples",
    )
    bins = calibrated["calibration_bins"]
    return {
        **{name: calibrated.get(name) for name in names},
        "calibration_bin_count": len(bins),
        "minimum_bin_independent_session_count": min(item["independent_session_count"] for item in bins),
    }


def test_artifact_digest_is_canonical_and_excludes_generated_at() -> None:
    first = _artifact(generated_at="2026-08-11T10:00:00+08:00")
    second = _artifact(generated_at="2026-08-11T11:00:00+08:00")
    first_integrity = first["integrity"]
    second_integrity = second["integrity"]

    assert isinstance(first_integrity, dict)
    assert isinstance(second_integrity, dict)
    assert first["schema_version"] == PROBABILITY_ARTIFACT_SCHEMA_VERSION
    assert first_integrity["notice"] == PROBABILITY_ARTIFACT_INTEGRITY_NOTICE
    assert first_integrity["integrity_digest"] == second_integrity["integrity_digest"]
    assert first_integrity["integrity_digest"] == probability_payload_integrity_digest(_payload())
    encoded = canonical_probability_artifact_json(first)
    assert ": " not in encoded
    assert ", " not in encoded
    assert encoded == canonical_probability_artifact_json(json.loads(encoded))


def test_self_contained_result_replays_offline_and_persists_every_contract_field() -> None:
    artifact = _artifact(status="calibrated_shadow")
    record = artifact["payload"]["records"][0]  # type: ignore[index]
    details = record["details"]

    assert replay_probability_artifact(artifact) == {
        "29:600519.SH:5:net_excess_positive": record["probability"],
    }
    assert details["record_contract_version"] == PROBABILITY_RESULT_CONTRACT_VERSION
    assert details["feature_evidence_key"] == "29:600519.SH"
    assert artifact["payload"]["feature_evidence"][0]["features"] == {"trend": 1.0}  # type: ignore[index]
    assert details["versions"] == artifact["payload"]["studies"][0]["versions"]  # type: ignore[index]
    assert details["digests"] == artifact["payload"]["studies"][0]["digests"]  # type: ignore[index]
    assert details["base_rate"] == 0.53
    assert details["training_cutoff"] == "2026-07-31"
    assert details["automatic_promotion"] is False


def test_oos_record_replays_with_its_fold_model_not_the_final_fold_model() -> None:
    payload = _payload(status="calibrated_shadow")
    study = payload["studies"][0]  # type: ignore[index]
    record = payload["records"][0]  # type: ignore[index]
    metadata = study["metadata"]
    first_fold = metadata["folds"][0]
    final_model = {**_model(), "intercept": -1.0}
    final_digest = probability_payload_integrity_digest(final_model)
    final_split = {**first_fold["split"], "test_dates": ["2026-08-01"]}
    final_fold = {
        **deepcopy(first_fold),
        "fold_id": 2,
        "split": final_split,
        "training_cutoff": "2026-08-01",
        "model": final_model,
        "model_digest": final_digest,
    }
    metadata.update(
        folds=[first_fold, final_fold],
        split=final_split,
        training_cutoff="2026-08-01",
        model=final_model,
        model_digest=final_digest,
    )
    study["digests"]["model"] = final_digest
    record["details"]["training_cutoff"] = "2026-08-01"

    artifact = build_probability_artifact(payload, generated_at="2026-08-11T10:00:00+08:00")

    assert record["details"]["digests"]["model"] != study["digests"]["model"]
    assert replay_probability_artifact(artifact)[record["details"]["sample_id"]] == record["probability"]


def test_complete_artifact_set_refits_full_study_and_preserves_historical_digests() -> None:
    samples, artifacts = _replay_set_artifacts()

    replay = replay_probability_artifact_set(artifacts)

    assert replay["schema_version"] == PROBABILITY_ARTIFACT_SET_REPLAY_SCHEMA_VERSION
    assert replay["run_ids"] == list(range(101, 109))
    assert replay["study_count"] == 1
    assert replay["studies"][0]["sample_count"] == len(samples)  # type: ignore[index]
    assert replay["studies"][0]["status"] == "calibrated_shadow"  # type: ignore[index]
    for artifact in artifacts:
        study = artifact["payload"]["studies"][0]  # type: ignore[index]
        record = artifact["payload"]["records"][0]  # type: ignore[index]
        assert study["status"] == "insufficient_data"
        assert all(study["digests"][name] is not None for name in ("model", "calibrator", "baseline"))
        assert record["details"]["digests"] == study["digests"]


def test_artifact_set_replays_each_cohort_with_its_own_model_and_input_digest() -> None:
    first_run_ids, second_run_ids = list(range(101, 109)), list(range(201, 209))
    manifest = [*first_run_ids, *second_run_ids]
    first_cohort = {"mode": "official", "scope": "A", "rule_version": "v1"}
    second_cohort = {"mode": "intraday", "scope": "B", "rule_version": "v2"}
    _first_samples, first = _replay_set_artifacts(
        artifact_set_run_ids=manifest,
        cohort_contract=first_cohort,
    )
    _second_samples, second = _replay_set_artifacts(
        run_start=201,
        artifact_set_run_ids=manifest,
        cohort_contract=second_cohort,
        feature_scale=2.0,
    )

    replay = replay_probability_artifact_set([*first, *second])

    assert replay["run_ids"] == manifest
    assert replay["study_count"] == 2
    studies = replay["studies"]
    assert [item["cohort_contract"] for item in studies] == [first_cohort, second_cohort]  # type: ignore[union-attr]
    assert [item["run_ids"] for item in studies] == [first_run_ids, second_run_ids]  # type: ignore[union-attr]
    assert len({item["input_digest"] for item in studies}) == 2  # type: ignore[union-attr]


def test_artifact_set_replay_rejects_missing_record_run_and_resealed_input_tampering() -> None:
    _samples, artifacts = _replay_set_artifacts()

    with pytest.raises(ProbabilityArtifactError, match="run 不完整"):
        replay_probability_artifact_set(artifacts[1:])
    with pytest.raises(ProbabilityArtifactError, match="重复 run"):
        replay_probability_artifact_set([*artifacts, artifacts[0]])
    _later_samples, later_artifacts = _replay_set_artifacts(generated_at="2026-08-11T09:00:00Z")
    with pytest.raises(ProbabilityArtifactError, match="generated_at"):
        replay_probability_artifact_set([later_artifacts[0], *artifacts[1:]])

    missing_record = deepcopy(artifacts[0])
    missing_record["payload"]["records"] = []  # type: ignore[index]
    _reseal(missing_record)
    with pytest.raises(ProbabilityArtifactError, match="同一个 run|缺少 feature 或 result record"):
        replay_probability_artifact_set([missing_record, *artifacts[1:]])

    tampered_payload = deepcopy(artifacts[0]["payload"])
    tampered_payload["records"][0]["details"]["net_return"] = 0.123  # type: ignore[index]
    tampered = build_probability_artifact(
        tampered_payload,  # type: ignore[arg-type]
        generated_at=str(artifacts[0]["generated_at"]),
    )
    with pytest.raises(ProbabilityArtifactError, match="input_digest"):
        replay_probability_artifact_set([tampered, *artifacts[1:]])


def test_artifact_set_path_replay_rejects_a_deleted_artifact(tmp_path: Path) -> None:
    _samples, artifacts = _replay_set_artifacts()
    database = tmp_path / "ashare.sqlite3"
    database.write_bytes(b"database")
    paths = [
        write_probability_artifact(
            tmp_path / "artifacts" / f"run-{index}.json",
            artifact,
            database_path=database,
        )
        for index, artifact in enumerate(artifacts)
    ]
    assert replay_probability_artifact_set(paths)["study_count"] == 1

    paths[0].unlink()
    with pytest.raises(ProbabilityArtifactError, match="读取失败"):
        replay_probability_artifact_set(paths)
    with pytest.raises(ProbabilityArtifactError, match="run 不完整"):
        replay_probability_artifact_set(paths[1:])
    assert database.read_bytes() == b"database"


@pytest.mark.parametrize("component", ["model", "calibrator", "input"])
def test_replay_rejects_resealed_component_and_input_digest_tampering(component: str) -> None:
    artifact = deepcopy(_artifact(status="calibrated_shadow"))
    study = artifact["payload"]["studies"][0]  # type: ignore[index]
    if component == "model":
        study["metadata"]["model"]["intercept"] = 0.2
    elif component == "calibrator":
        study["metadata"]["calibrator"]["slope"] = 0.8
    else:
        study["metadata"]["input_digest"] = "f" * 64
    _reseal(artifact)

    with pytest.raises(ProbabilityArtifactError, match="digest"):
        replay_probability_artifact(artifact)


def test_replay_rejects_resealed_probability_or_feature_tampering() -> None:
    changed_probability = deepcopy(_artifact(status="calibrated_shadow"))
    changed_probability["payload"]["records"][0]["probability"] = 0.7  # type: ignore[index]
    _reseal(changed_probability)
    with pytest.raises(ProbabilityArtifactError, match="重放"):
        replay_probability_artifact(changed_probability)

    changed_feature = deepcopy(_artifact(status="calibrated_shadow"))
    feature_row = changed_feature["payload"]["feature_evidence"][0]  # type: ignore[index]
    feature_row["features"]["trend"] = 0.5
    digest = probability_payload_integrity_digest(feature_row["features"])
    feature_row["feature_vector_digest"] = digest
    changed_feature["payload"]["records"][0]["details"]["feature_vector_digest"] = digest  # type: ignore[index]
    _reseal(changed_feature)
    with pytest.raises(ProbabilityArtifactError, match="重放"):
        replay_probability_artifact(changed_feature)


def test_null_and_calibrated_records_round_trip_atomically_without_database_writes(tmp_path: Path) -> None:
    database = tmp_path / "ashare.sqlite3"
    database.write_bytes(b"immutable-database-bytes")
    target = tmp_path / "probability" / "run-29.json"
    expected = _artifact(status="calibrated_shadow")

    written = write_probability_artifact(target, expected, database_path=database)

    assert written == target.resolve()
    assert load_probability_artifact(target) == expected
    assert target.read_text(encoding="utf-8") == canonical_probability_artifact_json(expected)
    assert database.read_bytes() == b"immutable-database-bytes"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []
    assert verify_probability_artifact(_artifact(status="insufficient_data"))["payload"] == _payload()
    assert write_probability_artifact(target, expected, database_path=database) == target.resolve()

    with pytest.raises(ProbabilityArtifactError, match="拒绝覆盖不可变证据"):
        write_probability_artifact(
            target,
            _artifact(status="insufficient_data"),
            database_path=database,
        )


def test_write_rejects_database_path_aliases_before_replacement(tmp_path: Path) -> None:
    database = tmp_path / "ashare.sqlite3"
    database.write_bytes(b"database")

    with pytest.raises(ProbabilityArtifactError, match="SQLite"):
        write_probability_artifact(database, _artifact(), database_path=database)

    hard_link = tmp_path / "artifact.json"
    os.link(database, hard_link)
    with pytest.raises(ProbabilityArtifactError, match="SQLite"):
        write_probability_artifact(hard_link, _artifact(), database_path=database)
    assert database.read_bytes() == b"database"


def test_atomic_write_failure_preserves_previous_artifact_and_cleans_tempfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "ashare.sqlite3"
    database.write_bytes(b"database")
    target = tmp_path / "artifact.json"
    original = _artifact(generated_at="2026-08-11T10:00:00+08:00")
    write_probability_artifact(target, original, database_path=database)
    original_bytes = target.read_bytes()
    failed_target = tmp_path / "new-artifact.json"

    def fail_publish(_source: Path, _target: Path) -> None:
        raise OSError("simulated publish failure")

    monkeypatch.setattr(artifact_module.os, "link", fail_publish)
    with pytest.raises(ProbabilityArtifactError, match="写入失败"):
        write_probability_artifact(
            failed_target,
            _artifact(generated_at="2026-08-11T11:00:00+08:00"),
            database_path=database,
        )
    assert target.read_bytes() == original_bytes
    assert not failed_target.exists()
    assert list(tmp_path.glob(f".{failed_target.name}.*.tmp")) == []


@pytest.mark.parametrize(
    ("status", "probability", "interval"),
    [
        ("insufficient_data", 0.5, None),
        ("insufficient_data", None, [0.4, 0.6]),
        ("calibrated_shadow", None, [0.4, 0.6]),
        ("calibrated_shadow", 1.01, [0.4, 0.6]),
        ("calibrated_shadow", 0.5, [0.7, 0.6]),
        ("calibrated_shadow", 0.8, [0.4, 0.6]),
        ("calibrated_shadow", 0.5, [0.4, float("nan")]),
    ],
)
def test_builder_rejects_status_probability_and_ci_inconsistency(
    status: str,
    probability: float | None,
    interval: list[float] | None,
) -> None:
    payload = _payload(status=status)
    record = payload["records"][0]  # type: ignore[index]
    record["probability"] = probability  # type: ignore[index]
    record["confidence_interval"] = interval  # type: ignore[index]

    with pytest.raises(ProbabilityArtifactError):
        build_probability_artifact(payload, generated_at="2026-08-11T10:00:00+08:00")


def test_builder_rejects_duplicates_or_record_study_mismatch() -> None:
    duplicate_record = _payload()
    duplicate_record["records"].append(deepcopy(duplicate_record["records"][0]))  # type: ignore[union-attr,index]
    with pytest.raises(ProbabilityArtifactError, match="重复 record"):
        build_probability_artifact(duplicate_record, generated_at="2026-08-11T10:00:00+08:00")

    duplicate_study = _payload()
    duplicate_study["studies"].append(deepcopy(duplicate_study["studies"][0]))  # type: ignore[union-attr,index]
    with pytest.raises(ProbabilityArtifactError, match="重复 study"):
        build_probability_artifact(duplicate_study, generated_at="2026-08-11T10:00:00+08:00")

    mismatch = _payload()
    mismatch["records"][0]["status"] = "calibrated_shadow"  # type: ignore[index]
    mismatch["records"][0]["probability"] = 0.6  # type: ignore[index]
    mismatch["records"][0]["confidence_interval"] = [0.5, 0.7]  # type: ignore[index]
    with pytest.raises(ProbabilityArtifactError, match="状态不一致"):
        build_probability_artifact(mismatch, generated_at="2026-08-11T10:00:00+08:00")


def test_loader_fails_closed_on_corruption_unknown_schema_and_duplicate_json_keys(tmp_path: Path) -> None:
    artifact = _artifact()
    corrupted = deepcopy(artifact)
    corrupted["payload"]["records"][0]["symbol"] = "000001.SZ"  # type: ignore[index]
    with pytest.raises(ProbabilityArtifactError, match="integrity digest"):
        verify_probability_artifact(corrupted)

    unknown = deepcopy(artifact)
    unknown["schema_version"] = "market-scan-probability-artifact-v2"
    with pytest.raises(ProbabilityArtifactError, match="schema_version"):
        verify_probability_artifact(unknown)

    source = tmp_path / "duplicate.json"
    source.write_text('{"schema_version":"v1","schema_version":"v1"}', encoding="utf-8")
    with pytest.raises(ProbabilityArtifactError, match="重复 key"):
        load_probability_artifact(source)


def test_loader_keeps_restart_compatibility_for_verified_legacy_artifacts(tmp_path: Path) -> None:
    legacy = deepcopy(_artifact())
    legacy["payload"].pop("record_contract_version")  # type: ignore[union-attr]
    legacy["payload"].pop("feature_evidence")  # type: ignore[union-attr]
    _reseal(legacy)
    source = tmp_path / "legacy-artifact.json"
    source.write_text(canonical_probability_artifact_json(legacy), encoding="utf-8")

    loaded = load_probability_artifact(source)

    assert loaded["payload"].get("record_contract_version") is None  # type: ignore[union-attr]
    with pytest.raises(ProbabilityArtifactError, match="schema"):
        verify_probability_artifact(legacy)


def test_builder_rejects_bad_versions_digests_targets_and_non_json_values() -> None:
    missing_version = _payload()
    del missing_version["studies"][0]["versions"]["label"]  # type: ignore[index]
    with pytest.raises(ProbabilityArtifactError, match="缺少版本"):
        build_probability_artifact(missing_version, generated_at="2026-08-11T10:00:00+08:00")

    bad_digest = _payload(status="calibrated_shadow")
    bad_digest["studies"][0]["digests"]["model"] = "NOT-SHA256"  # type: ignore[index]
    with pytest.raises(ProbabilityArtifactError, match="SHA-256"):
        build_probability_artifact(bad_digest, generated_at="2026-08-11T10:00:00+08:00")

    bad_target = _payload()
    bad_target["studies"][0]["target"] = "gross_return_positive"  # type: ignore[index]
    with pytest.raises(ProbabilityArtifactError, match="target"):
        build_probability_artifact(bad_target, generated_at="2026-08-11T10:00:00+08:00")

    bad_json = _payload()
    bad_json["studies"][0]["metadata"]["bad"] = Path("not-json")  # type: ignore[index]
    with pytest.raises(ProbabilityArtifactError, match="非 JSON"):
        build_probability_artifact(bad_json, generated_at="2026-08-11T10:00:00+08:00")


def _reseal(artifact: dict[str, object]) -> None:
    artifact["integrity"]["integrity_digest"] = probability_payload_integrity_digest(artifact["payload"])  # type: ignore[index,arg-type]
