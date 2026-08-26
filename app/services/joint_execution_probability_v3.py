"""Builders and sealed whole-corpus verifier for joint-execution v3 evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
from math import isclose, isfinite
from typing import TypeVar, cast, overload

from pydantic import BaseModel

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, sha256_hex
from app.models.joint_execution_probability import (
    JointExecutionEvidenceBundle,
    JointExecutionProbabilityComponents,
)
from app.models.joint_execution_probability_v3 import (
    DecisionTimeJointExecutionProbabilityEvidenceV3,
    JointExecutionAssessmentReplayEvidenceV3,
    JointExecutionCostEvidenceV3,
    JointExecutionDecisionSetEvidenceV3,
    JointExecutionHoldingPathEvidenceV3,
    JointExecutionObservedOutcomeV3,
    JointExecutionProbabilityEstimandV3,
    JointExecutionSessionStateEvidenceV3,
    joint_execution_v3_content_digest,
    joint_execution_v3_gate_findings,
)


_VERIFIED_CORPUS_SEAL = object()


@dataclass(frozen=True)
class _Candidate:
    sample_id: str
    symbol: str
    signal_session: str
    generated_at: str
    evidence: JointExecutionEvidenceBundle
    entry_state: JointExecutionSessionStateEvidenceV3
    exit_state: JointExecutionSessionStateEvidenceV3
    holding_path: JointExecutionHoldingPathEvidenceV3
    costs: JointExecutionCostEvidenceV3
    observed_outcome: JointExecutionObservedOutcomeV3
    decision_set: JointExecutionDecisionSetEvidenceV3
    probabilities: JointExecutionProbabilityComponents


@dataclass(frozen=True)
class _BoundEvidenceInputs:
    evidence: JointExecutionEvidenceBundle
    entry_state: JointExecutionSessionStateEvidenceV3
    exit_state: JointExecutionSessionStateEvidenceV3
    holding_path: JointExecutionHoldingPathEvidenceV3
    costs: JointExecutionCostEvidenceV3
    outcome: JointExecutionObservedOutcomeV3
    decision_set: JointExecutionDecisionSetEvidenceV3
    replay: JointExecutionAssessmentReplayEvidenceV3
    probabilities: JointExecutionProbabilityComponents


_ModelT = TypeVar("_ModelT", bound=BaseModel)


class VerifiedJointExecutionProbabilityCorpusV3(Sequence[Mapping[str, object]]):
    """Opaque immutable result of strict whole-corpus replay verification."""

    __slots__ = ("_encoded_reports", "integrity_digest")

    def __init__(
        self,
        *,
        encoded_reports: str,
        integrity_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _VERIFIED_CORPUS_SEAL:
            raise TypeError("joint execution corpus token 只能由 strict verifier 创建")
        self._encoded_reports = encoded_reports
        self.integrity_digest = integrity_digest

    @property
    def reports(self) -> list[dict[str, object]]:
        value = json.loads(self._encoded_reports)
        if not isinstance(value, list):  # pragma: no cover - verifier invariant
            raise TypeError("verified joint execution corpus is not an array")
        return [cast(dict[str, object], item) for item in value]

    @overload
    def __getitem__(self, index: int) -> Mapping[str, object]: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Mapping[str, object]]: ...

    def __getitem__(
        self, index: int | slice,
    ) -> Mapping[str, object] | Sequence[Mapping[str, object]]:
        return self.reports[index]

    def __len__(self) -> int:
        return len(self.reports)

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        return iter(self.reports)


def build_decision_time_joint_execution_probability_evidence_v3(
    *,
    sample_id: str,
    symbol: str,
    signal_session: str,
    generated_at: str,
    evidence: JointExecutionEvidenceBundle | Mapping[str, object],
    entry_state: JointExecutionSessionStateEvidenceV3 | Mapping[str, object],
    exit_state: JointExecutionSessionStateEvidenceV3 | Mapping[str, object],
    holding_path: JointExecutionHoldingPathEvidenceV3 | Mapping[str, object],
    costs: JointExecutionCostEvidenceV3 | Mapping[str, object],
    observed_outcome: JointExecutionObservedOutcomeV3 | Mapping[str, object],
    decision_set: JointExecutionDecisionSetEvidenceV3 | Mapping[str, object],
    assessment_replay: JointExecutionAssessmentReplayEvidenceV3 | Mapping[str, object],
    probabilities: JointExecutionProbabilityComponents | Mapping[str, object] | None,
) -> DecisionTimeJointExecutionProbabilityEvidenceV3:
    """Build one digest-bound v3 corpus candidate.

    This function never creates an authorization token.  Only the whole-corpus
    verifier below can do that after replaying completeness and prediction
    bindings.
    """

    bound = _bind_evidence_inputs(
        evidence=evidence,
        entry_state=entry_state,
        exit_state=exit_state,
        holding_path=holding_path,
        costs=costs,
        observed_outcome=observed_outcome,
        decision_set=decision_set,
        assessment_replay=assessment_replay,
        probabilities=probabilities,
    )
    shell = _candidate_shell(sample_id, symbol, signal_session, generated_at, bound)
    findings = joint_execution_v3_gate_findings(shell)
    requested = JointExecutionProbabilityComponents() if findings else bound.probabilities
    payload = _candidate_payload(
        sample_id, symbol, signal_session, generated_at, bound, requested, findings
    )
    payload["canonical_digest"] = joint_execution_v3_content_digest(payload)
    return DecisionTimeJointExecutionProbabilityEvidenceV3.model_validate(payload)


def _bind_evidence_inputs(
    *,
    evidence: JointExecutionEvidenceBundle | Mapping[str, object],
    entry_state: JointExecutionSessionStateEvidenceV3 | Mapping[str, object],
    exit_state: JointExecutionSessionStateEvidenceV3 | Mapping[str, object],
    holding_path: JointExecutionHoldingPathEvidenceV3 | Mapping[str, object],
    costs: JointExecutionCostEvidenceV3 | Mapping[str, object],
    observed_outcome: JointExecutionObservedOutcomeV3 | Mapping[str, object],
    decision_set: JointExecutionDecisionSetEvidenceV3 | Mapping[str, object],
    assessment_replay: JointExecutionAssessmentReplayEvidenceV3 | Mapping[str, object],
    probabilities: JointExecutionProbabilityComponents | Mapping[str, object] | None,
) -> _BoundEvidenceInputs:
    return _BoundEvidenceInputs(
        evidence=_typed_model(JointExecutionEvidenceBundle, evidence),
        entry_state=_typed_model(JointExecutionSessionStateEvidenceV3, entry_state),
        exit_state=_typed_model(JointExecutionSessionStateEvidenceV3, exit_state),
        holding_path=_typed_model(JointExecutionHoldingPathEvidenceV3, holding_path),
        costs=_typed_digest_model(JointExecutionCostEvidenceV3, costs),
        outcome=_typed_digest_model(JointExecutionObservedOutcomeV3, observed_outcome),
        decision_set=_typed_model(JointExecutionDecisionSetEvidenceV3, decision_set),
        replay=_typed_model(JointExecutionAssessmentReplayEvidenceV3, assessment_replay),
        probabilities=_probability_model(probabilities),
    )


def _candidate_shell(
    sample_id: str,
    symbol: str,
    signal_session: str,
    generated_at: str,
    bound: _BoundEvidenceInputs,
) -> DecisionTimeJointExecutionProbabilityEvidenceV3:
    return DecisionTimeJointExecutionProbabilityEvidenceV3.model_construct(
        sample_id=sample_id,
        symbol=symbol,
        signal_session=signal_session,
        generated_at=generated_at,
        status="unavailable",
        estimand=JointExecutionProbabilityEstimandV3(
            registered_net_target=bound.decision_set.target
        ),
        evidence=bound.evidence,
        entry_state=bound.entry_state,
        exit_state=bound.exit_state,
        holding_path=bound.holding_path,
        costs=bound.costs,
        observed_outcome=bound.outcome,
        decision_set=bound.decision_set,
        assessment_replay=bound.replay,
        probabilities=bound.probabilities,
        gate_findings=[],
        production_effect="none",
        canonical_digest="0" * 64,
    )


def _candidate_payload(
    sample_id: str,
    symbol: str,
    signal_session: str,
    generated_at: str,
    bound: _BoundEvidenceInputs,
    probabilities: JointExecutionProbabilityComponents,
    findings: Sequence[BaseModel],
) -> dict[str, object]:
    return {
        "schema_version": "decision-time-joint-execution-probability-v3",
        "sample_id": sample_id,
        "symbol": symbol,
        "signal_session": signal_session,
        "generated_at": generated_at,
        "status": "unavailable" if findings else "qualified_shadow",
        "estimand": JointExecutionProbabilityEstimandV3(
            registered_net_target=bound.decision_set.target
        ).model_dump(mode="json"),
        "evidence": bound.evidence.model_dump(mode="json"),
        "entry_state": bound.entry_state.model_dump(mode="json"),
        "exit_state": bound.exit_state.model_dump(mode="json"),
        "holding_path": bound.holding_path.model_dump(mode="json"),
        "costs": bound.costs.model_dump(mode="json"),
        "observed_outcome": bound.outcome.model_dump(mode="json"),
        "decision_set": bound.decision_set.model_dump(mode="json"),
        "assessment_replay": bound.replay.model_dump(mode="json"),
        "probabilities": probabilities.model_dump(mode="json"),
        "gate_findings": [item.model_dump(mode="json") for item in findings],
        "production_effect": "none",
    }


def build_joint_execution_probability_corpus_v3(
    candidates: Sequence[Mapping[str, object]],
    predictions: Sequence[Mapping[str, object]],
) -> VerifiedJointExecutionProbabilityCorpusV3:
    """Build replay digests and immediately subject the full corpus to verification."""

    normalized = [_candidate(item, index) for index, item in enumerate(candidates)]
    normalized.sort(key=lambda item: (item.signal_session, item.sample_id))
    prediction_by_id = _prediction_index(predictions)
    grouped: dict[tuple[object, ...], list[_Candidate]] = defaultdict(list)
    for item in normalized:
        grouped[
            (
                item.decision_set.source_run_id,
                item.signal_session,
                item.decision_set.horizon,
                item.decision_set.target,
            )
        ].append(item)
    reports: list[DecisionTimeJointExecutionProbabilityEvidenceV3] = []
    for group in grouped.values():
        reports.extend(_build_candidate_group(group, prediction_by_id))
    payload = [item.model_dump(mode="json") for item in reports]
    return verify_joint_execution_probability_corpus_v3(payload, predictions)


def verify_joint_execution_probability_evidence_v3(
    value: Mapping[str, object],
) -> DecisionTimeJointExecutionProbabilityEvidenceV3:
    """Validate one corpus candidate without granting corpus authorization."""

    return DecisionTimeJointExecutionProbabilityEvidenceV3.model_validate(dict(value))


def verify_joint_execution_probability_corpus_v3(
    value: object,
    predictions: Sequence[Mapping[str, object]],
) -> VerifiedJointExecutionProbabilityCorpusV3:
    """Replay and seal an exact all-decisions corpus against its OOS predictions."""

    if not isinstance(value, list) or not value:
        raise ValueError("joint execution v3 corpus 必须是非空数组")
    if len(value) != len(predictions):
        raise ValueError("joint execution v3 corpus 未与 OOS predictions 全覆盖绑定")
    reports = [
        verify_joint_execution_probability_evidence_v3(
            _mapping(item, f"joint_execution_v3[{index}]"),
        )
        for index, item in enumerate(value)
    ]
    _verify_unique_canonical_order(reports, predictions)
    prediction_by_id = _prediction_index(predictions)
    for report in reports:
        _verify_prediction_binding(report, prediction_by_id[report.sample_id])
    grouped: dict[tuple[object, ...], list[DecisionTimeJointExecutionProbabilityEvidenceV3]] = (
        defaultdict(list)
    )
    for report in reports:
        grouped[_decision_group_key(report)].append(report)
    for group in grouped.values():
        _verify_decision_group(group, prediction_by_id)
    payload = [report.model_dump(mode="json") for report in reports]
    encoded = canonical_json_bytes(payload)
    return VerifiedJointExecutionProbabilityCorpusV3(
        encoded_reports=encoded.decode("utf-8"),
        integrity_digest=sha256_hex(encoded),
        _seal=_VERIFIED_CORPUS_SEAL,
    )


def joint_execution_probability_corpus_v3_action_qualified(value: object) -> bool:
    """Accept only the opaque output of the whole-corpus strict verifier."""

    return isinstance(value, VerifiedJointExecutionProbabilityCorpusV3) and bool(value)


def encode_joint_execution_probability_corpus_v3(
    corpus: VerifiedJointExecutionProbabilityCorpusV3,
) -> bytes:
    """Encode only an already replay-verified corpus."""

    if not joint_execution_probability_corpus_v3_action_qualified(corpus):
        raise TypeError("joint execution v3 corpus 未通过 strict verifier")
    encoded = canonical_json_bytes(corpus.reports)
    if sha256_hex(encoded) != corpus.integrity_digest:  # pragma: no cover - immutable invariant
        raise ValueError("joint execution v3 corpus token integrity mismatch")
    return encoded


def decode_and_verify_joint_execution_probability_corpus_v3(
    encoded: bytes,
    predictions: Sequence[Mapping[str, object]],
) -> VerifiedJointExecutionProbabilityCorpusV3:
    """Decode untrusted JSON and perform the same whole-corpus verification."""

    value = decode_json_bytes(encoded)
    return verify_joint_execution_probability_corpus_v3(value, predictions)


def joint_execution_v3_decision_identity_digest(sample_ids: Sequence[str]) -> str:
    """Digest the exact canonical decision identities, rejecting duplicates."""

    normalized = sorted(sample_ids)
    if not normalized or any(not item for item in normalized) or len(set(normalized)) != len(normalized):
        raise ValueError("joint execution decision identities must be nonempty and unique")
    return _digest(normalized)


def joint_execution_v3_observed_label_digest(
    reports: Sequence[DecisionTimeJointExecutionProbabilityEvidenceV3],
) -> str:
    return _digest([_observed_label_row(item) for item in sorted(reports, key=lambda row: row.sample_id)])


def joint_execution_v3_prediction_digest(
    predictions: Sequence[Mapping[str, object]],
) -> str:
    normalized = [deepcopy(dict(item)) for item in sorted(predictions, key=_prediction_sort_key)]
    return _digest(normalized)


def joint_execution_v3_replay_digest(
    *,
    decision_set_digest: str,
    observed_label_digest: str,
    prediction_digest: str,
    out_of_sample_assessment_digest: str,
    fold_id: int,
    training_cutoff: str,
    decision_count: int,
) -> str:
    return _digest(
        {
            "schema_version": "joint-execution-corpus-replay-v1",
            "decision_set_digest": decision_set_digest,
            "observed_label_digest": observed_label_digest,
            "prediction_digest": prediction_digest,
            "out_of_sample_assessment_digest": out_of_sample_assessment_digest,
            "fold_id": fold_id,
            "training_cutoff": training_cutoff,
            "decision_count": decision_count,
        }
    )


def _verify_unique_canonical_order(
    reports: Sequence[DecisionTimeJointExecutionProbabilityEvidenceV3],
    predictions: Sequence[Mapping[str, object]],
) -> None:
    report_ids = [item.sample_id for item in reports]
    prediction_ids = [str(item.get("sample_id") or "") for item in predictions]
    if len(set(report_ids)) != len(report_ids) or len(set(prediction_ids)) != len(prediction_ids):
        raise ValueError("joint execution v3 corpus sample_id 重复")
    expected_reports = sorted(reports, key=lambda item: (item.signal_session, item.sample_id))
    expected_predictions = sorted(predictions, key=_prediction_sort_key)
    if list(reports) != expected_reports or list(predictions) != expected_predictions:
        raise ValueError("joint execution v3 corpus 与 predictions 必须使用 canonical 顺序")
    if set(report_ids) != set(prediction_ids):
        raise ValueError("joint execution v3 corpus 与 predictions identities 不一致")


def _candidate(value: Mapping[str, object], index: int) -> _Candidate:
    expected = {
        "sample_id",
        "symbol",
        "signal_session",
        "generated_at",
        "evidence",
        "entry_state",
        "exit_state",
        "holding_path",
        "costs",
        "observed_outcome",
        "decision_set",
        "probabilities",
    }
    if set(value) != expected:
        raise ValueError(f"joint_execution_candidate[{index}] 字段不符合 exact schema")
    return _Candidate(
        sample_id=str(value.get("sample_id") or ""),
        symbol=str(value.get("symbol") or ""),
        signal_session=str(value.get("signal_session") or ""),
        generated_at=str(value.get("generated_at") or ""),
        evidence=_candidate_field(value, "evidence", index, JointExecutionEvidenceBundle),
        entry_state=_candidate_field(
            value, "entry_state", index, JointExecutionSessionStateEvidenceV3
        ),
        exit_state=_candidate_field(
            value, "exit_state", index, JointExecutionSessionStateEvidenceV3
        ),
        holding_path=_candidate_field(
            value, "holding_path", index, JointExecutionHoldingPathEvidenceV3
        ),
        costs=_candidate_field(
            value, "costs", index, JointExecutionCostEvidenceV3, digest=True
        ),
        observed_outcome=_candidate_field(
            value, "observed_outcome", index, JointExecutionObservedOutcomeV3, digest=True
        ),
        decision_set=_candidate_field(
            value, "decision_set", index, JointExecutionDecisionSetEvidenceV3
        ),
        probabilities=_probability_model(
            _mapping(value.get("probabilities"), f"candidate[{index}].probabilities"),
        ),
    )


def _candidate_field(
    value: Mapping[str, object],
    key: str,
    index: int,
    model_type: type[_ModelT],
    *,
    digest: bool = False,
) -> _ModelT:
    raw = _mapping(value.get(key), f"candidate[{index}].{key}")
    return (
        _typed_digest_model(model_type, raw)
        if digest
        else _typed_model(model_type, raw)
    )


def _build_candidate_group(
    candidates: Sequence[_Candidate],
    prediction_by_id: Mapping[str, Mapping[str, object]],
) -> list[DecisionTimeJointExecutionProbabilityEvidenceV3]:
    first = candidates[0]
    decision_set = first.decision_set
    if any(item.decision_set != decision_set for item in candidates):
        raise ValueError("joint execution v3 candidate group 使用了不同决策全集")
    _verify_candidate_group_population(candidates, decision_set)
    group_predictions = [prediction_by_id[item.sample_id] for item in candidates]
    replay = _candidate_group_replay(candidates, group_predictions, decision_set)
    return [_build_candidate_report(item, replay) for item in candidates]


def _verify_candidate_group_population(
    candidates: Sequence[_Candidate],
    decision_set: JointExecutionDecisionSetEvidenceV3,
) -> None:
    identities = [item.sample_id for item in candidates]
    if decision_set.expected_decision_count != len(candidates):
        raise ValueError("joint execution v3 candidate group 未覆盖固定决策全集")
    if decision_set.decision_identity_digest != joint_execution_v3_decision_identity_digest(
        identities
    ):
        raise ValueError("joint execution v3 candidate group 未覆盖固定决策全集")


def _candidate_group_replay(
    candidates: Sequence[_Candidate],
    group_predictions: Sequence[Mapping[str, object]],
    decision_set: JointExecutionDecisionSetEvidenceV3,
) -> dict[str, object]:
    assessment_digests = {
        item.evidence.calibration.out_of_sample_assessment_digest for item in candidates
    }
    training_cutoffs = {item.evidence.calibration.training_cutoff for item in candidates}
    fold_ids = {
        _positive_integer(item.get("fold_id"), "prediction.fold_id")
        for item in group_predictions
    }
    assessment_digest = _unique_text_boundary(assessment_digests, "assessment")
    training_cutoff = _unique_text_boundary(training_cutoffs, "training")
    fold_id = _unique_integer_boundary(fold_ids, "fold")
    decision_set_digest = _digest(decision_set.model_dump(mode="json"))
    observed_label_digest = _digest(
        [
            _observed_label_values(item.sample_id, item.observed_outcome)
            for item in sorted(candidates, key=lambda row: row.sample_id)
        ]
    )
    prediction_digest = joint_execution_v3_prediction_digest(group_predictions)
    return {
        "fold_id": fold_id,
        "training_cutoff": training_cutoff,
        "decision_count": len(candidates),
        "decision_set_digest": decision_set_digest,
        "observed_label_digest": observed_label_digest,
        "prediction_digest": prediction_digest,
        "out_of_sample_assessment_digest": assessment_digest,
        "corpus_replay_digest": joint_execution_v3_replay_digest(
            decision_set_digest=decision_set_digest,
            observed_label_digest=observed_label_digest,
            prediction_digest=prediction_digest,
            out_of_sample_assessment_digest=assessment_digest,
            fold_id=fold_id,
            training_cutoff=training_cutoff,
            decision_count=len(candidates),
        ),
        "all_decisions_included": True,
        "no_lookahead_verified": True,
        "strict_replay_verified": True,
    }


def _build_candidate_report(
    item: _Candidate,
    replay: Mapping[str, object],
) -> DecisionTimeJointExecutionProbabilityEvidenceV3:
    return build_decision_time_joint_execution_probability_evidence_v3(
        sample_id=item.sample_id,
        symbol=item.symbol,
        signal_session=item.signal_session,
        generated_at=item.generated_at,
        evidence=item.evidence,
        entry_state=item.entry_state,
        exit_state=item.exit_state,
        holding_path=item.holding_path,
        costs=item.costs,
        observed_outcome=item.observed_outcome,
        decision_set=item.decision_set,
        assessment_replay=replay,
        probabilities=item.probabilities,
    )


def _prediction_index(
    predictions: Sequence[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    output: dict[str, Mapping[str, object]] = {}
    for index, prediction in enumerate(predictions):
        row = _mapping(prediction, f"oos_predictions[{index}]")
        sample_id = str(row.get("sample_id") or "")
        if not sample_id:
            raise ValueError("joint execution v3 prediction sample_id 为空")
        _finite_probability(row.get("probability"), "prediction.probability")
        _positive_integer(row.get("fold_id"), "prediction.fold_id")
        outcome = row.get("outcome")
        if not isinstance(outcome, (bool, int)) or isinstance(outcome, float) or int(outcome) not in {0, 1}:
            raise ValueError("joint execution v3 prediction outcome 必须是 0 或 1")
        output[sample_id] = row
    return output


def _verify_prediction_binding(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
    prediction: Mapping[str, object],
) -> None:
    sample_parts = report.sample_id.split(":")
    if report.status != "qualified_shadow":
        raise ValueError("joint execution v3 report status 不是 qualified_shadow")
    if (
        report.signal_session != str(prediction.get("session_date") or "")
        or report.symbol != sample_parts[1]
        or report.decision_set.source_run_id != int(sample_parts[0])
        or report.assessment_replay.fold_id != _positive_integer(
            prediction.get("fold_id"), "prediction.fold_id"
        )
    ):
        raise ValueError("joint execution v3 report 与 prediction identity 冲突")
    probability = _finite_probability(prediction.get("probability"), "prediction.probability")
    if report.probabilities.action_probability is None or not isclose(
        report.probabilities.action_probability,
        probability,
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise ValueError("joint execution v3 action probability 未绑定 OOS prediction")
    outcome = int(cast(bool | int, prediction.get("outcome")))
    if outcome != int(report.observed_outcome.joint_action_positive):
        raise ValueError("joint execution v3 observed action label 未绑定 prediction outcome")
    _verify_prediction_returns(report, prediction)


def _verify_prediction_returns(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
    prediction: Mapping[str, object],
) -> None:
    outcome = report.observed_outcome
    for name in ("net_return", "net_excess_return"):
        observed = getattr(outcome, name)
        predicted = prediction.get(name)
        if observed is None:
            if predicted is not None:
                raise ValueError(f"joint execution v3 unresolved {name} cannot be populated")
            continue
        value = _finite_number(predicted, f"prediction.{name}")
        if not isclose(observed, value, rel_tol=0, abs_tol=1e-12):
            raise ValueError(f"joint execution v3 observed {name} 未绑定 prediction")


def _verify_decision_group(
    reports: Sequence[DecisionTimeJointExecutionProbabilityEvidenceV3],
    prediction_by_id: Mapping[str, Mapping[str, object]],
) -> None:
    first = reports[0]
    decision_set = first.decision_set
    replay = first.assessment_replay
    _verify_report_group_population(reports, decision_set, replay)
    decision_set_digest = _digest(decision_set.model_dump(mode="json"))
    labels_digest = joint_execution_v3_observed_label_digest(reports)
    group_predictions = [prediction_by_id[item.sample_id] for item in reports]
    prediction_digest = joint_execution_v3_prediction_digest(group_predictions)
    assessment_digests = {
        item.evidence.calibration.out_of_sample_assessment_digest for item in reports
    }
    training_cutoffs = {item.evidence.calibration.training_cutoff for item in reports}
    fold_ids = {_positive_integer(item.get("fold_id"), "prediction.fold_id") for item in group_predictions}
    assessment_digest = _unique_text_boundary(assessment_digests, "assessment")
    training_cutoff = _unique_text_boundary(training_cutoffs, "training")
    fold_id = _unique_integer_boundary(fold_ids, "fold")
    expected_replay_digest = joint_execution_v3_replay_digest(
        decision_set_digest=decision_set_digest,
        observed_label_digest=labels_digest,
        prediction_digest=prediction_digest,
        out_of_sample_assessment_digest=assessment_digest,
        fold_id=fold_id,
        training_cutoff=training_cutoff,
        decision_count=len(reports),
    )
    _verify_replay_bindings(
        replay,
        decision_set_digest=decision_set_digest,
        labels_digest=labels_digest,
        prediction_digest=prediction_digest,
        assessment_digest=assessment_digest,
        training_cutoff=training_cutoff,
        fold_id=fold_id,
        expected_replay_digest=expected_replay_digest,
    )


def _verify_report_group_population(
    reports: Sequence[DecisionTimeJointExecutionProbabilityEvidenceV3],
    decision_set: JointExecutionDecisionSetEvidenceV3,
    replay: JointExecutionAssessmentReplayEvidenceV3,
) -> None:
    if any(item.decision_set != decision_set for item in reports):
        raise ValueError("joint execution v3 decision group 使用了不同决策全集")
    if any(item.assessment_replay != replay for item in reports):
        raise ValueError("joint execution v3 decision group 使用了不同重放 digest 摘要")
    identities = [item.sample_id for item in reports]
    if decision_set.expected_decision_count != len(reports) or replay.decision_count != len(
        reports
    ):
        raise ValueError("joint execution v3 未覆盖固定决策全集")
    if decision_set.decision_identity_digest != joint_execution_v3_decision_identity_digest(
        identities
    ):
        raise ValueError("joint execution v3 未覆盖固定决策全集")


def _verify_replay_bindings(
    replay: JointExecutionAssessmentReplayEvidenceV3,
    *,
    decision_set_digest: str,
    labels_digest: str,
    prediction_digest: str,
    assessment_digest: str,
    training_cutoff: str,
    fold_id: int,
    expected_replay_digest: str,
) -> None:
    if (
        replay.decision_set_digest != decision_set_digest
        or replay.observed_label_digest != labels_digest
        or replay.prediction_digest != prediction_digest
        or replay.out_of_sample_assessment_digest != assessment_digest
        or replay.training_cutoff != training_cutoff
        or replay.fold_id != fold_id
        or replay.corpus_replay_digest != expected_replay_digest
        or not replay.all_decisions_included
        or not replay.no_lookahead_verified
        or not replay.strict_replay_verified
    ):
        raise ValueError("joint execution v3 whole-corpus replay digest 不一致")


def _unique_text_boundary(values: set[str | None], label: str) -> str:
    if len(values) != 1 or None in values:
        raise ValueError(f"joint execution v3 group {label} 边界不唯一")
    return cast(str, next(iter(values)))


def _unique_integer_boundary(values: set[int], label: str) -> int:
    if len(values) != 1:
        raise ValueError(f"joint execution v3 group {label} 边界不唯一")
    return next(iter(values))


def _decision_group_key(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
) -> tuple[object, ...]:
    value = report.decision_set
    return (value.source_run_id, value.signal_session, value.horizon, value.target)


def _observed_label_row(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
) -> dict[str, object]:
    return _observed_label_values(report.sample_id, report.observed_outcome)


def _observed_label_values(
    sample_id: str,
    outcome: JointExecutionObservedOutcomeV3,
) -> dict[str, object]:
    return {
        "sample_id": sample_id,
        "entry_fill": outcome.entry_fill,
        "exit_executable": outcome.exit_executable,
        "net_positive": outcome.net_positive,
        "joint_action_positive": outcome.joint_action_positive,
        "net_return": outcome.net_return,
        "net_excess_return": outcome.net_excess_return,
        "observed_at": outcome.observed_at,
        "outcome_evidence_digest": outcome.evidence_digest,
    }


def _prediction_sort_key(value: Mapping[str, object]) -> tuple[str, str]:
    return (str(value.get("session_date") or ""), str(value.get("sample_id") or ""))


def _digest(value: object) -> str:
    try:
        return sha256_hex(canonical_json_bytes(value))
    except Exception as exc:
        raise ValueError("joint execution v3 value is not finite canonical JSON") from exc


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} 必须是 object")
    return cast(Mapping[str, object], value)


def _model(model_type: type[BaseModel], value: BaseModel | Mapping[str, object]) -> BaseModel:
    if isinstance(value, model_type):
        return value
    return model_type.model_validate(dict(value))


def _typed_model(
    model_type: type[_ModelT],
    value: BaseModel | Mapping[str, object],
) -> _ModelT:
    return cast(_ModelT, _model(model_type, value))


def _digest_model(model_type: type[BaseModel], value: BaseModel | Mapping[str, object]) -> BaseModel:
    if isinstance(value, model_type):
        return value
    payload = dict(value)
    payload.setdefault("evidence_digest", joint_execution_v3_content_digest(payload))
    return model_type.model_validate(payload)


def _typed_digest_model(
    model_type: type[_ModelT],
    value: BaseModel | Mapping[str, object],
) -> _ModelT:
    return cast(_ModelT, _digest_model(model_type, value))


def _probability_model(
    value: JointExecutionProbabilityComponents | Mapping[str, object] | None,
) -> JointExecutionProbabilityComponents:
    if value is None:
        return JointExecutionProbabilityComponents()
    if isinstance(value, JointExecutionProbabilityComponents):
        return value
    return JointExecutionProbabilityComponents.model_validate(dict(value))


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} 必须是正整数")
    return value


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是有限数值")
    output = float(value)
    if not isfinite(output):
        raise ValueError(f"{label} 必须是有限数值")
    return output


def _finite_probability(value: object, label: str) -> float:
    output = _finite_number(value, label)
    if not 0 <= output <= 1:
        raise ValueError(f"{label} 必须在 [0, 1]")
    return output


__all__ = [
    "VerifiedJointExecutionProbabilityCorpusV3",
    "build_decision_time_joint_execution_probability_evidence_v3",
    "build_joint_execution_probability_corpus_v3",
    "decode_and_verify_joint_execution_probability_corpus_v3",
    "encode_joint_execution_probability_corpus_v3",
    "joint_execution_probability_corpus_v3_action_qualified",
    "joint_execution_v3_decision_identity_digest",
    "joint_execution_v3_observed_label_digest",
    "joint_execution_v3_prediction_digest",
    "joint_execution_v3_replay_digest",
    "verify_joint_execution_probability_corpus_v3",
    "verify_joint_execution_probability_evidence_v3",
]
