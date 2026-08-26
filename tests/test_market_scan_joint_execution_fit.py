from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import math
from types import SimpleNamespace

import pytest

import app.services.market_scan_probability as probability
import app.services.market_scan_joint_execution_probability as joint_probability
import app.services.market_scan_joint_execution_outcomes as joint_outcomes
import app.services.market_scan_joint_execution_source as joint_source


def _synthetic_learning_rows() -> tuple[joint_probability._LearningRow, ...]:
    rows: list[joint_probability._LearningRow] = []
    roles = (
        ("positive", 3.0, True, True, True),
        ("net_negative", 1.0, True, True, False),
        ("exit_blocked", -1.0, True, False, None),
        ("unfilled", -3.0, False, None, None),
    )
    start = date(2026, 1, 2)
    for offset in range(20):
        session = (start + timedelta(days=offset)).isoformat()
        for index, (role, signal, entry, exit_value, net) in enumerate(roles):
            sample_id = f"{offset + 1}:{index:06d}.SH"
            joint = bool(entry and exit_value is True and net is True)
            rows.append(
                joint_probability._LearningRow(
                    sample_id=sample_id,
                    run_id=offset + 1,
                    session_date=session,
                    symbol=f"{600000 + index:06d}.SH",
                    features={
                        "execution_signal": signal,
                        "session_cycle": float(offset % 3) / 10.0,
                    },
                    entry_fill=entry,
                    exit_executable=exit_value,
                    net_positive=net,
                    joint_action_positive=joint,
                    net_return=0.03 if joint else (-0.02 if net is False else None),
                    net_excess_return=(
                        0.02 if joint else (-0.03 if net is False else None)
                    ),
                    observed_at=f"{session}T20:00:00+08:00",
                    source_record_digest=joint_probability._digest(
                        [sample_id, role, "source"]
                    ),
                    outcome_record_digest=joint_probability._digest(
                        [sample_id, role, "outcome"]
                    ),
                    holding_path_digest=joint_probability._digest(
                        [sample_id, role, "path"]
                    ),
                    benchmark_series_digest=joint_probability._digest(
                        [sample_id, role, "benchmark"]
                    ),
                )
            )
    return tuple(rows)


def _synthetic_corpus(
    rows: tuple[joint_probability._LearningRow, ...],
) -> joint_probability.VerifiedJointExecutionLearningCorpus:
    bindings = [
        {
            "run_id": offset + 1,
            "signal_session": (date(2026, 1, 2) + timedelta(days=offset)).isoformat(),
            "source_artifact_digest": joint_probability._digest([offset, "source"]),
            "source_snapshot_digest": joint_probability._digest([offset, "snapshot"]),
            "source_feature_schema_digest": joint_probability._digest([offset, "features"]),
            "outcome_artifact_digest": joint_probability._digest([offset, "outcome"]),
            "decision_identity_digest": joint_probability._digest([offset, "identity"]),
            "decision_membership_digest": joint_probability._digest([offset, "membership"]),
            "record_count": 4,
        }
        for offset in range(20)
    ]
    encoded_rows = joint_probability.canonical_json_bytes(
        [item.payload() for item in rows]
    ).decode("utf-8")
    encoded_bindings = joint_probability.canonical_json_bytes(bindings).decode("utf-8")
    return joint_probability.VerifiedJointExecutionLearningCorpus(
        encoded_rows,
        encoded_bindings,
        corpus_digest=joint_probability._digest(["synthetic", encoded_rows, bindings]),
        feature_contract_digest=joint_probability._digest(
            ["execution_signal", "session_cycle"]
        ),
        feature_version="joint-execution-test-features-v1",
        feature_names=("execution_signal", "session_cycle"),
        label_contract_digest=joint_probability._digest(["joint-label", 5]),
        _seal=joint_probability._LEARNING_CORPUS_SEAL,
    )


def _empty_source_token(
    run_id: int,
    *,
    signal_session: str,
    records: list[dict[str, object]] | None = None,
) -> joint_source.VerifiedJointExecutionSourceCorpus:
    values = records or []
    feature_schema = {
        "version": "joint-execution-test-features-v1",
        "base_version": "test-base-v1",
        "imputation_policy": "fixed_at_signal",
        "names": ["execution_signal", "session_cycle"],
        "base_names": [],
    }
    return joint_source.VerifiedJointExecutionSourceCorpus(
        joint_probability.canonical_json_bytes(values).decode("utf-8"),
        artifact_digest=joint_probability._digest([run_id, "source"]),
        run_id=run_id,
        signal_session=signal_session,
        source_snapshot_digest=joint_probability._digest([run_id, "snapshot"]),
        decision_identity_digest=joint_probability._digest(
            [item.get("decision_id") for item in values]
        ),
        decision_membership_digest=joint_probability._digest(
            [item.get("symbol") for item in values]
        ),
        decision_frozen_at=f"{signal_session}T16:00:00+08:00",
        feature_schema_digest=joint_probability._digest(feature_schema),
        encoded_feature_schema=joint_probability.canonical_json_bytes(
            feature_schema
        ).decode("utf-8"),
        _seal=joint_source._VERIFIED_SOURCE_SEAL,
    )


def _empty_outcome_token(
    run_id: int,
    *,
    signal_session: str,
    records: list[dict[str, object]] | None = None,
) -> joint_outcomes.VerifiedJointExecutionOutcomeCorpus:
    values = records or []
    return joint_outcomes.VerifiedJointExecutionOutcomeCorpus(
        joint_probability.canonical_json_bytes(values).decode("utf-8"),
        artifact_digest=joint_probability._digest([run_id, "outcome"]),
        run_id=run_id,
        signal_session=signal_session,
        decision_identity_digest=joint_probability._digest(
            [item.get("decision_id") for item in values]
        ),
        decision_membership_digest=joint_probability._digest(
            [item.get("symbol") for item in values]
        ),
        feature_schema_digest=joint_probability._digest(
            {
                "version": "joint-execution-test-features-v1",
                "base_version": "test-base-v1",
                "imputation_policy": "fixed_at_signal",
                "names": ["execution_signal", "session_cycle"],
                "base_names": [],
            }
        ),
        label_contract_digest=joint_probability._digest(["joint-label", 5]),
        benchmark_set_digest=joint_probability._digest(["benchmark"]),
        _seal=joint_outcomes._VERIFIED_OUTCOME_SEAL,
    )


def _small_estimator_config() -> joint_probability._EstimatorConfig:
    return joint_probability._EstimatorConfig(
        horizon=1,
        minimum_train_sessions=4,
        minimum_calibration_sessions=3,
        minimum_test_sessions=3,
        minimum_selection_folds=2,
        minimum_bin_sessions=1,
        calibration_bin_count=3,
        bootstrap_samples=100,
        l2_strength=0.5,
        maximum_iterations=300,
        convergence_tolerance=1e-8,
        preregistered_at="2026-01-01T00:00:00+08:00",
    )


def test_three_component_fit_exercises_complete_walk_forward_path() -> None:
    rows = _synthetic_learning_rows()
    corpus = _synthetic_corpus(rows)
    config = joint_probability._EstimatorConfig(
        horizon=1,
        minimum_train_sessions=4,
        minimum_calibration_sessions=3,
        minimum_test_sessions=3,
        minimum_selection_folds=2,
        minimum_bin_sessions=1,
        calibration_bin_count=3,
        bootstrap_samples=100,
        l2_strength=0.5,
        maximum_iterations=300,
        convergence_tolerance=1e-8,
        preregistered_at="2026-01-01T00:00:00+08:00",
    )

    evidence = joint_probability._fit_joint_rows(
        rows,
        corpus=corpus,
        generated_at="2026-03-01T12:00:00+08:00",
        config=config,
        formal_candidate=True,
    )

    assert evidence["status"] == "calibrated_shadow"
    assert evidence["fit_status"] == "fitted_oos_three_component"
    assert len(evidence["folds"]) >= 2
    assert len(evidence["predictions"]) == 36
    assert all(
        fold["all_components_applied_to_every_held_out_decision"] is True
        for fold in evidence["folds"]
    )
    assert joint_probability.verify_joint_execution_probability_evidence(evidence)


def test_three_component_fit_reports_predated_and_component_diversity_failures() -> None:
    rows = _synthetic_learning_rows()
    corpus = _synthetic_corpus(rows)
    config = joint_probability._EstimatorConfig(
        horizon=1,
        minimum_train_sessions=4,
        minimum_calibration_sessions=3,
        minimum_test_sessions=3,
        minimum_selection_folds=2,
        minimum_bin_sessions=1,
        calibration_bin_count=3,
        bootstrap_samples=100,
        preregistered_at="2027-01-01T00:00:00+08:00",
    )
    predated = joint_probability._fit_joint_rows(
        rows,
        corpus=corpus,
        generated_at="2027-03-01T12:00:00+08:00",
        config=config,
        formal_candidate=True,
    )
    assert predated["status"] == "insufficient_data"
    assert "formal_candidate_outcomes_not_strictly_after_preregistration" in predated[
        "limitations"
    ]

    no_entry_diversity = tuple(
        joint_probability._LearningRow(
            **{
                **item.__dict__,
                "entry_fill": True,
                "exit_executable": True,
                "net_positive": item.joint_action_positive,
            }
        )
        for item in rows
    )
    result = joint_probability._fit_joint_rows(
        no_entry_diversity,
        corpus=corpus,
        generated_at="2026-03-01T12:00:00+08:00",
        config=joint_probability._EstimatorConfig(
            horizon=1,
            minimum_train_sessions=4,
            minimum_calibration_sessions=3,
            minimum_test_sessions=3,
            minimum_selection_folds=2,
            minimum_bin_sessions=1,
            calibration_bin_count=3,
            bootstrap_samples=100,
            preregistered_at="2026-01-01T00:00:00+08:00",
        ),
        formal_candidate=False,
    )
    assert result["status"] == "insufficient_data"
    assert any("entry_fill_train_class_diversity" in item for item in result["limitations"])


def test_joint_estimator_config_and_freshness_fail_closed() -> None:
    for values in (
        {"horizon": 0},
        {"minimum_label_coverage": 0.0},
        {"bootstrap_samples": 99},
        {"l2_strength": 0.0},
    ):
        try:
            joint_probability._EstimatorConfig(**values)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion guard
            raise AssertionError(f"invalid config accepted: {values}")

    now = datetime.fromisoformat("2026-03-01T12:00:00+08:00")
    fresh = {
        "generated_at": now.isoformat(),
        "latest_outcome_observed_at": now.isoformat(),
        "calibration_cutoff": now.date().isoformat(),
        "oos_final_fold_reuse_forbidden": True,
    }
    assert joint_probability._joint_deployment_is_fresh(fresh, now.isoformat())
    assert not joint_probability._joint_deployment_is_fresh(
        {**fresh, "generated_at": "invalid"}, now.isoformat()
    )
    assert not joint_probability._joint_deployment_is_fresh(
        fresh, (now - timedelta(hours=1)).isoformat()
    )


def test_deployment_refit_and_new_decision_prediction_use_all_three_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _synthetic_learning_rows()
    corpus = _synthetic_corpus(rows)
    config = joint_probability._EstimatorConfig(
        horizon=1,
        minimum_train_sessions=4,
        minimum_calibration_sessions=3,
        minimum_test_sessions=3,
        minimum_selection_folds=2,
        minimum_bin_sessions=1,
        calibration_bin_count=3,
        bootstrap_samples=100,
        l2_strength=0.5,
        maximum_iterations=300,
        convergence_tolerance=1e-8,
        preregistered_at="2026-01-01T00:00:00+08:00",
    )
    evidence = joint_probability._fit_joint_rows(
        rows,
        corpus=corpus,
        generated_at="2026-03-01T12:00:00+08:00",
        config=config,
        formal_candidate=True,
    )
    evidence["selection_qualified"] = True
    evidence = joint_probability._seal_evidence(evidence)
    study = joint_probability.VerifiedJointExecutionProbabilityStudy(
        joint_probability.canonical_json_bytes(evidence).decode("utf-8"),
        evidence_digest=str(evidence["evidence_digest"]),
        _seal=joint_probability._VERIFIED_STUDY_SEAL,
    )
    now = probability.utc_now().astimezone(joint_probability._SHANGHAI)
    authorization = probability.VerifiedProbabilityFilterAuthorization(
        encoded_payload=json.dumps({"execution_validation": {}}, sort_keys=True),
        generated_at=(now - timedelta(minutes=2)).isoformat(),
        integrity_digest=joint_probability._digest(["authorization"]),
        _seal=probability._VERIFIED_AUTHORIZATION_SEAL,
    )
    context = joint_probability._DeploymentBuildContext(
        evidence=evidence,
        generated=now,
        rows=rows,
        latest_observed=now - timedelta(minutes=1),
    )
    monkeypatch.setattr(joint_probability, "_EstimatorConfig", lambda: config)
    fitted = joint_probability._fit_deployment_rows(corpus, context)
    monkeypatch.setattr(
        probability,
        "probability_deployment_joint_bindings",
        lambda _payload: {
            "joint_execution_evidence_digest": joint_probability._digest(
                ["joint-execution"]
            ),
            "joint_execution_assessment_digest": joint_probability._digest(
                ["assessments"]
            ),
            "joint_execution_estimand_digest": joint_probability._digest(["estimand"]),
        },
    )
    payload = joint_probability._deployment_payload(
        corpus,
        study,
        authorization,
        context,
        fitted,
    )
    deployment = joint_probability.VerifiedJointExecutionDeploymentEstimator(
        joint_probability.canonical_json_bytes(payload).decode("utf-8"),
        integrity_digest=joint_probability._digest(["deployment", payload]),
        _seal=joint_probability._VERIFIED_DEPLOYMENT_SEAL,
    )

    estimate = joint_probability.predict_joint_execution_probability(
        study,
        deployment,
        {"execution_signal": 2.5, "session_cycle": 0.1},
        sample_id="999:600519.SH:5:net_excess_positive",
        as_of=now.isoformat(),
    )

    assert 0.0 <= estimate["probability"] <= 1.0
    assert set(estimate["component_probabilities"]) == set(
        joint_probability.JOINT_EXECUTION_COMPONENTS
    )
    assert estimate["deployment_artifact_digest"] == deployment.integrity_digest
    assert joint_probability.joint_execution_deployment_is_fresh(
        deployment,
        as_of=now.isoformat(),
    )
    assert not joint_probability.joint_execution_deployment_is_fresh(
        object(),
        as_of=now.isoformat(),
    )
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="feature schema",
    ):
        joint_probability.predict_joint_execution_probability(
            study,
            deployment,
            {"wrong_feature": 1.0},
            sample_id="bad",
            as_of=now.isoformat(),
        )

    feature_schema = {
        "version": corpus.feature_version,
        "base_version": "synthetic-base-features-v1",
        "imputation_policy": "fixed_at_signal",
        "names": list(corpus.feature_names),
        "base_names": [],
    }
    feature_contract = joint_probability._feature_contract(feature_schema)
    current_deployment_payload = {
        **deployment.payload,
        "feature_contract_digest": joint_probability._digest(feature_contract),
    }
    current_deployment = joint_probability.VerifiedJointExecutionDeploymentEstimator(
        joint_probability.canonical_json_bytes(current_deployment_payload).decode("utf-8"),
        integrity_digest=joint_probability._digest(
            ["current-deployment", current_deployment_payload]
        ),
        _seal=joint_probability._VERIFIED_DEPLOYMENT_SEAL,
    )
    features = {"execution_signal": 2.5, "session_cycle": 0.1}
    source_records = [
        {
            "decision_id": "999:600519.SH",
            "symbol": "600519.SH",
            "result_status": "success",
            "feature_availability": "complete",
            "features": features,
            "feature_vector_digest": joint_probability._digest(features),
            "record_digest": joint_probability._digest(
                ["999:600519.SH", features]
            ),
        }
    ]
    current_source = joint_source.VerifiedJointExecutionSourceCorpus(
        joint_probability.canonical_json_bytes(source_records).decode("utf-8"),
        artifact_digest=joint_probability._digest(["current-source"]),
        run_id=999,
        signal_session=now.date().isoformat(),
        source_snapshot_digest=joint_probability._digest(["current-snapshot"]),
        decision_identity_digest=joint_probability._digest(["999:600519.SH"]),
        decision_membership_digest=joint_probability._digest(["600519.SH"]),
        decision_frozen_at=(now - timedelta(seconds=1)).isoformat(),
        feature_schema_digest=joint_probability._digest(feature_schema),
        encoded_feature_schema=joint_probability.canonical_json_bytes(
            feature_schema
        ).decode("utf-8"),
        _seal=joint_source._VERIFIED_SOURCE_SEAL,
    )
    current_artifact = joint_probability.build_joint_execution_current_prediction_artifact(
        current_source,
        study,
        current_deployment,
        generated_at=now.isoformat(),
    )
    verified_current = joint_probability.verify_joint_execution_current_prediction_artifact(
        current_artifact,
        source=current_source,
        study=study,
        deployment=current_deployment,
    )
    assert len(verified_current) == 1
    assert verified_current[0]["symbol"] == "600519.SH"
    assert len(verified_current[:]) == 1
    assert verified_current.run_id == current_source.run_id

    for changed in (
        {**current_artifact, "extra": True},
        {**current_artifact, "schema_version": "wrong"},
        {**current_artifact, "integrity": {}},
        {
            **current_artifact,
            "integrity": {
                **current_artifact["integrity"],
                "integrity_digest": "0" * 64,
            },
        },
    ):
        with pytest.raises(
            joint_probability.JointExecutionProbabilityError,
            match="strict verification",
        ):
            joint_probability.verify_joint_execution_current_prediction_artifact(
                changed,
                source=current_source,
                study=study,
                deployment=current_deployment,
            )
    changed_payload = {
        **current_artifact["payload"],
        "record_count": 2,
    }
    replay_mismatch = joint_probability.seal_joint_execution_current_prediction_artifact(
        changed_payload,
        generated_at=now.isoformat(),
    )
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="strict verification",
    ):
        joint_probability.verify_joint_execution_current_prediction_artifact(
            replay_mismatch,
            source=current_source,
            study=study,
            deployment=current_deployment,
        )
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="strict verification",
    ):
        joint_probability.verify_joint_execution_current_prediction_artifact(
            current_artifact,
            source=current_source,
            study=study,
            deployment=current_deployment,
            as_of=(now + timedelta(hours=37)).isoformat(),
        )

    deployment_artifact = joint_probability.seal_joint_execution_deployment_artifact(
        current_deployment_payload,
        generated_at=now.isoformat(),
    )
    monkeypatch.setattr(
        joint_probability,
        "fit_joint_execution_deployment_estimator",
        lambda *_args, **_kwargs: deployment_artifact,
    )
    verified_deployment = joint_probability.verify_joint_execution_deployment_artifact(
        deployment_artifact,
        corpus=corpus,
        study=study,
        authorization=authorization,
        as_of=now.isoformat(),
    )
    assert verified_deployment.payload == current_deployment_payload
    for changed in (
        {**deployment_artifact, "extra": True},
        {**deployment_artifact, "schema_version": "wrong"},
        {**deployment_artifact, "integrity": {}},
        {
            **deployment_artifact,
            "integrity": {
                **deployment_artifact["integrity"],
                "integrity_digest": "0" * 64,
            },
        },
    ):
        with pytest.raises(
            joint_probability.JointExecutionProbabilityError,
            match="strict verification",
        ):
            joint_probability.verify_joint_execution_deployment_artifact(
                changed,
                corpus=corpus,
                study=study,
                authorization=authorization,
                as_of=now.isoformat(),
            )
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="strict verification",
    ):
        joint_probability.verify_joint_execution_deployment_artifact(
            deployment_artifact,
            corpus=corpus,
            study=study,
            authorization=authorization,
            as_of=(now + timedelta(hours=37)).isoformat(),
        )


def test_deployment_envelope_and_build_context_reject_unverified_inputs() -> None:
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="timestamp mismatch",
    ):
        joint_probability.seal_joint_execution_deployment_artifact(
            {"generated_at": "2026-01-01T00:00:00+08:00"},
            generated_at="2026-01-02T00:00:00+08:00",
        )
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="verified learning and study",
    ):
        joint_probability._deployment_build_context(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),
            generated_at="2026-01-02T00:00:00+08:00",
        )


def test_joint_probability_tokens_and_evidence_verification_fail_closed() -> None:
    with pytest.raises(TypeError, match="strict token join"):
        joint_probability.VerifiedJointExecutionLearningCorpus(
            "[]",
            "[]",
            corpus_digest="a" * 64,
            feature_contract_digest="b" * 64,
            feature_version="test",
            feature_names=("feature",),
            label_contract_digest="c" * 64,
        )
    with pytest.raises(TypeError, match="strict replay"):
        joint_probability.VerifiedJointExecutionProbabilityStudy(
            "{}",
            evidence_digest="a" * 64,
        )
    with pytest.raises(TypeError, match="strict replay"):
        joint_probability.VerifiedJointExecutionDeploymentEstimator(
            "{}",
            integrity_digest="a" * 64,
        )
    with pytest.raises(TypeError, match="strict replay"):
        joint_probability.VerifiedJointExecutionCurrentPredictionCorpus(
            "[]",
            artifact_digest="a" * 64,
            deployment_artifact_digest="b" * 64,
            decision_identity_digest="c" * 64,
            decision_membership_digest="d" * 64,
            generated_at="2026-08-23T12:00:00+08:00",
            run_id=1,
            signal_session="2026-08-23",
            source_artifact_digest="e" * 64,
            source_snapshot_digest="f" * 64,
        )

    rows = _synthetic_learning_rows()
    corpus = _synthetic_corpus(rows)
    assert corpus[0]["sample_id"] == rows[0].sample_id
    assert len(corpus[:1]) == 1
    assert len(list(corpus)) == len(corpus)
    assert len(corpus.bindings) == 20

    evidence = joint_probability._fit_joint_rows(
        rows,
        corpus=corpus,
        generated_at="2026-03-01T12:00:00+08:00",
        config=_small_estimator_config(),
        formal_candidate=True,
    )
    assert joint_probability.verify_joint_execution_probability_evidence(evidence)
    study = joint_probability.VerifiedJointExecutionProbabilityStudy(
        joint_probability.canonical_json_bytes(evidence).decode("utf-8"),
        evidence_digest=str(evidence["evidence_digest"]),
        _seal=joint_probability._VERIFIED_STUDY_SEAL,
    )
    assert study["status"] == "calibrated_shadow"
    assert "status" in set(iter(study))
    assert len(study) == len(evidence)
    deployment = joint_probability.VerifiedJointExecutionDeploymentEstimator(
        joint_probability.canonical_json_bytes({"value": 1}).decode("utf-8"),
        integrity_digest="a" * 64,
        _seal=joint_probability._VERIFIED_DEPLOYMENT_SEAL,
    )
    assert deployment["value"] == 1
    assert list(deployment) == ["value"]
    assert len(deployment) == 1

    invalid_evidence: list[dict[str, object]] = []
    missing_key = deepcopy(evidence)
    missing_key.pop("model_digest")
    invalid_evidence.append(missing_key)
    wrong_schema = deepcopy(evidence)
    wrong_schema["schema_version"] = "unsupported"
    invalid_evidence.append(joint_probability._seal_evidence(wrong_schema))
    invalid_time = deepcopy(evidence)
    invalid_time["generated_at"] = "bad"
    invalid_evidence.append(joint_probability._seal_evidence(invalid_time))
    wrong_digest = deepcopy(evidence)
    wrong_digest["evidence_digest"] = "0" * 64
    invalid_evidence.append(wrong_digest)
    wrong_containers = deepcopy(evidence)
    wrong_containers["predictions"] = {}
    invalid_evidence.append(joint_probability._seal_evidence(wrong_containers))
    insufficient_with_model = deepcopy(evidence)
    insufficient_with_model.update(
        {
            "status": "insufficient_data",
            "predictions": [],
            "folds": [],
            "model": {},
        }
    )
    invalid_evidence.append(
        joint_probability._seal_evidence(insufficient_with_model)
    )
    wrong_contract = deepcopy(evidence)
    wrong_contract["contract"]["label"]["target"] = "wrong"  # type: ignore[index]
    invalid_evidence.append(joint_probability._seal_evidence(wrong_contract))
    exposed_probability = deepcopy(evidence)
    exposed_probability["probability"] = 0.5
    invalid_evidence.append(joint_probability._seal_evidence(exposed_probability))
    inconsistent_status = deepcopy(evidence)
    inconsistent_status["status"] = "published"
    invalid_evidence.append(joint_probability._seal_evidence(inconsistent_status))
    wrong_product = deepcopy(evidence)
    prediction = wrong_product["predictions"][0]  # type: ignore[index]
    prediction["probability"] = float(prediction["probability"]) + 0.01
    invalid_evidence.append(joint_probability._seal_evidence(wrong_product))
    incomplete_fold = deepcopy(evidence)
    fold = incomplete_fold["folds"][0]  # type: ignore[index]
    fold["prediction_count"] = int(fold["prediction_count"]) - 1
    invalid_evidence.append(joint_probability._seal_evidence(incomplete_fold))
    for changed in invalid_evidence:
        with pytest.raises(joint_probability.JointExecutionProbabilityError):
            joint_probability.verify_joint_execution_probability_evidence(changed)

    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="verified learning corpus",
    ):
        joint_probability.fit_joint_execution_probability(  # type: ignore[arg-type]
            object(),
            generated_at="2026-03-01T12:00:00+08:00",
        )
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="predates outcomes",
    ):
        joint_probability.fit_joint_execution_probability(
            corpus,
            generated_at="2026-01-01T12:00:00+08:00",
        )


def test_joint_probability_pairing_oos_and_learning_row_guards() -> None:
    source_one = _empty_source_token(1, signal_session="2026-01-02")
    source_two = _empty_source_token(2, signal_session="2026-01-03")
    outcome_one = _empty_outcome_token(1, signal_session="2026-01-02")
    outcome_two = _empty_outcome_token(2, signal_session="2026-01-03")
    source_index, outcome_index = joint_probability._verified_token_indexes(  # noqa: SLF001
        [source_one, source_two],
        [outcome_one, outcome_two],
    )
    assert set(source_index) == {1, 2}
    assert set(outcome_index) == {1, 2}

    invalid_pairings = (
        ([], []),
        ([object()], [outcome_one]),
        ([source_one, source_one], [outcome_one, outcome_two]),
        ([source_one], [object()]),
        ([source_one, source_two], [outcome_one, outcome_one]),
        ([source_one], [outcome_two]),
    )
    for sources, outcomes in invalid_pairings:
        with pytest.raises(joint_probability.JointExecutionProbabilityError):
            joint_probability._verified_token_indexes(sources, outcomes)  # type: ignore[arg-type]  # noqa: SLF001

    joint_probability._validate_token_pair(source_one, outcome_one)  # noqa: SLF001
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="boundary mismatch",
    ):
        joint_probability._validate_token_pair(source_one, outcome_two)  # noqa: SLF001
    binding = joint_probability._run_binding(source_one, outcome_one, 0)  # noqa: SLF001
    assert binding["run_id"] == 1
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="multiple runs",
    ):
        joint_probability._require_unique_signal_sessions(  # noqa: SLF001
            [binding, {**binding, "run_id": 2}]
        )

    rows = _synthetic_learning_rows()
    corpus = _synthetic_corpus(rows)
    evidence = joint_probability._fit_joint_rows(
        rows,
        corpus=corpus,
        generated_at="2026-03-01T12:00:00+08:00",
        config=_small_estimator_config(),
        formal_candidate=True,
    )
    evidence["selection_qualified"] = True
    evidence = joint_probability._seal_evidence(evidence)
    study = joint_probability.VerifiedJointExecutionProbabilityStudy(
        joint_probability.canonical_json_bytes(evidence).decode("utf-8"),
        evidence_digest=str(evidence["evidence_digest"]),
        _seal=joint_probability._VERIFIED_STUDY_SEAL,
    )
    assert joint_probability._verified_oos_study(study, corpus) == evidence  # noqa: SLF001
    with pytest.raises(joint_probability.JointExecutionProbabilityError):
        joint_probability._verified_oos_study(object(), corpus)  # type: ignore[arg-type]  # noqa: SLF001
    with pytest.raises(joint_probability.JointExecutionProbabilityError):
        joint_probability._verified_oos_study(study, object())  # type: ignore[arg-type]  # noqa: SLF001
    unselected_value = {**evidence, "selection_qualified": False}
    unselected_value = joint_probability._seal_evidence(unselected_value)
    unselected = joint_probability.VerifiedJointExecutionProbabilityStudy(
        joint_probability.canonical_json_bytes(unselected_value).decode("utf-8"),
        evidence_digest=str(unselected_value["evidence_digest"]),
        _seal=joint_probability._VERIFIED_STUDY_SEAL,
    )
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="selected exact-corpus",
    ):
        joint_probability._verified_oos_study(unselected, corpus)  # noqa: SLF001
    assert joint_probability._oos_predictions(evidence)  # noqa: SLF001
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="no predictions",
    ):
        joint_probability._oos_predictions({"predictions": []})  # noqa: SLF001
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="do not bind",
    ):
        joint_probability._validate_oos_source_bindings(  # noqa: SLF001
            corpus,
            evidence,
            {1: source_one},
            {1: outcome_one},
        )

    assert joint_probability._joint_sample_identity(  # noqa: SLF001
        "1:600519.SH:5:net_excess_positive"
    ) == (1, "600519.SH")
    for sample_id in (
        "bad",
        "1:600519.SH:1:net_excess_positive",
        "1:BAD:5:net_excess_positive",
    ):
        with pytest.raises(joint_probability.JointExecutionProbabilityError):
            joint_probability._joint_sample_identity(sample_id)  # noqa: SLF001

    assert joint_probability._unique_mapping_index(  # noqa: SLF001
        [{"sample_id": "one"}],
        "sample_id",
        "sample",
    ) == {"one": {"sample_id": "one"}}
    for indexed_rows in ([{}], [{"sample_id": "one"}, {"sample_id": "one"}]):
        with pytest.raises(joint_probability.JointExecutionProbabilityError):
            joint_probability._unique_mapping_index(  # noqa: SLF001
                indexed_rows,
                "sample_id",
                "sample",
            )
    assert joint_probability._positive_int_mapping_index(  # noqa: SLF001
        [{"fold_id": 1}],
        "fold_id",
        "fold",
    ) == {1: {"fold_id": 1}}
    for indexed_rows in (
        [{"fold_id": True}],
        [{"fold_id": 1}, {"fold_id": 1}],
    ):
        with pytest.raises(joint_probability.JointExecutionProbabilityError):
            joint_probability._positive_int_mapping_index(  # noqa: SLF001
                indexed_rows,
                "fold_id",
                "fold",
            )

    base = rows[0].payload()
    joint_probability._validate_learning_rows([base])  # noqa: SLF001
    invalid_rows = (
        [],
        [base, base],
        [base, {**base, "sample_id": "other", "features": {"other": 1.0}}],
    )
    for values in invalid_rows:
        with pytest.raises(joint_probability.JointExecutionProbabilityError):
            joint_probability._validate_learning_rows(values)  # noqa: SLF001
    invalid_labels = (
        {**base, "entry_fill": 1},
        {
            **base,
            "entry_fill": False,
            "exit_executable": True,
            "net_positive": None,
            "joint_action_positive": False,
        },
        {
            **base,
            "entry_fill": True,
            "exit_executable": False,
            "net_positive": True,
            "joint_action_positive": False,
        },
        {
            **base,
            "entry_fill": True,
            "exit_executable": True,
            "net_positive": None,
            "joint_action_positive": False,
        },
    )
    for value in invalid_labels:
        with pytest.raises(joint_probability.JointExecutionProbabilityError):
            joint_probability._validate_learning_labels(value)  # noqa: SLF001


def test_joint_probability_helper_contracts_cover_strict_edge_cases() -> None:
    assert joint_probability.joint_execution_preregistration_contract()[
        "candidate_id"
    ] == joint_probability.JOINT_EXECUTION_CANDIDATE_ID
    assert joint_probability._feature_contract(  # noqa: SLF001
        {
            "version": "v1",
            "base_version": "base-v1",
            "imputation_policy": "fixed",
            "names": ["a", "b"],
            "base_names": ["a"],
        }
    )["per_signal_imputation_values_allowed"] is True
    for schema in (
        {},
        {
            "version": "v1",
            "base_version": "base-v1",
            "imputation_policy": "fixed",
            "names": ["b", "a"],
            "base_names": [],
        },
    ):
        with pytest.raises(joint_probability.JointExecutionProbabilityError):
            joint_probability._feature_contract(schema)  # noqa: SLF001
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="H5"):
        joint_probability._selected_horizon({"horizons": []})  # noqa: SLF001
    with pytest.raises(
        joint_probability.JointExecutionProbabilityError,
        match="base-cost",
    ):
        joint_probability._selected_scenario({"scenarios": []})  # noqa: SLF001

    complete_artifacts = {
        name: {"component": name} for name in joint_probability.JOINT_EXECUTION_COMPONENTS
    }
    assert set(
        joint_probability._component_artifact_index(  # noqa: SLF001
            {"component_artifacts": complete_artifacts}
        )
    ) == set(joint_probability.JOINT_EXECUTION_COMPONENTS)
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="incomplete"):
        joint_probability._component_artifact_index(  # noqa: SLF001
            {"component_artifacts": {}}
        )
    wrong_artifacts = deepcopy(complete_artifacts)
    wrong_artifacts["entry_fill"]["component"] = "wrong"
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="identity"):
        joint_probability._component_artifact_index(  # noqa: SLF001
            {"component_artifacts": wrong_artifacts}
        )

    rows = _synthetic_learning_rows()
    corpus = _synthetic_corpus(rows)
    evidence = joint_probability._fit_joint_rows(
        rows,
        corpus=corpus,
        generated_at="2026-03-01T12:00:00+08:00",
        config=_small_estimator_config(),
        formal_candidate=True,
    )
    assert joint_probability._deployment_corpus_extends_selected_study(  # noqa: SLF001
        corpus,
        evidence,
    )
    assert not joint_probability._deployment_corpus_extends_selected_study(  # noqa: SLF001
        corpus,
        {**evidence, "feature_version": "wrong"},
    )
    assert not joint_probability._deployment_corpus_extends_selected_study(  # noqa: SLF001
        corpus,
        {},
    )
    bindings = joint_probability._selected_study_bindings(evidence)  # noqa: SLF001
    assert bindings == corpus.bindings
    with pytest.raises(ValueError, match="bindings are invalid"):
        joint_probability._selected_study_bindings(  # noqa: SLF001
            {**evidence, "source_binding_digest": "0" * 64}
        )
    assert not joint_probability._deployment_bindings_extend_selection(  # noqa: SLF001
        corpus,
        evidence,
        [bindings[0], bindings[0]],
    )
    missing_binding = {**bindings[0], "run_id": 999}
    assert not joint_probability._deployment_bindings_extend_selection(  # noqa: SLF001
        corpus,
        evidence,
        [missing_binding],
    )
    assert joint_probability._selection_limitations({  # noqa: SLF001
        "passed": True,
        "gates": {},
    }) == [
        "shadow_only_no_production_ranking_effect",
        "filter_requires_separate_exact_authorization",
        "deployment_requires_fresh_three_component_refit",
    ]
    assert "selection_gate_failed:skill" in joint_probability._selection_limitations(  # noqa: SLF001
        {"passed": False, "gates": {"skill": False}}
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="calibration block"):
        joint_probability._joint_deployment_split(  # noqa: SLF001
            rows[:2],
            _small_estimator_config(),
        )
    split = joint_probability._joint_deployment_split(  # noqa: SLF001
        rows,
        _small_estimator_config(),
    )
    partitions = joint_probability._joint_deployment_partitions(  # noqa: SLF001
        rows,
        split,
    )
    assert partitions["train"]
    assert partitions["calibration"]
    assert partitions["test"] == ()

    invalid_calls = (
        (joint_probability._positive_integer, (0, "positive")),  # noqa: SLF001
        (joint_probability._numeric_features, ({},)),  # noqa: SLF001
        (joint_probability._numeric_features, ({"b": 1.0, "a": 2.0},)),  # noqa: SLF001
        (joint_probability._finite_float, (True, "finite")),  # noqa: SLF001
        (joint_probability._finite_float, (math.inf, "finite")),  # noqa: SLF001
        (joint_probability._float_pair, ([1.0], "pair")),  # noqa: SLF001
        (joint_probability._smoothed_rate, ([],)),  # noqa: SLF001
        (joint_probability._mean, ([],)),  # noqa: SLF001
        (joint_probability._mapping, ([], "mapping")),  # noqa: SLF001
    )
    for function, arguments in invalid_calls:
        with pytest.raises(joint_probability.JointExecutionProbabilityError):
            function(*arguments)
    for value in ("bad", "2026-08-23T12:00:00"):
        with pytest.raises(ValueError):
            joint_probability._timestamp(value, "timestamp")  # noqa: SLF001
    assert joint_probability._optional_float(None, "optional") is None  # noqa: SLF001
    assert joint_probability._float_pair([0.1, 0.2], "pair") == (0.1, 0.2)  # noqa: SLF001
    assert joint_probability._smoothed_rate([0, 1]) == 0.5  # noqa: SLF001
    assert joint_probability._mean([1.0, 3.0]) == 2.0  # noqa: SLF001


def test_joint_probability_exact_source_outcome_and_oos_identity_bindings() -> None:
    features = {"execution_signal": 1.0, "session_cycle": 0.1}
    source_record = {
        "decision_id": "1:600519.SH",
        "symbol": "600519.SH",
        "features": features,
        "record_digest": "a" * 64,
    }
    observed = {
        "entry_fill": True,
        "exit_executable": True,
        "net_positive": True,
        "joint_action_positive": True,
        "net_return": 0.02,
        "net_excess_return": 0.01,
        "observed_at": "2026-01-10T20:00:00+08:00",
    }
    outcome_record = {
        "decision_id": source_record["decision_id"],
        "symbol": source_record["symbol"],
        "source_record_digest": source_record["record_digest"],
        "record_digest": "b" * 64,
        "horizons": [
            {
                "horizon": joint_probability.JOINT_EXECUTION_HORIZON,
                "holding_path": {"path_digest": "c" * 64},
                "scenarios": [
                    {
                        "profile_name": joint_probability.JOINT_EXECUTION_PRIMARY_PROFILE,
                        "observed_outcome": observed,
                        "benchmark_series_digest": "d" * 64,
                    }
                ],
            }
        ],
    }
    source = _empty_source_token(
        1,
        signal_session="2026-01-02",
        records=[source_record],
    )
    outcome = _empty_outcome_token(
        1,
        signal_session="2026-01-02",
        records=[outcome_record],
    )
    joined = joint_probability._join_run_rows(source, outcome)  # noqa: SLF001
    assert len(joined) == 1
    assert joined[0].joint_action_positive is True
    assert joined[0].net_excess_return == 0.01

    missing_outcome = _empty_outcome_token(
        1,
        signal_session="2026-01-02",
        records=[],
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="incomplete"):
        joint_probability._join_run_rows(source, missing_outcome)  # noqa: SLF001
    wrong_digest_outcome = _empty_outcome_token(
        1,
        signal_session="2026-01-02",
        records=[{**outcome_record, "source_record_digest": "0" * 64}],
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="digest"):
        joint_probability._join_run_rows(source, wrong_digest_outcome)  # noqa: SLF001
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="H5"):
        joint_probability._join_run_rows(  # noqa: SLF001
            source,
            _empty_outcome_token(
                1,
                signal_session="2026-01-02",
                records=[{**outcome_record, "horizons": []}],
            ),
        )
    wrong_scenarios = deepcopy(outcome_record)
    wrong_scenarios["horizons"][0]["scenarios"] = []  # type: ignore[index]
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="base-cost"):
        joint_probability._join_run_rows(  # noqa: SLF001
            source,
            _empty_outcome_token(
                1,
                signal_session="2026-01-02",
                records=[wrong_scenarios],
            ),
        )

    sample_id = "1:600519.SH:5:net_excess_positive"
    predictions = [{"sample_id": sample_id, "session_date": "2026-01-02"}]
    group_digests = joint_probability._verify_oos_groups_cover_source_decisions(  # noqa: SLF001
        predictions,
        {1: source},
    )
    assert (1, "2026-01-02") in group_digests
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="signal source"):
        joint_probability._verify_oos_groups_cover_source_decisions(  # noqa: SLF001
            [{"sample_id": sample_id, "session_date": "2026-01-03"}],
            {1: source},
        )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="outside"):
        joint_probability._verify_oos_groups_cover_source_decisions(  # noqa: SLF001
            [
                {
                    "sample_id": "1:300750.SZ:5:net_excess_positive",
                    "session_date": "2026-01-02",
                }
            ],
            {1: source},
        )
    two_record_source = _empty_source_token(
        1,
        signal_session="2026-01-02",
        records=[source_record, {**source_record, "decision_id": "1:300750.SZ", "symbol": "300750.SZ"}],
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="every fixed"):
        joint_probability._verify_oos_groups_cover_source_decisions(  # noqa: SLF001
            predictions,
            {1: two_record_source},
        )

    learning = joined[0].payload()
    prediction = {
        "sample_id": learning["sample_id"],
        "session_date": learning["session_date"],
        "source_record_digest": source_record["record_digest"],
        "outcome_record_digest": outcome_record["record_digest"],
        "holding_path_digest": "c" * 64,
        "benchmark_series_digest": "d" * 64,
        "feature_vector_digest": joint_probability._digest(features),
        "component_outcomes": {
            "entry_fill": 1,
            "exit_executable": 1,
            "net_positive": 1,
        },
        "outcome": 1,
    }
    horizon = outcome_record["horizons"][0]  # type: ignore[index]
    scenario = horizon["scenarios"][0]
    joint_probability._verify_learning_prediction_bindings(  # noqa: SLF001
        prediction,
        learning,
        source_record,
        outcome_record,
        horizon["holding_path"],
        scenario,
        observed,
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="learning row"):
        joint_probability._verify_learning_prediction_bindings(  # noqa: SLF001
            prediction,
            {**learning, "source_record_digest": "0" * 64},
            source_record,
            outcome_record,
            horizon["holding_path"],
            scenario,
            observed,
        )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="official labels"):
        joint_probability._verify_learning_prediction_bindings(  # noqa: SLF001
            {**prediction, "outcome": 0},
            learning,
            source_record,
            outcome_record,
            horizon["holding_path"],
            scenario,
            observed,
        )


def test_current_prediction_context_and_fixed_population_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = probability.utc_now().astimezone(joint_probability._SHANGHAI)
    session = now.date().isoformat()
    features = {"execution_signal": 1.0, "session_cycle": 0.1}
    source_record = {
        "decision_id": "1000:600519.SH",
        "symbol": "600519.SH",
        "result_status": "success",
        "feature_availability": "complete",
        "features": features,
        "feature_vector_digest": joint_probability._digest(features),
        "record_digest": "a" * 64,
    }
    source = _empty_source_token(
        1000,
        signal_session=session,
        records=[source_record],
    )
    source.decision_frozen_at = (now - timedelta(minutes=1)).isoformat()
    study = joint_probability.VerifiedJointExecutionProbabilityStudy(
        joint_probability.canonical_json_bytes(
            {"selection_qualified": True}
        ).decode("utf-8"),
        evidence_digest="b" * 64,
        _seal=joint_probability._VERIFIED_STUDY_SEAL,
    )
    feature_contract = joint_probability._feature_contract(source.feature_schema)
    deployment_payload = {
        "contract_version": joint_probability.JOINT_EXECUTION_DEPLOYMENT_CONTRACT_VERSION,
        "study_evidence_digest": study.evidence_digest,
        "generated_at": (now - timedelta(minutes=2)).isoformat(),
        "latest_outcome_observed_at": (now - timedelta(minutes=3)).isoformat(),
        "calibration_cutoff": (now - timedelta(days=1)).date().isoformat(),
        "oos_final_fold_reuse_forbidden": True,
        "latest_signal_session": (now - timedelta(days=1)).date().isoformat(),
        "feature_contract_digest": joint_probability._digest(feature_contract),
        "feature_names": list(features),
    }
    deployment = joint_probability.VerifiedJointExecutionDeploymentEstimator(
        joint_probability.canonical_json_bytes(deployment_payload).decode("utf-8"),
        integrity_digest="c" * 64,
        _seal=joint_probability._VERIFIED_DEPLOYMENT_SEAL,
    )
    context = joint_probability._current_prediction_build_context(  # noqa: SLF001
        source,
        study,
        deployment,
        generated_at=now.isoformat(),
    )
    assert context.feature_contract_digest == deployment_payload["feature_contract_digest"]

    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="source token"):
        joint_probability._current_prediction_build_context(  # type: ignore[arg-type]  # noqa: SLF001
            object(),
            study,
            deployment,
            generated_at=now.isoformat(),
        )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="study/deployment"):
        joint_probability._current_prediction_build_context(  # type: ignore[arg-type]  # noqa: SLF001
            source,
            object(),
            deployment,
            generated_at=now.isoformat(),
        )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="after freeze"):
        joint_probability._current_prediction_build_context(  # noqa: SLF001
            source,
            study,
            deployment,
            generated_at=(now - timedelta(minutes=2)).isoformat(),
        )
    stale_deployment = joint_probability.VerifiedJointExecutionDeploymentEstimator(
        joint_probability.canonical_json_bytes(
            {**deployment_payload, "generated_at": "bad"}
        ).decode("utf-8"),
        integrity_digest="d" * 64,
        _seal=joint_probability._VERIFIED_DEPLOYMENT_SEAL,
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="fresh"):
        joint_probability._current_prediction_build_context(  # noqa: SLF001
            source,
            study,
            stale_deployment,
            generated_at=now.isoformat(),
        )
    mismatch_deployment = joint_probability.VerifiedJointExecutionDeploymentEstimator(
        joint_probability.canonical_json_bytes(
            {**deployment_payload, "feature_contract_digest": "0" * 64}
        ).decode("utf-8"),
        integrity_digest="e" * 64,
        _seal=joint_probability._VERIFIED_DEPLOYMENT_SEAL,
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="feature contract"):
        joint_probability._current_prediction_build_context(  # noqa: SLF001
            source,
            study,
            mismatch_deployment,
            generated_at=now.isoformat(),
        )

    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="verified study"):
        joint_probability._joint_prediction_inputs(  # type: ignore[arg-type]  # noqa: SLF001
            object(),
            deployment,
            features,
            now.isoformat(),
        )
    wrong_binding = joint_probability.VerifiedJointExecutionDeploymentEstimator(
        joint_probability.canonical_json_bytes(
            {**deployment_payload, "study_evidence_digest": "0" * 64}
        ).decode("utf-8"),
        integrity_digest="f" * 64,
        _seal=joint_probability._VERIFIED_DEPLOYMENT_SEAL,
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="selected fresh"):
        joint_probability._joint_prediction_inputs(  # noqa: SLF001
            study,
            wrong_binding,
            features,
            now.isoformat(),
        )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="timestamp mismatch"):
        joint_probability.seal_joint_execution_current_prediction_artifact(
            {"generated_at": (now - timedelta(minutes=1)).isoformat()},
            generated_at=now.isoformat(),
        )

    monkeypatch.setattr(
        joint_probability,
        "predict_joint_execution_probability",
        lambda *_args, **_kwargs: {"feature_vector_digest": "0" * 64},
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="feature vector"):
        joint_probability._joint_current_prediction_record(  # noqa: SLF001
            source,
            source_record,
            study,
            deployment,
            generated_at=now.isoformat(),
        )
    monkeypatch.setattr(
        joint_probability,
        "_joint_current_prediction_record",
        lambda *_args, **_kwargs: {
            "symbol": "600519.SH",
            "decision_id": "1000:600519.SH",
        },
    )
    source.decision_membership_digest = "0" * 64
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="fixed decision set"):
        joint_probability._current_prediction_records(  # noqa: SLF001
            source,
            study,
            deployment,
            now,
        )


def test_deployment_authority_timing_and_oos_orchestration_are_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _synthetic_learning_rows()
    corpus = _synthetic_corpus(rows)
    evidence = joint_probability._fit_joint_rows(
        rows,
        corpus=corpus,
        generated_at="2026-01-21T20:01:00+08:00",
        config=_small_estimator_config(),
        formal_candidate=True,
    )
    evidence["selection_qualified"] = True
    evidence = joint_probability._seal_evidence(evidence)
    study = joint_probability.VerifiedJointExecutionProbabilityStudy(
        joint_probability.canonical_json_bytes(evidence).decode("utf-8"),
        evidence_digest=str(evidence["evidence_digest"]),
        _seal=joint_probability._VERIFIED_STUDY_SEAL,
    )
    authorization = probability.VerifiedProbabilityFilterAuthorization(
        encoded_payload="{}",
        generated_at="2026-01-21T20:01:00+08:00",
        integrity_digest="a" * 64,
        _seal=probability._VERIFIED_AUTHORIZATION_SEAL,
    )
    monkeypatch.setattr(
        probability,
        "probability_filter_qualified",
        lambda *_args: True,
    )
    context = joint_probability._deployment_build_context(  # noqa: SLF001
        corpus,
        study,
        authorization,
        generated_at="2026-01-21T20:02:00+08:00",
    )
    assert context.latest_observed.isoformat() == "2026-01-21T20:00:00+08:00"

    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="authorization"):
        joint_probability._deployment_build_context(  # noqa: SLF001
            corpus,
            study,
            object(),
            generated_at="2026-01-21T20:02:00+08:00",
        )
    monkeypatch.setattr(
        probability,
        "probability_filter_qualified",
        lambda *_args: False,
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="not authorized"):
        joint_probability._deployment_build_context(  # noqa: SLF001
            corpus,
            study,
            authorization,
            generated_at="2026-01-21T20:02:00+08:00",
        )
    monkeypatch.setattr(
        probability,
        "probability_filter_qualified",
        lambda *_args: True,
    )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="predates"):
        joint_probability._deployment_build_context(  # noqa: SLF001
            corpus,
            study,
            authorization,
            generated_at="2026-01-21T19:59:00+08:00",
        )
    with pytest.raises(joint_probability.JointExecutionProbabilityError, match="stale"):
        joint_probability._deployment_build_context(  # noqa: SLF001
            corpus,
            study,
            authorization,
            generated_at="2026-01-23T20:01:00+08:00",
        )

    generated_at = "2026-01-21T20:02:00+08:00"
    monkeypatch.setattr(
        joint_probability,
        "_deployment_build_context",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(
        joint_probability,
        "_fit_deployment_rows",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        joint_probability,
        "_deployment_payload",
        lambda *_args: {"generated_at": generated_at},
    )
    built = joint_probability.fit_joint_execution_deployment_estimator(
        corpus,
        study,
        authorization,
        generated_at=generated_at,
    )
    assert built["payload"] == {"generated_at": generated_at}

    oos_candidate_builder = joint_probability._oos_v3_candidates  # noqa: SLF001
    candidate_prediction = {
        "sample_id": "1:600519.SH:5:net_excess_positive",
        "session_date": "2026-01-02",
    }
    monkeypatch.setattr(
        joint_probability,
        "_unique_mapping_index",
        lambda *_args: {str(candidate_prediction["sample_id"]): candidate_prediction},
    )
    monkeypatch.setattr(
        joint_probability,
        "_positive_int_mapping_index",
        lambda *_args: {1: {"fold_id": 1}},
    )
    monkeypatch.setattr(
        joint_probability,
        "_verify_oos_groups_cover_source_decisions",
        lambda *_args: {},
    )
    monkeypatch.setattr(
        joint_probability,
        "_joint_execution_v3_candidate",
        lambda *_args, **_kwargs: {"candidate": "one"},
    )
    candidates = oos_candidate_builder(
        [candidate_prediction],
        {
            "folds": [{"fold_id": 1}],
            "generated_at": generated_at,
        },
        corpus,
        {},
        {},
    )
    assert candidates == [{"candidate": "one"}]

    sentinel = object()
    monkeypatch.setattr(
        joint_probability,
        "_verified_oos_study",
        lambda *_args: {"selected": True},
    )
    monkeypatch.setattr(
        joint_probability,
        "_verified_token_indexes",
        lambda *_args: ({}, {}),
    )
    monkeypatch.setattr(
        joint_probability,
        "_validate_oos_source_bindings",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        joint_probability,
        "_oos_predictions",
        lambda *_args: [{"sample_id": "one"}],
    )
    monkeypatch.setattr(
        joint_probability,
        "_oos_v3_candidates",
        lambda *_args: [{"candidate": "one"}],
    )
    monkeypatch.setattr(
        joint_probability,
        "build_joint_execution_probability_corpus_v3",
        lambda *_args: sentinel,
    )
    assert (
        joint_probability.build_joint_execution_probability_oos_corpus_v3(
            study,
            corpus,
            [],
            [],
        )
        is sentinel
    )
