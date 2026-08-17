"""Builders and strict parsers for joint execution probability evidence."""

from __future__ import annotations

from collections.abc import Mapping

from app.artifacts.io import canonical_json_bytes, decode_json_bytes
from app.models.joint_execution_probability import (
    DecisionTimeJointExecutionProbabilityEvidence,
    JointExecutionEvidenceBundle,
    JointExecutionProbabilityComponents,
    JointExecutionProbabilityEstimand,
    assess_joint_execution_probability_gate,
    joint_execution_evidence_findings,
    joint_execution_probability_evidence_digest,
)


def build_decision_time_joint_execution_probability_evidence(
    *,
    sample_id: str,
    symbol: str,
    signal_session: str,
    generated_at: str,
    evidence: JointExecutionEvidenceBundle | Mapping[str, object],
    probabilities: JointExecutionProbabilityComponents | Mapping[str, object] | None = None,
) -> DecisionTimeJointExecutionProbabilityEvidence:
    """Build one report, stripping probability values whenever evidence is not qualified."""

    bound_evidence = _evidence_model(evidence)
    requested = _probability_model(probabilities)
    if joint_execution_evidence_findings(bound_evidence, signal_session=signal_session):
        requested = JointExecutionProbabilityComponents()
    status, findings = assess_joint_execution_probability_gate(
        bound_evidence,
        signal_session=signal_session,
        probabilities=requested,
    )
    payload: dict[str, object] = {
        "schema_version": "decision-time-joint-execution-probability-v2",
        "sample_id": sample_id,
        "symbol": symbol,
        "signal_session": signal_session,
        "generated_at": generated_at,
        "status": status,
        "estimand": JointExecutionProbabilityEstimand().model_dump(mode="json"),
        "evidence": bound_evidence.model_dump(mode="json"),
        "probabilities": requested.model_dump(mode="json"),
        "gate_findings": [item.model_dump(mode="json") for item in findings],
        "production_effect": "none",
    }
    payload["canonical_digest"] = joint_execution_probability_evidence_digest(payload)
    return DecisionTimeJointExecutionProbabilityEvidence.model_validate(payload)


def verify_joint_execution_probability_evidence(
    value: Mapping[str, object],
) -> DecisionTimeJointExecutionProbabilityEvidence:
    """Validate exact schema, cross-field gates, and canonical content digest."""

    return DecisionTimeJointExecutionProbabilityEvidence.model_validate(dict(value))


def decode_joint_execution_probability_evidence(
    encoded: bytes,
) -> DecisionTimeJointExecutionProbabilityEvidence:
    """Decode untrusted JSON while rejecting duplicate keys and non-finite constants."""

    value = decode_json_bytes(encoded)
    if not isinstance(value, Mapping):
        raise ValueError("joint execution probability evidence root must be an object")
    return verify_joint_execution_probability_evidence(value)


def encode_joint_execution_probability_evidence(
    report: DecisionTimeJointExecutionProbabilityEvidence,
) -> bytes:
    """Return deterministic finite canonical JSON bytes after full revalidation."""

    verified = verify_joint_execution_probability_evidence(report.model_dump(mode="json"))
    return canonical_json_bytes(verified.model_dump(mode="json"))


def joint_execution_probability_action_qualified(value: object) -> bool:
    """Always fail closed until observed labels and strict assessment replay exist."""

    try:
        if isinstance(value, DecisionTimeJointExecutionProbabilityEvidence):
            verify_joint_execution_probability_evidence(value.model_dump(mode="json"))
        elif isinstance(value, Mapping):
            verify_joint_execution_probability_evidence(value)
        else:
            return False
    except (TypeError, ValueError):
        return False
    return False


def _evidence_model(
    value: JointExecutionEvidenceBundle | Mapping[str, object],
) -> JointExecutionEvidenceBundle:
    if isinstance(value, JointExecutionEvidenceBundle):
        return value
    return JointExecutionEvidenceBundle.model_validate(dict(value))


def _probability_model(
    value: JointExecutionProbabilityComponents | Mapping[str, object] | None,
) -> JointExecutionProbabilityComponents:
    if value is None:
        return JointExecutionProbabilityComponents()
    if isinstance(value, JointExecutionProbabilityComponents):
        return value
    return JointExecutionProbabilityComponents.model_validate(dict(value))


__all__ = [
    "build_decision_time_joint_execution_probability_evidence",
    "decode_joint_execution_probability_evidence",
    "encode_joint_execution_probability_evidence",
    "joint_execution_probability_action_qualified",
    "verify_joint_execution_probability_evidence",
]
