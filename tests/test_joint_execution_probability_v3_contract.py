from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

import app.models.joint_execution_probability_v3 as v3_model
from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.joint_execution_probability_v3 import joint_execution_v3_content_digest
from app.services.joint_execution_probability_v3 import (
    VerifiedJointExecutionProbabilityCorpusV3,
    build_joint_execution_probability_corpus_v3,
    decode_and_verify_joint_execution_probability_corpus_v3,
    encode_joint_execution_probability_corpus_v3,
    joint_execution_probability_corpus_v3_action_qualified,
    joint_execution_v3_decision_identity_digest,
    verify_joint_execution_probability_corpus_v3,
    verify_joint_execution_probability_evidence_v3,
)


def _bar(role: str, symbol_index: int, *, suspended: bool = False) -> dict[str, object]:
    session = "2026-07-02" if role == "entry" else "2026-07-03"
    price = 10.0 + symbol_index
    return {
        "role": role,
        "session_date": session,
        "session_offset_from_signal": 1 if role == "entry" else 2,
        "source_kind": "official_exchange_daily_ohlcv_amount",
        "adjustment_mode": "none",
        "open": None if suspended else price,
        "high": None if suspended else price * 1.04,
        "low": None if suspended else price * 0.98,
        "close": None if suspended else (price if role == "entry" else price + 0.3),
        "volume": 0.0 if suspended else 1_000_000.0,
        "amount": 0.0 if suspended else 20_000_000.0,
        "source_dataset_digest": "a" * 64,
    }


def _rules(role: str) -> dict[str, object]:
    session = "2026-07-02" if role == "entry" else "2026-07-03"
    return {
        "role": role,
        "session_date": session,
        "source_kind": "official_effective_dated",
        "effective_date": session,
        "board": "main",
        "is_st": False,
        "listing_status": "listed",
        "board_rule_id": "main-v1",
        "st_rule_id": "st-v1",
        "delisting_rule_id": "delisting-v1",
        "ruleset_digest": "b" * 64,
    }


def _reference(role: str, symbol_index: int) -> dict[str, object]:
    session = "2026-07-02" if role == "entry" else "2026-07-03"
    price = 9.9 + symbol_index
    return {
        "role": role,
        "session_date": session,
        "basis": "official_unadjusted_reference_with_effective_corporate_action",
        "previous_close": price,
        "reference_price": price,
        "corporate_action_status": "none",
        "reference_price_rule_id": "exchange-ref-v1",
        "source_dataset_digest": "c" * 64,
    }


def _state(role: str, *, suspended: bool = False) -> dict[str, object]:
    session = "2026-07-02" if role == "entry" else "2026-07-03"
    return {
        "role": role,
        "session_date": session,
        "source_kind": "official_effective_dated_trading_state",
        "exchange_session_state": "suspended" if suspended else "trading",
        "execution_state": "suspended" if suspended else "executable",
        "reason_code": "official_suspension" if suspended else "official_open_executable",
        "observed_at": f"{session}T15:05:00+08:00",
        "effective_rules_digest": "b" * 64,
        "trading_state_digest": "d" * 64,
    }


def _holding_path(symbol_index: int) -> dict[str, object]:
    steps: list[dict[str, object]] = []
    for offset, session in enumerate(("2026-07-02", "2026-07-03"), start=1):
        reference = _reference("entry" if offset == 1 else "exit", symbol_index)
        step: dict[str, object] = {
            "offset_from_signal": offset,
            "session_date": session,
            "official_session_artifact_digest": str(offset) * 64,
            "official_raw_file_set_digest": str(offset + 2) * 64,
            "official_row_digest": str(offset + 4) * 64,
            "official_reference_evidence_digest": str(offset + 6) * 64,
            "corporate_action_status": "none",
            "corporate_action_event_id": None,
            "previous_close": reference["previous_close"],
            "reference_price": reference["reference_price"],
            "reference_continuity_factor": 1.0,
            "applied_to_holding_return": offset > 1,
        }
        step["step_digest"] = sha256_hex(canonical_json_bytes(step))
        steps.append(step)
    path: dict[str, object] = {
        "horizon": 1,
        "entry_session": "2026-07-02",
        "exit_session": "2026-07-03",
        "steps": steps,
        "corporate_action_factor": 1.0,
    }
    path["path_digest"] = sha256_hex(canonical_json_bytes(path))
    return path


def _evidence(
    symbol_index: int,
    *,
    suspended_entry: bool,
    decision_identity_digest: str,
) -> dict[str, object]:
    entry_amount = 0.0 if suspended_entry else 20_000_000.0
    return {
        "entry_bar": _bar("entry", symbol_index, suspended=suspended_entry),
        "exit_bar": _bar("exit", symbol_index),
        "entry_rules": _rules("entry"),
        "exit_rules": _rules("exit"),
        "entry_reference": _reference("entry", symbol_index),
        "exit_reference": _reference("exit", symbol_index),
        "participation": {
            "basis": "entry_and_exit_same_session_amount",
            "entry_order_notional": 100_000.0,
            "entry_session_amount": entry_amount,
            "entry_participation_rate": None if suspended_entry else 0.005,
            "exit_order_notional": 103_000.0,
            "exit_session_amount": 20_000_000.0,
            "exit_participation_rate": 0.00515,
            "maximum_participation_rate": 0.01,
            "evidence_digest": "e" * 64,
        },
        "benchmark": {
            "universe_basis": "fixed_full_market_at_signal",
            "outcome_population": "all_decisions",
            "benchmark_method": "fixed_universe_leave_one_out",
            "universe_frozen_before_outcomes": True,
            "benchmark_predeclared": True,
            "subject_excluded": True,
            "universe_definition_digest": "f" * 64,
            "universe_membership_digest": "1" * 64,
            "decision_cohort_digest": decision_identity_digest,
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


def _fixture() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    sample_ids = [
        "71:600519.SH:1:net_excess_positive",
        "71:600520.SH:1:net_excess_positive",
    ]
    identity_digest = joint_execution_v3_decision_identity_digest(sample_ids)
    decision_set = {
        "population_policy": "all_fixed_full_market_decisions_including_unfilled_and_unexecutable",
        "signal_session": "2026-07-01",
        "horizon": 1,
        "target": "net_excess_positive",
        "source_run_id": 71,
        "expected_decision_count": 2,
        "decision_identity_digest": identity_digest,
        "universe_definition_digest": "f" * 64,
        "universe_membership_digest": "1" * 64,
        "source_snapshot_digest": "0" * 64,
        "frozen_at": "2026-07-01T15:01:00+08:00",
        "universe_frozen_before_outcomes": True,
    }
    filled_gross = 10.3 / 10.0 - 1.0
    filled_net = filled_gross - 0.002
    candidates = [
        {
            "sample_id": sample_ids[0],
            "symbol": "600519.SH",
            "signal_session": "2026-07-01",
            "generated_at": "2026-07-04T09:00:00+08:00",
            "evidence": _evidence(0, suspended_entry=False, decision_identity_digest=identity_digest),
            "entry_state": _state("entry"),
            "exit_state": _state("exit"),
            "holding_path": _holding_path(0),
            "costs": {
                "model_version": "ashare-executable-round-trip-cost-v1",
                "profile_id": "production-base-v1",
                "slippage_model_version": "amount-participation-slippage-v1",
                "entry_order_notional": 100_000.0,
                "exit_order_notional": 103_000.0,
                "maximum_participation_rate": 0.01,
                "entry_cost_return": 0.0005,
                "exit_cost_return": 0.0015,
                "total_cost_return": 0.002,
            },
            "observed_outcome": {
                "target": "net_excess_positive",
                "entry_fill": True,
                "exit_executable": True,
                "net_positive": True,
                "joint_action_positive": True,
                "entry_price": 10.0,
                "exit_price": 10.3,
                "gross_return": filled_gross,
                "net_return": filled_net,
                "benchmark_return": 0.01,
                "net_excess_return": filled_net - 0.01,
                "observed_at": "2026-07-03T15:05:00+08:00",
                "outcome_reason_codes": ["entry_filled_exit_executable_net_positive"],
            },
            "decision_set": decision_set,
            "probabilities": {
                "entry_fill_probability": 0.8,
                "exit_executable_given_entry_probability": 0.9,
                "net_positive_given_entry_and_exit_probability": 0.75,
                "joint_net_positive_probability": 0.54,
                "action_probability": 0.54,
            },
        },
        {
            "sample_id": sample_ids[1],
            "symbol": "600520.SH",
            "signal_session": "2026-07-01",
            "generated_at": "2026-07-04T09:00:00+08:00",
            "evidence": _evidence(1, suspended_entry=True, decision_identity_digest=identity_digest),
            "entry_state": _state("entry", suspended=True),
            "exit_state": _state("exit"),
            "holding_path": _holding_path(1),
            "costs": {
                "model_version": "ashare-executable-round-trip-cost-v1",
                "profile_id": "production-base-v1",
                "slippage_model_version": "amount-participation-slippage-v1",
                "entry_order_notional": 100_000.0,
                "exit_order_notional": 103_000.0,
                "maximum_participation_rate": 0.01,
                "entry_cost_return": 0.0,
                "exit_cost_return": 0.0,
                "total_cost_return": 0.0,
            },
            "observed_outcome": {
                "target": "net_excess_positive",
                "entry_fill": False,
                "exit_executable": None,
                "net_positive": None,
                "joint_action_positive": False,
                "entry_price": None,
                "exit_price": None,
                "gross_return": None,
                "net_return": 0.0,
                "benchmark_return": 0.01,
                "net_excess_return": -0.01,
                "observed_at": "2026-07-03T15:05:00+08:00",
                "outcome_reason_codes": ["official_entry_suspension_no_fill"],
            },
            "decision_set": decision_set,
            "probabilities": {
                "entry_fill_probability": 0.5,
                "exit_executable_given_entry_probability": 0.5,
                "net_positive_given_entry_and_exit_probability": 0.8,
                "joint_net_positive_probability": 0.2,
                "action_probability": 0.2,
            },
        },
    ]
    predictions = [
        {
            "sample_id": sample_ids[0],
            "session_date": "2026-07-01",
            "fold_id": 1,
            "outcome": 1,
            "probability": 0.54,
            "net_return": filled_net,
            "net_excess_return": filled_net - 0.01,
        },
        {
            "sample_id": sample_ids[1],
            "session_date": "2026-07-01",
            "fold_id": 1,
            "outcome": 0,
            "probability": 0.2,
            "net_return": 0.0,
            "net_excess_return": -0.01,
        },
    ]
    return candidates, predictions


def test_whole_corpus_builder_includes_unfilled_decision_and_returns_opaque_token() -> None:
    candidates, predictions = _fixture()
    token = build_joint_execution_probability_corpus_v3(candidates, predictions)

    assert isinstance(token, VerifiedJointExecutionProbabilityCorpusV3)
    assert joint_execution_probability_corpus_v3_action_qualified(token)
    assert len(token) == 2
    assert token[0]["status"] == "qualified_shadow"
    assert token[1]["observed_outcome"]["entry_fill"] is False
    assert token[1]["gate_findings"] == []
    assert encode_joint_execution_probability_corpus_v3(token) == encode_joint_execution_probability_corpus_v3(
        decode_and_verify_joint_execution_probability_corpus_v3(
            encode_joint_execution_probability_corpus_v3(token), predictions,
        )
    )


def test_official_nontrading_session_may_have_no_amount_but_proxy_cannot_waive_it() -> None:
    candidates, predictions = _fixture()
    suspended = candidates[1]
    suspended["evidence"]["participation"]["entry_session_amount"] = None
    suspended["evidence"]["participation"]["entry_participation_rate"] = None

    token = build_joint_execution_probability_corpus_v3(candidates, predictions)

    assert token[1]["status"] == "qualified_shadow"
    assert token[1]["gate_findings"] == []

    suspended["entry_state"]["source_kind"] = "vendor_state_proxy"
    with pytest.raises(ValueError, match="qualified_shadow"):
        build_joint_execution_probability_corpus_v3(candidates, predictions)


def test_individual_mapping_never_counts_as_corpus_authorization() -> None:
    candidates, predictions = _fixture()
    token = build_joint_execution_probability_corpus_v3(candidates, predictions)
    report = verify_joint_execution_probability_evidence_v3(token.reports[0])

    assert report.status == "qualified_shadow"
    assert not joint_execution_probability_corpus_v3_action_qualified(report)
    assert not joint_execution_probability_corpus_v3_action_qualified(report.model_dump(mode="json"))
    with pytest.raises(TypeError, match="strict verifier"):
        VerifiedJointExecutionProbabilityCorpusV3(
            encoded_reports="[]",
            integrity_digest="a" * 64,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("drop_decision", "全覆盖|全集|identities"),
        ("prediction_label", "label|outcome"),
        ("prediction_probability", "probability"),
        ("prediction_return", "net_return"),
        ("decision_identity", "digest|全集|benchmark"),
        ("corpus_replay", "digest"),
    ],
)
def test_corpus_verifier_rejects_incomplete_or_tampered_bindings(
    mutation: str,
    message: str,
) -> None:
    candidates, predictions = _fixture()
    token = build_joint_execution_probability_corpus_v3(candidates, predictions)
    reports = deepcopy(token.reports)
    tampered_predictions = deepcopy(predictions)
    if mutation == "drop_decision":
        reports.pop()
        tampered_predictions.pop()
    elif mutation == "prediction_label":
        tampered_predictions[0]["outcome"] = 0
    elif mutation == "prediction_probability":
        tampered_predictions[0]["probability"] = 0.55
    elif mutation == "prediction_return":
        tampered_predictions[0]["net_return"] = 0.5
    elif mutation == "decision_identity":
        reports[0]["decision_set"]["decision_identity_digest"] = "a" * 64
        reports[0]["canonical_digest"] = joint_execution_v3_content_digest(reports[0])
    else:
        reports[0]["assessment_replay"]["corpus_replay_digest"] = "a" * 64
        reports[0]["canonical_digest"] = joint_execution_v3_content_digest(reports[0])

    with pytest.raises((KeyError, ValueError, ValidationError), match=message):
        verify_joint_execution_probability_corpus_v3(reports, tampered_predictions)


def test_report_rejects_adjusted_bar_proxy_and_strips_probabilities() -> None:
    candidates, predictions = _fixture()
    candidates[0]["evidence"]["entry_bar"]["adjustment_mode"] = "qfq"

    with pytest.raises(ValueError, match="qualified_shadow|action probability|status"):
        build_joint_execution_probability_corpus_v3(candidates, predictions)


def test_outcome_math_and_official_price_binding_are_strict() -> None:
    candidates, predictions = _fixture()
    candidates[0]["observed_outcome"]["exit_price"] = 99.0

    with pytest.raises(ValidationError, match="official horizon close|returns do not replay"):
        build_joint_execution_probability_corpus_v3(candidates, predictions)


def test_holding_path_corporate_action_factor_is_applied_and_digest_bound() -> None:
    candidates, predictions = _fixture()
    path = candidates[0]["holding_path"]
    assert isinstance(path, dict)
    steps = path["steps"]
    assert isinstance(steps, list)
    second = steps[1]
    assert isinstance(second, dict)
    second.update(
        corporate_action_status="effective_event",
        corporate_action_event_id="cash-distribution-20260703",
        previous_close=10.0,
        reference_price=5.0,
        reference_continuity_factor=2.0,
    )
    second.pop("step_digest")
    second["step_digest"] = sha256_hex(canonical_json_bytes(second))
    path["corporate_action_factor"] = 2.0
    path.pop("path_digest")
    path["path_digest"] = sha256_hex(canonical_json_bytes(path))

    gross = 10.3 * 2.0 / 10.0 - 1.0
    net = gross - 0.002
    outcome = candidates[0]["observed_outcome"]
    assert isinstance(outcome, dict)
    outcome.update(
        gross_return=gross,
        net_return=net,
        net_excess_return=net - 0.01,
    )
    predictions[0]["net_return"] = net
    predictions[0]["net_excess_return"] = net - 0.01

    token = build_joint_execution_probability_corpus_v3(candidates, predictions)
    assert token.reports[0]["observed_outcome"]["gross_return"] == pytest.approx(gross)

    tampered = deepcopy(candidates)
    cast_path = tampered[0]["holding_path"]
    assert isinstance(cast_path, dict)
    cast_path["corporate_action_factor"] = 1.0
    with pytest.raises(ValidationError, match="factor mismatch"):
        build_joint_execution_probability_corpus_v3(tampered, predictions)


def _sealed_nested(value: dict[str, object], digest_field: str) -> dict[str, object]:
    payload = deepcopy(value)
    payload.pop(digest_field, None)
    payload[digest_field] = sha256_hex(canonical_json_bytes(payload))
    return payload


def _valid_v3_report() -> v3_model.DecisionTimeJointExecutionProbabilityEvidenceV3:
    candidates, predictions = _fixture()
    token = build_joint_execution_probability_corpus_v3(candidates, predictions)
    return v3_model.DecisionTimeJointExecutionProbabilityEvidenceV3.model_validate(
        token.reports[0]
    )


def test_v3_holding_path_nested_digests_and_economics_fail_closed() -> None:
    report = _valid_v3_report()
    step = report.holding_path.steps[0].model_dump(mode="json")
    mutations = (
        ("event identity", {"corporate_action_status": "effective_event"}),
        ("continuity factor", {"reference_continuity_factor": 2.0}),
        ("cannot be reapplied", {"applied_to_holding_return": True}),
    )
    for message, values in mutations:
        candidate = _sealed_nested({**step, **values}, "step_digest")
        with pytest.raises(ValidationError, match=message):
            v3_model.JointExecutionHoldingPathStepEvidenceV3.model_validate(candidate)
    with pytest.raises(ValidationError, match="step digest mismatch"):
        v3_model.JointExecutionHoldingPathStepEvidenceV3.model_validate(
            {**step, "step_digest": "f" * 64}
        )

    path = report.holding_path.model_dump(mode="json")
    duplicate_date_second = _sealed_nested(
        {**path["steps"][1], "session_date": path["steps"][0]["session_date"]},
        "step_digest",
    )
    path_mutations = (
        ("every official session", {"steps": list(reversed(path["steps"]))}),
        (
            "dates are not canonical",
            {"steps": [path["steps"][0], duplicate_date_second]},
        ),
        ("endpoint mismatch", {"entry_session": "2026-07-03"}),
        ("factor mismatch", {"corporate_action_factor": 2.0}),
    )
    for message, values in path_mutations:
        candidate = _sealed_nested({**path, **values}, "path_digest")
        with pytest.raises(ValidationError, match=message):
            v3_model.JointExecutionHoldingPathEvidenceV3.model_validate(candidate)
    with pytest.raises(ValidationError, match="path digest mismatch"):
        v3_model.JointExecutionHoldingPathEvidenceV3.model_validate(
            {**path, "path_digest": "f" * 64}
        )

    costs = report.costs.model_dump(mode="json")
    with pytest.raises(ValidationError, match="entry plus exit"):
        v3_model.JointExecutionCostEvidenceV3.model_validate(
            _sealed_nested({**costs, "total_cost_return": 0.5}, "evidence_digest")
        )
    with pytest.raises(ValidationError, match="digest mismatch"):
        v3_model.JointExecutionCostEvidenceV3.model_validate(
            {**costs, "evidence_digest": "f" * 64}
        )


def test_v3_session_state_decision_set_and_reason_codes_fail_closed() -> None:
    report = _valid_v3_report()
    state = report.entry_state.model_dump(mode="json")
    with pytest.raises(ValidationError, match="before its session"):
        v3_model.JointExecutionSessionStateEvidenceV3.model_validate(
            {**state, "observed_at": "2026-07-01T15:00:00+08:00"}
        )
    with pytest.raises(ValidationError, match="conflict"):
        v3_model.JointExecutionSessionStateEvidenceV3.model_validate(
            {**state, "exchange_session_state": "suspended"}
        )
    decision_set = report.decision_set.model_dump(mode="json")
    for frozen_at in (
        "2026-06-30T16:00:00+08:00",
        "2026-07-01T15:00:00+08:00",
    ):
        with pytest.raises(ValidationError, match="frozen after"):
            v3_model.JointExecutionDecisionSetEvidenceV3.model_validate(
                {**decision_set, "frozen_at": frozen_at}
            )
    outcome = report.observed_outcome.model_dump(mode="json")
    for codes in (["same", "same"], ["Not_Normalized"]):
        with pytest.raises(ValidationError, match="reason codes"):
            v3_model.JointExecutionObservedOutcomeV3.model_validate(
                _sealed_nested(
                    {**outcome, "outcome_reason_codes": codes},
                    "evidence_digest",
                )
            )


def test_v3_outcome_shapes_cover_unfilled_unexecutable_and_executable_contracts() -> None:
    report = _valid_v3_report()
    observed_at = report.observed_outcome.observed_at
    base = {
        "target": "net_excess_positive",
        "observed_at": observed_at,
        "outcome_reason_codes": ["test_outcome"],
    }

    unfilled = {
        **base,
        "entry_fill": False,
        "exit_executable": None,
        "net_positive": None,
        "joint_action_positive": False,
        "entry_price": None,
        "exit_price": None,
        "gross_return": None,
        "net_return": 0.0,
        "benchmark_return": 0.01,
        "net_excess_return": -0.01,
    }
    invalid_unfilled = (
        ("cannot claim exit", {"exit_executable": False}),
        ("fill-price", {"entry_price": 10.0}),
        ("cash and benchmark", {"net_return": None}),
        ("cash return", {"net_return": 0.1}),
        ("joint-action positive", {"joint_action_positive": True}),
    )
    for message, values in invalid_unfilled:
        with pytest.raises(ValidationError, match=message):
            v3_model.JointExecutionObservedOutcomeV3.model_validate(
                _sealed_nested({**unfilled, **values}, "evidence_digest")
            )

    blocked = {
        **base,
        "entry_fill": True,
        "exit_executable": False,
        "net_positive": None,
        "joint_action_positive": False,
        "entry_price": 10.0,
        "exit_price": None,
        "gross_return": None,
        "net_return": None,
        "benchmark_return": None,
        "net_excess_return": None,
    }
    for message, values in (
        ("bind its entry price", {"entry_price": None}),
        ("cannot claim completed", {"net_return": 0.0}),
        ("cannot be joint-action positive", {"joint_action_positive": True}),
    ):
        with pytest.raises(ValidationError, match=message):
            v3_model.JointExecutionObservedOutcomeV3.model_validate(
                _sealed_nested({**blocked, **values}, "evidence_digest")
            )

    executable = report.observed_outcome.model_dump(mode="json")
    with pytest.raises(ValidationError, match="complete return"):
        v3_model.JointExecutionObservedOutcomeV3.model_validate(
            _sealed_nested({**executable, "gross_return": None}, "evidence_digest")
        )
    with pytest.raises(ValidationError, match="label must equal"):
        v3_model.JointExecutionObservedOutcomeV3.model_validate(
            _sealed_nested(
                {**executable, "joint_action_positive": False},
                "evidence_digest",
            )
        )
    with pytest.raises(ValidationError, match="filled decision requires"):
        v3_model.JointExecutionObservedOutcomeV3.model_validate(
            _sealed_nested(
                {**executable, "exit_executable": None},
                "evidence_digest",
            )
        )
    with pytest.raises(ValidationError, match="digest mismatch"):
        v3_model.JointExecutionObservedOutcomeV3.model_validate(
            {**executable, "evidence_digest": "f" * 64}
        )


def test_v3_report_binding_helpers_reject_each_independent_identity_break() -> None:
    report = _valid_v3_report()
    bad_entry_role = report.model_copy(
        update={"entry_state": report.entry_state.model_copy(update={"role": "exit"})}
    )
    with pytest.raises(ValueError, match="roles conflict"):
        v3_model._state_session_bindings(bad_entry_role)
    with pytest.raises(ValueError, match="entry evidence session"):
        v3_model._state_session_bindings(
            report.model_copy(
                update={
                    "entry_state": report.entry_state.model_copy(
                        update={"session_date": "2026-07-03"}
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="exit evidence session"):
        v3_model._state_session_bindings(
            report.model_copy(
                update={
                    "exit_state": report.exit_state.model_copy(
                        update={"session_date": "2026-07-02"}
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="decision-set identity"):
        v3_model._identity_bindings(
            report.model_copy(
                update={
                    "decision_set": report.decision_set.model_copy(
                        update={"horizon": 5}
                    )
                }
            ),
            1,
            "net_excess_positive",
        )
    with pytest.raises(ValueError, match="estimand target"):
        v3_model._identity_bindings(
            report.model_copy(
                update={
                    "estimand": report.estimand.model_copy(
                        update={"registered_net_target": "net_return_positive"}
                    )
                }
            ),
            1,
            "net_excess_positive",
        )
    with pytest.raises(ValueError, match="report endpoints"):
        v3_model._path_benchmark_bindings(
            report.model_copy(
                update={"holding_path": report.holding_path.model_copy(update={"horizon": 5})}
            ),
            1,
        )
    with pytest.raises(ValueError, match="benchmark universe"):
        v3_model._path_benchmark_bindings(
            report.model_copy(
                update={
                    "decision_set": report.decision_set.model_copy(
                        update={"universe_definition_digest": "0" * 64}
                    )
                }
            ),
            1,
        )
    with pytest.raises(ValueError, match="assessment replay"):
        v3_model._replay_bindings(
            report.model_copy(
                update={
                    "assessment_replay": report.assessment_replay.model_copy(
                        update={"decision_count": 999}
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="participation"):
        v3_model._cost_bindings(
            report.model_copy(
                update={"costs": report.costs.model_copy(update={"entry_order_notional": 1.0})}
            )
        )


def test_v3_state_outcome_price_return_and_time_helpers_fail_closed() -> None:
    report = _valid_v3_report()
    with pytest.raises(ValueError, match="target does not bind"):
        v3_model._state_outcome_bindings(
            report.model_copy(
                update={
                    "observed_outcome": report.observed_outcome.model_copy(
                        update={"target": "net_return_positive"}
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="entry-fill"):
        v3_model._entry_outcome_bindings(
            report.model_copy(
                update={
                    "observed_outcome": report.observed_outcome.model_copy(
                        update={"entry_fill": False}
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="exit-executable"):
        v3_model._entry_outcome_bindings(
            report.model_copy(
                update={
                    "exit_state": report.exit_state.model_copy(
                        update={"execution_state": "suspended"}
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="entry fill price"):
        v3_model._entry_outcome_bindings(
            report.model_copy(
                update={
                    "observed_outcome": report.observed_outcome.model_copy(
                        update={"entry_price": 9.0}
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="exit price"):
        v3_model._exit_outcome_bindings(
            report.model_copy(
                update={
                    "observed_outcome": report.observed_outcome.model_copy(
                        update={"exit_price": 9.0}
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="returns do not replay"):
        v3_model._outcome_math(
            report.model_copy(
                update={
                    "observed_outcome": report.observed_outcome.model_copy(
                        update={"net_return": 0.0}
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="net-positive"):
        v3_model._outcome_math(
            report.model_copy(
                update={
                    "observed_outcome": report.observed_outcome.model_copy(
                        update={"net_positive": False}
                    )
                }
            )
        )
    for sample_id, message in (
        ("bad", "run:symbol"),
        ("0:600519.SH:1:net_excess_positive", "identity mismatch"),
        ("1:600519.SH:2:net_excess_positive", "horizon"),
        ("1:600519.SH:1:unsupported", "target"),
    ):
        with pytest.raises(ValueError, match=message):
            v3_model._sample_identity(sample_id, "600519.SH")
    with pytest.raises(TypeError, match="model or mapping"):
        v3_model.joint_execution_v3_content_digest(object())
    with pytest.raises(ValueError, match="ISO date"):
        v3_model._iso_date("not-a-date", "date")
    with pytest.raises(ValueError, match="timezone"):
        v3_model._aware_datetime("2026-07-01T15:00:00", "time")


def test_v3_gate_findings_cover_proxy_unknown_missing_digest_and_no_bar() -> None:
    report = _valid_v3_report()
    states = (
        report.entry_state.model_copy(update={"source_kind": "unknown"}),
        report.entry_state.model_copy(update={"source_kind": "vendor_state_proxy"}),
        report.entry_state.model_copy(update={"effective_rules_digest": None}),
        report.entry_state.model_copy(update={"execution_state": "no_bar"}),
    )
    codes = {
        finding.code
        for state in states
        for finding in v3_model._state_findings(state)
    }
    assert {
        "entry_trading_state_unknown",
        "entry_trading_state_proxy",
        "entry_trading_state_digest_missing",
        "entry_official_bar_unresolved",
    } <= codes
