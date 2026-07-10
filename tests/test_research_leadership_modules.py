from __future__ import annotations

from datetime import datetime
import math

from app.models.schemas import StockConceptItem
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_features import build_feature_snapshot, build_leadership_report
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_plate_item, make_quote


def test_leadership_evidence_keeps_hot_concepts_sorted_and_limited() -> None:
    analysis, insights, feature = _leadership_inputs(
        industry_context=make_plate_item(change_pct=2.4).model_copy(update={"name": "白酒"})
    )

    report = build_leadership_report(
        analysis,
        insights,
        feature,
        [_concept("异常题材", math.inf), _concept("低位题材", -3.0), _concept("机器人", 4.2), _concept("AI应用", 1.1)],
    )

    joined_evidence = "；".join(report.evidence)
    assert "行业 白酒 涨跌幅 2.40%" in joined_evidence
    assert "概念背景：机器人4.20%、AI应用1.10%。" in joined_evidence
    assert "异常题材" not in joined_evidence
    assert "行业强度排名" not in report.missing_data
    assert "概念归属" not in report.missing_data


def test_feature_snapshot_sanitizes_leader_score_inputs_like_display_fields() -> None:
    dirty_quote = make_quote(change_pct=math.inf, turnover_rate=math.inf).model_copy(
        update={"amount": math.inf}
    )
    dirty_industry = make_plate_item(change_pct=math.inf)
    analysis, _insights, feature = _leadership_inputs(quote=dirty_quote, industry_context=dirty_industry)

    assert feature.change_pct == 0
    assert feature.amount is None
    assert feature.turnover_rate is None
    assert feature.industry_change_pct is None
    assert "情绪强" not in feature.tags
    assert "换手活跃" not in feature.tags


def test_feature_snapshot_clamps_dirty_scores_and_metrics_before_output() -> None:
    analysis, insights, _feature = _leadership_inputs()
    analysis = analysis.model_copy(
        update={
            "trend_score": math.inf,
            "trend_label": "强势",
            "support": math.inf,
            "resistance": math.nan,
            "ma5": -math.inf,
            "ma10": math.inf,
            "ma20": math.nan,
            "signal_snapshot": analysis.signal_snapshot.model_copy(update={"confidence": math.inf}),
            "data_quality": analysis.data_quality.model_copy(update={"score": math.nan, "level": "优秀"}),
        }
    )
    insights = insights.model_copy(
        update={
            "valuation": insights.valuation.model_copy(update={"score": math.inf}),
            "financial_health": insights.financial_health.model_copy(update={"score": -20}),
            "fund_flow": insights.fund_flow.model_copy(update={"overall_score": math.nan}),
            "order_pressure": insights.order_pressure.model_copy(update={"pressure_level": ""}),
        }
    )

    feature = build_feature_snapshot(analysis, insights)

    assert feature.trend_score == 0
    assert feature.trend_label == "数据不足"
    assert feature.signal_confidence == 0
    assert feature.data_quality_score == 0
    assert feature.data_quality_level == "待确认"
    assert feature.support == 0
    assert feature.resistance == 0
    assert feature.ma5 == 0
    assert feature.ma10 == 0
    assert feature.ma20 == 0
    assert feature.valuation_score == 0
    assert feature.financial_score == 0
    assert feature.fund_flow_score == 0
    assert feature.order_pressure == "--"
    assert "趋势强" not in feature.tags
    assert "资金配合" not in feature.tags
    assert "数据降权" in feature.tags
    numeric_fields = (
        feature.leader_score,
        feature.volume_ratio,
        feature.atr14,
        feature.atr_pct,
        feature.volatility_pct,
    )
    assert all(math.isfinite(value) for value in numeric_fields)


def test_leadership_missing_data_tracks_unavailable_inputs() -> None:
    quote = make_quote().model_copy(update={"amount": 0})
    analysis, insights, feature = _leadership_inputs(quote=quote)

    report = build_leadership_report(analysis, insights, feature)

    assert report.missing_data == ["龙虎榜席位", "逐笔大单资金流", "公司画像", "行业强度排名", "概念归属"]
    assert any("量能比" in item for item in report.evidence)


def test_leadership_missing_data_preserves_concept_source_error() -> None:
    quote = make_quote().model_copy(update={"amount": 0})
    analysis, insights, feature = _leadership_inputs(quote=quote)

    report = build_leadership_report(
        analysis,
        insights,
        feature,
        concept_error="概念归属不可用：600706.SH；akshare: concept down",
    )

    assert "概念归属：概念归属不可用：600706.SH；akshare: concept down" in report.missing_data
    assert "概念归属" not in report.missing_data


def test_leadership_report_sanitizes_dirty_feature_snapshot_before_text() -> None:
    analysis, insights, feature = _leadership_inputs()
    dirty_feature = feature.model_copy(
        update={
            "leader_score": math.inf,
            "leader_level": "强",
            "data_quality_score": math.nan,
            "data_quality_level": "优秀",
            "trend_score": math.inf,
            "trend_label": "强势",
            "change_pct": math.nan,
            "volume_ratio": math.inf,
            "amount": math.inf,
            "fund_flow_score": math.nan,
            "order_pressure": "",
            "industry_change_pct": math.inf,
            "tags": ["", "有效标签"],
        }
    )

    report = build_leadership_report(
        analysis,
        insights,
        dirty_feature,
        [_concept("异常题材", math.inf), _concept("平稳题材", 0), _concept("热题材", 3.1)],
    )

    joined_evidence = "；".join(report.evidence)
    assert report.score == 0
    assert report.level == "弱"
    assert report.summary == "数据质量待确认，暂不具备龙头特征需要降权。"
    assert report.tags == ["有效标签"]
    assert "inf" not in joined_evidence
    assert "nan" not in joined_evidence
    assert "盘口状态：--" in joined_evidence
    assert "概念背景：热题材3.10%、平稳题材0.00%。" in joined_evidence
    assert "异常题材" not in joined_evidence


def test_leadership_summary_thresholds_are_stable() -> None:
    analysis, insights, feature = _leadership_inputs()
    feature = feature.model_copy(update={"data_quality_score": 90, "data_quality_level": "优秀"})

    leader = build_leadership_report(analysis, insights, feature.model_copy(update={"leader_score": 70}))
    strong_watch = build_leadership_report(analysis, insights, feature.model_copy(update={"leader_score": 55}))
    weak = build_leadership_report(analysis, insights, feature.model_copy(update={"leader_score": 54}))

    assert leader.summary == "具备龙头候选特征"
    assert strong_watch.summary == "属于强势观察个股"
    assert weak.summary == "暂不具备龙头特征"


def test_leadership_summary_downgrades_low_quality_data_and_limits_tags() -> None:
    analysis, insights, feature = _leadership_inputs()
    feature = feature.model_copy(
        update={
            "data_quality_score": 65,
            "data_quality_level": "可用",
            "leader_score": 70,
            "tags": [f"标签{index}" for index in range(10)],
        }
    )

    report = build_leadership_report(analysis, insights, feature)

    assert report.summary == "数据质量可用，具备龙头候选特征需要降权。"
    assert report.tags == [f"标签{index}" for index in range(8)]


def _leadership_inputs(*, quote=None, industry_context=None):
    quote = quote or make_quote(change_pct=2.5, turnover_rate=4.2)
    klines = [
        make_kline(
            date=f"2026-05-{index + 1:02d}",
            close=100 + index * 0.7,
            high=101 + index * 0.7,
            low=99 + index * 0.7,
            volume=1600 + index * 20,
        )
        for index in range(40)
    ]
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
    analysis = build_analysis(quote, klines, industry_context=industry_context, data_quality=quality)
    insights = build_stock_insight_bundle(analysis)
    feature = build_feature_snapshot(analysis, insights)
    return analysis, insights, feature


def _concept(name: str, change_pct: float) -> StockConceptItem:
    return StockConceptItem(
        symbol="600519.SH",
        rank=1,
        name=name,
        change_pct=change_pct,
        leading_stock="测试龙头",
        source="测试概念源",
        updated_at="2026-05-13 16:00:00",
    )
