"""Production inputs must be determined by the frozen market observations."""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.models.market_scan import MarketScanMode, MarketScanRun
from app.services import market_scan_scoring as scoring
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence_context
from tests.test_market_scan_scoring import (
    AS_OF,
    DATA_DATE,
    PREOPEN_AS_OF,
    _item,
    _persisted_item,
    _quote,
    _rows,
)


def _snapshot(monkeypatch: pytest.MonkeyPatch, *, mutation: str, mode: MarketScanMode):
    rows = _rows(DATA_DATE, 80)
    quote = _quote()
    as_of = AS_OF if mode == "official" else PREOPEN_AS_OF.replace(hour=10 if mode == "intraday" else 8)
    if mode == "intraday":
        quote = quote.model_copy(update={
            "timestamp": "2026-07-20 10:00:00",
            "prev_close": quote.price,
            "price": quote.price * 1.01,
            "high": quote.price * 1.02,
            "change": quote.price * 0.01,
            "change_pct": 1.0,
        })
    original_trend = scoring.trend_score
    original_refinement = scoring.market_scan_rank_refinement
    original_volume = scoring.recent_volume_ratio
    with monkeypatch.context() as patch:
        if mutation == "trend":
            def altered_trend(quote, rows, *, mode):
                score, label = original_trend(quote, rows, mode=mode)
                return score + 1, label

            patch.setattr(scoring, "trend_score", altered_trend)
        elif mutation == "continuous_trend":
            def altered_refinement(quote, rows, *, mode):
                return original_refinement(quote.model_copy(update={"price": quote.price * 1.01}), rows, mode=mode)

            patch.setattr(scoring, "market_scan_rank_refinement", altered_refinement)
        elif mutation == "volume_ratio":
            patch.setattr(scoring, "recent_volume_ratio", lambda *args, **kwargs: original_volume(*args, **kwargs) + 0.01)
        result = scoring.score_market_scan_item(
            _item(), quote, rows, as_of=as_of, completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE, expected_quote_date=as_of.date() if mode == "intraday" else DATA_DATE,
            min_history_rows=60, min_data_quality_score=50, mode=mode,
            rule_version=f"full-market-scan-v6:{'a' * 64}",
            quote_observed_at=quote.timestamp,
        )
    item = _persisted_item(result).model_copy(update={
        "quote_observed_at": quote.timestamp, "updated_at": as_of.isoformat(),
    })
    run = MarketScanRun(
        id=1, status="running", trigger="manual", mode=mode,
        rule_version=f"full-market-scan-v6:{'a' * 64}", as_of=as_of.isoformat(),
        data_date=DATA_DATE.isoformat(), quote_date=quote.timestamp[:10],
        scope="沪市 + 深市 + 北交所当前上市A股", total_count=1, excluded_count=0,
        processed_count=1, success_count=1, missing_count=0, skipped_count=0,
        retry_count=0, progress_pct=100, coverage_pct=100,
        created_at=as_of.isoformat(), updated_at=as_of.isoformat(),
        quote_capture_started_at=as_of.isoformat(), quote_capture_finished_at=as_of.isoformat(),
        quote_capture_duration_ms=0, quote_capture_count=1,
    )
    return item, run


@pytest.mark.parametrize("mode", ["official", "preopen", "intraday"])
@pytest.mark.parametrize("mutation", ["trend", "continuous_trend"])
def test_self_consistent_scores_cannot_disagree_with_frozen_market_evidence(monkeypatch, mode, mutation):
    baseline, run = _snapshot(monkeypatch, mutation="none", mode=mode)
    corrupted, _ = _snapshot(monkeypatch, mutation=mutation, mode=mode)
    baseline_evidence = baseline.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    corrupted_evidence = corrupted.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    assert baseline_evidence == corrupted_evidence
    assert baseline.score_details["score_spec_hash"] == corrupted.score_details["score_spec_hash"]
    assert baseline.raw_score != corrupted.raw_score
    # All downstream arithmetic agrees, so the raw market replay is essential.
    assert scoring.replay_score_details(corrupted.score_details).raw_score == corrupted.raw_score
    scoring.verify_persisted_market_scan_result(baseline, run)
    with pytest.raises(scoring.MarketScanReplayError, match="逐时点证据"):
        scoring.verify_persisted_market_scan_result(corrupted, run)
    assert not verify_market_scan_point_in_time_evidence_context(
        corrupted_evidence, item=corrupted,
        expected_data_date=run.data_date, expected_quote_date=run.quote_date,
        expected_as_of=run.as_of, expected_mode=run.mode, require_action_eligible=False,
    )


@pytest.mark.parametrize("mode", ["official", "preopen", "intraday"])
def test_raw_market_replay_does_not_mutate_frozen_evidence(monkeypatch, mode):
    item, run = _snapshot(monkeypatch, mutation="none", mode=mode)
    original = deepcopy(item.score_details)
    scoring.verify_persisted_market_scan_result(item, run)
    assert item.score_details == original


@pytest.mark.parametrize("mode", ["official", "preopen", "intraday"])
def test_reported_volume_ratio_must_replay_from_frozen_bar_volumes(monkeypatch, mode):
    baseline, run = _snapshot(monkeypatch, mutation="none", mode=mode)
    corrupted, _ = _snapshot(monkeypatch, mutation="volume_ratio", mode=mode)
    baseline_evidence = baseline.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    corrupted_evidence = corrupted.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    assert baseline_evidence["payload"]["bar_contract_61"] == corrupted_evidence["payload"]["bar_contract_61"]
    assert baseline.volume_ratio != corrupted.volume_ratio
    assert scoring.replay_score_details(corrupted.score_details).raw_score == corrupted.raw_score
    with pytest.raises(scoring.MarketScanReplayError, match="逐时点证据"):
        scoring.verify_persisted_market_scan_result(corrupted, run)


@pytest.mark.parametrize("field", ["trend_score", "volume_ratio", "continuous_trend_return_5d_pct"])
@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), "1.0"])
def test_context_rejects_missing_or_non_numeric_production_inputs(monkeypatch, field, value):
    item, run = _snapshot(monkeypatch, mutation="none", mode="official")
    item.score_details["inputs"][field] = value
    evidence = item.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    assert not verify_market_scan_point_in_time_evidence_context(
        evidence, item=item, expected_data_date=run.data_date,
        expected_quote_date=run.quote_date, expected_as_of=run.as_of,
    )
