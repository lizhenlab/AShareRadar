"""Licensed-official all-decisions joint-execution outcome artifacts.

This module is the forward-only label boundary for the production probability
research path.  It accepts only an opaque, replay-verified signal corpus and a
complete official session path from D+1 through D+21.  Every source decision
is retained.  Missing fills and target-session exits are labels, never reasons
to select a smaller post-outcome cohort.

Unadjusted prices require special care across distributions and splits.  The
holding return therefore applies the product of each official post-entry
``previous_close / reference_price`` continuity factor.  The complete daily
path is digest-bound; looking only at the entry and exit bars is forbidden.

Serialized artifacts are integrity evidence, not authority.  Downstream code
must call :func:`replay_and_verify_joint_execution_outcome_artifact` with the
original opaque source token and raw-file-verified official sessions.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import json
from math import floor, isclose, isfinite, prod
from typing import Literal, Self, cast, overload
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.joint_execution_probability import (
    JointExecutionParticipationEvidence,
    JointExecutionReferencePriceEvidence,
    JointExecutionRuleEvidence,
    JointExecutionSessionBarEvidence,
)
from app.models.joint_execution_probability_v3 import (
    JointExecutionCostEvidenceV3,
    JointExecutionObservedOutcomeV3,
    JointExecutionSessionStateEvidenceV3,
    joint_execution_v3_content_digest,
)
from app.models.paper_trading import CostProfileName, PaperCostProfile
from app.services.market_scan_joint_execution_source import (
    VerifiedJointExecutionSourceCorpus,
)
from app.services.market_scan_official_execution import (
    BoundOfficialExecutionDecisionSession,
    VerifiedOfficialExecutionSession,
    bind_official_execution_session_to_decisions,
)
from app.services.paper_trading_costs import (
    PAPER_COST_PROFILE_VERSION,
    resolve_cost_profile,
    trade_costs,
)
from app.services.trading_calendar import (
    TradeCalendarSource,
    next_trade_dates,
    trading_date_range,
)


JOINT_EXECUTION_OUTCOME_SCHEMA_VERSION = (
    "market-scan-joint-execution-outcome-artifact-v1"
)
JOINT_EXECUTION_OUTCOME_CONTRACT_VERSION = (
    "official-fixed-session-all-decisions-joint-action-outcomes-v1"
)
JOINT_EXECUTION_LABEL_VERSION = "market-scan-joint-execution-label-v1"
JOINT_EXECUTION_EXECUTION_MODEL = (
    "signal-D-frozen,D+1-official-open,D+H+1-official-close,T+1,"
    "no-delayed-fill,no-delayed-exit"
)
JOINT_EXECUTION_BENCHMARK_METHOD = (
    "fixed-signal-universe-leave-one-out-official-path-v1"
)
JOINT_EXECUTION_CORPORATE_ACTION_METHOD = (
    "official-reference-price-continuity-product-post-entry-v1"
)
JOINT_EXECUTION_SLIPPAGE_MODEL_VERSION = "declared-paper-profile-fixed-notional-v1"
JOINT_EXECUTION_HORIZONS = (1, 5, 20)
JOINT_EXECUTION_PROFILE_NAMES = ("base", "conservative", "stress")
JOINT_EXECUTION_PRIMARY_PROFILE = "base"
JOINT_EXECUTION_TARGET = "net_excess_positive"
JOINT_EXECUTION_ORDER_NOTIONAL = 100_000.0
JOINT_EXECUTION_MAXIMUM_PARTICIPATION_RATE = 0.01
JOINT_EXECUTION_REQUIRED_FORWARD_SESSION_COUNT = max(JOINT_EXECUTION_HORIZONS) + 1
OutcomeExecutionState = Literal[
    "executable",
    "suspended",
    "locked_limit",
    "no_bar",
    "rule_ineligible",
    "capacity_exceeded",
]

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_VERIFIED_OUTCOME_SEAL = object()


class JointExecutionOutcomeError(ValueError):
    """Raised when a formal joint-execution outcome cannot be replayed."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class JointExecutionOutcomeSourceBinding(_StrictModel):
    run_id: int = Field(gt=0)
    signal_session: str
    decision_frozen_at: str
    source_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_membership_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_decision_count: int = Field(gt=1)

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        signal = _date(self.signal_session, "source.signal_session")
        frozen = _timestamp(self.decision_frozen_at, "source.decision_frozen_at")
        if frozen.astimezone(_SHANGHAI).date() != signal:
            raise ValueError("joint outcome source freeze is outside signal session")
        return self


class JointExecutionOutcomeCalendar(_StrictModel):
    version: Literal["trusted-fixed-exchange-session-path-v1"] = (
        "trusted-fixed-exchange-session-path-v1"
    )
    signal_session: str
    forward_sessions: list[str] = Field(
        min_length=2,
        max_length=JOINT_EXECUTION_REQUIRED_FORWARD_SESSION_COUNT,
    )
    entry_session: str
    horizon_exit_sessions: dict[str, str]
    calendar_source: Literal["runtime_cache", "bundled_baseline"]
    calendar_provider_source: str | None = None
    calendar_updated_at: str | None = None
    coverage_min_date: str
    coverage_max_date: str
    session_path_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_calendar(self) -> Self:
        signal = _date(self.signal_session, "calendar.signal_session")
        sessions = [_date(item, "calendar.forward_sessions[]") for item in self.forward_sessions]
        if sessions != sorted(set(sessions)) or any(item <= signal for item in sessions):
            raise ValueError("joint outcome forward session path is not canonical")
        if self.entry_session != self.forward_sessions[0]:
            raise ValueError("joint outcome entry session must be D+1")
        horizons = _registered_horizons(self.horizon_exit_sessions)
        if len(self.forward_sessions) != max(horizons) + 1:
            raise ValueError("joint outcome calendar path length does not match horizons")
        expected_exits = {
            str(horizon): self.forward_sessions[horizon] for horizon in horizons
        }
        if self.horizon_exit_sessions != expected_exits:
            raise ValueError("joint outcome horizon exits must be D+H+1")
        minimum = _date(self.coverage_min_date, "calendar.coverage_min_date")
        maximum = _date(self.coverage_max_date, "calendar.coverage_max_date")
        if minimum > signal or maximum < sessions[-1]:
            raise ValueError("joint outcome calendar coverage does not contain the path")
        if self.calendar_updated_at is not None:
            _calendar_metadata_timestamp(
                self.calendar_updated_at, "calendar.calendar_updated_at"
            )
        expected_digest = _digest(
            {
                "version": self.version,
                "signal_session": self.signal_session,
                "forward_sessions": self.forward_sessions,
                "entry_session": self.entry_session,
                "horizon_exit_sessions": self.horizon_exit_sessions,
                "calendar_source": self.calendar_source,
                "calendar_provider_source": self.calendar_provider_source,
                "calendar_updated_at": self.calendar_updated_at,
                "coverage_min_date": self.coverage_min_date,
                "coverage_max_date": self.coverage_max_date,
            }
        )
        if self.session_path_digest != expected_digest:
            raise ValueError("joint outcome calendar digest mismatch")
        return self


class JointExecutionOutcomeSessionBinding(_StrictModel):
    offset_from_signal: int = Field(ge=1, le=JOINT_EXECUTION_REQUIRED_FORWARD_SESSION_COUNT)
    session_date: str
    official_session_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_raw_file_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_universe_membership_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_membership_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_generated_at: str
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        _date(self.session_date, "session_binding.session_date")
        generated = _timestamp(
            self.official_generated_at, "session_binding.official_generated_at"
        )
        if generated.astimezone(_SHANGHAI).date() < date.fromisoformat(self.session_date):
            raise ValueError("official session artifact predates its session")
        if self.binding_digest != joint_execution_outcome_content_digest(
            self, "binding_digest"
        ):
            raise ValueError("official outcome session binding digest mismatch")
        return self


class JointExecutionOutcomeCostProfile(_StrictModel):
    profile_name: Literal["base", "conservative", "stress"]
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    effective_from: str
    commission_rate_pct: float = Field(ge=0)
    minimum_commission: float = Field(ge=0)
    stamp_duty_sell_pct: float = Field(ge=0)
    transfer_fee_pct: float = Field(ge=0)
    slippage_buy_pct: float = Field(ge=0)
    slippage_sell_pct: float = Field(ge=0)
    source_urls: list[str] = Field(min_length=1)
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        _date(self.effective_from, "cost_profile.effective_from")
        if self.profile_digest != joint_execution_outcome_content_digest(
            self, "profile_digest"
        ):
            raise ValueError("joint outcome cost profile digest mismatch")
        return self


class JointExecutionOutcomeCostContract(_StrictModel):
    label_version: Literal["market-scan-joint-execution-label-v1"] = (
        "market-scan-joint-execution-label-v1"
    )
    execution_model: Literal[
        "signal-D-frozen,D+1-official-open,D+H+1-official-close,T+1,"
        "no-delayed-fill,no-delayed-exit"
    ] = "signal-D-frozen,D+1-official-open,D+H+1-official-close,T+1,no-delayed-fill,no-delayed-exit"
    horizons: list[int] = Field(min_length=1, max_length=3)
    entry_session_offset: Literal[1] = 1
    target_session_offsets: dict[str, int]
    target: Literal["net_excess_positive"] = "net_excess_positive"
    target_population: Literal[
        "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"
    ] = "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"
    observed_components: list[str] = Field(min_length=3, max_length=3)
    execution_notional: float = Field(gt=0)
    maximum_participation_rate: float = Field(gt=0, le=1)
    cost_model_version: str = Field(min_length=1)
    slippage_model_version: str = Field(min_length=1)
    primary_profile: Literal["base"] = "base"
    profiles: list[JointExecutionOutcomeCostProfile] = Field(min_length=3, max_length=3)
    corporate_action_method: Literal[
        "official-reference-price-continuity-product-post-entry-v1"
    ] = "official-reference-price-continuity-product-post-entry-v1"
    benchmark_method: Literal[
        "fixed-signal-universe-leave-one-out-official-path-v1"
    ] = "fixed-signal-universe-leave-one-out-official-path-v1"
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if tuple(self.horizons) != _registered_horizons(self.horizons):
            raise ValueError("joint outcome horizons are not preregistered")
        if self.target_session_offsets != {
            str(value): value + 1 for value in self.horizons
        }:
            raise ValueError("joint outcome target offsets are not horizon+1")
        if self.observed_components != [
            "entry_fill",
            "exit_executable",
            "net_positive",
        ]:
            raise ValueError("joint outcome observed components are incomplete")
        if not isclose(
            self.execution_notional,
            JOINT_EXECUTION_ORDER_NOTIONAL,
            rel_tol=0,
            abs_tol=0,
        ) or not isclose(
            self.maximum_participation_rate,
            JOINT_EXECUTION_MAXIMUM_PARTICIPATION_RATE,
            rel_tol=0,
            abs_tol=0,
        ):
            raise ValueError("joint outcome notional/capacity contract drifted")
        if self.cost_model_version != PAPER_COST_PROFILE_VERSION:
            raise ValueError("joint outcome cost model version drifted")
        if [item.profile_name for item in self.profiles] != list(
            JOINT_EXECUTION_PROFILE_NAMES
        ):
            raise ValueError("joint outcome cost profiles are not canonical")
        for item in self.profiles:
            expected = _cost_profile_payload(item.profile_name)
            if item.model_dump(mode="json") != expected:
                raise ValueError("joint outcome cost profile does not replay locally")
        if self.contract_digest != joint_execution_outcome_content_digest(
            self, "contract_digest"
        ):
            raise ValueError("joint outcome cost contract digest mismatch")
        return self


class JointExecutionHoldingPathStep(_StrictModel):
    offset_from_signal: int = Field(ge=1, le=JOINT_EXECUTION_REQUIRED_FORWARD_SESSION_COUNT)
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
        _date(self.session_date, "holding_path.step.session_date")
        if (self.corporate_action_status == "effective_event") is not bool(
            self.corporate_action_event_id
        ):
            raise ValueError("joint outcome corporate-action event identity mismatch")
        expected = self.previous_close / self.reference_price
        if not isclose(
            self.reference_continuity_factor, expected, rel_tol=0, abs_tol=1e-12
        ):
            raise ValueError("joint outcome corporate-action continuity factor mismatch")
        if self.applied_to_holding_return is not (self.offset_from_signal > 1):
            raise ValueError("joint outcome entry-session corporate action must not be reapplied")
        if self.step_digest != joint_execution_outcome_content_digest(self, "step_digest"):
            raise ValueError("joint outcome holding-path step digest mismatch")
        return self


class JointExecutionHoldingPath(_StrictModel):
    horizon: Literal[1, 5, 20]
    entry_session: str
    exit_session: str
    steps: list[JointExecutionHoldingPathStep] = Field(min_length=2, max_length=21)
    corporate_action_factor: float = Field(gt=0)
    path_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        expected_offsets = list(range(1, self.horizon + 2))
        if [item.offset_from_signal for item in self.steps] != expected_offsets:
            raise ValueError("joint outcome holding path has a missing official session")
        dates = [item.session_date for item in self.steps]
        if dates != sorted(set(dates)):
            raise ValueError("joint outcome holding path dates are not canonical")
        if self.entry_session != dates[0] or self.exit_session != dates[-1]:
            raise ValueError("joint outcome holding path endpoints mismatch")
        factor = prod(
            item.reference_continuity_factor
            for item in self.steps
            if item.applied_to_holding_return
        )
        if not isclose(self.corporate_action_factor, factor, rel_tol=0, abs_tol=1e-12):
            raise ValueError("joint outcome holding path factor does not replay")
        if self.path_digest != joint_execution_outcome_content_digest(self, "path_digest"):
            raise ValueError("joint outcome holding path digest mismatch")
        return self


class JointExecutionBenchmarkPoint(_StrictModel):
    decision_id: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    hypothetical_return: float
    reason_code: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    point_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_point(self) -> Self:
        if not self.decision_id.endswith(f":{self.symbol}"):
            raise ValueError("joint benchmark decision identity mismatch")
        if self.point_digest != joint_execution_outcome_content_digest(
            self, "point_digest"
        ):
            raise ValueError("joint benchmark point digest mismatch")
        return self


class JointExecutionBenchmarkSeries(_StrictModel):
    horizon: Literal[1, 5, 20]
    profile_name: Literal["base", "conservative", "stress"]
    profile_id: str = Field(min_length=1)
    method: Literal[
        "fixed-signal-universe-leave-one-out-official-path-v1"
    ] = "fixed-signal-universe-leave-one-out-official-path-v1"
    population_count: int = Field(gt=1)
    points: list[JointExecutionBenchmarkPoint] = Field(min_length=2)
    series_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_series(self) -> Self:
        identities = [item.decision_id for item in self.points]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("joint benchmark points are not canonical")
        if self.population_count != len(self.points):
            raise ValueError("joint benchmark population is incomplete")
        if self.series_digest != joint_execution_outcome_content_digest(
            self, "series_digest"
        ):
            raise ValueError("joint benchmark series digest mismatch")
        return self


class JointExecutionOutcomeScenario(_StrictModel):
    profile_name: Literal["base", "conservative", "stress"]
    profile_id: str = Field(min_length=1)
    benchmark_series_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    costs: JointExecutionCostEvidenceV3
    observed_outcome: JointExecutionObservedOutcomeV3
    scenario_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_scenario(self) -> Self:
        if self.profile_id != self.costs.profile_id:
            raise ValueError("joint outcome scenario cost profile identity mismatch")
        if self.observed_outcome.target != JOINT_EXECUTION_TARGET:
            raise ValueError("joint outcome scenario target drifted")
        if self.scenario_digest != joint_execution_outcome_content_digest(
            self, "scenario_digest"
        ):
            raise ValueError("joint outcome scenario digest mismatch")
        return self


class JointExecutionOutcomeHorizon(_StrictModel):
    horizon: Literal[1, 5, 20]
    target_session: str
    entry_bar: JointExecutionSessionBarEvidence
    exit_bar: JointExecutionSessionBarEvidence
    entry_rules: JointExecutionRuleEvidence
    exit_rules: JointExecutionRuleEvidence
    entry_reference: JointExecutionReferencePriceEvidence
    exit_reference: JointExecutionReferencePriceEvidence
    entry_state: JointExecutionSessionStateEvidenceV3
    exit_state: JointExecutionSessionStateEvidenceV3
    participation: JointExecutionParticipationEvidence
    holding_path: JointExecutionHoldingPath
    scenarios: list[JointExecutionOutcomeScenario] = Field(min_length=3, max_length=3)
    horizon_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_horizon(self) -> Self:
        if self.target_session != self.holding_path.exit_session:
            raise ValueError("joint outcome target session does not bind holding path")
        if self.holding_path.horizon != self.horizon:
            raise ValueError("joint outcome holding path horizon mismatch")
        _validate_horizon_roles(self)
        if [item.profile_name for item in self.scenarios] != list(
            JOINT_EXECUTION_PROFILE_NAMES
        ):
            raise ValueError("joint outcome scenarios are not canonical")
        for scenario in self.scenarios:
            _validate_scenario_math(self, scenario)
        if self.horizon_digest != joint_execution_outcome_content_digest(
            self, "horizon_digest"
        ):
            raise ValueError("joint outcome horizon digest mismatch")
        return self


class JointExecutionOutcomeRecord(_StrictModel):
    decision_id: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    source_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    horizons: list[JointExecutionOutcomeHorizon] = Field(min_length=1, max_length=3)
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if not self.decision_id.endswith(f":{self.symbol}"):
            raise ValueError("joint outcome decision identity mismatch")
        values = [item.horizon for item in self.horizons]
        if tuple(values) != _registered_horizons(values):
            raise ValueError("joint outcome record horizons are not canonical")
        if self.record_digest != joint_execution_outcome_content_digest(
            self, "record_digest"
        ):
            raise ValueError("joint outcome record digest mismatch")
        return self


class JointExecutionOutcomeQualityRow(_StrictModel):
    horizon: Literal[1, 5, 20]
    profile_name: Literal["base", "conservative", "stress"]
    decision_count: int = Field(gt=1)
    entry_fill_count: int = Field(ge=0)
    exit_executable_count: int = Field(ge=0)
    joint_action_positive_count: int = Field(ge=0)
    unresolved_round_trip_return_count: int = Field(ge=0)
    joint_component_label_coverage: float = Field(ge=0, le=1)


class JointExecutionOutcomeQuality(_StrictModel):
    expected_decision_count: int = Field(gt=1)
    record_count: int = Field(gt=1)
    official_forward_session_count: int = Field(gt=0)
    all_decisions_included: bool
    complete_official_session_path: bool
    corporate_action_path_replayed: bool
    no_delayed_fill_or_exit: Literal[True] = True
    no_post_outcome_selection: Literal[True] = True
    formal_forward_outcome_eligible: bool
    rows: list[JointExecutionOutcomeQualityRow] = Field(min_length=3, max_length=9)


class JointExecutionOutcomeArtifact(_StrictModel):
    schema_version: Literal[
        "market-scan-joint-execution-outcome-artifact-v1"
    ] = "market-scan-joint-execution-outcome-artifact-v1"
    contract_version: Literal[
        "official-fixed-session-all-decisions-joint-action-outcomes-v1"
    ] = "official-fixed-session-all-decisions-joint-action-outcomes-v1"
    generated_at: str
    source: JointExecutionOutcomeSourceBinding
    calendar: JointExecutionOutcomeCalendar
    session_bindings: list[JointExecutionOutcomeSessionBinding] = Field(
        min_length=2,
        max_length=JOINT_EXECUTION_REQUIRED_FORWARD_SESSION_COUNT,
    )
    label_contract: JointExecutionOutcomeCostContract
    benchmarks: list[JointExecutionBenchmarkSeries] = Field(min_length=3, max_length=9)
    records: list[JointExecutionOutcomeRecord] = Field(min_length=2)
    quality: JointExecutionOutcomeQuality
    benchmark_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_record_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        generated = _timestamp(self.generated_at, "outcome.generated_at")
        if generated < _timestamp(
            self.source.decision_frozen_at, "outcome.source.decision_frozen_at"
        ):
            raise ValueError("joint outcome artifact predates the source freeze")
        _validate_session_bindings(self, generated)
        _validate_artifact_records(self)
        _validate_benchmark_bindings(self)
        _validate_outcome_quality(self)
        if self.benchmark_set_digest != _digest(
            [item.series_digest for item in self.benchmarks]
        ):
            raise ValueError("joint outcome benchmark-set digest mismatch")
        if self.outcome_record_set_digest != _digest(
            [item.record_digest for item in self.records]
        ):
            raise ValueError("joint outcome record-set digest mismatch")
        if self.artifact_digest != joint_execution_outcome_content_digest(
            self, "artifact_digest"
        ):
            raise ValueError("joint outcome artifact digest mismatch")
        return self


class VerifiedJointExecutionOutcomeCorpus(Sequence[Mapping[str, object]]):
    """Opaque outcome token returned only after full source/raw replay."""

    __slots__ = (
        "_encoded_records",
        "artifact_digest",
        "run_id",
        "signal_session",
        "decision_identity_digest",
        "decision_membership_digest",
        "feature_schema_digest",
        "label_contract_digest",
        "benchmark_set_digest",
    )

    def __init__(
        self,
        encoded_records: str,
        *,
        artifact_digest: str,
        run_id: int,
        signal_session: str,
        decision_identity_digest: str,
        decision_membership_digest: str,
        feature_schema_digest: str,
        label_contract_digest: str,
        benchmark_set_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _VERIFIED_OUTCOME_SEAL:
            raise TypeError("joint execution outcome token can only be created by strict replay")
        self._encoded_records = encoded_records
        self.artifact_digest = artifact_digest
        self.run_id = run_id
        self.signal_session = signal_session
        self.decision_identity_digest = decision_identity_digest
        self.decision_membership_digest = decision_membership_digest
        self.feature_schema_digest = feature_schema_digest
        self.label_contract_digest = label_contract_digest
        self.benchmark_set_digest = benchmark_set_digest

    @property
    def records(self) -> list[dict[str, object]]:
        value = json.loads(self._encoded_records)
        if not isinstance(value, list):  # pragma: no cover - sealed invariant
            raise TypeError("verified joint outcome records are invalid")
        return [cast(dict[str, object], item) for item in value]

    @overload
    def __getitem__(self, index: int) -> Mapping[str, object]: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Mapping[str, object]]: ...

    def __getitem__(
        self, index: int | slice
    ) -> Mapping[str, object] | Sequence[Mapping[str, object]]:
        return self.records[index]

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        return iter(self.records)


@dataclass(frozen=True)
class _OutcomeFacts:
    source_record: Mapping[str, object]
    symbol: str
    decision_id: str
    horizon: int
    entry_row: Mapping[str, object]
    exit_row: Mapping[str, object]
    entry_bar: dict[str, object]
    exit_bar: dict[str, object]
    entry_rules: dict[str, object]
    exit_rules: dict[str, object]
    entry_reference: dict[str, object]
    exit_reference: dict[str, object]
    entry_state: dict[str, object]
    exit_state: dict[str, object]
    participation: dict[str, object]
    holding_path: dict[str, object]
    entry_fill: bool
    exit_executable: bool | None
    exit_order_notional: float


@dataclass(frozen=True)
class _OutcomeBuildPopulation:
    session_bindings: list[dict[str, object]]
    benchmarks: list[dict[str, object]]
    records: list[dict[str, object]]


@dataclass(frozen=True)
class _OutcomePathContext:
    source_record: Mapping[str, object]
    symbol: str
    decision_id: str
    horizon: int
    entry_row: Mapping[str, object]
    exit_row: Mapping[str, object]
    holding_path: dict[str, object]


@dataclass(frozen=True)
class _OutcomeExecutionContext:
    entry_state: dict[str, object]
    exit_state: dict[str, object]
    participation: dict[str, object]
    entry_fill: bool
    exit_executable: bool | None
    exit_order_notional: float


def joint_execution_outcome_content_digest(
    value: BaseModel | Mapping[str, object], digest_field: str
) -> str:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = deepcopy(dict(value))
    else:
        raise TypeError("joint execution outcome evidence must be a model or mapping")
    payload.pop(digest_field, None)
    return _digest(payload)


def build_joint_execution_outcome_artifact(
    source: VerifiedJointExecutionSourceCorpus,
    official_sessions: Mapping[str, VerifiedOfficialExecutionSession],
    *,
    generated_at: str,
    horizons: Sequence[int] = JOINT_EXECUTION_HORIZONS,
) -> dict[str, object]:
    """Build a candidate from every official forward session and decision."""

    if not isinstance(source, VerifiedJointExecutionSourceCorpus):
        raise JointExecutionOutcomeError(
            "joint outcome requires a replay-verified all-decisions source token"
        )
    generated = _timestamp(generated_at, "generated_at")
    registered_horizons = _registered_horizons(horizons)
    calendar = _calendar_contract(source.signal_session, registered_horizons)
    return _build_outcome_with_calendar(
        source, official_sessions, generated, registered_horizons, calendar,
    )


def _build_outcome_with_calendar(
    source: VerifiedJointExecutionSourceCorpus,
    official_sessions: Mapping[str, VerifiedOfficialExecutionSession],
    generated: datetime,
    registered_horizons: Sequence[int],
    calendar: Mapping[str, object],
) -> dict[str, object]:
    source_records, symbols = _outcome_source_population(source)
    population = _build_outcome_population(
        source,
        source_records,
        symbols,
        official_sessions,
        calendar,
        registered_horizons,
    )
    payload = _outcome_artifact_payload(
        source,
        generated,
        calendar,
        registered_horizons,
        population,
    )
    payload["artifact_digest"] = joint_execution_outcome_content_digest(
        payload, "artifact_digest"
    )
    try:
        return JointExecutionOutcomeArtifact.model_validate(payload).model_dump(mode="json")
    except (TypeError, ValueError) as exc:
        raise JointExecutionOutcomeError(
            "joint execution outcome artifact failed strict construction"
        ) from exc


def _outcome_source_population(
    source: VerifiedJointExecutionSourceCorpus,
) -> tuple[list[dict[str, object]], list[str]]:
    source_records = source.records
    if len(source_records) <= 1:
        raise JointExecutionOutcomeError(
            "joint outcome leave-one-out benchmark requires at least two decisions"
        )
    symbols = [str(item["symbol"]) for item in source_records]
    if symbols != sorted(set(symbols)):
        raise JointExecutionOutcomeError("joint outcome source decisions are not canonical")
    return source_records, symbols


def _build_outcome_population(
    source: VerifiedJointExecutionSourceCorpus,
    source_records: Sequence[Mapping[str, object]],
    symbols: Sequence[str],
    official_sessions: Mapping[str, VerifiedOfficialExecutionSession],
    calendar: Mapping[str, object],
    horizons: Sequence[int],
) -> _OutcomeBuildPopulation:
    bound, session_bindings = _bound_forward_sessions(
        source,
        official_sessions,
        calendar,
        symbols,
    )
    facts = [
        _outcome_facts(
            source_record,
            horizon,
            calendar,
            bound,
            source_snapshot_digest=source.source_snapshot_digest,
        )
        for source_record in source_records
        for horizon in horizons
    ]
    profiles = {
        name: resolve_cost_profile(cast(CostProfileName, name))
        for name in JOINT_EXECUTION_PROFILE_NAMES
    }
    benchmarks = _benchmark_series(facts, profiles, horizons)
    benchmark_by_key = {
        (int(cast(int, item["horizon"])), str(item["profile_name"])): item
        for item in benchmarks
    }
    facts_by_decision = {
        (item.decision_id, item.horizon): item
        for item in facts
    }
    records = [
        _outcome_record(
            source_record,
            facts_by_decision,
            benchmark_by_key,
            profiles,
            horizons,
        )
        for source_record in source_records
    ]
    return _OutcomeBuildPopulation(
        session_bindings=session_bindings,
        benchmarks=benchmarks,
        records=records,
    )


def _outcome_source_binding(
    source: VerifiedJointExecutionSourceCorpus,
    decision_count: int,
) -> dict[str, object]:
    return {
        "run_id": source.run_id,
        "signal_session": source.signal_session,
        "decision_frozen_at": source.decision_frozen_at,
        "source_artifact_digest": source.artifact_digest,
        "source_snapshot_digest": source.source_snapshot_digest,
        "decision_identity_digest": source.decision_identity_digest,
        "decision_membership_digest": source.decision_membership_digest,
        "feature_schema_digest": source.feature_schema_digest,
        "expected_decision_count": decision_count,
    }


def _outcome_artifact_payload(
    source: VerifiedJointExecutionSourceCorpus,
    generated: datetime,
    calendar: Mapping[str, object],
    horizons: Sequence[int],
    population: _OutcomeBuildPopulation,
) -> dict[str, object]:
    records = population.records
    benchmarks = population.benchmarks
    return {
        "schema_version": JOINT_EXECUTION_OUTCOME_SCHEMA_VERSION,
        "contract_version": JOINT_EXECUTION_OUTCOME_CONTRACT_VERSION,
        "generated_at": generated.isoformat(),
        "source": _outcome_source_binding(source, len(records)),
        "calendar": dict(calendar),
        "session_bindings": population.session_bindings,
        "label_contract": _cost_contract(horizons),
        "benchmarks": benchmarks,
        "records": records,
        "quality": _quality(records, population.session_bindings),
        "benchmark_set_digest": _digest([item["series_digest"] for item in benchmarks]),
        "outcome_record_set_digest": _digest([item["record_digest"] for item in records]),
    }


def verify_joint_execution_outcome_artifact(
    value: Mapping[str, object],
) -> dict[str, object]:
    try:
        return JointExecutionOutcomeArtifact.model_validate(dict(value)).model_dump(mode="json")
    except (TypeError, ValueError) as exc:
        raise JointExecutionOutcomeError(
            "joint execution outcome artifact failed verification"
        ) from exc


def replay_and_verify_joint_execution_outcome_artifact(
    artifact: Mapping[str, object],
    source: VerifiedJointExecutionSourceCorpus,
    official_sessions: Mapping[str, VerifiedOfficialExecutionSession],
) -> VerifiedJointExecutionOutcomeCorpus:
    """Replay source, calendar, official rows, costs, labels, and benchmarks."""

    verified = verify_joint_execution_outcome_artifact(artifact)
    if not isinstance(source, VerifiedJointExecutionSourceCorpus):
        raise JointExecutionOutcomeError("joint outcome replay requires a verified source token")
    calendar = _verified_replay_calendar(verified, source.signal_session)
    rebuilt = _build_outcome_with_calendar(
        source,
        official_sessions,
        _timestamp(str(verified["generated_at"]), "generated_at"),
        cast(
            list[int], cast(dict[str, object], verified["label_contract"])["horizons"]
        ),
        calendar,
    )
    if rebuilt != verified:
        raise JointExecutionOutcomeError("joint execution outcome artifact does not replay")
    source_binding = cast(dict[str, object], verified["source"])
    label = cast(dict[str, object], verified["label_contract"])
    records = cast(list[dict[str, object]], verified["records"])
    return VerifiedJointExecutionOutcomeCorpus(
        canonical_json_bytes(records).decode("utf-8"),
        artifact_digest=str(verified["artifact_digest"]),
        run_id=int(cast(int, source_binding["run_id"])),
        signal_session=str(source_binding["signal_session"]),
        decision_identity_digest=str(source_binding["decision_identity_digest"]),
        decision_membership_digest=str(source_binding["decision_membership_digest"]),
        feature_schema_digest=str(source_binding["feature_schema_digest"]),
        label_contract_digest=str(label["contract_digest"]),
        benchmark_set_digest=str(verified["benchmark_set_digest"]),
        _seal=_VERIFIED_OUTCOME_SEAL,
    )


def _verified_replay_calendar(
    artifact: Mapping[str, object], signal_session: str,
) -> Mapping[str, object]:
    frozen = _mapping(artifact["calendar"], "calendar")
    exits = _mapping(frozen["horizon_exit_sessions"], "calendar.horizon_exit_sessions")
    current = _calendar_contract(signal_session, _registered_horizons(exits))
    path_fields = (
        "version", "signal_session", "forward_sessions", "entry_session", "horizon_exit_sessions",
    )
    if any(frozen[field] != current[field] for field in path_fields):
        raise JointExecutionOutcomeError("joint outcome frozen calendar path conflicts with trusted sessions")
    # Metadata belongs to the original observation; current trusted sessions still authorize every slot.
    return frozen


def _calendar_contract(
    signal_session: str,
    horizons: Sequence[int] = JOINT_EXECUTION_HORIZONS,
) -> dict[str, object]:
    signal = _date(signal_session, "signal_session")
    registered_horizons = _registered_horizons(horizons)
    forward = next_trade_dates(signal, max(registered_horizons) + 1)
    inclusive, status = trading_date_range(signal, forward[-1])
    expected = (signal, *forward)
    if inclusive != expected or status.source not in {
        TradeCalendarSource.RUNTIME_CACHE,
        TradeCalendarSource.BUNDLED_BASELINE,
    }:
        raise JointExecutionOutcomeError(
            "joint outcome requires one complete trusted exchange calendar path"
        )
    payload: dict[str, object] = {
        "version": "trusted-fixed-exchange-session-path-v1",
        "signal_session": signal.isoformat(),
        "forward_sessions": [item.isoformat() for item in forward],
        "entry_session": forward[0].isoformat(),
        "horizon_exit_sessions": {
            str(horizon): forward[horizon].isoformat()
            for horizon in registered_horizons
        },
        "calendar_source": status.source.value,
        "calendar_provider_source": status.provider_source,
        "calendar_updated_at": (
            status.updated_at.isoformat() if status.updated_at is not None else None
        ),
        "coverage_min_date": cast(date, status.min_date).isoformat(),
        "coverage_max_date": cast(date, status.max_date).isoformat(),
    }
    payload["session_path_digest"] = _digest(payload)
    return JointExecutionOutcomeCalendar.model_validate(payload).model_dump(mode="json")


def _bound_forward_sessions(
    source: VerifiedJointExecutionSourceCorpus,
    official_sessions: Mapping[str, VerifiedOfficialExecutionSession],
    calendar: Mapping[str, object],
    symbols: Sequence[str],
) -> tuple[dict[str, BoundOfficialExecutionDecisionSession], list[dict[str, object]]]:
    required = [str(item) for item in cast(list[object], calendar["forward_sessions"])]
    if set(official_sessions) != set(required):
        missing = sorted(set(required) - set(official_sessions))
        extra = sorted(set(official_sessions) - set(required))
        raise JointExecutionOutcomeError(
            f"joint outcome official session path mismatch; missing={missing[:3]}, extra={extra[:3]}"
        )
    bound: dict[str, BoundOfficialExecutionDecisionSession] = {}
    bindings: list[dict[str, object]] = []
    for offset, session_date in enumerate(required, start=1):
        session = official_sessions[session_date]
        if (
            not isinstance(session, VerifiedOfficialExecutionSession)
            or session.session_date != session_date
        ):
            raise JointExecutionOutcomeError(
                f"joint outcome official session token mismatch: {session_date}"
            )
        projection = bind_official_execution_session_to_decisions(
            session,
            expected_symbols=symbols,
            source_snapshot_digest=source.source_snapshot_digest,
        )
        bound[session_date] = projection
        artifact = session.artifact
        payload: dict[str, object] = {
            "offset_from_signal": offset,
            "session_date": session_date,
            "official_session_artifact_digest": session.artifact_digest,
            "official_raw_file_set_digest": session.raw_file_set_digest,
            "official_universe_membership_digest": str(
                artifact["universe_membership_digest"]
            ),
            "decision_membership_digest": projection.decision_membership_digest,
            "source_snapshot_digest": projection.source_snapshot_digest,
            "official_generated_at": str(artifact["generated_at"]),
        }
        payload["binding_digest"] = joint_execution_outcome_content_digest(
            payload, "binding_digest"
        )
        bindings.append(
            JointExecutionOutcomeSessionBinding.model_validate(payload).model_dump(
                mode="json"
            )
        )
    return bound, bindings


def _outcome_facts(
    source_record: Mapping[str, object],
    horizon: int,
    calendar: Mapping[str, object],
    bound: Mapping[str, BoundOfficialExecutionDecisionSession],
    *,
    source_snapshot_digest: str,
) -> _OutcomeFacts:
    context = _outcome_path_context(
        source_record,
        horizon,
        calendar,
        bound,
        source_snapshot_digest=source_snapshot_digest,
    )
    execution = _outcome_execution_context(context)
    return _OutcomeFacts(
        source_record=context.source_record,
        symbol=context.symbol,
        decision_id=context.decision_id,
        horizon=context.horizon,
        entry_row=context.entry_row,
        exit_row=context.exit_row,
        entry_bar=_bar_evidence(context.entry_row, "entry", 1),
        exit_bar=_bar_evidence(context.exit_row, "exit", horizon + 1),
        entry_rules=_rule_evidence(context.entry_row, "entry"),
        exit_rules=_rule_evidence(context.exit_row, "exit"),
        entry_reference=_reference_evidence(context.entry_row, "entry"),
        exit_reference=_reference_evidence(context.exit_row, "exit"),
        entry_state=execution.entry_state,
        exit_state=execution.exit_state,
        participation=execution.participation,
        holding_path=context.holding_path,
        entry_fill=execution.entry_fill,
        exit_executable=execution.exit_executable,
        exit_order_notional=execution.exit_order_notional,
    )


def _outcome_path_context(
    source_record: Mapping[str, object],
    horizon: int,
    calendar: Mapping[str, object],
    bound: Mapping[str, BoundOfficialExecutionDecisionSession],
    *,
    source_snapshot_digest: str,
) -> _OutcomePathContext:
    symbol = str(source_record["symbol"])
    decision_id = str(source_record["decision_id"])
    forward = [str(item) for item in cast(list[object], calendar["forward_sessions"])]
    path_dates = forward[: horizon + 1]
    path_rows = [bound[item].row_by_symbol()[symbol] for item in path_dates]
    entry_row, exit_row = path_rows[0], path_rows[-1]
    if source_snapshot_digest != bound[path_dates[0]].source_snapshot_digest:
        raise JointExecutionOutcomeError("joint outcome source snapshot binding drifted")
    return _OutcomePathContext(
        source_record=source_record,
        symbol=symbol,
        decision_id=decision_id,
        horizon=horizon,
        entry_row=entry_row,
        exit_row=exit_row,
        holding_path=_holding_path(horizon, path_dates, path_rows, bound),
    )


def _outcome_execution_context(context: _OutcomePathContext) -> _OutcomeExecutionContext:
    entry_state = _execution_state(
        context.entry_row,
        "entry",
        order_notional=JOINT_EXECUTION_ORDER_NOTIONAL,
    )
    entry_fill = entry_state["execution_state"] == "executable"
    exit_notional = _exit_order_notional(
        context.entry_row,
        context.exit_row,
        float(cast(float, context.holding_path["corporate_action_factor"])),
    )
    exit_state = _execution_state(
        context.exit_row,
        "exit",
        order_notional=exit_notional,
    )
    exit_executable = (
        exit_state["execution_state"] == "executable" if entry_fill else None
    )
    participation = _participation_evidence(
        context.entry_row,
        context.exit_row,
        entry_notional=JOINT_EXECUTION_ORDER_NOTIONAL,
        exit_notional=exit_notional,
    )
    return _OutcomeExecutionContext(
        entry_state=entry_state,
        exit_state=exit_state,
        participation=participation,
        entry_fill=entry_fill,
        exit_executable=exit_executable,
        exit_order_notional=exit_notional,
    )


def _holding_path(
    horizon: int,
    session_dates: Sequence[str],
    rows: Sequence[Mapping[str, object]],
    bound: Mapping[str, BoundOfficialExecutionDecisionSession],
) -> dict[str, object]:
    steps: list[dict[str, object]] = []
    for offset, (session_date, row) in enumerate(
        zip(session_dates, rows, strict=True), start=1
    ):
        corporate = _mapping(row["corporate_action"], "official.corporate_action")
        step: dict[str, object] = {
            "offset_from_signal": offset,
            "session_date": session_date,
            "official_session_artifact_digest": bound[
                session_date
            ].official_session_artifact_digest,
            "official_raw_file_set_digest": _official_raw_digest(bound, session_date),
            "official_row_digest": str(row["row_digest"]),
            "official_reference_evidence_digest": str(corporate["evidence_digest"]),
            "corporate_action_status": str(corporate["status"]),
            "corporate_action_event_id": corporate.get("event_id"),
            "previous_close": float(cast(float, corporate["previous_close"])),
            "reference_price": float(cast(float, corporate["reference_price"])),
            "reference_continuity_factor": float(
                cast(float, corporate["previous_close"])
            )
            / float(cast(float, corporate["reference_price"])),
            "applied_to_holding_return": offset > 1,
        }
        step["step_digest"] = joint_execution_outcome_content_digest(
            step, "step_digest"
        )
        steps.append(step)
    factor = 1.0
    for item in steps:
        if item["applied_to_holding_return"] is True:
            factor *= float(cast(float, item["reference_continuity_factor"]))
    payload: dict[str, object] = {
        "horizon": horizon,
        "entry_session": session_dates[0],
        "exit_session": session_dates[-1],
        "steps": steps,
        "corporate_action_factor": factor,
    }
    payload["path_digest"] = joint_execution_outcome_content_digest(
        payload, "path_digest"
    )
    return JointExecutionHoldingPath.model_validate(payload).model_dump(mode="json")


def _official_raw_digest(
    bound: Mapping[str, BoundOfficialExecutionDecisionSession], session_date: str
) -> str:
    # The bound projection intentionally exposes the artifact digest but not
    # the raw set.  The caller has already verified the session token; the raw
    # digest is recovered later from the top-level binding during construction.
    # This placeholder is replaced by _replace_path_raw_digests before sealing.
    projection = bound[session_date]
    value = getattr(projection, "official_raw_file_set_digest", None)
    if not isinstance(value, str):
        raise JointExecutionOutcomeError(
            "bound official session did not preserve raw-file-set identity"
        )
    return value


def _bar_evidence(
    row: Mapping[str, object], role: Literal["entry", "exit"], offset: int
) -> dict[str, object]:
    bar = _mapping(row["bar"], "official.bar")
    return JointExecutionSessionBarEvidence(
        role=role,
        session_date=str(row["session_date"]),
        session_offset_from_signal=offset,
        source_kind="official_exchange_daily_ohlcv_amount",
        adjustment_mode="none",
        open=cast(float | None, bar.get("open")),
        high=cast(float | None, bar.get("high")),
        low=cast(float | None, bar.get("low")),
        close=cast(float | None, bar.get("close")),
        volume=cast(float | None, bar.get("volume")),
        amount=cast(float | None, bar.get("amount")),
        source_dataset_digest=str(row["receipt_digest"]),
    ).model_dump(mode="json")


def _rule_evidence(
    row: Mapping[str, object], role: Literal["entry", "exit"]
) -> dict[str, object]:
    rules = _mapping(row["instrument_rules"], "official.instrument_rules")
    return JointExecutionRuleEvidence(
        role=role,
        session_date=str(row["session_date"]),
        source_kind="official_effective_dated",
        effective_date=str(rules["effective_date"]),
        board=cast(str, rules["board"]),
        is_st=cast(bool, rules["is_st"]),
        listing_status=cast(str, rules["listing_status"]),
        board_rule_id=str(rules["board_rule_id"]),
        st_rule_id=str(rules["st_rule_id"]),
        delisting_rule_id=str(rules["delisting_rule_id"]),
        ruleset_digest=str(rules["ruleset_digest"]),
    ).model_dump(mode="json")


def _reference_evidence(
    row: Mapping[str, object], role: Literal["entry", "exit"]
) -> dict[str, object]:
    value = _mapping(row["corporate_action"], "official.corporate_action")
    return JointExecutionReferencePriceEvidence(
        role=role,
        session_date=str(row["session_date"]),
        basis="official_unadjusted_reference_with_effective_corporate_action",
        previous_close=float(cast(float, value["previous_close"])),
        reference_price=float(cast(float, value["reference_price"])),
        corporate_action_status=cast(str, value["status"]),
        reference_price_rule_id=str(value["reference_price_rule_id"]),
        source_dataset_digest=str(row["receipt_digest"]),
    ).model_dump(mode="json")


def _execution_state(
    row: Mapping[str, object],
    role: Literal["entry", "exit"],
    *,
    order_notional: float,
) -> dict[str, object]:
    official_state = str(row[f"{role}_execution_state"])
    reason = str(row[f"{role}_reason_code"])
    bar = _mapping(row["bar"], "official.bar")
    rules = _mapping(row["instrument_rules"], "official.instrument_rules")
    amount = bar.get("amount")
    if official_state == "capacity_exceeded":
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
            raise JointExecutionOutcomeError("official capacity state lacks session amount")
        if order_notional / float(amount) <= JOINT_EXECUTION_MAXIMUM_PARTICIPATION_RATE:
            raise JointExecutionOutcomeError(
                "official capacity state conflicts with preregistered participation"
            )
    elif official_state == "executable" and str(row["exchange_session_state"]) == "trading":
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
            raise JointExecutionOutcomeError("official trading state lacks positive amount")
        if order_notional / float(amount) > JOINT_EXECUTION_MAXIMUM_PARTICIPATION_RATE:
            official_state = "capacity_exceeded"
            reason = "registered_participation_capacity_exceeded"
        elif role == "entry" and not _minimum_quantity_affordable(
            order_notional,
            cast(float, bar.get("open")),
            int(cast(int, rules["minimum_buy_quantity"])),
            int(cast(int, rules["buy_quantity_step"])),
        ):
            official_state = "rule_ineligible"
            reason = "registered_minimum_quantity_ineligible"
    return JointExecutionSessionStateEvidenceV3(
        role=role,
        session_date=str(row["session_date"]),
        source_kind="official_effective_dated_trading_state",
        exchange_session_state=cast(str, row["exchange_session_state"]),
        execution_state=cast(OutcomeExecutionState, official_state),
        reason_code=reason,
        observed_at=str(row["observed_at"]),
        effective_rules_digest=str(rules["ruleset_digest"]),
        trading_state_digest=str(row["trading_state_digest"]),
    ).model_dump(mode="json")


def _minimum_quantity_affordable(
    notional: float, price: float | None, minimum: int, step: int
) -> bool:
    if price is None or price <= 0 or minimum <= 0 or step <= 0:
        return False
    return (floor(notional / price) // step) * step >= minimum


def _exit_order_notional(
    entry_row: Mapping[str, object],
    exit_row: Mapping[str, object],
    corporate_action_factor: float,
) -> float:
    entry_bar = _mapping(entry_row["bar"], "official.entry_bar")
    exit_bar = _mapping(exit_row["bar"], "official.exit_bar")
    entry_price, exit_price = entry_bar.get("open"), exit_bar.get("close")
    if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in (entry_price, exit_price)):
        value = (
            JOINT_EXECUTION_ORDER_NOTIONAL
            * float(cast(float, exit_price))
            * corporate_action_factor
            / float(cast(float, entry_price))
        )
        if isfinite(value) and value > 0:
            return value
    return JOINT_EXECUTION_ORDER_NOTIONAL


def _participation_evidence(
    entry_row: Mapping[str, object],
    exit_row: Mapping[str, object],
    *,
    entry_notional: float,
    exit_notional: float,
) -> dict[str, object]:
    entry_amount = _mapping(entry_row["bar"], "entry.bar").get("amount")
    exit_amount = _mapping(exit_row["bar"], "exit.bar").get("amount")
    payload: dict[str, object] = {
        "basis": "entry_and_exit_same_session_amount",
        "entry_order_notional": entry_notional,
        "entry_session_amount": entry_amount,
        "entry_participation_rate": (
            entry_notional / float(cast(float, entry_amount))
            if isinstance(entry_amount, (int, float))
            and not isinstance(entry_amount, bool)
            and entry_amount > 0
            else None
        ),
        "exit_order_notional": exit_notional,
        "exit_session_amount": exit_amount,
        "exit_participation_rate": (
            exit_notional / float(cast(float, exit_amount))
            if isinstance(exit_amount, (int, float))
            and not isinstance(exit_amount, bool)
            and exit_amount > 0
            else None
        ),
        "maximum_participation_rate": JOINT_EXECUTION_MAXIMUM_PARTICIPATION_RATE,
    }
    payload["evidence_digest"] = _digest(payload)
    return JointExecutionParticipationEvidence.model_validate(payload).model_dump(mode="json")


def _benchmark_series(
    facts: Sequence[_OutcomeFacts],
    profiles: Mapping[str, PaperCostProfile],
    horizons: Sequence[int] = JOINT_EXECUTION_HORIZONS,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for horizon in _registered_horizons(horizons):
        cohort = sorted(
            (item for item in facts if item.horizon == horizon),
            key=lambda item: item.decision_id,
        )
        for profile_name in JOINT_EXECUTION_PROFILE_NAMES:
            profile = profiles[profile_name]
            points = [
                _benchmark_point(item, profile)
                for item in cohort
            ]
            payload: dict[str, object] = {
                "horizon": horizon,
                "profile_name": profile_name,
                "profile_id": profile.profile_id,
                "method": JOINT_EXECUTION_BENCHMARK_METHOD,
                "population_count": len(points),
                "points": points,
            }
            payload["series_digest"] = joint_execution_outcome_content_digest(
                payload, "series_digest"
            )
            output.append(
                JointExecutionBenchmarkSeries.model_validate(payload).model_dump(
                    mode="json"
                )
            )
    return output


def _benchmark_point(
    facts: _OutcomeFacts, profile: PaperCostProfile
) -> dict[str, object]:
    if str(facts.entry_row["exchange_session_state"]) != "trading":
        value, reason = 0.0, "cash_nontrading_entry_session"
    elif str(facts.exit_row["exchange_session_state"]) != "trading":
        value, reason = 0.0, "cash_nontrading_exit_session"
    else:
        gross = _gross_return(facts)
        entry_cost, exit_cost = _cost_returns(
            profile,
            exit_order_notional=facts.exit_order_notional,
            entry_filled=True,
            exit_executed=True,
        )
        value, reason = gross - entry_cost - exit_cost, "official_path_hypothetical_return"
    point: dict[str, object] = {
        "decision_id": facts.decision_id,
        "symbol": facts.symbol,
        "hypothetical_return": value,
        "reason_code": reason,
    }
    point["point_digest"] = joint_execution_outcome_content_digest(
        point, "point_digest"
    )
    return JointExecutionBenchmarkPoint.model_validate(point).model_dump(mode="json")


def _outcome_record(
    source_record: Mapping[str, object],
    facts: Mapping[tuple[str, int], _OutcomeFacts],
    benchmarks: Mapping[tuple[int, str], Mapping[str, object]],
    profiles: Mapping[str, PaperCostProfile],
    horizons: Sequence[int] = JOINT_EXECUTION_HORIZONS,
) -> dict[str, object]:
    decision_id = str(source_record["decision_id"])
    horizon_payloads = [
        _outcome_horizon(
            facts[(decision_id, horizon)],
            benchmarks,
            profiles,
        )
        for horizon in _registered_horizons(horizons)
    ]
    payload: dict[str, object] = {
        "decision_id": decision_id,
        "symbol": str(source_record["symbol"]),
        "source_record_digest": str(source_record["record_digest"]),
        "horizons": horizon_payloads,
    }
    payload["record_digest"] = joint_execution_outcome_content_digest(
        payload, "record_digest"
    )
    return JointExecutionOutcomeRecord.model_validate(payload).model_dump(mode="json")


def _outcome_horizon(
    facts: _OutcomeFacts,
    benchmarks: Mapping[tuple[int, str], Mapping[str, object]],
    profiles: Mapping[str, PaperCostProfile],
) -> dict[str, object]:
    scenarios = [
        _scenario(
            facts,
            profile_name,
            profiles[profile_name],
            benchmarks[(facts.horizon, profile_name)],
        )
        for profile_name in JOINT_EXECUTION_PROFILE_NAMES
    ]
    payload: dict[str, object] = {
        "horizon": facts.horizon,
        "target_session": str(facts.exit_row["session_date"]),
        "entry_bar": facts.entry_bar,
        "exit_bar": facts.exit_bar,
        "entry_rules": facts.entry_rules,
        "exit_rules": facts.exit_rules,
        "entry_reference": facts.entry_reference,
        "exit_reference": facts.exit_reference,
        "entry_state": facts.entry_state,
        "exit_state": facts.exit_state,
        "participation": facts.participation,
        "holding_path": facts.holding_path,
        "scenarios": scenarios,
    }
    payload["horizon_digest"] = joint_execution_outcome_content_digest(
        payload, "horizon_digest"
    )
    return JointExecutionOutcomeHorizon.model_validate(payload).model_dump(mode="json")


def _scenario(
    facts: _OutcomeFacts,
    profile_name: str,
    profile: PaperCostProfile,
    benchmark: Mapping[str, object],
) -> dict[str, object]:
    points = [
        _mapping(item, "benchmark.points[]")
        for item in cast(list[object], benchmark["points"])
    ]
    point_by_id = {str(item["decision_id"]): item for item in points}
    subject = point_by_id[facts.decision_id]
    total = sum(float(cast(float, item["hypothetical_return"])) for item in points)
    benchmark_return = (
        total - float(cast(float, subject["hypothetical_return"]))
    ) / (len(points) - 1)
    entry_cost, exit_cost = _cost_returns(
        profile,
        exit_order_notional=facts.exit_order_notional,
        entry_filled=facts.entry_fill,
        exit_executed=facts.exit_executable is True,
    )
    costs_payload: dict[str, object] = {
        "model_version": PAPER_COST_PROFILE_VERSION,
        "profile_id": profile.profile_id,
        "slippage_model_version": JOINT_EXECUTION_SLIPPAGE_MODEL_VERSION,
        "entry_order_notional": JOINT_EXECUTION_ORDER_NOTIONAL,
        "exit_order_notional": facts.exit_order_notional,
        "maximum_participation_rate": JOINT_EXECUTION_MAXIMUM_PARTICIPATION_RATE,
        "entry_cost_return": entry_cost,
        "exit_cost_return": exit_cost,
        "total_cost_return": entry_cost + exit_cost,
    }
    costs_payload["evidence_digest"] = joint_execution_v3_content_digest(costs_payload)
    costs = JointExecutionCostEvidenceV3.model_validate(costs_payload).model_dump(mode="json")
    outcome = _observed_outcome(
        facts,
        costs,
        benchmark_return=benchmark_return,
    )
    payload: dict[str, object] = {
        "profile_name": profile_name,
        "profile_id": profile.profile_id,
        "benchmark_series_digest": str(benchmark["series_digest"]),
        "costs": costs,
        "observed_outcome": outcome,
    }
    payload["scenario_digest"] = joint_execution_outcome_content_digest(
        payload, "scenario_digest"
    )
    return JointExecutionOutcomeScenario.model_validate(payload).model_dump(mode="json")


def _cost_returns(
    profile: PaperCostProfile,
    *,
    exit_order_notional: float,
    entry_filled: bool,
    exit_executed: bool,
) -> tuple[float, float]:
    entry = (
        trade_costs(
            profile,
            side="buy",
            gross_amount=JOINT_EXECUTION_ORDER_NOTIONAL,
        ).total
        / JOINT_EXECUTION_ORDER_NOTIONAL
        if entry_filled
        else 0.0
    )
    exit_value = (
        trade_costs(profile, side="sell", gross_amount=exit_order_notional).total
        / JOINT_EXECUTION_ORDER_NOTIONAL
        if exit_executed
        else 0.0
    )
    return entry, exit_value


def _observed_outcome(
    facts: _OutcomeFacts,
    costs: Mapping[str, object],
    *,
    benchmark_return: float,
) -> dict[str, object]:
    observed_at = str(facts.exit_row["observed_at"])
    if not facts.entry_fill:
        payload = _unfilled_outcome_payload(facts, benchmark_return, observed_at)
    elif facts.exit_executable is not True:
        payload = _unexecutable_exit_outcome_payload(facts, observed_at)
    else:
        payload = _executed_outcome_payload(facts, costs, benchmark_return, observed_at)
    payload["evidence_digest"] = joint_execution_v3_content_digest(payload)
    return JointExecutionObservedOutcomeV3.model_validate(payload).model_dump(mode="json")


def _unfilled_outcome_payload(
    facts: _OutcomeFacts,
    benchmark_return: float,
    observed_at: str,
) -> dict[str, object]:
    return {
        "target": JOINT_EXECUTION_TARGET,
        "entry_fill": False,
        "exit_executable": None,
        "net_positive": None,
        "joint_action_positive": False,
        "entry_price": None,
        "exit_price": None,
        "gross_return": None,
        "net_return": 0.0,
        "benchmark_return": benchmark_return,
        "net_excess_return": -benchmark_return,
        "observed_at": observed_at,
        "outcome_reason_codes": [f"entry_{facts.entry_state['reason_code']}_no_fill"],
    }


def _unexecutable_exit_outcome_payload(
    facts: _OutcomeFacts,
    observed_at: str,
) -> dict[str, object]:
    return {
        "target": JOINT_EXECUTION_TARGET,
        "entry_fill": True,
        "exit_executable": False,
        "net_positive": None,
        "joint_action_positive": False,
        "entry_price": cast(float, facts.entry_bar["open"]),
        "exit_price": None,
        "gross_return": None,
        "net_return": None,
        "benchmark_return": None,
        "net_excess_return": None,
        "observed_at": observed_at,
        "outcome_reason_codes": [
            f"exit_{facts.exit_state['reason_code']}_unexecutable"
        ],
    }


def _executed_outcome_payload(
    facts: _OutcomeFacts,
    costs: Mapping[str, object],
    benchmark_return: float,
    observed_at: str,
) -> dict[str, object]:
    gross = _gross_return(facts)
    net = gross - float(cast(float, costs["total_cost_return"]))
    excess = net - benchmark_return
    return {
        "target": JOINT_EXECUTION_TARGET,
        "entry_fill": True,
        "exit_executable": True,
        "net_positive": excess > 0,
        "joint_action_positive": excess > 0,
        "entry_price": cast(float, facts.entry_bar["open"]),
        "exit_price": cast(float, facts.exit_bar["close"]),
        "gross_return": gross,
        "net_return": net,
        "benchmark_return": benchmark_return,
        "net_excess_return": excess,
        "observed_at": observed_at,
        "outcome_reason_codes": [
            "entry_filled_exit_executable_net_excess_observed",
            "official_corporate_action_path_applied",
        ],
    }


def _gross_return(facts: _OutcomeFacts) -> float:
    entry = facts.entry_bar.get("open")
    exit_price = facts.exit_bar.get("close")
    if not isinstance(entry, (int, float)) or isinstance(entry, bool) or entry <= 0:
        raise JointExecutionOutcomeError("joint outcome executable entry price is missing")
    if (
        not isinstance(exit_price, (int, float))
        or isinstance(exit_price, bool)
        or exit_price <= 0
    ):
        raise JointExecutionOutcomeError("joint outcome executable exit price is missing")
    value = (
        float(exit_price)
        * float(cast(float, facts.holding_path["corporate_action_factor"]))
        / float(entry)
        - 1.0
    )
    if not isfinite(value):
        raise JointExecutionOutcomeError("joint outcome return is not finite")
    return value


def _cost_contract(
    horizons: Sequence[int] = JOINT_EXECUTION_HORIZONS,
) -> dict[str, object]:
    registered_horizons = _registered_horizons(horizons)
    profiles = [_cost_profile_payload(name) for name in JOINT_EXECUTION_PROFILE_NAMES]
    payload: dict[str, object] = {
        "label_version": JOINT_EXECUTION_LABEL_VERSION,
        "execution_model": JOINT_EXECUTION_EXECUTION_MODEL,
        "horizons": list(registered_horizons),
        "entry_session_offset": 1,
        "target_session_offsets": {
            str(value): value + 1 for value in registered_horizons
        },
        "target": JOINT_EXECUTION_TARGET,
        "target_population": (
            "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"
        ),
        "observed_components": ["entry_fill", "exit_executable", "net_positive"],
        "execution_notional": JOINT_EXECUTION_ORDER_NOTIONAL,
        "maximum_participation_rate": JOINT_EXECUTION_MAXIMUM_PARTICIPATION_RATE,
        "cost_model_version": PAPER_COST_PROFILE_VERSION,
        "slippage_model_version": JOINT_EXECUTION_SLIPPAGE_MODEL_VERSION,
        "primary_profile": JOINT_EXECUTION_PRIMARY_PROFILE,
        "profiles": profiles,
        "corporate_action_method": JOINT_EXECUTION_CORPORATE_ACTION_METHOD,
        "benchmark_method": JOINT_EXECUTION_BENCHMARK_METHOD,
    }
    payload["contract_digest"] = joint_execution_outcome_content_digest(
        payload, "contract_digest"
    )
    return JointExecutionOutcomeCostContract.model_validate(payload).model_dump(mode="json")


def _cost_profile_payload(profile_name: str) -> dict[str, object]:
    profile = resolve_cost_profile(cast(CostProfileName, profile_name))
    payload: dict[str, object] = {
        "profile_name": profile_name,
        "profile_id": profile.profile_id,
        "version": profile.version,
        "effective_from": profile.effective_from,
        "commission_rate_pct": profile.commission_rate_pct,
        "minimum_commission": profile.minimum_commission,
        "stamp_duty_sell_pct": profile.stamp_duty_sell_pct,
        "transfer_fee_pct": profile.transfer_fee_pct,
        "slippage_buy_pct": profile.slippage_buy_pct,
        "slippage_sell_pct": profile.slippage_sell_pct,
        "source_urls": profile.source_urls,
    }
    payload["profile_digest"] = joint_execution_outcome_content_digest(
        payload, "profile_digest"
    )
    return JointExecutionOutcomeCostProfile.model_validate(payload).model_dump(mode="json")


def _quality(
    records: Sequence[Mapping[str, object]],
    session_bindings: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if not records:
        raise JointExecutionOutcomeError("joint outcome quality requires records")
    first_horizons = cast(list[object], records[0]["horizons"])
    horizons = _registered_horizons(
        [int(cast(int, _mapping(item, "record.horizons[]")["horizon"])) for item in first_horizons]
    )
    rows: list[dict[str, object]] = []
    for horizon in horizons:
        horizon_rows = [
            _horizon_mapping(record, horizon)
            for record in records
        ]
        for profile_name in JOINT_EXECUTION_PROFILE_NAMES:
            outcomes = [
                _scenario_mapping(item, profile_name)["observed_outcome"]
                for item in horizon_rows
            ]
            normalized = [
                _mapping(item, "quality.observed_outcome") for item in outcomes
            ]
            rows.append(
                {
                    "horizon": horizon,
                    "profile_name": profile_name,
                    "decision_count": len(records),
                    "entry_fill_count": sum(
                        item["entry_fill"] is True for item in normalized
                    ),
                    "exit_executable_count": sum(
                        item["exit_executable"] is True for item in normalized
                    ),
                    "joint_action_positive_count": sum(
                        item["joint_action_positive"] is True for item in normalized
                    ),
                    "unresolved_round_trip_return_count": sum(
                        item["entry_fill"] is True
                        and item["exit_executable"] is False
                        for item in normalized
                    ),
                    "joint_component_label_coverage": 1.0,
                }
            )
    return {
        "expected_decision_count": len(records),
        "record_count": len(records),
        "official_forward_session_count": len(session_bindings),
        "all_decisions_included": True,
        "complete_official_session_path": len(session_bindings)
        == max(horizons) + 1,
        "corporate_action_path_replayed": True,
        "no_delayed_fill_or_exit": True,
        "no_post_outcome_selection": True,
        "formal_forward_outcome_eligible": True,
        "rows": rows,
    }


def _validate_horizon_roles(value: JointExecutionOutcomeHorizon) -> None:
    entry_date, exit_date = value.holding_path.entry_session, value.holding_path.exit_session
    _validate_endpoint_role_date(
        "entry",
        entry_date,
        (value.entry_bar, value.entry_rules, value.entry_reference, value.entry_state),
    )
    _validate_endpoint_role_date(
        "exit",
        exit_date,
        (value.exit_bar, value.exit_rules, value.exit_reference, value.exit_state),
    )
    _validate_endpoint_offsets_and_rules(value)
    _validate_scenario_execution_roles(value)


def _validate_endpoint_role_date(
    role: Literal["entry", "exit"],
    session_date: str,
    items: Sequence[object],
) -> None:
    if any(
        getattr(item, "role", None) != role
        or getattr(item, "session_date", None) != session_date
        for item in items
    ):
        raise ValueError("joint outcome endpoint evidence role/date mismatch")


def _validate_endpoint_offsets_and_rules(value: JointExecutionOutcomeHorizon) -> None:
    if value.entry_bar.session_offset_from_signal != 1 or (
        value.exit_bar.session_offset_from_signal != value.horizon + 1
    ):
        raise ValueError("joint outcome endpoint offsets mismatch")
    if (
        value.entry_rules.ruleset_digest != value.entry_state.effective_rules_digest
        or value.exit_rules.ruleset_digest != value.exit_state.effective_rules_digest
    ):
        raise ValueError("joint outcome state/rules digest mismatch")


def _validate_scenario_execution_roles(value: JointExecutionOutcomeHorizon) -> None:
    entry_fill = value.entry_state.execution_state == "executable"
    exit_executable = value.exit_state.execution_state == "executable"
    for scenario in value.scenarios:
        observed = scenario.observed_outcome
        if observed.entry_fill is not entry_fill or (
            entry_fill and observed.exit_executable is not exit_executable
        ):
            raise ValueError("joint outcome components conflict with execution states")


def _validate_scenario_math(
    horizon: JointExecutionOutcomeHorizon,
    scenario: JointExecutionOutcomeScenario,
) -> None:
    outcome = scenario.observed_outcome
    costs = scenario.costs
    participation = horizon.participation
    if not isclose(
        costs.entry_order_notional,
        participation.entry_order_notional,
        rel_tol=0,
        abs_tol=1e-9,
    ) or not isclose(
        costs.exit_order_notional,
        participation.exit_order_notional,
        rel_tol=0,
        abs_tol=1e-9,
    ):
        raise ValueError("joint outcome costs do not bind participation")
    if outcome.exit_executable is not True:
        return
    entry = cast(float, outcome.entry_price)
    exit_price = cast(float, outcome.exit_price)
    gross = exit_price * horizon.holding_path.corporate_action_factor / entry - 1.0
    net = gross - costs.total_cost_return
    benchmark = cast(float, outcome.benchmark_return)
    excess = net - benchmark
    if not all(
        (
            isclose(cast(float, outcome.gross_return), gross, rel_tol=0, abs_tol=1e-12),
            isclose(cast(float, outcome.net_return), net, rel_tol=0, abs_tol=1e-12),
            isclose(
                cast(float, outcome.net_excess_return),
                excess,
                rel_tol=0,
                abs_tol=1e-12,
            ),
            outcome.net_positive is (excess > 0),
            outcome.joint_action_positive is (excess > 0),
        )
    ):
        raise ValueError("joint outcome scenario returns do not replay")


def _validate_session_bindings(
    artifact: JointExecutionOutcomeArtifact, generated: datetime
) -> None:
    if _registered_horizons(artifact.calendar.horizon_exit_sessions) != tuple(
        artifact.label_contract.horizons
    ):
        raise ValueError("joint outcome calendar/label horizons differ")
    dates = [item.session_date for item in artifact.session_bindings]
    offsets = [item.offset_from_signal for item in artifact.session_bindings]
    if dates != artifact.calendar.forward_sessions or offsets != list(
        range(1, len(artifact.calendar.forward_sessions) + 1)
    ):
        raise ValueError("joint outcome session bindings do not cover calendar path")
    if any(
        item.source_snapshot_digest != artifact.source.source_snapshot_digest
        or item.decision_membership_digest
        != artifact.source.decision_membership_digest
        for item in artifact.session_bindings
    ):
        raise ValueError("joint outcome official session/source binding mismatch")
    if any(
        generated < _timestamp(item.official_generated_at, "official_generated_at")
        for item in artifact.session_bindings
    ):
        raise ValueError("joint outcome artifact predates official session evidence")


def _validate_artifact_records(artifact: JointExecutionOutcomeArtifact) -> None:
    _validate_artifact_record_population(artifact)
    binding_by_date = {item.session_date: item for item in artifact.session_bindings}
    expected_horizons = artifact.label_contract.horizons
    for record in artifact.records:
        _validate_artifact_record_paths(record, expected_horizons, binding_by_date)


def _validate_artifact_record_population(artifact: JointExecutionOutcomeArtifact) -> None:
    symbols = [item.symbol for item in artifact.records]
    identities = [item.decision_id for item in artifact.records]
    if symbols != sorted(set(symbols)) or len(set(identities)) != len(identities):
        raise ValueError("joint outcome records are not canonical and unique")
    if len(artifact.records) != artifact.source.expected_decision_count:
        raise ValueError("joint outcome fixed decisions are incomplete")
    if _digest(symbols) != artifact.source.decision_membership_digest:
        raise ValueError("joint outcome source membership digest mismatch")
    if _digest(identities) != artifact.source.decision_identity_digest:
        raise ValueError("joint outcome source identity digest mismatch")


def _validate_artifact_record_paths(
    record: JointExecutionOutcomeRecord,
    expected_horizons: Sequence[int],
    binding_by_date: Mapping[str, JointExecutionOutcomeSessionBinding],
) -> None:
    if [item.horizon for item in record.horizons] != expected_horizons:
        raise ValueError("joint outcome record/label horizons differ")
    for horizon in record.horizons:
        for step in horizon.holding_path.steps:
            binding = binding_by_date[step.session_date]
            if (
                step.official_session_artifact_digest
                != binding.official_session_artifact_digest
                or step.official_raw_file_set_digest
                != binding.official_raw_file_set_digest
            ):
                raise ValueError("joint outcome holding path session binding mismatch")


def _validate_benchmark_bindings(artifact: JointExecutionOutcomeArtifact) -> None:
    keys = [(item.horizon, item.profile_name) for item in artifact.benchmarks]
    expected_keys = [
        (horizon, profile)
        for horizon in artifact.label_contract.horizons
        for profile in JOINT_EXECUTION_PROFILE_NAMES
    ]
    if keys != expected_keys:
        raise ValueError("joint outcome benchmark series are not canonical")
    profile_ids: dict[str, str] = {
        str(item.profile_name): item.profile_id for item in artifact.label_contract.profiles
    }
    record_ids = [item.decision_id for item in artifact.records]
    for series in artifact.benchmarks:
        _validate_benchmark_series_identity(series, profile_ids, record_ids)
        _validate_benchmark_series_outcomes(series, artifact.records)


def _validate_benchmark_series_identity(
    series: JointExecutionBenchmarkSeries,
    profile_ids: Mapping[str, str],
    record_ids: Sequence[str],
) -> None:
    if (
        series.profile_id != profile_ids[series.profile_name]
        or [item.decision_id for item in series.points] != record_ids
    ):
        raise ValueError("joint outcome benchmark does not bind fixed decisions/profile")


def _validate_benchmark_series_outcomes(
    series: JointExecutionBenchmarkSeries,
    records: Sequence[JointExecutionOutcomeRecord],
) -> None:
    total = sum(item.hypothetical_return for item in series.points)
    by_id = {item.decision_id: item for item in series.points}
    for record in records:
        horizon = next(item for item in record.horizons if item.horizon == series.horizon)
        scenario = next(
            item for item in horizon.scenarios if item.profile_name == series.profile_name
        )
        expected = (total - by_id[record.decision_id].hypothetical_return) / (
            series.population_count - 1
        )
        _validate_benchmark_outcome(scenario, series.series_digest, expected)


def _validate_benchmark_outcome(
    scenario: JointExecutionOutcomeScenario,
    series_digest: str,
    expected: float,
) -> None:
    if scenario.benchmark_series_digest != series_digest:
        raise ValueError("joint outcome scenario benchmark digest mismatch")
    outcome = scenario.observed_outcome
    if outcome.exit_executable is not False and not isclose(
        cast(float, outcome.benchmark_return), expected, rel_tol=0, abs_tol=1e-12
    ):
        raise ValueError("joint outcome leave-one-out benchmark does not replay")


def _validate_outcome_quality(artifact: JointExecutionOutcomeArtifact) -> None:
    expected = _quality(
        [item.model_dump(mode="json") for item in artifact.records],
        [item.model_dump(mode="json") for item in artifact.session_bindings],
    )
    if artifact.quality.model_dump(mode="json") != expected:
        raise ValueError("joint outcome quality does not replay")
    if not all(
        (
            artifact.quality.all_decisions_included,
            artifact.quality.complete_official_session_path,
            artifact.quality.corporate_action_path_replayed,
            artifact.quality.formal_forward_outcome_eligible,
        )
    ):
        raise ValueError("joint outcome formal evidence is incomplete")


def _horizon_mapping(
    record: Mapping[str, object], horizon: int
) -> Mapping[str, object]:
    values = [
        _mapping(item, "record.horizons[]")
        for item in cast(list[object], record["horizons"])
    ]
    return next(item for item in values if int(cast(int, item["horizon"])) == horizon)


def _scenario_mapping(
    horizon: Mapping[str, object], profile_name: str
) -> Mapping[str, object]:
    values = [
        _mapping(item, "horizon.scenarios[]")
        for item in cast(list[object], horizon["scenarios"])
    ]
    return next(item for item in values if item["profile_name"] == profile_name)


def _registered_horizons(value: Sequence[int] | Mapping[str, object]) -> tuple[int, ...]:
    if isinstance(value, Mapping):
        raw_keys = list(value)
        if any(not key.isdigit() or str(int(key)) != key for key in raw_keys):
            raise ValueError("joint outcome horizon keys are not canonical")
        values = [int(key) for key in raw_keys]
    else:
        values = list(value)
    if (
        not values
        or any(isinstance(item, bool) or not isinstance(item, int) for item in values)
        or values != [item for item in JOINT_EXECUTION_HORIZONS if item in values]
        or len(values) != len(set(values))
    ):
        raise ValueError("joint outcome horizons are not a canonical registered subset")
    return tuple(values)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise JointExecutionOutcomeError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return parsed


def _calendar_metadata_timestamp(value: str, label: str) -> datetime:
    """Parse the calendar file's exact timestamp, including legacy naive metadata."""

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO timestamp")
    return parsed


def _digest(value: object) -> str:
    return sha256_hex(canonical_json_bytes(value))


__all__ = [
    "JOINT_EXECUTION_BENCHMARK_METHOD",
    "JOINT_EXECUTION_CORPORATE_ACTION_METHOD",
    "JOINT_EXECUTION_HORIZONS",
    "JOINT_EXECUTION_LABEL_VERSION",
    "JOINT_EXECUTION_MAXIMUM_PARTICIPATION_RATE",
    "JOINT_EXECUTION_ORDER_NOTIONAL",
    "JOINT_EXECUTION_OUTCOME_CONTRACT_VERSION",
    "JOINT_EXECUTION_OUTCOME_SCHEMA_VERSION",
    "JOINT_EXECUTION_PROFILE_NAMES",
    "JOINT_EXECUTION_REQUIRED_FORWARD_SESSION_COUNT",
    "JointExecutionHoldingPath",
    "JointExecutionHoldingPathStep",
    "JointExecutionOutcomeArtifact",
    "JointExecutionOutcomeError",
    "VerifiedJointExecutionOutcomeCorpus",
    "build_joint_execution_outcome_artifact",
    "joint_execution_outcome_content_digest",
    "replay_and_verify_joint_execution_outcome_artifact",
    "verify_joint_execution_outcome_artifact",
]
