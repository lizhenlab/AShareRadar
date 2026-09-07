"""Causal state transitions over original prediction and label events."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time
import math

from app.services.market_scan_delayed_feedback_contracts import (
    MAX_LEDGER_EVENTS, SHANGHAI, FeedbackConfig, FeedbackLabel, FeedbackPrediction,
    StoredFeedbackEvent, feedback_digest, feedback_time,
)
from app.services.trading_calendar import is_trading_day, next_trade_dates


@dataclass
class FeedbackState:
    config: FeedbackConfig
    created_at: str
    records: list[StoredFeedbackEvent] = field(default_factory=list)
    predictions: dict[str, StoredFeedbackEvent] = field(default_factory=dict)
    labelled: dict[str, StoredFeedbackEvent] = field(default_factory=dict)
    identities: set[tuple[str, datetime]] = field(default_factory=set)
    residuals: deque[tuple[str, str, float]] = field(default_factory=deque)
    last_update_at: str | None = None
    consumed_count: int = 0

    def update_summary(self) -> dict[str, object]:
        count, dates = len(self.residuals), len({row[1] for row in self.residuals})
        ready = count >= self.config.minimum_labels and dates >= self.config.minimum_dates
        bias = math.fsum(row[2] for row in self.residuals) / count if ready and self.config.update_rule == "rolling_signed_bias" else 0.0
        return {"status": "ready" if ready else "insufficient_data", "bias": bias,
                "window_label_count": count, "window_date_count": dates, "rule": self.config.update_rule}

    def state_digest(self) -> str:
        return feedback_digest({"config_digest": feedback_digest(self.config.model_dump(mode="json")),
                                "consumed_labels": self.consumed_count, "last_update_at": self.last_update_at,
                                "window": [list(row) for row in self.residuals], "update": self.update_summary()})

    def genesis_digest(self) -> str:
        return feedback_digest({"created_at": self.created_at, "config": self.config.model_dump(mode="json")})


def apply_feedback_event(state: FeedbackState, event: FeedbackPrediction | FeedbackLabel, recorded_at: str) -> None:
    stamp = feedback_time(recorded_at)
    previous_time = state.records[-1].recorded_at if state.records else state.created_at
    if stamp < feedback_time(previous_time) or len(state.records) >= MAX_LEDGER_EVENTS:
        raise ValueError("recorded clock rollback or event limit exceeded")
    recorded_at = stamp.isoformat()
    before = state.state_digest()
    if isinstance(event, FeedbackPrediction):
        baseline, probability, outcome = _prediction(state, event, stamp)
    else:
        baseline, probability, outcome = _label(state, event, stamp, recorded_at)
    data = {"sequence": len(state.records) + 1, "event": event.model_dump(mode="json"),
            "recorded_at": recorded_at, "baseline_probability": baseline, "applied_probability": probability,
            "outcome": outcome, "state_before_digest": before, "state_after_digest": state.state_digest(),
            "previous_event_digest": state.records[-1].event_digest if state.records else state.genesis_digest()}
    record = StoredFeedbackEvent.model_validate({**data, "event_digest": feedback_digest(data)})
    state.records.append(record)
    if isinstance(event, FeedbackPrediction):
        state.predictions[event.prediction_id] = record
    else:
        state.labelled[event.prediction_id] = record


def _prediction(state: FeedbackState, event: FeedbackPrediction, stamp: datetime) -> tuple[float, float, None]:
    decision, end = feedback_time(event.decision_at), feedback_time(event.label_end_at)
    if event.prediction_id in state.predictions or (event.symbol, decision) in state.identities:
        raise ValueError("duplicate prediction or forecast identity")
    if not feedback_time(state.created_at) <= decision <= stamp < end:
        raise ValueError("prediction must be recorded prospectively after configuration freeze")
    if state.last_update_at is not None and decision < feedback_time(state.last_update_at):
        raise ValueError("prediction decision predates the available model state")
    _validate_endpoint(state.config, event)
    baseline = min(1.0, max(0.0, event.raw_probability + state.config.baseline_bias))
    update = state.update_summary()["bias"]
    if not isinstance(update, float):
        raise ValueError("invalid feedback bias")
    probability = min(1.0, max(0.0, baseline + update))
    state.identities.add((event.symbol, decision))
    return baseline, probability, None


def _validate_endpoint(config: FeedbackConfig, event: FeedbackPrediction) -> None:
    signal = date.fromisoformat(event.signal_date)
    if not is_trading_day(signal):
        raise ValueError("signal date must be a covered trading session")
    offset = config.event_definition.target_offset_sessions
    target = next_trade_dates(signal, offset)[-1] if offset else signal
    expected = datetime.combine(target, time(15), SHANGHAI)
    if feedback_time(event.label_end_at) != expected:
        raise ValueError("label endpoint does not match frozen target offset and Shanghai close")


def _label(state: FeedbackState, event: FeedbackLabel, stamp: datetime, recorded_at: str) -> tuple[float, float, int]:
    if event.prediction_id in state.labelled:
        raise ValueError("duplicate label; prediction already consumed")
    original = state.predictions.get(event.prediction_id)
    if original is None or not isinstance(original.event, FeedbackPrediction):
        raise ValueError("label references unknown prediction")
    prediction = original.event
    if feedback_time(event.label_end_at) != feedback_time(prediction.label_end_at):
        raise ValueError("label endpoint differs from frozen prediction")
    if not feedback_time(event.label_end_at) <= feedback_time(event.available_at) <= stamp:
        raise ValueError("label is not yet available")
    relative = event.realized_return - event.benchmark_return
    if not math.isfinite(relative):
        raise ValueError("benchmark-relative return must be finite")
    definition = state.config.event_definition
    outcome = int(relative > definition.threshold if definition.operator == "gt" else relative >= definition.threshold)
    state.residuals.append((event.prediction_id, prediction.signal_date, outcome - original.baseline_probability))
    while len(state.residuals) > state.config.window_labels:
        state.residuals.popleft()
    state.last_update_at = recorded_at
    state.consumed_count += 1
    return original.baseline_probability, original.applied_probability, outcome
