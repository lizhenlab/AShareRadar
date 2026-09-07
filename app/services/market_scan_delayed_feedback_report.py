"""Descriptive matched-cohort metrics over labels consumed by the local ledger."""

from __future__ import annotations

from app.services.market_scan_delayed_feedback_contracts import FeedbackConfig, FeedbackPrediction, StoredFeedbackEvent
from app.services.market_scan_delayed_feedback_state import FeedbackState
from app.services.market_scan_probability_metrics import evaluate_probability_predictions


FeedbackObservation = tuple[FeedbackPrediction, StoredFeedbackEvent]


def feedback_report(state: FeedbackState) -> dict[str, object]:
    rows: list[FeedbackObservation] = []
    for identifier, label in state.labelled.items():
        prediction = state.predictions[identifier].event
        if isinstance(prediction, FeedbackPrediction):
            rows.append((prediction, label))
    selected = [row for row in rows if row[0].selected_top100]
    return {
        "prediction_count": len(state.predictions), "matured_label_count": len(rows),
        "pending_label_count": len(state.predictions) - len(rows), "current_update": state.update_summary(),
        "all": _metrics(rows, state.config), "top100": _metrics(selected, state.config),
        "strata": {"all": _strata(rows, state.config), "top100": _strata(selected, state.config)},
        "interpretation": "descriptive consumed labels only; no calibration guarantee or promotion evidence",
        "date_unit": "prediction_signal_session; correlated symbols are not independent dates",
    }


def _metrics(rows: list[FeedbackObservation], config: FeedbackConfig) -> dict[str, object]:
    dates = [prediction.signal_date for prediction, _ in rows]
    ready = len(rows) >= config.minimum_labels and len(set(dates)) >= config.minimum_dates
    labels = [record.outcome for _, record in rows if record.outcome is not None]
    return {
        "status": "ready" if ready else "insufficient_data", "observation_count": len(rows),
        "independent_session_count": len(set(dates)), "minimum_labels": config.minimum_labels,
        "minimum_dates": config.minimum_dates,
        "fixed_baseline": evaluate_probability_predictions(
            [record.baseline_probability for _, record in rows], labels, dates, base_rate=config.reference_base_rate,
        ) if rows else None,
        "recorded_candidate": evaluate_probability_predictions(
            [record.applied_probability for _, record in rows], labels, dates, base_rate=config.reference_base_rate,
        ) if rows else None,
    }


def _strata(rows: list[FeedbackObservation], config: FeedbackConfig) -> dict[str, object]:
    result: dict[str, object] = {}
    for dimension in ("industry", "regime", "industry_and_regime"):
        groups: dict[tuple[str | None, ...], list[FeedbackObservation]] = {}
        for prediction, record in rows:
            key: tuple[str | None, ...] = (prediction.industry,) if dimension == "industry" else (prediction.regime,)
            if dimension == "industry_and_regime":
                key = prediction.industry, prediction.regime
            groups.setdefault(key, []).append((prediction, record))
        result[dimension] = [{"values": list(key), **_metrics(items, config)}
                             for key, items in sorted(groups.items(), key=lambda item: tuple((value is None, value or "") for value in item[0]))]
    return result
