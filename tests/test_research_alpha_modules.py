from __future__ import annotations

import math
import unittest
from datetime import datetime
from types import SimpleNamespace

from app.models.schemas import AlphaEvidencePoint, FactorCalibration, FactorLabReport, StandardFactor
from app.services.research_alpha import (
    ALPHA_VERDICT_RULES,
    MAX_MISSING_DATA_ITEMS,
    _alpha_confidence,
    _alpha_confidence_adjustments,
    _alpha_data_quality_notes,
    _alpha_missing_data,
    _alpha_summary,
    _alpha_verdict,
    _top_alpha_points,
)
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_alpha_points import (
    collect_alpha_points,
    event_impact,
    factor_lab_points,
    impact_level,
    risk_reward_impact,
    rule_match_impact,
)
from app.services.research_features import build_feature_snapshot
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline as _kline
from tests.factories import make_quote as _quote


class ResearchAlphaModuleTests(unittest.TestCase):
    def test_collect_alpha_points_includes_trend_overview_and_rules(self) -> None:
        quote = _quote(price=138.0, prev_close=132.0, high=139.0, low=131.0, change_pct=4.55, turnover_rate=5.2, timestamp="2026-05-13 15:00:00")
        klines = [
            _kline(date="2026-05-13", close=100 + index * 1.1, high=101 + index * 1.1, low=99 + index * 1.1, volume=1000 + index * 80)
            for index in range(80)
        ]
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )
        insights = build_stock_insight_bundle(analysis)
        feature = build_feature_snapshot(analysis, insights)

        points = collect_alpha_points(analysis, insights)
        sources = {item.source for item in points}

        self.assertEqual(feature.symbol, "600519.SH")
        self.assertTrue(any(source.startswith("趋势/") for source in sources))
        self.assertIn("五维诊断", sources)
        self.assertIn("规则引擎", sources)
        self.assertTrue(any(item.title for item in points))

    def test_alpha_point_impact_helpers_keep_direction(self) -> None:
        self.assertEqual(rule_match_impact("命中", "积极"), 16)
        self.assertEqual(rule_match_impact("命中", "风险"), -18)
        self.assertEqual(rule_match_impact("接近", "观察"), 6)
        self.assertEqual(event_impact("风险"), -14)
        self.assertEqual(event_impact("积极"), 10)
        self.assertEqual(risk_reward_impact("性价比较好"), 10)
        self.assertEqual(risk_reward_impact("风险优先"), -12)
        self.assertEqual(impact_level(0), "观察")
        self.assertEqual(impact_level(4, positive_threshold=3), "积极")

    def test_factor_lab_points_filter_neutral_scores_and_apply_eligible_calibration(self) -> None:
        report = FactorLabReport(
            symbol="600519.SH",
            updated_at="2026-05-13 10:00:00",
            total_score=60,
            calibrated_confidence=70,
            factors=[
                _factor("中性因子", score=60),
                _factor("积极因子", score=80, calibration_sample_count=5, expected_level="积极"),
                _factor("风险因子", score=45, calibration_sample_count=4, expected_level="风险"),
            ],
            summary="测试",
        )

        points = factor_lab_points(report)

        self.assertEqual([item.title for item in points], ["积极因子", "风险因子"])
        self.assertGreater(points[0].impact, 0)
        self.assertLess(points[1].impact, 0)

    def test_alpha_confidence_adjustments_are_explicit_and_clamped(self) -> None:
        analysis = _analysis_stub(signal_confidence=80, data_quality_score=70)
        insights = _insights_stub(overview_score=60)
        feature = _feature_stub(data_quality_score=80, leader_score=50)
        factor_lab = SimpleNamespace(calibrated_confidence=90)
        regime = SimpleNamespace(confidence_adjustment=3)
        timeframe = SimpleNamespace(conflict_level="中冲突")
        risk_reward = SimpleNamespace(rating="性价比不足")

        adjustments = _alpha_confidence_adjustments(
            analysis,
            insights,
            feature,
            ["估值", "财务", "资金"],
            factor_lab,
            regime,
            timeframe,
            risk_reward,
        )

        self.assertEqual(
            [(item.name, item.value) for item in adjustments],
            [
                ("base_with_factor_lab", 75),
                ("market_regime", 3),
                ("timeframe_conflict_penalty", -8),
                ("risk_reward_penalty", -6),
                ("missing_data_penalty", -6),
            ],
        )
        self.assertEqual(
            _alpha_confidence(analysis, insights, feature, ["估值"] * 20, factor_lab, regime, timeframe, risk_reward),
            52,
        )

    def test_alpha_confidence_without_factor_lab_uses_original_weight_mix(self) -> None:
        confidence = _alpha_confidence(
            _analysis_stub(signal_confidence=80, data_quality_score=70),
            _insights_stub(overview_score=60),
            _feature_stub(data_quality_score=80, leader_score=50),
            [],
        )

        self.assertEqual(confidence, 70)

    def test_alpha_confidence_ignores_non_finite_inputs_and_adjustments(self) -> None:
        confidence = _alpha_confidence(
            _analysis_stub(signal_confidence=math.inf, data_quality_score=80),
            _insights_stub(overview_score=60),
            _feature_stub(data_quality_score=80, leader_score=math.inf),
            [],
            SimpleNamespace(calibrated_confidence=90),
            SimpleNamespace(confidence_adjustment=math.inf),
        )

        self.assertEqual(confidence, 48)

    def test_alpha_confidence_bounds_component_scores_before_weighting(self) -> None:
        confidence = _alpha_confidence(
            _analysis_stub(signal_confidence=500, data_quality_score=-20),
            _insights_stub(overview_score=120),
            _feature_stub(data_quality_score=80, leader_score=50),
            [],
        )

        self.assertEqual(confidence, 68)

    def test_alpha_verdict_rule_priority_is_explicit(self) -> None:
        self.assertEqual(
            [rule.name for rule in ALPHA_VERDICT_RULES],
            [
                "low_data_quality",
                "blocking_timeframe",
                "blocking_risk_reward",
                "market_risk_suppression",
                "positive_evidence",
                "negative_evidence",
            ],
        )
        self.assertEqual(
            _alpha_verdict(
                _feature_stub(data_quality_score=45),
                [_point(50)],
                [],
                timeframe=SimpleNamespace(conflict_level="高冲突"),
                risk_reward=SimpleNamespace(rating="风险优先"),
            ),
            "暂停主动判断",
        )
        self.assertEqual(
            _alpha_verdict(
                _feature_stub(),
                [_point(50)],
                [],
                timeframe=SimpleNamespace(conflict_level="高冲突"),
                risk_reward=SimpleNamespace(rating="风险优先"),
            ),
            "周期冲突",
        )
        self.assertEqual(
            _alpha_verdict(_feature_stub(), [_point(50)], [], risk_reward=SimpleNamespace(rating="周期冲突")),
            "环境风险压制",
        )

    def test_alpha_verdict_evidence_boundaries_are_stable(self) -> None:
        self.assertEqual(
            _alpha_verdict(_feature_stub(), [_point(20)], [_point(-16)], market_regime=SimpleNamespace(risk_multiplier=1.25)),
            "环境风险压制",
        )
        self.assertEqual(_alpha_verdict(_feature_stub(), [_point(22)], [_point(-10)]), "积极证据占优")
        self.assertEqual(_alpha_verdict(_feature_stub(), [_point(8)], [_point(-21)]), "风险证据占优")
        self.assertEqual(_alpha_verdict(_feature_stub(), [_point(12)], [_point(-8)]), "等待确认")

    def test_alpha_verdict_uses_conservative_non_finite_context(self) -> None:
        self.assertEqual(_alpha_verdict(_feature_stub(data_quality_score=math.nan), [_point(50)], []), "暂停主动判断")
        self.assertEqual(
            _alpha_verdict(
                _feature_stub(),
                [_raw_point(math.inf, title="异常正向"), _point(20)],
                [_raw_point(-math.inf, title="异常风险"), _point(-16)],
                market_regime=SimpleNamespace(risk_multiplier=math.inf),
            ),
            "等待确认",
        )

    def test_alpha_point_bucket_helpers_sort_and_limit_by_direction(self) -> None:
        points = [_point(2), _point(-1), _point(10), _point(-12), _point(6), _point(0), _point(-4)]

        self.assertEqual([item.impact for item in _top_alpha_points(points, positive=True, limit=2)], [10, 6])
        self.assertEqual([item.impact for item in _top_alpha_points(points, positive=False, limit=2)], [-12, -4])
        self.assertEqual(_top_alpha_points(points, positive=True, limit=0), [])

    def test_alpha_point_bucket_helpers_filter_non_finite_and_dedupe_before_limit(self) -> None:
        points = [
            _raw_point(2, title="弱支撑"),
            _raw_point(math.inf, title="异常支撑"),
            _raw_point(10, title="重复支撑"),
            _raw_point(12, title="重复支撑"),
            _raw_point(6, title="有效支撑"),
            _raw_point(0, title="中性"),
            _raw_point(-math.inf, title="异常风险"),
            _raw_point(-4, title="重复风险"),
            _raw_point(-12, title="重复风险"),
            _raw_point(-6, title="有效风险"),
        ]

        positives = _top_alpha_points(points, positive=True, limit=2)
        negatives = _top_alpha_points(points, positive=False, limit=2)

        self.assertEqual([(item.title, item.impact) for item in positives], [("重复支撑", 12), ("有效支撑", 6)])
        self.assertEqual(
            [(item.title, item.impact) for item in negatives],
            [("重复风险", -12), ("有效风险", -6)],
        )

    def test_alpha_point_bucket_helpers_dedupe_dirty_title_reason_across_sources(self) -> None:
        points = [
            _raw_point(9, title=" 主题\n共振 ", source="新闻", reason="成交 放量"),
            _raw_point(12, title="主题 共振", source="研报", reason="成交\n放量"),
            _raw_point(8, title="主题共振", source="策略", reason="成交放量"),
            _raw_point(-7, title=" 筹码\n松动 ", source="资金", reason="连续 流出"),
            _raw_point(-11, title="筹码 松动", source="盘口", reason="连续\n流出"),
            _raw_point(-6, title="筹码松动", source="风控", reason="连续流出"),
        ]

        positives = _top_alpha_points(points, positive=True, limit=3)
        negatives = _top_alpha_points(points, positive=False, limit=3)

        self.assertEqual(
            [(item.source, item.title, item.impact) for item in positives],
            [("研报", "主题 共振", 12), ("策略", "主题共振", 8)],
        )
        self.assertEqual(
            [(item.source, item.title, item.impact) for item in negatives],
            [("盘口", "筹码 松动", -11), ("风控", "筹码松动", -6)],
        )

    def test_alpha_point_bucket_helpers_tolerate_dirty_items_and_numeric_impact_strings(self) -> None:
        points = [
            None,
            SimpleNamespace(title="无影响字段", reason="有效理由"),
            _raw_point("15", title="字符串正向"),
            _raw_point("-13", title="字符串风险"),
            _raw_point("bad", title="坏影响值"),
            _raw_point(math.nan, title="NaN 影响值"),
        ]

        self.assertEqual(
            [(item.title, item.impact) for item in _top_alpha_points(points, positive=True, limit=2)],
            [("字符串正向", "15")],
        )
        self.assertEqual(
            [(item.title, item.impact) for item in _top_alpha_points(points, positive=False, limit=2)],
            [("字符串风险", "-13")],
        )
        self.assertEqual(_top_alpha_points(None, positive=True, limit=2), [])

    def test_alpha_point_bucket_helpers_reject_invalid_limits(self) -> None:
        points = [_point(10), _point(8), _point(-9)]

        for invalid_limit in (None, -1, 0, 0.9, math.nan, math.inf, "bad"):
            with self.subTest(limit=invalid_limit):
                self.assertEqual(_top_alpha_points(points, positive=True, limit=invalid_limit), [])

        self.assertEqual(
            [item.impact for item in _top_alpha_points(points, positive=True, limit="2")],
            [10, 8],
        )

    def test_alpha_point_bucket_helpers_skip_undisplayable_items(self) -> None:
        points = [
            _raw_point(99, title=" "),
            _raw_point(98, title="nan"),
            _raw_point(97, title="有效标题", reason="inf"),
            _raw_point(96, title="异常倍率", reason="风险倍率 inf。"),
            _raw_point(20, title="可展示支撑", reason="有效理由"),
            _raw_point(-99, title="null"),
            _raw_point(-98, title="异常风险", reason="N/A"),
            _raw_point(-20, title="可展示风险", reason="有效理由"),
        ]

        self.assertEqual([item.title for item in _top_alpha_points(points, positive=True, limit=3)], ["可展示支撑"])
        self.assertEqual([item.title for item in _top_alpha_points(points, positive=False, limit=3)], ["可展示风险"])

    def test_alpha_summary_uses_fallback_titles_when_points_are_blank(self) -> None:
        summary = _alpha_summary(
            [_raw_point(20, title=" ", reason="有效理由")],
            [_raw_point(-20, title="nan", reason="有效理由")],
            [],
            55,
        )

        self.assertIn("暂无核心加分项", summary)
        self.assertIn("暂无核心风险项", summary)

    def test_alpha_summary_sanitizes_confidence_and_missing_data_text(self) -> None:
        summary = _alpha_summary(
            [_raw_point(20, title=" 支撑\n延续 ", reason="有效理由")],
            [],
            ["nan", " 估值\n口径 ", "估值 口径"],
            math.inf,
        )

        self.assertIn("支撑 延续", summary)
        self.assertIn("Alpha证据充分度 0/100", summary)
        self.assertNotIn("置信度", summary)
        self.assertIn("缺少估值 口径等数据", summary)
        self.assertNotIn("nan", summary.lower())
        self.assertNotIn("inf", summary.lower())

    def test_alpha_missing_data_keeps_first_reason_and_applies_report_limit(self) -> None:
        insights = SimpleNamespace(
            valuation=SimpleNamespace(missing_data=["估值", "财务", ""]),
            financial_health=SimpleNamespace(missing_data=["财务", "现金流"]),
            lhb=SimpleNamespace(missing_data=["龙虎榜"]),
            rule_matches=SimpleNamespace(
                matches=[
                    SimpleNamespace(missing_data=["估值", "规则A"]),
                    SimpleNamespace(missing_data=[f"规则{index}" for index in range(20)]),
                ]
            ),
        )

        missing_data = _alpha_missing_data(insights)

        self.assertEqual(missing_data[:5], ["估值", "财务", "现金流", "龙虎榜", "规则A"])
        self.assertEqual(len(missing_data), MAX_MISSING_DATA_ITEMS)

    def test_alpha_missing_data_filters_invalid_literal_text(self) -> None:
        insights = SimpleNamespace(
            valuation=SimpleNamespace(missing_data=[None, "nan", "估值", "inf"]),
            financial_health=SimpleNamespace(missing_data=["null", "现金流"]),
            lhb=SimpleNamespace(missing_data=["估值", "龙虎榜"]),
            rule_matches=SimpleNamespace(matches=[SimpleNamespace(missing_data=["None", "规则A"])]),
        )

        self.assertEqual(_alpha_missing_data(insights), ["估值", "现金流", "龙虎榜", "规则A"])

    def test_alpha_missing_data_normalizes_dirty_text_and_empty_rule_matches(self) -> None:
        insights = SimpleNamespace(
            valuation=SimpleNamespace(missing_data=[" PE ", "pe", "估值\n口径", "N/A"]),
            financial_health=SimpleNamespace(missing_data=None),
            lhb=SimpleNamespace(missing_data=[]),
            rule_matches=SimpleNamespace(matches=None),
        )

        self.assertEqual(_alpha_missing_data(insights), ["PE", "估值 口径"])

    def test_alpha_data_quality_notes_are_cleaned_deduped_and_limited(self) -> None:
        analysis = SimpleNamespace(
            data_quality=SimpleNamespace(
                notes=[" 报价滞后 ", "nan", "报价滞后", None, "inf", "K线正常", "K线正常", "多源未校验"]
            )
        )

        self.assertEqual(_alpha_data_quality_notes(analysis), ["报价滞后", "K线正常", "多源未校验"])

    def test_alpha_missing_data_marks_requested_module_gaps_and_feature_fields(self) -> None:
        missing_data = _alpha_missing_data(
            _insights_stub(),
            None,
            feature=_feature_stub(data_quality_score=math.nan, leader_score=math.inf),
            market_regime=None,
            timeframe=None,
            risk_reward=None,
        )

        self.assertEqual(
            missing_data,
            [
                "特征快照数据质量分",
                "特征快照龙头评分",
                "因子实验室报告",
                "市场环境报告",
                "多周期报告",
                "风险收益报告",
            ],
        )

def _analysis_stub(*, signal_confidence: int = 70, data_quality_score: int = 80):
    return SimpleNamespace(
        signal_snapshot=SimpleNamespace(confidence=signal_confidence),
        data_quality=SimpleNamespace(score=data_quality_score),
    )


def _insights_stub(*, overview_score: int = 60):
    return SimpleNamespace(overview=SimpleNamespace(total_score=overview_score))


def _feature_stub(*, data_quality_score: int = 80, leader_score: int = 50):
    return SimpleNamespace(data_quality_score=data_quality_score, leader_score=leader_score)


def _point(impact: int) -> AlphaEvidencePoint:
    return AlphaEvidencePoint(source="测试", title=f"impact {impact}", impact=impact, level="观察", reason="测试证据")


def _raw_point(impact: object, *, title: str, source: str = "测试", reason: str = "测试证据"):
    return SimpleNamespace(source=source, title=title, impact=impact, level="观察", reason=reason)


def _factor(name: str, *, score: int, calibration_sample_count: int = 0, expected_level: str = "观察") -> StandardFactor:
    calibration = (
        FactorCalibration(
            sample_count=calibration_sample_count,
            win_rate=70 if expected_level == "积极" else 40,
            avg_forward_5d_return=1 if expected_level == "积极" else -1,
            avg_forward_10d_return=1 if expected_level == "积极" else -1,
            max_adverse_return=-2,
            stability_score=60,
            expected_level=expected_level,
            confidence_level="中",
            note="测试校准",
        )
        if calibration_sample_count
        else None
    )
    return StandardFactor(
        id=name,
        name=name,
        category="测试",
        value="测试",
        score=score,
        level="良好" if score >= 62 else "风险" if score <= 45 else "观察",
        direction="正向" if score >= 58 else "负向" if score <= 45 else "中性",
        weight=1,
        calibration=calibration,
    )


if __name__ == "__main__":
    unittest.main()
