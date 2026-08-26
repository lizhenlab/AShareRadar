"""Observed all-decisions contract for joint execution probability research.

Version 2 intentionally remains an audit-only skeleton.  Version 3 adds the
observed components and full-decision-set bindings required by the production
filter authorization replay.  A valid report is still only a corpus candidate;
authorization requires the sealed whole-corpus verifier result from the service
module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime, time
from math import isclose, prod
from typing import Literal, Self, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.joint_execution_probability import (
    JointExecutionEvidenceBundle,
    JointExecutionGateFinding,
    JointExecutionProbabilityComponents,
    joint_execution_base_evidence_findings,
)


JointExecutionV3Status = Literal["unavailable", "qualified_shadow"]
JointExecutionState = Literal[
    "executable",
    "suspended",
    "locked_limit",
    "no_bar",
    "rule_ineligible",
    "capacity_exceeded",
]
ExchangeSessionState = Literal["trading", "suspended", "not_listed", "delisted"]
ProbabilityTarget = Literal["net_excess_positive", "net_return_positive"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class JointExecutionProbabilityEstimandV3(_StrictModel):
    contract_version: Literal["decision-time-joint-execution-estimand-v3"] = (
        "decision-time-joint-execution-estimand-v3"
    )
    information_set: Literal["completed_session_D_only"] = "completed_session_D_only"
    registered_net_target: ProbabilityTarget
    entry_component: Literal["P(entry_fill|I_D)"] = "P(entry_fill|I_D)"
    exit_component: Literal["P(exit_executable|entry_fill,I_D)"] = (
        "P(exit_executable|entry_fill,I_D)"
    )
    net_component: Literal[
        "P(registered_net_target_positive|entry_fill,exit_executable,I_D)"
    ] = "P(registered_net_target_positive|entry_fill,exit_executable,I_D)"
    joint_formula: Literal[
        "P(entry_fill|I_D)*P(exit_executable|entry_fill,I_D)"
        "*P(registered_net_target_positive|entry_fill,exit_executable,I_D)"
    ] = (
        "P(entry_fill|I_D)*P(exit_executable|entry_fill,I_D)"
        "*P(registered_net_target_positive|entry_fill,exit_executable,I_D)"
    )
    action_event: Literal[
        "entry_fill_and_exit_executable_and_registered_net_target_positive"
    ] = "entry_fill_and_exit_executable_and_registered_net_target_positive"
    target_population: Literal[
        "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"
    ] = "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"


class JointExecutionHoldingPathStepEvidenceV3(_StrictModel):
    offset_from_signal: int = Field(ge=1, le=21)
    session_date: str
    official_session_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_raw_file_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_row_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_reference_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    corporate_action_status: Literal["none", "effective_event"]
    corporate_action_event_id: str | None = None
    previous_close: float = Field(gt=0)
    reference_price: float = Field(gt=0)
    reference_continuity_factor: float = Field(gt=0)
    applied_to_holding_return: bool
    step_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_step(self) -> Self:
        _iso_date(self.session_date, "holding_path.step.session_date")
        if (self.corporate_action_status == "effective_event") is not bool(
            self.corporate_action_event_id
        ):
            raise ValueError("holding-path corporate-action event identity mismatch")
        if not isclose(
            self.reference_continuity_factor,
            self.previous_close / self.reference_price,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise ValueError("holding-path reference continuity factor mismatch")
        if self.applied_to_holding_return is not (self.offset_from_signal > 1):
            raise ValueError("entry-session corporate action cannot be reapplied")
        if self.step_digest != _content_digest_without(self, "step_digest"):
            raise ValueError("holding-path step digest mismatch")
        return self


class JointExecutionHoldingPathEvidenceV3(_StrictModel):
    horizon: Literal[1, 5, 20]
    entry_session: str
    exit_session: str
    steps: list[JointExecutionHoldingPathStepEvidenceV3] = Field(
        min_length=2, max_length=21
    )
    corporate_action_factor: float = Field(gt=0)
    path_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        expected_offsets = list(range(1, self.horizon + 2))
        if [item.offset_from_signal for item in self.steps] != expected_offsets:
            raise ValueError("holding path does not cover every official session")
        dates = [item.session_date for item in self.steps]
        if dates != sorted(set(dates)):
            raise ValueError("holding path dates are not canonical")
        if self.entry_session != dates[0] or self.exit_session != dates[-1]:
            raise ValueError("holding path endpoint mismatch")
        factor = prod(
            item.reference_continuity_factor
            for item in self.steps
            if item.applied_to_holding_return
        )
        if not isclose(self.corporate_action_factor, factor, rel_tol=0, abs_tol=1e-12):
            raise ValueError("holding path corporate-action factor mismatch")
        if self.path_digest != _content_digest_without(self, "path_digest"):
            raise ValueError("holding path digest mismatch")
        return self


class JointExecutionSessionStateEvidenceV3(_StrictModel):
    role: Literal["entry", "exit"]
    session_date: str
    source_kind: Literal[
        "official_effective_dated_trading_state",
        "vendor_state_proxy",
        "unknown",
    ]
    exchange_session_state: ExchangeSessionState
    execution_state: JointExecutionState
    reason_code: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    observed_at: str
    effective_rules_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trading_state_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        session = _iso_date(self.session_date, "session_state.session_date")
        observed = _aware_datetime(self.observed_at, "session_state.observed_at")
        if observed.astimezone(ZoneInfo("Asia/Shanghai")).date() < session:
            raise ValueError("session state cannot be observed before its session")
        allowed: dict[ExchangeSessionState, frozenset[JointExecutionState]] = {
            "trading": frozenset(
                {"executable", "locked_limit", "no_bar", "rule_ineligible", "capacity_exceeded"}
            ),
            "suspended": frozenset({"suspended", "no_bar"}),
            "not_listed": frozenset({"rule_ineligible", "no_bar"}),
            "delisted": frozenset({"rule_ineligible", "no_bar"}),
        }
        if self.execution_state not in allowed[self.exchange_session_state]:
            raise ValueError("exchange session state and execution state conflict")
        return self


class JointExecutionCostEvidenceV3(_StrictModel):
    model_version: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    slippage_model_version: str = Field(min_length=1)
    entry_order_notional: float = Field(gt=0)
    exit_order_notional: float = Field(gt=0)
    maximum_participation_rate: float = Field(gt=0, le=1)
    entry_cost_return: float = Field(ge=0, lt=1)
    exit_cost_return: float = Field(ge=0, lt=1)
    total_cost_return: float = Field(ge=0, lt=1)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_costs(self) -> Self:
        if not isclose(
            self.total_cost_return,
            self.entry_cost_return + self.exit_cost_return,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise ValueError("total execution cost must equal entry plus exit cost")
        if self.evidence_digest != joint_execution_v3_content_digest(self):
            raise ValueError("joint execution cost evidence digest mismatch")
        return self


class JointExecutionObservedOutcomeV3(_StrictModel):
    target: ProbabilityTarget
    entry_fill: bool
    exit_executable: bool | None
    net_positive: bool | None
    joint_action_positive: bool
    entry_price: float | None = Field(default=None, gt=0)
    exit_price: float | None = Field(default=None, gt=0)
    gross_return: float | None = None
    net_return: float | None = None
    benchmark_return: float | None = None
    net_excess_return: float | None = None
    observed_at: str
    outcome_reason_codes: list[str] = Field(min_length=1)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> Self:
        _aware_datetime(self.observed_at, "outcome.observed_at")
        if len(set(self.outcome_reason_codes)) != len(self.outcome_reason_codes) or any(
            not _valid_reason_code(item) for item in self.outcome_reason_codes
        ):
            raise ValueError("outcome reason codes must be unique normalized identifiers")
        if not self.entry_fill:
            _validate_unfilled_outcome(self)
        elif self.exit_executable is False:
            _validate_unexecutable_exit_outcome(self)
        elif self.exit_executable is True:
            _validate_executable_outcome(self)
        else:
            raise ValueError("filled decision requires an observed exit-executable component")
        if self.evidence_digest != joint_execution_v3_content_digest(self):
            raise ValueError("joint execution outcome evidence digest mismatch")
        return self


class JointExecutionDecisionSetEvidenceV3(_StrictModel):
    population_policy: Literal[
        "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"
    ] = "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"
    signal_session: str
    horizon: Literal[1, 5, 20]
    target: ProbabilityTarget
    source_run_id: int = Field(gt=0)
    expected_decision_count: int = Field(gt=0)
    decision_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    universe_definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    universe_membership_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: str
    universe_frozen_before_outcomes: bool

    @model_validator(mode="after")
    def validate_decision_set(self) -> Self:
        signal = _iso_date(self.signal_session, "decision_set.signal_session")
        frozen = _aware_datetime(self.frozen_at, "decision_set.frozen_at")
        market_frozen = frozen.astimezone(ZoneInfo("Asia/Shanghai"))
        if market_frozen.date() != signal or market_frozen.time() <= time(15, 0):
            raise ValueError("decision set must be frozen after the signal close on signal date")
        return self


class JointExecutionAssessmentReplayEvidenceV3(_StrictModel):
    fold_id: int = Field(gt=0)
    training_cutoff: str
    decision_count: int = Field(gt=0)
    decision_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_label_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    prediction_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    out_of_sample_assessment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_replay_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    all_decisions_included: bool
    no_lookahead_verified: bool
    strict_replay_verified: bool

    @model_validator(mode="after")
    def validate_cutoff(self) -> Self:
        _iso_date(self.training_cutoff, "assessment_replay.training_cutoff")
        return self


class DecisionTimeJointExecutionProbabilityEvidenceV3(_StrictModel):
    schema_version: Literal["decision-time-joint-execution-probability-v3"] = (
        "decision-time-joint-execution-probability-v3"
    )
    sample_id: str = Field(min_length=1, max_length=160)
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    signal_session: str
    generated_at: str
    status: JointExecutionV3Status
    estimand: JointExecutionProbabilityEstimandV3
    evidence: JointExecutionEvidenceBundle
    entry_state: JointExecutionSessionStateEvidenceV3
    exit_state: JointExecutionSessionStateEvidenceV3
    holding_path: JointExecutionHoldingPathEvidenceV3
    costs: JointExecutionCostEvidenceV3
    observed_outcome: JointExecutionObservedOutcomeV3
    decision_set: JointExecutionDecisionSetEvidenceV3
    assessment_replay: JointExecutionAssessmentReplayEvidenceV3
    probabilities: JointExecutionProbabilityComponents
    gate_findings: list[JointExecutionGateFinding]
    production_effect: Literal["none"] = "none"
    canonical_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        horizon, target = _sample_identity(self.sample_id, self.symbol)
        _report_time_order(self, horizon)
        _report_bindings(self, horizon, target)
        _outcome_math(self)
        expected_findings = joint_execution_v3_gate_findings(self)
        expected_status: JointExecutionV3Status = (
            "unavailable" if expected_findings else "qualified_shadow"
        )
        if self.status != expected_status or self.gate_findings != list(expected_findings):
            raise ValueError("joint execution v3 gate state is inconsistent")
        if self.status == "qualified_shadow" and self.probabilities.is_null():
            raise ValueError("qualified joint execution v3 report requires probabilities")
        if self.status == "unavailable" and not self.probabilities.is_null():
            raise ValueError("unavailable joint execution v3 report cannot expose probabilities")
        if self.canonical_digest != joint_execution_v3_content_digest(self):
            raise ValueError("joint execution v3 canonical digest mismatch")
        return self


def joint_execution_v3_gate_findings(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
) -> tuple[JointExecutionGateFinding, ...]:
    """Derive deterministic candidate-level findings.

    Corpus completeness is deliberately not asserted here.  It is verified by
    the service-level whole-corpus verifier before any authorization use.
    """

    base = list(
        joint_execution_base_evidence_findings(
            report.evidence,
            signal_session=report.signal_session,
        )
    )
    base = _waive_official_nontrading_bar_findings(base, report)
    findings = [
        *base,
        *_state_findings(report.entry_state),
        *_state_findings(report.exit_state),
    ]
    replay = report.assessment_replay
    for passed, code in (
        (report.decision_set.universe_frozen_before_outcomes, "decision_universe_not_frozen"),
        (replay.all_decisions_included, "all_decisions_not_included"),
        (replay.no_lookahead_verified, "joint_no_lookahead_not_verified"),
        (replay.strict_replay_verified, "joint_corpus_replay_not_verified"),
    ):
        if not passed:
            findings.append(_finding(code, "unavailable"))
    if report.observed_outcome.entry_fill and report.costs.entry_cost_return <= 0:
        findings.append(_finding("entry_cost_not_observed", "unavailable"))
    if report.observed_outcome.exit_executable is True and report.costs.exit_cost_return <= 0:
        findings.append(_finding("exit_cost_not_observed", "unavailable"))
    unique = {(item.code, item.severity): item for item in findings}
    return tuple(sorted(unique.values(), key=lambda item: (item.code, item.severity)))


def joint_execution_v3_content_digest(value: BaseModel | Mapping[str, object]) -> str:
    """Hash strict finite canonical JSON excluding its own digest field."""

    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = deepcopy(dict(value))
    else:
        raise TypeError("joint execution v3 evidence must be a model or mapping")
    payload.pop("canonical_digest", None)
    payload.pop("evidence_digest", None)
    return sha256_hex(canonical_json_bytes(payload))


def _report_bindings(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
    horizon: int,
    target: ProbabilityTarget,
) -> None:
    _state_session_bindings(report)
    _identity_bindings(report, horizon, target)
    _path_benchmark_bindings(report, horizon)
    _replay_bindings(report)
    _cost_bindings(report)
    _state_outcome_bindings(report)


def _state_session_bindings(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
) -> None:
    if report.entry_state.role != "entry" or report.exit_state.role != "exit":
        raise ValueError("joint execution v3 session-state roles conflict")
    if report.entry_state.session_date != report.evidence.entry_bar.session_date:
        raise ValueError("entry state does not bind entry evidence session")
    if report.exit_state.session_date != report.evidence.exit_bar.session_date:
        raise ValueError("exit state does not bind exit evidence session")


def _identity_bindings(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
    horizon: int,
    target: ProbabilityTarget,
) -> None:
    decision_set = report.decision_set
    if (
        decision_set.signal_session != report.signal_session
        or decision_set.horizon != horizon
        or decision_set.target != target
    ):
        raise ValueError("decision-set identity does not bind sample identity")
    if report.estimand.registered_net_target != target:
        raise ValueError("joint execution estimand target does not bind sample identity")


def _path_benchmark_bindings(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
    horizon: int,
) -> None:
    decision_set = report.decision_set
    path = report.holding_path
    if (
        path.horizon != horizon
        or path.entry_session != report.evidence.entry_bar.session_date
        or path.exit_session != report.evidence.exit_bar.session_date
    ):
        raise ValueError("holding path does not bind report endpoints")
    benchmark = report.evidence.benchmark
    if (
        benchmark.universe_definition_digest != decision_set.universe_definition_digest
        or benchmark.universe_membership_digest != decision_set.universe_membership_digest
        or benchmark.decision_cohort_digest != decision_set.decision_identity_digest
    ):
        raise ValueError("decision set does not bind benchmark universe evidence")


def _replay_bindings(report: DecisionTimeJointExecutionProbabilityEvidenceV3) -> None:
    decision_set = report.decision_set
    replay = report.assessment_replay
    calibration = report.evidence.calibration
    if (
        replay.training_cutoff != calibration.training_cutoff
        or replay.out_of_sample_assessment_digest
        != calibration.out_of_sample_assessment_digest
        or replay.decision_count != decision_set.expected_decision_count
    ):
        raise ValueError("assessment replay does not bind calibration and decision set")


def _cost_bindings(report: DecisionTimeJointExecutionProbabilityEvidenceV3) -> None:
    participation = report.evidence.participation
    if not isclose(
        report.costs.entry_order_notional,
        participation.entry_order_notional,
        rel_tol=0,
        abs_tol=1e-9,
    ) or not isclose(
        report.costs.exit_order_notional,
        participation.exit_order_notional,
        rel_tol=0,
        abs_tol=1e-9,
    ) or not isclose(
        report.costs.maximum_participation_rate,
        participation.maximum_participation_rate,
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise ValueError("cost evidence does not bind participation evidence")


def _state_outcome_bindings(report: DecisionTimeJointExecutionProbabilityEvidenceV3) -> None:
    outcome = report.observed_outcome
    _horizon, target = _sample_identity(report.sample_id, report.symbol)
    if outcome.target != target:
        raise ValueError("observed outcome target does not bind sample identity")
    _entry_outcome_bindings(report)
    _exit_outcome_bindings(report)


def _entry_outcome_bindings(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
) -> None:
    outcome = report.observed_outcome
    entry_executable = report.entry_state.execution_state == "executable"
    if outcome.entry_fill is not entry_executable:
        raise ValueError("entry-fill observation conflicts with official execution state")
    if outcome.entry_fill and (
        outcome.exit_executable is not (report.exit_state.execution_state == "executable")
    ):
        raise ValueError("exit-executable observation conflicts with official execution state")
    if not outcome.entry_fill and (
        report.costs.entry_cost_return != 0 or report.costs.exit_cost_return != 0
    ):
        raise ValueError("unfilled decision cannot carry execution costs")
    if outcome.entry_fill and (
        report.evidence.entry_bar.open is None
        or outcome.entry_price is None
        or not isclose(
            outcome.entry_price,
            report.evidence.entry_bar.open,
            rel_tol=0,
            abs_tol=1e-12,
        )
    ):
        raise ValueError("observed entry fill price must bind official D+1 open")


def _exit_outcome_bindings(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
) -> None:
    outcome = report.observed_outcome
    if outcome.entry_fill and outcome.exit_executable is False and report.costs.exit_cost_return != 0:
        raise ValueError("unexecuted exit cannot carry exit cost")
    if outcome.exit_executable is True and (
        report.evidence.exit_bar.close is None
        or outcome.exit_price is None
        or not isclose(
            outcome.exit_price,
            report.evidence.exit_bar.close,
            rel_tol=0,
            abs_tol=1e-12,
        )
    ):
        raise ValueError("observed exit price must bind official horizon close")


def _outcome_math(report: DecisionTimeJointExecutionProbabilityEvidenceV3) -> None:
    outcome = report.observed_outcome
    if outcome.exit_executable is not True:
        return
    entry = cast(float, outcome.entry_price)
    exit_price = cast(float, outcome.exit_price)
    gross = exit_price * report.holding_path.corporate_action_factor / entry - 1.0
    net = gross - report.costs.total_cost_return
    benchmark = cast(float, outcome.benchmark_return)
    excess = net - benchmark
    if not all(
        (
            isclose(cast(float, outcome.gross_return), gross, rel_tol=0, abs_tol=1e-12),
            isclose(cast(float, outcome.net_return), net, rel_tol=0, abs_tol=1e-12),
            isclose(cast(float, outcome.net_excess_return), excess, rel_tol=0, abs_tol=1e-12),
        )
    ):
        raise ValueError("observed joint execution returns do not replay")
    target_value = excess if outcome.target == "net_excess_positive" else net
    if outcome.net_positive is not (target_value > 0):
        raise ValueError("observed net-positive component does not match target return")


def _report_time_order(
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
    horizon: int,
) -> None:
    signal = _iso_date(report.signal_session, "signal_session")
    entry = _iso_date(report.evidence.entry_bar.session_date, "entry_session")
    exit_session = _iso_date(report.evidence.exit_bar.session_date, "exit_session")
    if not signal < entry <= exit_session:
        raise ValueError("signal, entry and exit sessions must be forward ordered")
    if report.evidence.entry_bar.session_offset_from_signal != 1:
        raise ValueError("joint execution v3 entry offset must be D+1")
    if report.evidence.exit_bar.session_offset_from_signal != horizon + 1:
        raise ValueError("joint execution v3 exit offset must equal horizon+1")
    if (exit_session - signal).days < horizon + 1:
        raise ValueError("joint execution v3 exit session is too early for horizon")
    generated = _aware_datetime(report.generated_at, "generated_at")
    observed = _aware_datetime(report.observed_outcome.observed_at, "outcome.observed_at")
    observed_market = observed.astimezone(ZoneInfo("Asia/Shanghai"))
    if observed_market.date() < exit_session or (
        observed_market.date() == exit_session and observed_market.time() <= time(15, 0)
    ):
        raise ValueError("observed outcome requires after-close horizon maturity")
    if generated < observed:
        raise ValueError("report cannot be generated before its observed outcome")
    prediction_at = report.evidence.calibration.prediction_generated_at
    if prediction_at is None:
        return
    prediction = _aware_datetime(prediction_at, "calibration.prediction_generated_at")
    frozen = _aware_datetime(report.decision_set.frozen_at, "decision_set.frozen_at")
    if prediction < frozen or prediction.date() > signal:
        raise ValueError("prediction must follow the frozen decision set on signal date")


def _validate_unfilled_outcome(outcome: JointExecutionObservedOutcomeV3) -> None:
    if outcome.exit_executable is not None or outcome.net_positive is not None:
        raise ValueError("unfilled decision cannot claim exit or net-positive observations")
    if any(
        value is not None
        for value in (
            outcome.entry_price,
            outcome.exit_price,
            outcome.gross_return,
        )
    ):
        raise ValueError("unfilled decision must not carry fill-price evidence")
    if outcome.net_return is None or outcome.benchmark_return is None or outcome.net_excess_return is None:
        raise ValueError("unfilled decision requires cash and benchmark economics")
    if not isclose(outcome.net_return, 0.0, rel_tol=0, abs_tol=1e-12) or not isclose(
        outcome.net_excess_return,
        -outcome.benchmark_return,
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise ValueError("unfilled decision economics must use cash return against benchmark")
    if outcome.joint_action_positive:
        raise ValueError("unfilled decision cannot be joint-action positive")


def _validate_unexecutable_exit_outcome(outcome: JointExecutionObservedOutcomeV3) -> None:
    if outcome.entry_price is None:
        raise ValueError("filled decision must bind its entry price")
    if outcome.net_positive is not None or any(
        value is not None
        for value in (
            outcome.exit_price,
            outcome.gross_return,
            outcome.net_return,
            outcome.benchmark_return,
            outcome.net_excess_return,
        )
    ):
        raise ValueError("unexecutable exit cannot claim completed round-trip returns")
    if outcome.joint_action_positive:
        raise ValueError("unexecutable exit cannot be joint-action positive")


def _validate_executable_outcome(outcome: JointExecutionObservedOutcomeV3) -> None:
    required = (
        outcome.entry_price,
        outcome.exit_price,
        outcome.gross_return,
        outcome.net_return,
        outcome.benchmark_return,
        outcome.net_excess_return,
        outcome.net_positive,
    )
    if any(value is None for value in required):
        raise ValueError("executable round trip requires complete return evidence")
    if outcome.joint_action_positive is not outcome.net_positive:
        raise ValueError("joint action label must equal the observed net-positive component")


def _waive_official_nontrading_bar_findings(
    findings: Sequence[JointExecutionGateFinding],
    report: DecisionTimeJointExecutionProbabilityEvidenceV3,
) -> list[JointExecutionGateFinding]:
    waived: set[str] = set()
    for state in (report.entry_state, report.exit_state):
        if (
            state.source_kind == "official_effective_dated_trading_state"
            and state.exchange_session_state in {"suspended", "not_listed", "delisted"}
            and state.execution_state != "executable"
        ):
            waived.add(f"{state.role}_ohlcv_amount_missing")
            waived.add(f"{state.role}_session_amount_missing")
    return [item for item in findings if item.code not in waived]


def _state_findings(
    state: JointExecutionSessionStateEvidenceV3,
) -> list[JointExecutionGateFinding]:
    findings: list[JointExecutionGateFinding] = []
    if state.source_kind == "unknown":
        findings.append(_finding(f"{state.role}_trading_state_unknown", "unavailable"))
    elif state.source_kind != "official_effective_dated_trading_state":
        findings.append(_finding(f"{state.role}_trading_state_proxy", "audit_only"))
    if state.effective_rules_digest is None or state.trading_state_digest is None:
        findings.append(_finding(f"{state.role}_trading_state_digest_missing", "unavailable"))
    if state.execution_state == "no_bar" and state.exchange_session_state == "trading":
        findings.append(_finding(f"{state.role}_official_bar_unresolved", "unavailable"))
    return findings


def _sample_identity(sample_id: str, symbol: str) -> tuple[int, ProbabilityTarget]:
    parts = sample_id.split(":")
    if len(parts) != 4 or not parts[0].isdigit() or not parts[2].isdigit():
        raise ValueError("joint execution v3 sample_id must be run:symbol:horizon:target")
    run_id, sample_symbol, horizon_text, raw_target = parts
    horizon = int(horizon_text)
    if run_id != str(int(run_id)) or int(run_id) <= 0 or sample_symbol != symbol:
        raise ValueError("joint execution v3 sample identity mismatch")
    if horizon not in {1, 5, 20}:
        raise ValueError("joint execution v3 horizon must be 1, 5, or 20")
    if raw_target not in {"net_excess_positive", "net_return_positive"}:
        raise ValueError("joint execution v3 target is unsupported")
    return horizon, cast(ProbabilityTarget, raw_target)


def _finding(
    code: str,
    severity: Literal["audit_only", "unavailable"],
) -> JointExecutionGateFinding:
    return JointExecutionGateFinding(code=code, severity=severity)


def _valid_reason_code(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return all(character.islower() or character.isdigit() or character == "_" for character in value)


def _iso_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _content_digest_without(value: BaseModel, field: str) -> str:
    payload = value.model_dump(mode="json")
    payload.pop(field, None)
    return sha256_hex(canonical_json_bytes(payload))


def _aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return parsed


__all__ = [
    "DecisionTimeJointExecutionProbabilityEvidenceV3",
    "JointExecutionAssessmentReplayEvidenceV3",
    "JointExecutionCostEvidenceV3",
    "JointExecutionDecisionSetEvidenceV3",
    "JointExecutionHoldingPathEvidenceV3",
    "JointExecutionHoldingPathStepEvidenceV3",
    "JointExecutionObservedOutcomeV3",
    "JointExecutionProbabilityEstimandV3",
    "JointExecutionSessionStateEvidenceV3",
    "JointExecutionV3Status",
    "joint_execution_v3_content_digest",
    "joint_execution_v3_gate_findings",
]
