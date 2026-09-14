from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.repositories.fuyao_research import FuyaoResearchRepository
from app.services.fuyao_scoring import build_fuyao_valuation_score
from app.services.fuyao_valuation_adapter import align_fuyao_valuation, apply_fuyao_valuation, fuyao_fundamental_factor
from app.services.research_factor_current import valuation_anchor_factor
from app.services.research_features import build_feature_snapshot
from app.services.research_risk import RiskRadarContext, _valuation_risk
from app.services.stock_insights import build_stock_insight_bundle
from app.services.stock_overview import _fundamental_factor
from app.services.workbench_context import WorkbenchContextCache
from app.workflows import workbench_pipeline as pipeline
from tests.test_stock_overview_modules import _analysis
from tests.test_workbench_context_cache_modules import _bound_context


SYMBOL = "600519.SH"
CUTOFF = "2026-08-13T06:00:00+08:00"
OBSERVED = "2026-08-12T15:00:00+08:00"


def _observation(tmp_path, pe=75, pb=9, fetched_at=OBSERVED):
    repository = FuyaoResearchRepository(tmp_path / "research.sqlite3")
    return repository.save_observation("valuations", SYMBOL, fetched_at, {
        "symbol": SYMBOL, "values": {"pe_ttm": pe, "pb_mrq": pb},
        "batch_timestamp": None, "individual_timestamp": None,
    })


def _research_analysis():
    analysis = _analysis(pe=12, pb=1, market_cap=80_000_000_000)
    return analysis.model_copy(update={
        "quote": analysis.quote.model_copy(update={"timestamp": "2026-08-13 05:59:00"}),
        "trend_score": 90,
    })


def test_same_observation_replaces_only_existing_factor_and_reaches_research_risk(tmp_path):
    analysis = _research_analysis()
    original_quote = analysis.quote.model_dump()
    score = build_fuyao_valuation_score(SYMBOL, _observation(tmp_path), CUTOFF)
    baseline = build_stock_insight_bundle(analysis)
    bundle = build_stock_insight_bundle(analysis, fuyao_valuation=score)
    factor = next(item for item in bundle.overview.factors if item.name == "基本面")
    assert score.score == factor.score == bundle.valuation.score == 41
    assert len(bundle.overview.factors) == len(baseline.overview.factors) == 5
    assert [item for item in bundle.overview.factors if item.name != "基本面"] == [
        item for item in baseline.overview.factors if item.name != "基本面"
    ]
    assert bundle.overview.total_score < baseline.overview.total_score
    assert analysis.quote.model_dump() == original_quote
    assert bundle.valuation.pe == 75 and bundle.valuation.pb == 9
    assert bundle.valuation.input_basis == "fuyao_ttm_mrq"
    assert bundle.valuation.observation_digest == score.observation_digest
    assert bundle.valuation.observation_id == score.observation_id
    assert bundle.valuation.observation_fetched_at == OBSERVED
    assert bundle.valuation.updated_at == analysis.quote.timestamp
    assert OBSERVED in " ".join(factor.evidence + bundle.valuation.evidence)
    assert bundle.valuation.pe_percentile is None and bundle.valuation.peer_pb_percentile is None
    feature = build_feature_snapshot(analysis, bundle)
    assert feature.valuation_score == score.score and feature.valuation_score_available
    assert feature.financial_score is None and not bundle.financial_health.score_available
    current_factor = valuation_anchor_factor(feature, bundle)
    assert current_factor.score == score.score
    assert not current_factor.calibration.participates_in_historical_aggregate
    risk = _valuation_risk(RiskRadarContext(analysis, bundle, feature, None, None, None))
    assert risk.score == 59 and risk.score_available
    rule = next(item for item in bundle.rule_matches.matches if item.rule_id == "high_valuation_chase_risk")
    assert "41" in " ".join(rule.evidence)


@pytest.mark.parametrize("reason", ["missing", "stale", "future", "zero"])
def test_unadmitted_observation_preserves_quote_path_and_explains_fallback(tmp_path, reason):
    analysis = _research_analysis()
    observation = None if reason == "missing" else _observation(
        tmp_path, pe=0 if reason == "zero" else 75, pb=None if reason == "zero" else 9,
        fetched_at={"stale": "2026-08-01T00:00:00+08:00", "future": "2026-08-14T00:00:00+08:00"}.get(reason, OBSERVED),
    )
    score = build_fuyao_valuation_score(SYMBOL, observation, CUTOFF)
    assert not score.score_available
    baseline = build_stock_insight_bundle(analysis)
    bundle = build_stock_insight_bundle(analysis, fuyao_valuation=score)
    assert bundle.overview.total_score == baseline.overview.total_score
    assert bundle.valuation.score == baseline.valuation.score
    assert bundle.valuation.input_basis == "quote_fields"
    assert bundle.valuation.pe == analysis.quote.pe and bundle.valuation.pb == analysis.quote.pb
    assert bundle.valuation.score_unavailable_reason == score.unavailable_reason
    assert "未采用" in " ".join(bundle.valuation.evidence)
    assert "未采用" in " ".join(bundle.overview.factors[2].evidence)


def test_unavailable_observation_cannot_make_missing_quote_fields_available():
    analysis = _analysis(pe=None, pb=None, market_cap=None, industry=None, stock_profile=None)
    score = build_fuyao_valuation_score(SYMBOL, None, CUTOFF)
    bundle = build_stock_insight_bundle(analysis, fuyao_valuation=score)
    factor = bundle.overview.factors[2]
    assert not factor.score_available and not factor.participates_in_total_score
    assert factor.summary == "基础财务数据待接入"
    assert not bundle.valuation.score_available


@pytest.mark.parametrize("pe,pb", [(None, None), (0, None), (None, 0), (0, 0)])
def test_market_cap_only_quote_fallback_stays_out_of_research_scores_and_rules(pe, pb):
    analysis = _research_analysis()
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"pe": pe, "pb": pb})})
    bundle = build_stock_insight_bundle(analysis, fuyao_valuation=build_fuyao_valuation_score(SYMBOL, None, CUTOFF))
    assert not bundle.valuation.score_available
    assert bundle.valuation.input_basis == "quote_fields"
    assert "未采用" in " ".join(bundle.valuation.evidence)
    feature = build_feature_snapshot(analysis, bundle)
    assert not feature.valuation_score_available
    current_factor = valuation_anchor_factor(feature, bundle)
    assert not current_factor.participates_in_current_score
    assert not current_factor.calibration.participates_in_historical_aggregate
    assert current_factor.data_nature == "unavailable"
    risk = _valuation_risk(RiskRadarContext(analysis, bundle, feature, None, None, None))
    assert not risk.score_available
    rule = next(item for item in bundle.rule_matches.matches if item.rule_id == "high_valuation_chase_risk")
    assert rule.status == "未触发"
    assert "估值证据不可用" in " ".join(rule.evidence)
    assert "估值评分" in rule.missing_data


def test_adapters_reject_wrong_stock_and_unavailable_direct_factor(tmp_path):
    score = build_fuyao_valuation_score(SYMBOL, _observation(tmp_path), CUTOFF)
    analysis = _research_analysis()
    wrong = score.model_copy(update={"symbol": "000001.SZ"})
    with pytest.raises(ValueError, match="当前股票"):
        _fundamental_factor(analysis, wrong)
    with pytest.raises(ValueError, match="当前股票"):
        apply_fuyao_valuation(build_stock_insight_bundle(analysis).valuation, wrong)
    with pytest.raises(ValueError, match="不可用"):
        fuyao_fundamental_factor(build_fuyao_valuation_score(SYMBOL, None, CUTOFF))


def _patch_input_reads(monkeypatch, analysis):
    monkeypatch.setattr(pipeline, "analyze_individual_stock", AsyncMock(return_value=analysis))
    monkeypatch.setattr(pipeline, "_market_breadth_sample_or_empty", AsyncMock(return_value=SimpleNamespace(quotes=[], warnings=())))
    monkeypatch.setattr(pipeline, "_order_book_or_error", AsyncMock(return_value=(None, "未配置盘口")))
    monkeypatch.setattr(pipeline, "_stock_concepts_or_error", AsyncMock(return_value=([], None)))
    clock = []
    def now():
        clock.append(CUTOFF)
        return CUTOFF
    monkeypatch.setattr(pipeline, "audit_now_text", now)
    return clock


def test_pipeline_reads_local_score_once_at_fixed_cutoff_and_cache_keeps_identity(tmp_path, monkeypatch):
    import app.services.workbench_context as cache_module
    analysis = _research_analysis()
    clock = _patch_input_reads(monkeypatch, analysis)
    observations = [_observation(tmp_path)]
    async def load(symbol, evaluated_at):
        assert len(clock) == 1
        return build_fuyao_valuation_score(symbol, observations[-1], evaluated_at)
    source = SimpleNamespace(valuation_score=AsyncMock(side_effect=load))
    hub = SimpleNamespace(fuyao=source)
    monkeypatch.setattr(cache_module, "_context_cache_cohort_is_current", lambda _context: True)
    async def run():
        cache = WorkbenchContextCache()
        async def build(symbol):
            inputs = await pipeline._collect_workbench_inputs(hub, symbol)
            context = _bound_context(symbol)
            context.insights = pipeline._build_research_core(inputs).insights
            return context
        first = await cache.get(SYMBOL, build)
        observations.append(_observation(tmp_path, pe=12, pb=1))
        second = await cache.get(SYMBOL, build)
        assert first is second
        assert first.insights.valuation.score == first.insights.overview.factors[2].score == 41
        assert first.insights.valuation.observation_digest == observations[0].digest
        assert source.valuation_score.await_count == 1
        assert source.valuation_score.await_args.args == (SYMBOL, CUTOFF)
        assert len(clock) == 1
        await cache.aclose()
    asyncio.run(run())


def test_pipeline_missing_dummy_source_and_storage_failure_remain_local(monkeypatch):
    _patch_input_reads(monkeypatch, _research_analysis())
    async def run():
        dummy = await pipeline._collect_workbench_inputs(SimpleNamespace(), SYMBOL)
        assert dummy.fuyao_valuation is None
        source = SimpleNamespace(valuation_score=AsyncMock(side_effect=OSError("private-storage-details")))
        inputs = await pipeline._collect_workbench_inputs(SimpleNamespace(fuyao=source), SYMBOL)
        bundle = build_stock_insight_bundle(inputs.analysis, fuyao_valuation=inputs.fuyao_valuation)
        assert "本地估值读取失败" in bundle.valuation.score_unavailable_reason
        assert "private-storage-details" not in bundle.model_dump_json()
        assert source.valuation_score.await_count == 1
    asyncio.run(run())


def test_local_score_read_cancellation_propagates():
    source = SimpleNamespace(valuation_score=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pipeline._fuyao_valuation_or_unavailable(SimpleNamespace(fuyao=source), SYMBOL, CUTOFF))


@pytest.mark.parametrize("quote_time,fetched_at,evaluated_at,adopted", [
    ("2026-09-07 15:00:00", "2026-09-10T15:00:00+08:00", "2026-09-12T10:00:00+08:00", False),
    ("2026-09-11 15:00:00", "2026-09-10T15:00:00+08:00", "2026-09-12T10:00:00+08:00", True),
    ("2026-09-07 15:00:00", "2026-09-01T15:00:00+08:00", "2026-09-12T10:00:00+08:00", False),
    ("2026-09-11 10:00:00", "2026-09-11T10:01:00+08:00", "2026-09-11T10:05:00+08:00", False),
    ("2026-09-11 10:00:00", "2026-09-11T10:00:00+08:00", "2026-09-11T10:05:00+08:00", True),
    ("2026-09-11 10:00:00", "2026-09-11T02:00:00Z", "2026-09-11T10:05:00+08:00", True),
])
def test_quote_time_alignment_preserves_real_freshness_window_across_all_entries(
    tmp_path, quote_time, fetched_at, evaluated_at, adopted,
):
    analysis = _research_analysis()
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"timestamp": quote_time})})
    score = build_fuyao_valuation_score(SYMBOL, _observation(tmp_path, fetched_at=fetched_at), evaluated_at)
    original = score.model_dump()
    aligned = align_fuyao_valuation(score, quote_time)
    assert aligned.score_available is adopted
    assert aligned.evaluated_at == score.evaluated_at and aligned.fetched_at == fetched_at
    baseline = build_stock_insight_bundle(analysis)
    bundle = build_stock_insight_bundle(analysis, fuyao_valuation=score)
    direct_valuation = apply_fuyao_valuation(baseline.valuation, score)
    direct_factor = _fundamental_factor(analysis, score)
    assert bundle.valuation == direct_valuation
    assert bundle.overview.factors[2] == direct_factor
    assert bundle.valuation.input_basis == ("fuyao_ttm_mrq" if adopted else "quote_fields")
    expected_score = 41 if adopted else baseline.valuation.score
    assert build_feature_snapshot(analysis, bundle).valuation_score == expected_score
    assert bundle.valuation.updated_at == quote_time
    if not adopted:
        assert bundle.overview.total_score == baseline.overview.total_score
        assert "未采用" in " ".join(bundle.valuation.evidence)
        if score.score_available:
            assert "晚于本次行情时点" in aligned.unavailable_reason
    assert score.model_dump() == original


@pytest.mark.parametrize("quote_time", ["not-a-time", ""])
def test_unclear_quote_time_cannot_admit_an_observation(tmp_path, quote_time):
    score = build_fuyao_valuation_score(SYMBOL, _observation(tmp_path), CUTOFF)
    aligned = align_fuyao_valuation(score, quote_time)
    assert not aligned.score_available and aligned.score is None
    assert "时点无法核验" in aligned.unavailable_reason
