"""Strict, finite contracts for a local research-only delayed-feedback ledger."""

from __future__ import annotations

from datetime import date, datetime, timezone
import re
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, sha256_hex


FEEDBACK_VERSION = "market-scan-delayed-feedback-ledger-v1"
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_EVENTS = 100_000
MAX_BATCH_EVENTS = 20_000
SHANGHAI = ZoneInfo("Asia/Shanghai")
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
LabelText = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S(?:.*\S)?$")]


def feedback_time(value: str) -> datetime:
    if "T" not in value:
        raise ValueError("timestamp requires ISO T separator and explicit timezone")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamp requires an explicit timezone")
    try:
        return stamp.astimezone(timezone.utc)
    except OverflowError as error:
        raise ValueError("timestamp cannot be represented in UTC") from error


def feedback_digest(value: object) -> str:
    return sha256_hex(feedback_json_bytes(value))


def feedback_market_date(value: str) -> date:
    try:
        return feedback_time(value).astimezone(SHANGHAI).date()
    except OverflowError as error:
        raise ValueError("timestamp cannot be represented in Shanghai") from error


def feedback_json_bytes(value: object) -> bytes:
    try:
        encoded = canonical_json_bytes(value)
    except ArtifactIOError as error:
        raise ValueError("feedback payload must contain finite JSON") from error
    if len(encoded) > MAX_LEDGER_BYTES:
        raise ValueError("feedback ledger exceeds byte limit")
    return encoded


class FeedbackModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class FeedbackEventDefinition(FeedbackModel):
    horizon_sessions: int = Field(ge=0, le=250)
    target_offset_sessions: int = Field(ge=0, le=251)
    return_definition: LabelText
    benchmark_definition: LabelText
    threshold: float
    operator: Literal["gt", "ge"]
    conditioning_digest: Digest

    @model_validator(mode="after")
    def valid_horizon(self) -> Self:
        if self.target_offset_sessions not in (self.horizon_sessions, self.horizon_sessions + 1):
            raise ValueError("target offset must explicitly equal H or H+1 sessions")
        return self


class FeedbackConfig(FeedbackModel):
    event_definition: FeedbackEventDefinition
    model_digest: Digest
    update_rule: Literal["fixed_baseline", "rolling_signed_bias"]
    baseline_bias: float = Field(ge=-1, le=1)
    window_labels: int = Field(ge=1, le=10_000)
    minimum_labels: int = Field(ge=1)
    minimum_dates: int = Field(ge=1)
    reference_base_rate: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if not self.minimum_dates <= self.minimum_labels <= self.window_labels:
            raise ValueError("minimum dates <= minimum labels <= window labels is required")
        return self


class FeedbackPrediction(FeedbackModel):
    kind: Literal["prediction"]
    prediction_id: LabelText
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    cohort_id: LabelText
    source_digest: Digest
    signal_date: str
    decision_at: str
    label_end_at: str
    raw_probability: float = Field(ge=0, le=1)
    selected_top100: bool
    industry: LabelText | None
    regime: LabelText | None

    @field_validator("signal_date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("signal date must be canonical ISO")
        return value

    @model_validator(mode="after")
    def valid_timestamps(self) -> Self:
        decision, end = feedback_time(self.decision_at), feedback_time(self.label_end_at)
        if decision >= end or feedback_market_date(self.decision_at).isoformat() != self.signal_date:
            raise ValueError("prediction decision must belong to signal date and precede endpoint")
        return self


class FeedbackLabel(FeedbackModel):
    kind: Literal["label"]
    prediction_id: LabelText
    label_end_at: str
    available_at: str
    realized_return: float
    benchmark_return: float
    source_digest: Digest

    @model_validator(mode="after")
    def valid_availability(self) -> Self:
        if feedback_time(self.available_at) < feedback_time(self.label_end_at):
            raise ValueError("label cannot be available before its endpoint")
        return self


FeedbackEvent = Annotated[FeedbackPrediction | FeedbackLabel, Field(discriminator="kind")]


class StoredFeedbackEvent(FeedbackModel):
    sequence: int = Field(ge=1)
    event: FeedbackEvent
    recorded_at: str
    baseline_probability: float = Field(ge=0, le=1)
    applied_probability: float = Field(ge=0, le=1)
    outcome: Literal[0, 1] | None
    state_before_digest: Digest
    state_after_digest: Digest
    previous_event_digest: Digest
    event_digest: Digest

    @field_validator("outcome", mode="before")
    @classmethod
    def exact_outcome(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("stored outcome must be exact 0/1 or null")
        return value


class FeedbackLedger(FeedbackModel):
    schema_version: Literal["market-scan-delayed-feedback-ledger-v1"]
    config: FeedbackConfig
    config_digest: Digest
    created_at: str
    events: list[StoredFeedbackEvent] = Field(max_length=MAX_LEDGER_EVENTS)
    event_count: int = Field(ge=0)
    head_digest: Digest
    final_state_digest: Digest
    summary: dict[str, object]
    provenance: dict[str, object]
    promotion_eligible: Literal[False]
    limitations: list[str]
    ledger_digest: Digest

    @field_validator("promotion_eligible", mode="before")
    @classmethod
    def exact_promotion_flag(cls, value: object) -> object:
        if value is not False:
            raise ValueError("local feedback ledger must have promotion_eligible=false")
        return value


_EVENTS = TypeAdapter(list[FeedbackEvent])


def admit_feedback_events(payload: object) -> list[FeedbackPrediction | FeedbackLabel]:
    events = _EVENTS.validate_json(feedback_json_bytes(payload), strict=True)
    if not 1 <= len(events) <= MAX_BATCH_EVENTS:
        raise ValueError("feedback batch size outside limits")
    return events


def admit_feedback_ledger(payload: object, expected_digest: str) -> FeedbackLedger:
    ledger = FeedbackLedger.model_validate_json(feedback_json_bytes(payload), strict=True)
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None or ledger.ledger_digest != expected_digest:
        raise ValueError("independent ledger digest pin mismatch")
    if not isinstance(payload, dict):
        raise ValueError("feedback ledger must be an object")
    if feedback_digest({key: value for key, value in payload.items() if key != "ledger_digest"}) != expected_digest:
        raise ValueError("feedback ledger digest mismatch")
    return ledger
