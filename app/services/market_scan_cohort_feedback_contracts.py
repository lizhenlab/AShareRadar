"""A distinct date-cohort contract; no implicit migration of event-window v1."""

from __future__ import annotations

from datetime import datetime, time
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from app.services.market_scan_cohort_feedback_calendar import FrozenCohortCalendar, cohort_date
from app.services.market_scan_delayed_feedback_contracts import (
    MAX_BATCH_EVENTS, MAX_LEDGER_EVENTS, Digest, FeedbackEventDefinition, FeedbackLabel,
    FeedbackModel, LabelText, SHANGHAI, feedback_json_bytes, feedback_market_date,
)


COHORT_FEEDBACK_VERSION = "market-scan-date-cohort-feedback-ledger-v1"


class CohortFeedbackConfig(FeedbackModel):
    event_definition: FeedbackEventDefinition
    model_digest: Digest
    update_rule: Literal["fixed_baseline", "rolling_signed_bias"]
    baseline_bias: float = Field(ge=-1, le=1)
    reference_base_rate: float = Field(ge=0, le=1)
    signal_start: str
    signal_end: str
    window_dates: int = Field(ge=1, le=250)
    minimum_complete_dates: int = Field(ge=1)
    minimum_labels: int = Field(ge=1)

    @model_validator(mode="after")
    def valid_plan(self) -> Self:
        if cohort_date(self.signal_start) > cohort_date(self.signal_end) or self.minimum_complete_dates > self.window_dates:
            raise ValueError("invalid date plan or minimum complete dates")
        return self


class CohortMember(FeedbackModel):
    prediction_id: LabelText
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    raw_probability: float = Field(ge=0, le=1)
    selected_top100: bool
    industry: LabelText | None
    regime: LabelText | None


class PredictionCohort(FeedbackModel):
    kind: Literal["prediction_cohort"]
    cohort_id: LabelText
    signal_date: str
    decision_at: str
    source_digest: Digest
    members: list[CohortMember] = Field(max_length=10_000)

    @model_validator(mode="after")
    def valid_members(self) -> Self:
        if feedback_market_date(self.decision_at) != cohort_date(self.signal_date):
            raise ValueError("cohort decision belongs to a different signal date")
        if len({member.prediction_id for member in self.members}) != len(self.members) or len({member.symbol for member in self.members}) != len(self.members):
            raise ValueError("cohort member IDs and symbols must be unique")
        return self


CohortEvent = Annotated[PredictionCohort | FeedbackLabel, Field(discriminator="kind")]
_EVENTS = TypeAdapter(list[CohortEvent])


class StoredCohortEvent(FeedbackModel):
    sequence: int = Field(ge=1)
    recorded_at: str
    event: CohortEvent
    derived: dict[str, object]
    state_before_digest: Digest
    state_after_digest: Digest
    previous_event_digest: Digest
    event_digest: Digest


class CohortFeedbackLedger(FeedbackModel):
    schema_version: Literal["market-scan-date-cohort-feedback-ledger-v1"]
    config: CohortFeedbackConfig
    calendar: FrozenCohortCalendar
    created_at: str
    events: list[StoredCohortEvent] = Field(max_length=MAX_LEDGER_EVENTS)
    summary: dict[str, object]
    provenance: dict[str, object]
    promotion_eligible: bool
    final_state_digest: Digest
    ledger_digest: Digest


def cohort_events(payload: object) -> list[PredictionCohort | FeedbackLabel]:
    events = _EVENTS.validate_json(feedback_json_bytes(payload), strict=True)
    if not 1 <= len(events) <= MAX_BATCH_EVENTS:
        raise ValueError("cohort feedback batch size outside limits")
    return events


def planned_dates(config: CohortFeedbackConfig, calendar: FrozenCohortCalendar) -> list[str]:
    days = [day for day in calendar.sessions if config.signal_start <= day <= config.signal_end]
    if not days or (days[0], days[-1]) != (config.signal_start, config.signal_end):
        raise ValueError("signal plan must start/end on covered frozen trading dates")
    cohort_endpoint(config, calendar, days[-1])
    return days


def cohort_endpoint(config: CohortFeedbackConfig, calendar: FrozenCohortCalendar, signal_date: str) -> datetime:
    if signal_date not in calendar.sessions:
        raise ValueError("signal date is not in frozen calendar")
    index = calendar.sessions.index(signal_date) + config.event_definition.target_offset_sessions
    if index >= len(calendar.sessions):
        raise ValueError("frozen calendar lacks target horizon coverage")
    return datetime.combine(cohort_date(calendar.sessions[index]), time(15), SHANGHAI)
