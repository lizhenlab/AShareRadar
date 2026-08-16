"""Fail-closed evidence contract for decision-time joint execution probability."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime, time
from math import isclose
from typing import cast, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex


JointProbabilityStatus = Literal["unavailable", "audit_only"]
GateSeverity = Literal["audit_only", "unavailable"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class JointExecutionProbabilityEstimand(_StrictModel):
    contract_version: Literal["decision-time-joint-execution-estimand-v1"] = (
        "decision-time-joint-execution-estimand-v1"
    )
    information_set: Literal["completed_session_D_only"] = "completed_session_D_only"
    entry_component: Literal["P(entry_fill|I_D)"] = "P(entry_fill|I_D)"
    exit_component: Literal["P(exit_executable|entry_fill,I_D)"] = (
        "P(exit_executable|entry_fill,I_D)"
    )
    net_component: Literal["P(net_positive|entry_fill,exit_executable,I_D)"] = (
        "P(net_positive|entry_fill,exit_executable,I_D)"
    )
    joint_formula: Literal[
        "P(entry_fill|I_D)*P(exit_executable|entry_fill,I_D)"
        "*P(net_positive|entry_fill,exit_executable,I_D)"
    ] = (
        "P(entry_fill|I_D)*P(exit_executable|entry_fill,I_D)"
        "*P(net_positive|entry_fill,exit_executable,I_D)"
    )
    action_event: Literal[
        "entry_fill_and_exit_executable_and_round_trip_net_return_positive"
    ] = "entry_fill_and_exit_executable_and_round_trip_net_return_positive"
    target_population: Literal[
        "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"
    ] = "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"


class JointExecutionSessionBarEvidence(_StrictModel):
    role: Literal["entry", "exit"]
    session_date: str
    session_offset_from_signal: int = Field(ge=1, le=21)
    source_kind: Literal[
        "official_exchange_daily_ohlcv_amount",
        "vendor_adjusted_daily",
        "other_daily_source",
        "unknown",
    ]
    adjustment_mode: Literal["none", "qfq", "hfq", "unknown"]
    open: float | None = Field(default=None, gt=0)
    high: float | None = Field(default=None, gt=0)
    low: float | None = Field(default=None, gt=0)
    close: float | None = Field(default=None, gt=0)
    volume: float | None = Field(default=None, ge=0)
    amount: float | None = Field(default=None, ge=0)
    source_dataset_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        _iso_date(self.session_date, "session_date")
        prices = (self.open, self.high, self.low, self.close)
        if all(value is not None for value in prices):
            open_price, high, low, close = cast(tuple[float, float, float, float], prices)
            if low > high or not all(low <= value <= high for value in (open_price, close)):
                raise ValueError("entry/exit official bar price bounds are inconsistent")
        return self


class JointExecutionRuleEvidence(_StrictModel):
    role: Literal["entry", "exit"]
    session_date: str
    source_kind: Literal["official_effective_dated", "signal_date_static_proxy", "unknown"]
    effective_date: str | None = None
    board: Literal["main", "chinext", "star", "beijing", "other"] | None = None
    is_st: bool | None = None
    listing_status: Literal["listed", "delisting_period", "delisted"] | None = None
    board_rule_id: str | None = Field(default=None, min_length=1)
    st_rule_id: str | None = Field(default=None, min_length=1)
    delisting_rule_id: str | None = Field(default=None, min_length=1)
    ruleset_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        _iso_date(self.session_date, "session_date")
        if self.effective_date is not None:
            _iso_date(self.effective_date, "effective_date")
        return self


class JointExecutionReferencePriceEvidence(_StrictModel):
    role: Literal["entry", "exit"]
    session_date: str
    basis: Literal[
        "official_unadjusted_reference_with_effective_corporate_action",
        "adjusted_series_previous_close_proxy",
        "unknown",
    ]
    previous_close: float | None = Field(default=None, gt=0)
    reference_price: float | None = Field(default=None, gt=0)
    corporate_action_status: Literal["none", "effective_event", "unknown"]
    reference_price_rule_id: str | None = Field(default=None, min_length=1)
    source_dataset_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_date(self) -> Self:
        _iso_date(self.session_date, "session_date")
        return self


class JointExecutionParticipationEvidence(_StrictModel):
    basis: Literal[
        "entry_and_exit_same_session_amount",
        "signal_session_D_amount_proxy",
        "posthoc_filled_only",
        "unknown",
    ]
    entry_order_notional: float = Field(gt=0)
    entry_session_amount: float | None = Field(default=None, ge=0)
    entry_participation_rate: float | None = Field(default=None, ge=0)
    exit_order_notional: float = Field(gt=0)
    exit_session_amount: float | None = Field(default=None, ge=0)
    exit_participation_rate: float | None = Field(default=None, ge=0)
    maximum_participation_rate: float = Field(gt=0, le=1)
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_rates(self) -> Self:
        _validate_participation_rate(
            self.entry_order_notional,
            self.entry_session_amount,
            self.entry_participation_rate,
            "entry",
        )
        _validate_participation_rate(
            self.exit_order_notional,
            self.exit_session_amount,
            self.exit_participation_rate,
            "exit",
        )
        return self


class JointExecutionBenchmarkEvidence(_StrictModel):
    universe_basis: Literal["fixed_full_market_at_signal", "dynamic_eligible_universe", "unknown"]
    outcome_population: Literal["all_decisions", "posthoc_filled_only", "unknown"]
    benchmark_method: Literal[
        "fixed_universe_leave_one_out",
        "external_predeclared_benchmark",
        "dynamic_modelled_mean",
        "unknown",
    ]
    universe_frozen_before_outcomes: bool
    benchmark_predeclared: bool
    subject_excluded: bool
    universe_definition_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    universe_membership_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_cohort_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    benchmark_series_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class JointExecutionCalibrationEvidence(_StrictModel):
    estimator_contract: Literal[
        "three_component_joint_chain",
        "legacy_conditional_filled_only",
        "unknown",
    ]
    training_cutoff: str | None = None
    prediction_generated_at: str | None = None
    entry_model_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    exit_model_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    net_model_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    calibrator_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    feature_schema_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_information_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    out_of_sample_assessment_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    out_of_sample_verified: bool
    calibration_verified: bool
    selection_qualified: bool

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        if self.training_cutoff is not None:
            _iso_date(self.training_cutoff, "training_cutoff")
        if self.prediction_generated_at is not None:
            _aware_datetime(self.prediction_generated_at, "prediction_generated_at")
        return self


class JointExecutionEvidenceBundle(_StrictModel):
    entry_bar: JointExecutionSessionBarEvidence
    exit_bar: JointExecutionSessionBarEvidence
    entry_rules: JointExecutionRuleEvidence
    exit_rules: JointExecutionRuleEvidence
    entry_reference: JointExecutionReferencePriceEvidence
    exit_reference: JointExecutionReferencePriceEvidence
    participation: JointExecutionParticipationEvidence
    benchmark: JointExecutionBenchmarkEvidence
    calibration: JointExecutionCalibrationEvidence

    @model_validator(mode="after")
    def validate_roles_and_sessions(self) -> Self:
        _require_role(self.entry_bar, "entry")
        _require_role(self.exit_bar, "exit")
        _require_role(self.entry_rules, "entry")
        _require_role(self.exit_rules, "exit")
        _require_role(self.entry_reference, "entry")
        _require_role(self.exit_reference, "exit")
        _require_same_session(self.entry_bar, self.entry_rules, self.entry_reference)
        _require_same_session(self.exit_bar, self.exit_rules, self.exit_reference)
        return self


class JointExecutionProbabilityComponents(_StrictModel):
    entry_fill_probability: float | None = Field(default=None, ge=0, le=1)
    exit_executable_given_entry_probability: float | None = Field(default=None, ge=0, le=1)
    net_positive_given_entry_and_exit_probability: float | None = Field(default=None, ge=0, le=1)
    joint_net_positive_probability: float | None = Field(default=None, ge=0, le=1)
    action_probability: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_joint_probability(self) -> Self:
        values = _probability_values(self)
        if all(value is None for value in values):
            return self
        if any(value is None for value in values):
            raise ValueError("joint execution probability components must be all-null or all-present")
        entry, exit_probability, net, joint, action = cast(tuple[float, float, float, float, float], values)
        if not isclose(joint, entry * exit_probability * net, rel_tol=0, abs_tol=1e-12):
            raise ValueError("joint probability must equal the three conditional components")
        if not isclose(action, joint, rel_tol=0, abs_tol=1e-12):
            raise ValueError("action probability must equal joint net-positive probability")
        return self

    def is_null(self) -> bool:
        return all(value is None for value in _probability_values(self))


class JointExecutionGateFinding(_StrictModel):
    code: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    severity: GateSeverity


class DecisionTimeJointExecutionProbabilityEvidence(_StrictModel):
    schema_version: Literal["decision-time-joint-execution-probability-v2"] = (
        "decision-time-joint-execution-probability-v2"
    )
    sample_id: str = Field(min_length=1, max_length=160)
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    signal_session: str
    generated_at: str
    status: JointProbabilityStatus
    estimand: JointExecutionProbabilityEstimand
    evidence: JointExecutionEvidenceBundle
    probabilities: JointExecutionProbabilityComponents
    gate_findings: list[JointExecutionGateFinding]
    production_effect: Literal["none"] = "none"
    canonical_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        horizon = _validate_sample_identity(self)
        _validate_report_times(self)
        _validate_horizon_maturity(self, horizon)
        expected_status, expected_findings = assess_joint_execution_probability_gate(
            self.evidence,
            signal_session=self.signal_session,
            probabilities=self.probabilities,
        )
        if self.status != expected_status or self.gate_findings != list(expected_findings):
            raise ValueError("joint execution probability gate state is inconsistent")
        if not self.probabilities.is_null():
            raise ValueError("joint execution contract skeleton cannot expose probability values")
        if self.canonical_digest != joint_execution_probability_evidence_digest(self):
            raise ValueError("joint execution probability evidence digest mismatch")
        return self


def assess_joint_execution_probability_gate(
    evidence: JointExecutionEvidenceBundle,
    *,
    signal_session: str,
    probabilities: JointExecutionProbabilityComponents,
) -> tuple[JointProbabilityStatus, tuple[JointExecutionGateFinding, ...]]:
    """Derive the only valid authorization state from bound evidence."""

    findings = joint_execution_evidence_findings(evidence, signal_session=signal_session)
    if not probabilities.is_null():
        raise ValueError("joint execution contract skeleton cannot carry probabilities")
    return _status_for_findings(findings), findings


def joint_execution_evidence_findings(
    evidence: JointExecutionEvidenceBundle,
    *,
    signal_session: str,
) -> tuple[JointExecutionGateFinding, ...]:
    """Return deterministic evidence-only findings without inspecting probabilities."""

    _iso_date(signal_session, "signal_session")
    return _evidence_findings(evidence, signal_session)


def joint_execution_probability_evidence_digest(
    value: BaseModel | Mapping[str, object],
) -> str:
    """Hash finite canonical JSON while excluding the digest field itself."""

    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = deepcopy(dict(value))
    else:
        raise TypeError("joint execution probability evidence must be a model or mapping")
    payload.pop("canonical_digest", None)
    return sha256_hex(canonical_json_bytes(payload))


def _evidence_findings(
    evidence: JointExecutionEvidenceBundle,
    signal_session: str,
) -> tuple[JointExecutionGateFinding, ...]:
    findings = [
        *_bar_findings(evidence.entry_bar),
        *_bar_findings(evidence.exit_bar),
        *_rule_findings(evidence.entry_rules),
        *_rule_findings(evidence.exit_rules),
        *_reference_findings(evidence.entry_reference),
        *_reference_findings(evidence.exit_reference),
        *_participation_findings(evidence.participation),
        *_benchmark_findings(evidence.benchmark),
        *_calibration_findings(evidence.calibration, signal_session),
        _finding("observed_joint_outcome_components_unavailable", "unavailable"),
        _finding("strict_joint_assessment_replay_not_verified", "unavailable"),
    ]
    unique = {(item.code, item.severity): item for item in findings}
    return tuple(sorted(unique.values(), key=lambda item: (item.code, item.severity)))


def _bar_findings(bar: JointExecutionSessionBarEvidence) -> list[JointExecutionGateFinding]:
    findings: list[JointExecutionGateFinding] = []
    if bar.source_kind == "unknown":
        findings.append(_finding(f"{bar.role}_bar_source_unknown", "unavailable"))
    elif bar.source_kind != "official_exchange_daily_ohlcv_amount":
        findings.append(_finding(f"{bar.role}_bar_source_not_official", "audit_only"))
    if bar.adjustment_mode == "unknown":
        findings.append(_finding(f"{bar.role}_adjustment_unknown", "unavailable"))
    elif bar.adjustment_mode != "none":
        findings.append(_finding(f"{bar.role}_adjustment_{bar.adjustment_mode}", "audit_only"))
    if any(value is None for value in (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount)):
        findings.append(_finding(f"{bar.role}_ohlcv_amount_missing", "unavailable"))
    if bar.source_dataset_digest is None:
        findings.append(_finding(f"{bar.role}_bar_digest_missing", "unavailable"))
    return findings


def _rule_findings(rules: JointExecutionRuleEvidence) -> list[JointExecutionGateFinding]:
    findings: list[JointExecutionGateFinding] = []
    if rules.source_kind == "unknown":
        findings.append(_finding(f"{rules.role}_rules_source_unknown", "unavailable"))
    elif rules.source_kind != "official_effective_dated":
        findings.append(_finding(f"{rules.role}_rules_static_proxy", "audit_only"))
    required = (
        rules.effective_date,
        rules.board,
        rules.is_st,
        rules.listing_status,
        rules.board_rule_id,
        rules.st_rule_id,
        rules.delisting_rule_id,
        rules.ruleset_digest,
    )
    if any(value is None for value in required):
        findings.append(_finding(f"{rules.role}_effective_rules_missing", "unavailable"))
    elif rules.effective_date != rules.session_date:
        findings.append(_finding(f"{rules.role}_effective_date_mismatch", "unavailable"))
    return findings


def _reference_findings(
    evidence: JointExecutionReferencePriceEvidence,
) -> list[JointExecutionGateFinding]:
    findings: list[JointExecutionGateFinding] = []
    if evidence.basis == "unknown":
        findings.append(_finding(f"{evidence.role}_reference_basis_unknown", "unavailable"))
    elif evidence.basis != "official_unadjusted_reference_with_effective_corporate_action":
        findings.append(_finding(f"{evidence.role}_reference_adjusted_proxy", "audit_only"))
    required = (
        evidence.previous_close,
        evidence.reference_price,
        evidence.reference_price_rule_id,
        evidence.source_dataset_digest,
    )
    if evidence.corporate_action_status == "unknown" or any(value is None for value in required):
        findings.append(_finding(f"{evidence.role}_reference_evidence_missing", "unavailable"))
    return findings


def _participation_findings(
    evidence: JointExecutionParticipationEvidence,
) -> list[JointExecutionGateFinding]:
    findings: list[JointExecutionGateFinding] = []
    if evidence.basis == "unknown":
        findings.append(_finding("participation_basis_unknown", "unavailable"))
    elif evidence.basis != "entry_and_exit_same_session_amount":
        findings.append(_finding(f"participation_{evidence.basis.lower()}", "audit_only"))
    if evidence.entry_session_amount is None:
        findings.append(_finding("entry_session_amount_missing", "unavailable"))
    if evidence.exit_session_amount is None:
        findings.append(_finding("exit_session_amount_missing", "unavailable"))
    if evidence.evidence_digest is None:
        findings.append(_finding("participation_digest_missing", "unavailable"))
    return findings


def _benchmark_findings(
    evidence: JointExecutionBenchmarkEvidence,
) -> list[JointExecutionGateFinding]:
    findings: list[JointExecutionGateFinding] = []
    findings.extend(_benchmark_basis_findings(evidence))
    digests = (
        evidence.universe_definition_digest,
        evidence.universe_membership_digest,
        evidence.decision_cohort_digest,
        evidence.benchmark_series_digest,
    )
    if any(value is None for value in digests):
        findings.append(_finding("benchmark_or_universe_digest_missing", "unavailable"))
    if not evidence.universe_frozen_before_outcomes:
        findings.append(_finding("universe_not_frozen_before_outcomes", "unavailable"))
    if not evidence.benchmark_predeclared:
        findings.append(_finding("benchmark_not_predeclared", "unavailable"))
    if evidence.benchmark_method == "fixed_universe_leave_one_out" and not evidence.subject_excluded:
        findings.append(_finding("leave_one_out_subject_not_excluded", "unavailable"))
    return findings


def _benchmark_basis_findings(
    evidence: JointExecutionBenchmarkEvidence,
) -> list[JointExecutionGateFinding]:
    findings: list[JointExecutionGateFinding] = []
    if evidence.universe_basis == "unknown":
        findings.append(_finding("universe_basis_unknown", "unavailable"))
    elif evidence.universe_basis != "fixed_full_market_at_signal":
        findings.append(_finding("universe_dynamic_eligible", "audit_only"))
    if evidence.outcome_population == "unknown":
        findings.append(_finding("outcome_population_unknown", "unavailable"))
    elif evidence.outcome_population != "all_decisions":
        findings.append(_finding("outcome_population_posthoc_filled_only", "audit_only"))
    if evidence.benchmark_method == "unknown":
        findings.append(_finding("benchmark_method_unknown", "unavailable"))
    elif evidence.benchmark_method == "dynamic_modelled_mean":
        findings.append(_finding("benchmark_dynamic_modelled_mean", "audit_only"))
    return findings


def _calibration_findings(
    evidence: JointExecutionCalibrationEvidence,
    signal_session: str,
) -> list[JointExecutionGateFinding]:
    findings: list[JointExecutionGateFinding] = []
    if evidence.estimator_contract == "unknown":
        findings.append(_finding("estimator_contract_unknown", "unavailable"))
    elif evidence.estimator_contract != "three_component_joint_chain":
        findings.append(_finding("estimator_legacy_conditional_filled_only", "audit_only"))
    required = _calibration_required_values(evidence)
    if any(value is None for value in required):
        findings.append(_finding("calibration_evidence_missing", "unavailable"))
    findings.extend(_calibration_time_findings(evidence, signal_session))
    for verified, code in (
        (evidence.out_of_sample_verified, "out_of_sample_not_verified"),
        (evidence.calibration_verified, "calibration_not_verified"),
        (evidence.selection_qualified, "selection_not_qualified"),
    ):
        if not verified:
            findings.append(_finding(code, "audit_only"))
    return findings


def _calibration_required_values(
    evidence: JointExecutionCalibrationEvidence,
) -> tuple[str | None, ...]:
    return (
        evidence.training_cutoff,
        evidence.prediction_generated_at,
        evidence.entry_model_digest,
        evidence.exit_model_digest,
        evidence.net_model_digest,
        evidence.calibrator_digest,
        evidence.feature_schema_digest,
        evidence.decision_information_digest,
        evidence.out_of_sample_assessment_digest,
    )


def _calibration_time_findings(
    evidence: JointExecutionCalibrationEvidence,
    signal_session: str,
) -> list[JointExecutionGateFinding]:
    findings: list[JointExecutionGateFinding] = []
    if evidence.training_cutoff is not None and evidence.training_cutoff >= signal_session:
        findings.append(_finding("training_cutoff_not_before_signal", "unavailable"))
    if evidence.prediction_generated_at is not None:
        prediction_date = _aware_datetime(evidence.prediction_generated_at, "prediction_generated_at").date()
        if prediction_date.isoformat() > signal_session:
            findings.append(_finding("prediction_generated_after_signal", "unavailable"))
    return findings


def _validate_report_times(report: DecisionTimeJointExecutionProbabilityEvidence) -> None:
    signal = _iso_date(report.signal_session, "signal_session")
    entry = _iso_date(report.evidence.entry_bar.session_date, "entry_session")
    exit_session = _iso_date(report.evidence.exit_bar.session_date, "exit_session")
    generated = _aware_datetime(report.generated_at, "generated_at")
    if not signal < entry <= exit_session:
        raise ValueError("signal, entry and exit sessions must be strictly forward ordered")
    market_generated = generated.astimezone(ZoneInfo("Asia/Shanghai"))
    if market_generated.date() < exit_session:
        raise ValueError("historical joint execution evidence cannot be generated before exit")
    if market_generated.date() == exit_session and market_generated.time() <= time(15, 0):
        raise ValueError("historical joint execution evidence requires after-close maturity")


def _validate_sample_identity(report: DecisionTimeJointExecutionProbabilityEvidence) -> int:
    parts = report.sample_id.split(":")
    if len(parts) != 4 or not parts[0].isdigit() or not parts[2].isdigit():
        raise ValueError("joint execution sample_id must be run:symbol:horizon:target")
    run_id, symbol, horizon_text, target = parts
    horizon = int(horizon_text)
    if run_id != str(int(run_id)) or int(run_id) <= 0 or symbol != report.symbol:
        raise ValueError("joint execution sample_id identity mismatch")
    if horizon not in {1, 5, 20}:
        raise ValueError("joint execution sample_id horizon must be 1, 5, or 20")
    if target not in {"net_excess_positive", "net_return_positive"}:
        raise ValueError("joint execution sample_id target is not a production probability target")
    return horizon


def _validate_horizon_maturity(
    report: DecisionTimeJointExecutionProbabilityEvidence, horizon: int,
) -> None:
    signal = _iso_date(report.signal_session, "signal_session")
    exit_session = _iso_date(report.evidence.exit_bar.session_date, "exit_session")
    if report.evidence.entry_bar.session_offset_from_signal != 1:
        raise ValueError("joint execution entry session offset must be D+1")
    if report.evidence.exit_bar.session_offset_from_signal != horizon + 1:
        raise ValueError("joint execution exit session offset must equal horizon+1")
    if (exit_session - signal).days < horizon + 1:
        raise ValueError("joint execution exit session is too early for sample horizon")


def _validate_participation_rate(
    notional: float,
    session_amount: float | None,
    rate: float | None,
    label: str,
) -> None:
    if session_amount is None:
        if rate is not None:
            raise ValueError(f"{label} participation rate requires same-session amount")
        return
    if session_amount == 0:
        if rate is not None:
            raise ValueError(f"{label} participation rate must be null when session amount is zero")
        return
    if rate is None or not isclose(rate, notional / session_amount, rel_tol=0, abs_tol=1e-12):
        raise ValueError(f"{label} participation rate does not match notional/session amount")


def _require_role(value: object, expected: Literal["entry", "exit"]) -> None:
    if getattr(value, "role", None) != expected:
        raise ValueError(f"joint execution evidence role must be {expected}")


def _require_same_session(first: object, *others: object) -> None:
    session = getattr(first, "session_date", None)
    if any(getattr(item, "session_date", None) != session for item in others):
        raise ValueError("bar, rule and reference evidence sessions must match")


def _probability_values(
    value: JointExecutionProbabilityComponents,
) -> tuple[float | None, ...]:
    return (
        value.entry_fill_probability,
        value.exit_executable_given_entry_probability,
        value.net_positive_given_entry_and_exit_probability,
        value.joint_net_positive_probability,
        value.action_probability,
    )


def _finding(code: str, severity: GateSeverity) -> JointExecutionGateFinding:
    return JointExecutionGateFinding(code=code, severity=severity)


def _status_for_findings(
    findings: Sequence[JointExecutionGateFinding],
) -> JointProbabilityStatus:
    if any(item.severity == "unavailable" for item in findings):
        return "unavailable"
    return "audit_only"


def _iso_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return parsed


__all__ = [
    "DecisionTimeJointExecutionProbabilityEvidence",
    "JointExecutionBenchmarkEvidence",
    "JointExecutionCalibrationEvidence",
    "JointExecutionEvidenceBundle",
    "JointExecutionGateFinding",
    "JointExecutionParticipationEvidence",
    "JointExecutionProbabilityComponents",
    "JointExecutionProbabilityEstimand",
    "JointExecutionReferencePriceEvidence",
    "JointExecutionRuleEvidence",
    "JointExecutionSessionBarEvidence",
    "JointProbabilityStatus",
    "assess_joint_execution_probability_gate",
    "joint_execution_evidence_findings",
    "joint_execution_probability_evidence_digest",
]
