"""Date-equal updates from complete, predeclared prediction cohorts only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math

from app.services.market_scan_cohort_feedback_calendar import FrozenCohortCalendar
from app.services.market_scan_cohort_feedback_contracts import (
    CohortFeedbackConfig, CohortMember, PredictionCohort, StoredCohortEvent, cohort_endpoint, planned_dates,
)
from app.services.market_scan_delayed_feedback_contracts import MAX_LEDGER_EVENTS, FeedbackLabel, feedback_digest, feedback_time, feedback_market_date


@dataclass(frozen=True)
class FrozenMember:
    member: CohortMember
    signal_date: str
    baseline_probability: float
    applied_probability: float


@dataclass
class CohortTrack:
    event: PredictionCohort
    recorded_at: str
    identity_digest: str
    labels: dict[str, tuple[str, int, float]] = field(default_factory=dict)
    label_history_digest: str = "0" * 64
    last_received_at: str | None = None
    complete_mean: float | None = None

    def evidence(self, at: datetime, deadline: datetime) -> dict[str, object]:
        count = len(self.labels)
        if self.last_received_at is not None and feedback_time(self.last_received_at) > at:
            count = sum(feedback_time(row[0]) <= at for row in self.labels.values())
        complete = count == len(self.event.members)
        status = "empty" if not self.event.members else "complete" if complete else "incomplete"
        return {"signal_date": self.event.signal_date, "label_end_at": deadline.isoformat(), "status": status,
                "expected_labels": len(self.event.members), "received_labels": count,
                "daily_mean_residual": self.complete_mean if complete else None}


@dataclass
class CohortState:
    config: CohortFeedbackConfig
    calendar: FrozenCohortCalendar
    created_at: str
    cohorts: dict[str, CohortTrack] = field(default_factory=dict)
    predictions: dict[str, FrozenMember] = field(default_factory=dict)
    events: list[StoredCohortEvent] = field(default_factory=list)

    def digest(self) -> str:
        return feedback_digest({"config": self.config.model_dump(mode="json"), "calendar": self.calendar.calendar_digest,
                                "cohorts": [{"day": day, "identity": track.identity_digest,
                                             "label_count": len(track.labels), "label_history": track.label_history_digest,
                                             "mean": track.complete_mean} for day, track in sorted(self.cohorts.items())]})

    def update(self, at: datetime) -> dict[str, object]:
        days, immature = [], []
        for day in planned_dates(self.config, self.calendar):
            deadline = cohort_endpoint(self.config, self.calendar, day)
            if deadline > at:
                immature.append(day)
                continue
            track = self.cohorts.get(day)
            days.append(track.evidence(at, deadline) if track is not None else {
                "signal_date": day, "label_end_at": deadline.isoformat(), "status": "undeclared",
                "expected_labels": None, "received_labels": 0, "daily_mean_residual": None})
        return update_from_days(days, immature, self.config)


def update_from_days(days: list[dict[str, object]], immature: list[str], config: CohortFeedbackConfig) -> dict[str, object]:
    active = days[-config.window_dates:]
    gaps = [row["signal_date"] for row in days if row["status"] in {"undeclared", "incomplete"}]
    means = [float(value) for row in active if isinstance(value := row["daily_mean_residual"], float)]
    labels = sum(int(value) for row in active if isinstance(value := row["received_labels"], int))
    ready = not gaps and len(means) >= config.minimum_complete_dates and labels >= config.minimum_labels
    bias = math.fsum(means) / len(means) if ready and config.update_rule == "rolling_signed_bias" else 0.0
    return {"status": "withheld_matured_gaps" if gaps else "ready" if ready else "insufficient_data", "bias": bias,
            "days": active, "all_matured_days": days, "matured_gap_dates": gaps, "not_yet_matured_dates": immature,
            "complete_nonempty_dates": len(means), "received_labels_in_window": labels,
            "window_dates": config.window_dates, "rule": config.update_rule,
            "weighting": "equal_complete_signal_dates;within_date_mean_residual;all_matured_gaps_withhold"}


def apply_cohort_event(state: CohortState, event: PredictionCohort | FeedbackLabel, recorded_at: str) -> None:
    stamp = feedback_time(recorded_at)
    previous = state.events[-1].recorded_at if state.events else state.created_at
    if stamp < feedback_time(previous) or len(state.events) >= MAX_LEDGER_EVENTS:
        raise ValueError("cohort receipt clock rollback or event limit")
    recorded_at = stamp.isoformat()
    before = state.digest()
    derived = freeze_cohort(state, event, stamp) if isinstance(event, PredictionCohort) else consume_cohort_label(state, event, recorded_at)
    data = {"sequence": len(state.events) + 1, "recorded_at": recorded_at, "event": event.model_dump(mode="json"),
            "derived": derived, "state_before_digest": before, "state_after_digest": state.digest(),
            "previous_event_digest": state.events[-1].event_digest if state.events else feedback_digest(state.created_at)}
    state.events.append(StoredCohortEvent.model_validate({**data, "event_digest": feedback_digest(data)}))


def freeze_cohort(state: CohortState, event: PredictionCohort, stamp: datetime) -> dict[str, object]:
    decision = feedback_time(event.decision_at)
    if event.signal_date not in planned_dates(state.config, state.calendar) or event.signal_date in state.cohorts:
        raise ValueError("cohort date is undeclared or already frozen")
    if any(track.event.cohort_id == event.cohort_id for track in state.cohorts.values()):
        raise ValueError("cohort identity is already frozen")
    deadline = cohort_endpoint(state.config, state.calendar, event.signal_date)
    if not feedback_time(state.created_at) <= decision <= stamp < deadline or feedback_market_date(stamp.isoformat()).isoformat() != event.signal_date:
        raise ValueError("cohort must freeze prospectively on its signal date")
    update = state.update(decision)
    bias = update["bias"]
    if not isinstance(bias, float):
        raise ValueError("invalid derived cohort bias")
    predictions = freeze_members(state, event, bias)
    derived: dict[str, object] = {"label_end_at": deadline.isoformat(), "update": update, "predictions": predictions}
    identity = feedback_digest({"event": event.model_dump(mode="json"), "derived": derived})
    state.cohorts[event.signal_date] = CohortTrack(event, stamp.isoformat(), identity)
    return derived


def freeze_members(state: CohortState, event: PredictionCohort, bias: float) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for member in event.members:
        if member.prediction_id in state.predictions:
            raise ValueError("prediction ID is already frozen")
        baseline = min(1.0, max(0.0, member.raw_probability + state.config.baseline_bias))
        applied = min(1.0, max(0.0, baseline + bias))
        state.predictions[member.prediction_id] = FrozenMember(member, event.signal_date, baseline, applied)
        result.append({"prediction_id": member.prediction_id, "baseline_probability": baseline, "applied_probability": applied})
    return result


def consume_cohort_label(state: CohortState, event: FeedbackLabel, recorded_at: str) -> dict[str, object]:
    frozen = state.predictions.get(event.prediction_id)
    if frozen is None:
        raise ValueError("label references unknown cohort member")
    track = state.cohorts[frozen.signal_date]
    if event.prediction_id in track.labels:
        raise ValueError("cohort label already consumed; revisions are forbidden")
    deadline = cohort_endpoint(state.config, state.calendar, frozen.signal_date)
    if feedback_time(event.label_end_at) != deadline or not deadline <= feedback_time(event.available_at) <= feedback_time(recorded_at):
        raise ValueError("label target or availability differs from frozen cohort")
    relative = event.realized_return - event.benchmark_return
    if not math.isfinite(relative):
        raise ValueError("relative return must be finite")
    definition = state.config.event_definition
    outcome = int(relative > definition.threshold if definition.operator == "gt" else relative >= definition.threshold)
    track.labels[event.prediction_id] = recorded_at, outcome, outcome - frozen.baseline_probability
    track.last_received_at = recorded_at
    track.label_history_digest = feedback_digest([track.label_history_digest, event.model_dump(mode="json"), recorded_at])
    if len(track.labels) == len(track.event.members):
        track.complete_mean = math.fsum(track.labels[key][2] for key in sorted(track.labels)) / len(track.labels)
    return {"prediction_id": event.prediction_id, "outcome": outcome,
            "baseline_probability": frozen.baseline_probability, "applied_probability": frozen.applied_probability}
