from __future__ import annotations

import math
from datetime import datetime

from app.models.schemas import StockConceptItem
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_features import build_feature_snapshot
from app.services.research_theme import build_theme_context_report
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_plate_item, make_quote, make_stock_info


_DEFAULT_PROFILE = object()


def test_theme_context_uses_industry_context_when_profile_missing() -> None:
    quote = make_quote(change_pct=2.2)
    analysis, feature = _theme_inputs(
        quote=quote,
        stock_profile=None,
        industry_context=make_plate_item(change_pct=1.3).model_copy(update={"name": "白酒"}),
    )

    report = build_theme_context_report(analysis, feature, [_concept("白酒概念", 1.1)])

    assert report.industry == "白酒"
    assert "行业归属或行业涨跌强度" not in report.missing_data
    assert "行业「白酒」涨跌幅 1.30%" in report.summary


def test_theme_context_deduplicates_concepts_before_scoring() -> None:
    analysis, feature = _theme_inputs(industry_context=make_plate_item(change_pct=0.5))
    report = build_theme_context_report(
        analysis,
        feature,
        [
            _concept("AI应用", 5.0, rank=1),
            _concept("AI应用", -4.0, rank=2),
            _concept("机器人", -1.0, rank=3),
        ],
    )

    assert [item.name for item in report.concepts] == ["AI应用", "机器人"]
    assert "概念平均涨跌幅 2.00%" in report.summary
    assert "AI应用5.00%" in "；".join(report.evidence)


def test_theme_context_caps_returned_concepts_after_scoring() -> None:
    analysis, feature = _theme_inputs(industry_context=make_plate_item(change_pct=0.2))
    concepts = [_concept(f"概念{index}", float(index), rank=index) for index in range(1, 11)]

    report = build_theme_context_report(analysis, feature, concepts)

    assert [item.name for item in report.concepts] == [f"概念{index}" for index in range(1, 9)]
    assert "概念平均涨跌幅 5.50%" in report.summary
    assert "最强概念为「概念10」10.00%" in report.summary


def test_theme_context_evidence_keeps_stable_source_order() -> None:
    analysis, feature = _theme_inputs(industry_context=make_plate_item(change_pct=1.2).model_copy(update={"name": "白酒"}))
    report = build_theme_context_report(
        analysis,
        feature,
        [
            _concept("低位题材", -1.0, rank=2),
            _concept("机器人", 2.0, rank=3),
            _concept("AI应用", 4.0, rank=1),
        ],
    )

    assert len(report.evidence) == 6
    assert report.evidence[0].startswith("个股涨跌幅")
    assert report.evidence[1].startswith("行业「白酒」")
    assert report.evidence[2] == "相关概念：AI应用4.00%、机器人2.00%、低位题材-1.00%。"
    assert report.evidence[3].startswith("概念平均涨跌幅")
    assert report.evidence[4].startswith("个股相对行业强弱差")
    assert report.evidence[5].startswith("个股相对概念均值强弱差")


def test_hot_concept_but_weak_stock_is_flagged_as_risk() -> None:
    quote = make_quote(price=98, prev_close=100, high=101, low=97.5, change_pct=-2.0)
    analysis, feature = _theme_inputs(quote=quote, industry_context=make_plate_item(change_pct=1.4))
    weak_feature = feature.model_copy(update={"trend_score": 42, "leader_score": 38})

    report = build_theme_context_report(analysis, weak_feature, [_concept("热门题材", 3.0)])

    assert report.style == "主题热个股弱"
    assert any("概念热但个股弱" in item for item in report.risks)
    assert any("龙头强度不足" in item for item in report.risks)


def test_theme_context_filters_blank_and_non_finite_concepts_before_scoring() -> None:
    analysis, feature = _theme_inputs(industry_context=make_plate_item(change_pct=0.5))
    report = build_theme_context_report(
        analysis,
        feature,
        [
            _concept("  AI应用  ", 2.0, rank=2),
            _concept("AI应用", 4.0, rank=3),
            _concept(" ", 9.0, rank=1),
            _concept("机器人", math.inf, rank=1),
            _concept("低位题材", -1.0, rank=4),
        ],
    )
    report_text = " ".join([report.summary, *report.evidence, *report.opportunities, *report.risks])

    assert [item.name for item in report.concepts] == ["AI应用", "低位题材"]
    assert "概念平均涨跌幅 0.50%" in report.summary
    assert "机器人" not in report_text
    assert "inf" not in report_text.lower()
    assert "nan" not in report_text.lower()


def test_theme_context_sanitizes_non_finite_industry_and_stock_change_text() -> None:
    quote = make_quote(change_pct=1.0).model_copy(update={"change_pct": math.nan})
    analysis, feature = _theme_inputs(
        quote=quote,
        stock_profile=make_stock_info().model_copy(update={"industry": "   "}),
        industry_context=make_plate_item(change_pct=1.3).model_copy(update={"name": "  半导体  ", "change_pct": math.inf}),
    )
    noisy_feature = feature.model_copy(
        update={"trend_score": math.inf, "leader_score": math.nan, "data_quality_score": math.nan, "data_quality_level": "   "}
    )

    report = build_theme_context_report(analysis, noisy_feature, [_concept("芯片", 1.0)])
    report_text = " ".join([report.summary, *report.evidence, *report.opportunities, *report.risks, *report.missing_data])

    assert report.industry == "半导体"
    assert report.industry_change_pct is None
    assert "行业「半导体」涨跌幅" not in report_text
    assert "个股涨跌幅 0.00%" in report_text
    assert "趋势评分 0" in report_text
    assert "龙头评分 0" in report_text
    assert "数据质量待确认" in report.missing_data
    assert "inf" not in report_text.lower()
    assert "nan" not in report_text.lower()


def _theme_inputs(
    *,
    quote=None,
    stock_profile=_DEFAULT_PROFILE,
    industry_context=None,
):
    quote = quote or make_quote(change_pct=2.5)
    klines = [
        make_kline(date=f"2026-05-{index + 1:02d}", close=100 + index * 0.5, high=101 + index * 0.5, low=99 + index * 0.5, volume=1600)
        for index in range(40)
    ]
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
    analysis = build_analysis(
        quote,
        klines,
        stock_profile=make_stock_info() if stock_profile is _DEFAULT_PROFILE else stock_profile,
        industry_context=industry_context,
        data_quality=quality,
    )
    feature = build_feature_snapshot(analysis, build_stock_insight_bundle(analysis))
    return analysis, feature


def _concept(name: str, change_pct: float, *, rank: int = 1) -> StockConceptItem:
    return StockConceptItem(
        symbol="600519.SH",
        rank=rank,
        name=name,
        change_pct=change_pct,
        leading_stock="测试龙头",
        source="测试概念源",
        updated_at="2026-05-13 16:00:00",
    )
