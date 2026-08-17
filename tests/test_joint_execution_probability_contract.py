from __future__ import annotations

from copy import deepcopy
import json
import math

import pytest
from pydantic import ValidationError

from app.artifacts.io import ArtifactDuplicateKeyError, ArtifactNonFiniteConstantError
from app.models.joint_execution_probability import (
    JointExecutionEvidenceBundle,
    JointExecutionProbabilityComponents,
    joint_execution_probability_evidence_digest,
)
from app.services.joint_execution_probability import (
    build_decision_time_joint_execution_probability_evidence,
    decode_joint_execution_probability_evidence,
    encode_joint_execution_probability_evidence,
    joint_execution_probability_action_qualified,
    verify_joint_execution_probability_evidence,
)


_D = "a" * 64


def _bar(role: str, session: str, *, price: float) -> dict[str, object]:
    return {
        "role": role,
        "session_date": session,
        "session_offset_from_signal": 1 if role == "entry" else 6,
        "source_kind": "official_exchange_daily_ohlcv_amount",
        "adjustment_mode": "none",
        "open": price,
        "high": price * 1.02,
        "low": price * 0.98,
        "close": price * 1.01,
        "volume": 1_000_000.0,
        "amount": 20_000_000.0,
        "source_dataset_digest": _D,
    }


def _rules(role: str, session: str) -> dict[str, object]:
    return {
        "role": role,
        "session_date": session,
        "source_kind": "official_effective_dated",
        "effective_date": session,
        "board": "main",
        "is_st": False,
        "listing_status": "listed",
        "board_rule_id": "main-board-v1",
        "st_rule_id": "st-effective-date-v1",
        "delisting_rule_id": "delisting-effective-date-v1",
        "ruleset_digest": "b" * 64,
    }


def _reference(role: str, session: str, *, price: float) -> dict[str, object]:
    return {
        "role": role,
        "session_date": session,
        "basis": "official_unadjusted_reference_with_effective_corporate_action",
        "previous_close": price,
        "reference_price": price,
        "corporate_action_status": "none",
        "reference_price_rule_id": "exchange-reference-price-v1",
        "source_dataset_digest": "c" * 64,
    }


def _qualified_evidence() -> dict[str, object]:
    entry_notional, entry_amount = 100_000.0, 20_000_000.0
    exit_notional, exit_amount = 102_000.0, 18_000_000.0
    return {
        "entry_bar": _bar("entry", "2026-07-02", price=10.0),
        "exit_bar": _bar("exit", "2026-07-07", price=10.2),
        "entry_rules": _rules("entry", "2026-07-02"),
        "exit_rules": _rules("exit", "2026-07-07"),
        "entry_reference": _reference("entry", "2026-07-02", price=9.9),
        "exit_reference": _reference("exit", "2026-07-07", price=10.1),
        "participation": {
            "basis": "entry_and_exit_same_session_amount",
            "entry_order_notional": entry_notional,
            "entry_session_amount": entry_amount,
            "entry_participation_rate": entry_notional / entry_amount,
            "exit_order_notional": exit_notional,
            "exit_session_amount": exit_amount,
            "exit_participation_rate": exit_notional / exit_amount,
            "maximum_participation_rate": 0.01,
            "evidence_digest": "d" * 64,
        },
        "benchmark": {
            "universe_basis": "fixed_full_market_at_signal",
            "outcome_population": "all_decisions",
            "benchmark_method": "fixed_universe_leave_one_out",
            "universe_frozen_before_outcomes": True,
            "benchmark_predeclared": True,
            "subject_excluded": True,
            "universe_definition_digest": "e" * 64,
            "universe_membership_digest": "f" * 64,
            "decision_cohort_digest": "1" * 64,
            "benchmark_series_digest": "2" * 64,
        },
        "calibration": {
            "estimator_contract": "three_component_joint_chain",
            "training_cutoff": "2026-06-30",
            "prediction_generated_at": "2026-07-01T15:05:00+08:00",
            "entry_model_digest": "3" * 64,
            "exit_model_digest": "4" * 64,
            "net_model_digest": "5" * 64,
            "calibrator_digest": "6" * 64,
            "feature_schema_digest": "7" * 64,
            "decision_information_digest": "8" * 64,
            "out_of_sample_assessment_digest": "9" * 64,
            "out_of_sample_verified": True,
            "calibration_verified": True,
            "selection_qualified": True,
        },
    }


def _probabilities() -> dict[str, float]:
    return {
        "entry_fill_probability": 0.8,
        "exit_executable_given_entry_probability": 0.9,
        "net_positive_given_entry_and_exit_probability": 0.6,
        "joint_net_positive_probability": 0.432,
        "action_probability": 0.432,
    }


def _build(
    evidence: dict[str, object] | None = None,
    probabilities: dict[str, float] | None = None,
):
    return build_decision_time_joint_execution_probability_evidence(
        sample_id="71:600519.SH:5:net_excess_positive",
        symbol="600519.SH",
        signal_session="2026-07-01",
        generated_at="2026-07-08T09:00:00+08:00",
        evidence=evidence or _qualified_evidence(),
        probabilities=probabilities or _probabilities(),
    )


def test_current_skeleton_strips_unverifiable_joint_components_and_has_stable_digest() -> None:
    report = _build()

    assert report.schema_version == "decision-time-joint-execution-probability-v2"
    assert report.sample_id == "71:600519.SH:5:net_excess_positive"
    assert report.status == "unavailable"
    assert [(item.code, item.severity) for item in report.gate_findings] == [
        ("observed_joint_outcome_components_unavailable", "unavailable"),
        ("strict_joint_assessment_replay_not_verified", "unavailable"),
    ]
    assert report.probabilities.is_null()
    assert report.canonical_digest == joint_execution_probability_evidence_digest(report)
    assert encode_joint_execution_probability_evidence(report) == encode_joint_execution_probability_evidence(_build())


def test_sample_identity_is_required_bounded_and_digest_bound() -> None:
    with pytest.raises(ValidationError):
        build_decision_time_joint_execution_probability_evidence(
            sample_id="",
            symbol="600519.SH",
            signal_session="2026-07-01",
            generated_at="2026-07-08T09:00:00+08:00",
            evidence=_qualified_evidence(),
            probabilities=_probabilities(),
        )
    with pytest.raises(ValidationError):
        build_decision_time_joint_execution_probability_evidence(
            sample_id="x" * 161,
            symbol="600519.SH",
            signal_session="2026-07-01",
            generated_at="2026-07-08T09:00:00+08:00",
            evidence=_qualified_evidence(),
            probabilities=_probabilities(),
        )

    payload = _build().model_dump(mode="json")
    payload["sample_id"] = "72:600519.SH:5:net_excess_positive"
    with pytest.raises(ValidationError, match="digest mismatch"):
        verify_joint_execution_probability_evidence(payload)


@pytest.mark.parametrize(
    ("path", "value", "expected_code"),
    [
        (("entry_bar", "adjustment_mode"), "qfq", "entry_adjustment_qfq"),
        (
            ("participation", "basis"),
            "signal_session_D_amount_proxy",
            "participation_signal_session_d_amount_proxy",
        ),
        (("benchmark", "outcome_population"), "posthoc_filled_only", "outcome_population_posthoc_filled_only"),
        (("benchmark", "benchmark_method"), "dynamic_modelled_mean", "benchmark_dynamic_modelled_mean"),
        (
            ("calibration", "estimator_contract"),
            "legacy_conditional_filled_only",
            "estimator_legacy_conditional_filled_only",
        ),
    ],
)
def test_known_legacy_proxies_are_audit_only_and_cannot_emit_probability(
    path: tuple[str, str],
    value: str,
    expected_code: str,
) -> None:
    evidence = _qualified_evidence()
    section = evidence[path[0]]
    assert isinstance(section, dict)
    section[path[1]] = value

    report = _build(evidence)

    assert report.status == "unavailable"
    assert report.probabilities.is_null()
    assert expected_code in {finding.code for finding in report.gate_findings}


@pytest.mark.parametrize(
    ("section_name", "field_name", "expected_code"),
    [
        ("entry_bar", "amount", "entry_ohlcv_amount_missing"),
        ("exit_rules", "delisting_rule_id", "exit_effective_rules_missing"),
        ("entry_reference", "reference_price", "entry_reference_evidence_missing"),
        ("benchmark", "universe_membership_digest", "benchmark_or_universe_digest_missing"),
        ("calibration", "exit_model_digest", "calibration_evidence_missing"),
    ],
)
def test_missing_required_execution_evidence_is_unavailable(
    section_name: str,
    field_name: str,
    expected_code: str,
) -> None:
    evidence = _qualified_evidence()
    section = evidence[section_name]
    assert isinstance(section, dict)
    section[field_name] = None

    report = _build(evidence)

    assert report.status == "unavailable"
    assert report.probabilities.is_null()
    assert expected_code in {finding.code for finding in report.gate_findings}


def test_external_predeclared_benchmark_is_allowed_without_subject_exclusion() -> None:
    evidence = _qualified_evidence()
    benchmark = evidence["benchmark"]
    assert isinstance(benchmark, dict)
    benchmark["benchmark_method"] = "external_predeclared_benchmark"
    benchmark["subject_excluded"] = False

    assert _build(evidence).status == "unavailable"


def test_zero_same_session_amount_remains_in_all_decision_population() -> None:
    evidence = _qualified_evidence()
    participation = evidence["participation"]
    assert isinstance(participation, dict)
    participation["entry_session_amount"] = 0.0
    participation["entry_participation_rate"] = None

    report = _build(evidence)

    assert report.status == "unavailable"
    assert report.evidence.benchmark.outcome_population == "all_decisions"


def test_joint_probability_must_equal_all_three_conditionals() -> None:
    values = _probabilities()
    values["joint_net_positive_probability"] = 0.43
    values["action_probability"] = 0.43

    with pytest.raises(ValidationError, match="three conditional components"):
        JointExecutionProbabilityComponents.model_validate(values)


def test_probability_components_are_all_present_or_all_null() -> None:
    with pytest.raises(ValidationError, match="all-null or all-present"):
        JointExecutionProbabilityComponents(entry_fill_probability=0.8)


def test_resealed_legacy_evidence_cannot_claim_calibrated_probability() -> None:
    payload = _build().model_dump(mode="json")
    payload["evidence"]["entry_bar"]["adjustment_mode"] = "qfq"
    payload["canonical_digest"] = joint_execution_probability_evidence_digest(payload)

    with pytest.raises(ValidationError, match="gate state"):
        verify_joint_execution_probability_evidence(payload)


def test_resealed_skeleton_can_never_expose_supplied_probabilities() -> None:
    payload = _build().model_dump(mode="json")
    payload["probabilities"] = _probabilities()
    payload["canonical_digest"] = joint_execution_probability_evidence_digest(payload)

    with pytest.raises(ValidationError, match="contract skeleton cannot carry"):
        verify_joint_execution_probability_evidence(payload)


def test_exact_schema_nan_and_digest_tampering_are_rejected() -> None:
    payload = _build().model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        verify_joint_execution_probability_evidence(payload)

    evidence = _qualified_evidence()
    entry = evidence["entry_bar"]
    assert isinstance(entry, dict)
    entry["open"] = math.nan
    with pytest.raises(ValidationError):
        _build(evidence)

    payload = _build().model_dump(mode="json")
    payload["symbol"] = "000001.SZ"
    with pytest.raises(ValidationError, match="identity mismatch"):
        verify_joint_execution_probability_evidence(payload)


def test_strict_decoder_rejects_duplicate_keys_and_nonfinite_constants() -> None:
    with pytest.raises(ArtifactDuplicateKeyError):
        decode_joint_execution_probability_evidence(b'{"schema_version":"a","schema_version":"b"}')
    with pytest.raises(ArtifactNonFiniteConstantError):
        decode_joint_execution_probability_evidence(b'{"value":NaN}')


def test_canonical_round_trip_preserves_exact_report() -> None:
    report = _build()
    encoded = encode_joint_execution_probability_evidence(report)

    decoded = decode_joint_execution_probability_evidence(encoded)

    assert decoded == report
    assert json.loads(encoded)["canonical_digest"] == report.canonical_digest


def test_lookahead_calibration_is_unavailable_and_probability_is_stripped() -> None:
    evidence = _qualified_evidence()
    calibration = evidence["calibration"]
    assert isinstance(calibration, dict)
    calibration["training_cutoff"] = "2026-07-01"
    calibration["prediction_generated_at"] = "2026-07-02T09:30:00+08:00"

    report = _build(evidence)

    assert report.status == "unavailable"
    assert report.probabilities.is_null()
    assert {item.code for item in report.gate_findings} >= {
        "training_cutoff_not_before_signal",
        "prediction_generated_after_signal",
    }


def test_session_roles_dates_and_participation_math_are_bound() -> None:
    evidence = _qualified_evidence()
    entry_rules = evidence["entry_rules"]
    assert isinstance(entry_rules, dict)
    entry_rules["session_date"] = "2026-07-03"
    with pytest.raises(ValidationError, match="sessions must match"):
        _build(evidence)

    evidence = _qualified_evidence()
    participation = evidence["participation"]
    assert isinstance(participation, dict)
    participation["exit_participation_rate"] = 0.5
    with pytest.raises(ValidationError, match="does not match"):
        _build(evidence)


def test_generated_at_must_follow_exit_and_include_timezone() -> None:
    with pytest.raises(ValidationError, match="cannot be generated before exit"):
        build_decision_time_joint_execution_probability_evidence(
            sample_id="71:600519.SH:5:net_excess_positive",
            symbol="600519.SH",
            signal_session="2026-07-01",
            generated_at="2026-07-06T09:00:00+08:00",
            evidence=_qualified_evidence(),
            probabilities=_probabilities(),
        )
    with pytest.raises(ValidationError, match="after-close maturity"):
        build_decision_time_joint_execution_probability_evidence(
            sample_id="71:600519.SH:5:net_excess_positive",
            symbol="600519.SH",
            signal_session="2026-07-01",
            generated_at="2026-07-07T15:00:00+08:00",
            evidence=_qualified_evidence(),
            probabilities=_probabilities(),
        )
    with pytest.raises(ValidationError, match="timezone"):
        build_decision_time_joint_execution_probability_evidence(
            sample_id="71:600519.SH:5:net_excess_positive",
            symbol="600519.SH",
            signal_session="2026-07-01",
            generated_at="2026-07-08T09:00:00",
            evidence=_qualified_evidence(),
            probabilities=_probabilities(),
        )


@pytest.mark.parametrize(
    "sample_id",
    [
        "71:600519.SH:20:net_excess_positive",
        "071:600519.SH:5:net_excess_positive",
        "71:600519.SH:5:joint_execution_action_positive",
        "71:600519.SH:5:unknown_target",
    ],
)
def test_sample_id_horizon_target_and_canonical_run_are_strict(sample_id: str) -> None:
    with pytest.raises(ValidationError):
        build_decision_time_joint_execution_probability_evidence(
            sample_id=sample_id,
            symbol="600519.SH",
            signal_session="2026-07-01",
            generated_at="2026-07-08T09:00:00+08:00",
            evidence=_qualified_evidence(),
            probabilities=_probabilities(),
        )


def test_absolute_production_target_identity_is_accepted_but_stays_unavailable() -> None:
    report = build_decision_time_joint_execution_probability_evidence(
        sample_id="71:600519.SH:5:net_return_positive",
        symbol="600519.SH",
        signal_session="2026-07-01",
        generated_at="2026-07-08T09:00:00+08:00",
        evidence=_qualified_evidence(),
        probabilities=_probabilities(),
    )

    assert report.status == "unavailable"
    assert report.probabilities.is_null()


@pytest.mark.parametrize(
    ("role", "offset"),
    [("entry", 2), ("exit", 5)],
)
def test_entry_and_exit_session_offsets_are_bound_to_horizon(role: str, offset: int) -> None:
    evidence = _qualified_evidence()
    bar = evidence[f"{role}_bar"]
    assert isinstance(bar, dict)
    bar["session_offset_from_signal"] = offset

    with pytest.raises(ValidationError, match="session offset"):
        _build(evidence)


def test_builder_does_not_mutate_untrusted_inputs() -> None:
    evidence = _qualified_evidence()
    probabilities = _probabilities()
    before_evidence, before_probabilities = deepcopy(evidence), deepcopy(probabilities)

    _build(evidence, probabilities)

    assert evidence == before_evidence
    assert probabilities == before_probabilities


def test_unknown_and_nonofficial_bar_states_fail_closed() -> None:
    evidence = _qualified_evidence()
    entry = evidence["entry_bar"]
    exit_bar = evidence["exit_bar"]
    assert isinstance(entry, dict) and isinstance(exit_bar, dict)
    entry.update(source_kind="unknown", adjustment_mode="unknown", source_dataset_digest=None)
    exit_bar.update(source_kind="other_daily_source", adjustment_mode="hfq")

    report = _build(evidence)
    codes = {item.code for item in report.gate_findings}

    assert report.status == "unavailable"
    assert codes >= {
        "entry_bar_source_unknown",
        "entry_adjustment_unknown",
        "entry_bar_digest_missing",
        "exit_bar_source_not_official",
        "exit_adjustment_hfq",
    }


@pytest.mark.parametrize(
    ("source", "effective_date", "expected"),
    [
        ("unknown", "2026-07-02", "entry_rules_source_unknown"),
        ("signal_date_static_proxy", "2026-07-02", "entry_rules_static_proxy"),
        ("official_effective_dated", "2026-07-03", "entry_effective_date_mismatch"),
    ],
)
def test_effective_day_rule_proxies_never_qualify(source: str, effective_date: str, expected: str) -> None:
    evidence = _qualified_evidence()
    rules = evidence["entry_rules"]
    assert isinstance(rules, dict)
    rules.update(source_kind=source, effective_date=effective_date)

    assert expected in {item.code for item in _build(evidence).gate_findings}


@pytest.mark.parametrize(
    ("basis", "expected"),
    [
        ("unknown", "exit_reference_basis_unknown"),
        ("adjusted_series_previous_close_proxy", "exit_reference_adjusted_proxy"),
    ],
)
def test_reference_price_proxy_never_qualifies(basis: str, expected: str) -> None:
    evidence = _qualified_evidence()
    reference = evidence["exit_reference"]
    assert isinstance(reference, dict)
    reference["basis"] = basis

    assert expected in {item.code for item in _build(evidence).gate_findings}


def test_unknown_participation_and_missing_bilateral_amounts_are_unavailable() -> None:
    evidence = _qualified_evidence()
    participation = evidence["participation"]
    assert isinstance(participation, dict)
    participation.update(
        basis="unknown",
        entry_session_amount=None,
        entry_participation_rate=None,
        exit_session_amount=None,
        exit_participation_rate=None,
        evidence_digest=None,
    )

    report = _build(evidence)

    assert {item.code for item in report.gate_findings} >= {
        "participation_basis_unknown",
        "entry_session_amount_missing",
        "exit_session_amount_missing",
        "participation_digest_missing",
    }


def test_unknown_benchmark_and_unfrozen_cohort_are_unavailable() -> None:
    evidence = _qualified_evidence()
    benchmark = evidence["benchmark"]
    assert isinstance(benchmark, dict)
    benchmark.update(
        universe_basis="unknown",
        outcome_population="unknown",
        benchmark_method="unknown",
        universe_frozen_before_outcomes=False,
        benchmark_predeclared=False,
    )

    codes = {item.code for item in _build(evidence).gate_findings}

    assert codes >= {
        "universe_basis_unknown",
        "outcome_population_unknown",
        "benchmark_method_unknown",
        "universe_not_frozen_before_outcomes",
        "benchmark_not_predeclared",
    }


def test_dynamic_universe_and_unexcluded_leave_one_out_fail_closed() -> None:
    evidence = _qualified_evidence()
    benchmark = evidence["benchmark"]
    assert isinstance(benchmark, dict)
    benchmark.update(universe_basis="dynamic_eligible_universe", subject_excluded=False)

    codes = {item.code for item in _build(evidence).gate_findings}

    assert codes >= {"universe_dynamic_eligible", "leave_one_out_subject_not_excluded"}


def test_unknown_unverified_calibration_is_never_authorized() -> None:
    evidence = _qualified_evidence()
    calibration = evidence["calibration"]
    assert isinstance(calibration, dict)
    calibration.update(
        estimator_contract="unknown",
        prediction_generated_at=None,
        out_of_sample_verified=False,
        calibration_verified=False,
        selection_qualified=False,
    )

    codes = {item.code for item in _build(evidence).gate_findings}

    assert codes >= {
        "estimator_contract_unknown",
        "calibration_evidence_missing",
        "out_of_sample_not_verified",
        "calibration_not_verified",
        "selection_not_qualified",
    }


def test_qualified_evidence_without_probability_is_typed_unavailable() -> None:
    report = build_decision_time_joint_execution_probability_evidence(
        sample_id="71:600519.SH:5:net_excess_positive",
        symbol="600519.SH",
        signal_session="2026-07-01",
        generated_at="2026-07-08T09:00:00+08:00",
        evidence=_qualified_evidence(),
    )

    assert report.status == "unavailable"
    assert [(item.code, item.severity) for item in report.gate_findings] == [
        ("observed_joint_outcome_components_unavailable", "unavailable"),
        ("strict_joint_assessment_replay_not_verified", "unavailable"),
    ]


def test_action_probability_cannot_diverge_from_joint_probability() -> None:
    values = _probabilities()
    values["action_probability"] = 0.4

    with pytest.raises(ValidationError, match="action probability"):
        JointExecutionProbabilityComponents.model_validate(values)


def test_gate_status_and_findings_cannot_be_resealed() -> None:
    payload = _build().model_dump(mode="json")
    payload["status"] = "audit_only"
    payload["canonical_digest"] = joint_execution_probability_evidence_digest(payload)

    with pytest.raises(ValidationError, match="gate state"):
        verify_joint_execution_probability_evidence(payload)


def test_digest_helper_rejects_non_mapping_non_model() -> None:
    with pytest.raises(TypeError, match="model or mapping"):
        joint_execution_probability_evidence_digest([])  # type: ignore[arg-type]


def test_invalid_session_order_and_roles_are_rejected() -> None:
    evidence = _qualified_evidence()
    entry = evidence["entry_bar"]
    assert isinstance(entry, dict)
    entry["role"] = "exit"
    with pytest.raises(ValidationError, match="role must be entry"):
        _build(evidence)

    with pytest.raises(ValidationError, match="forward ordered"):
        build_decision_time_joint_execution_probability_evidence(
            sample_id="71:600519.SH:5:net_excess_positive",
            symbol="600519.SH",
            signal_session="2026-07-02",
            generated_at="2026-07-08T09:00:00+08:00",
            evidence=_qualified_evidence(),
            probabilities=_probabilities(),
        )


def test_bar_bounds_and_participation_null_invariants_are_rejected() -> None:
    evidence = _qualified_evidence()
    bar = evidence["entry_bar"]
    assert isinstance(bar, dict)
    bar["low"], bar["high"] = 11.0, 10.0
    with pytest.raises(ValidationError, match="price bounds"):
        _build(evidence)

    evidence = _qualified_evidence()
    participation = evidence["participation"]
    assert isinstance(participation, dict)
    participation["entry_session_amount"] = None
    with pytest.raises(ValidationError, match="requires same-session amount"):
        _build(evidence)

    evidence = _qualified_evidence()
    participation = evidence["participation"]
    assert isinstance(participation, dict)
    participation["exit_session_amount"] = 0.0
    with pytest.raises(ValidationError, match="must be null"):
        _build(evidence)


def test_model_instances_and_non_object_json_are_supported_strictly() -> None:
    evidence = JointExecutionEvidenceBundle.model_validate(_qualified_evidence())
    probabilities = JointExecutionProbabilityComponents.model_validate(_probabilities())

    report = build_decision_time_joint_execution_probability_evidence(
        sample_id="71:600519.SH:5:net_excess_positive",
        symbol="600519.SH",
        signal_session="2026-07-01",
        generated_at="2026-07-08T09:00:00+08:00",
        evidence=evidence,
        probabilities=probabilities,
    )

    assert report.status == "unavailable"
    assert report.probabilities.is_null()
    assert joint_execution_probability_action_qualified(report) is False
    assert joint_execution_probability_action_qualified(report.model_dump(mode="json")) is False
    assert joint_execution_probability_action_qualified([]) is False
    with pytest.raises(ValueError, match="root must be an object"):
        decode_joint_execution_probability_evidence(b"[]")


def test_action_qualification_is_fail_closed_for_audit_and_tampering() -> None:
    evidence = _qualified_evidence()
    entry = evidence["entry_bar"]
    assert isinstance(entry, dict)
    entry["adjustment_mode"] = "qfq"
    audit_report = _build(evidence)
    tampered = _build().model_dump(mode="json")
    tampered["symbol"] = "000001.SZ"

    assert joint_execution_probability_action_qualified(audit_report) is False
    assert joint_execution_probability_action_qualified(tampered) is False


@pytest.mark.parametrize("value", ["2026-02-30", "not-a-date"])
def test_invalid_iso_dates_are_rejected(value: str) -> None:
    evidence = _qualified_evidence()
    rules = evidence["entry_rules"]
    assert isinstance(rules, dict)
    rules["effective_date"] = value

    with pytest.raises(ValidationError, match="ISO date"):
        _build(evidence)
