from __future__ import annotations

import pytest

from app.models.schemas import (
    ActionAdvice,
    AnalysisResult,
    DataQuality,
    EventDigestReport,
    EvidenceChainReport,
    Kline,
    MarketRegimeReport,
    PeerComparisonReport,
    Quote,
    RiskRadarItem,
    RiskRadarReport,
    RiskRewardReport,
    ScenarioPlan,
    SignalItem,
    SignalSnapshot,
    SignalValidationItem,
    SignalValidationReport,
    StockConceptItem,
    StockDiagnosis,
    TStrategyAssistantReport,
    ThemeContextReport,
    TimeframeAlignmentReport,
)
from app.services.research_qa_answer import (
    TOPIC_ANSWER_STRATEGIES,
    answer_stock_question,
    question_actions,
    question_answer_text,
    question_conclusion,
    question_confidence,
    question_evidence,
    question_invalidations,
)
from app.services.research_qa_topics import QUESTION_TOPIC_KEYWORDS, RELATED_QUESTIONS


@pytest.mark.parametrize(
    ("question", "topic"),
    [
        ("适合做T吗", "做T"),
        ("风险在哪里", "风险"),
        ("当前风险收益比够不够", "风险收益"),
        ("能不能低吸买一点", "买点"),
        ("要不要冲高卖一点", "卖点"),
        ("同行里算不算龙头", "同行龙头"),
        ("它有什么概念题材", "主题概念"),
        ("近期事件有什么影响", "事件"),
        ("短线怎么看", "短线观察"),
        ("综合说一下", "综合判断"),
    ],
)
def test_stock_question_topics_have_complete_strategy_outputs(question: str, topic: str) -> None:
    args = _qa_args()

    result = answer_stock_question(question, *args)

    assert result.topic == topic
    assert result.conclusion
    assert result.answer
    assert result.evidence
    assert result.actions
    assert result.invalidations
    assert result.related_questions


def test_question_topic_tables_have_registered_answer_strategies() -> None:
    configured_topics = {topic for topic, _keywords in QUESTION_TOPIC_KEYWORDS} | set(RELATED_QUESTIONS) | {"综合判断"}

    assert configured_topics <= set(TOPIC_ANSWER_STRATEGIES)


def test_risk_actions_are_deduped_after_priority_sorting() -> None:
    analysis, diagnosis, _evidence_chain, risk_radar, _event_digest, _peer, t_strategy, regime, risk_reward, validation, _timeframe, theme = _qa_args()
    risk_radar = risk_radar.model_copy(
        update={
            "items": [
                RiskRadarItem(name="放量下跌", level="高", score=90, reason="破位", action="先降仓观察"),
                RiskRadarItem(name="均线压制", level="中", score=78, reason="短线弱", action="先降仓观察"),
                RiskRadarItem(name="支撑未稳", level="中", score=60, reason="靠近支撑", action="等支撑确认"),
            ]
        }
    )

    actions = question_actions("风险", analysis, diagnosis, risk_radar, t_strategy, regime, risk_reward, validation, theme)

    assert actions == ["先降仓观察", "等支撑确认"]


def test_unregistered_topic_uses_default_answer_strategy() -> None:
    args = _qa_args()
    analysis, diagnosis, evidence_chain, risk_radar, event_digest, peer, t_strategy, regime, risk_reward, validation, _timeframe, theme = args

    evidence = question_evidence("未配置主题", *args)
    actions = question_actions(
        "未配置主题",
        analysis,
        diagnosis,
        risk_radar,
        t_strategy,
        regime,
        risk_reward,
        validation,
        theme,
    )
    invalidations = question_invalidations(
        "未配置主题",
        analysis,
        diagnosis,
        evidence_chain,
        risk_radar,
        t_strategy,
        validation,
        theme,
    )
    conclusion = question_conclusion(
        "未配置主题",
        diagnosis,
        risk_radar,
        t_strategy,
        peer,
        event_digest,
        risk_reward,
        validation,
        theme,
    )

    assert evidence[0] == diagnosis.headline
    assert actions == [diagnosis.action, *diagnosis.watch_focus, validation.summary]
    assert invalidations == ["跌破支撑", "放量下跌", "风险收益比不足"]
    assert conclusion == "当前总建议「控制风险」，风险收益评级「性价比一般」"


def test_topic_strategy_lookup_normalizes_topic_whitespace() -> None:
    analysis, diagnosis, _evidence_chain, risk_radar, _event_digest, _peer, t_strategy, regime, risk_reward, validation, _timeframe, theme = _qa_args()
    theme = theme.model_copy(update={"missing_data": ["行业"]})

    actions = question_actions(" 风险 ", analysis, diagnosis, risk_radar, t_strategy, regime, risk_reward, validation, theme)
    confidence = question_confidence(analysis, diagnosis, regime, validation, " 主题概念 ", theme)
    answer = question_answer_text(" 风险 ", analysis, diagnosis, "中等风险", actions, confidence)

    assert actions == ["先降仓观察", "等支撑确认"]
    assert confidence == 78
    assert "当前风险判断" in answer


def test_topic_strategy_empty_actions_fall_back_to_default_actions() -> None:
    analysis, diagnosis, _evidence_chain, risk_radar, _event_digest, _peer, t_strategy, regime, risk_reward, validation, _timeframe, theme = _qa_args()
    risk_radar = risk_radar.model_copy(update={"items": []})

    actions = question_actions("风险", analysis, diagnosis, risk_radar, t_strategy, regime, risk_reward, validation, theme)

    assert actions == [diagnosis.action, *diagnosis.watch_focus, validation.summary]


def test_answer_items_are_cleaned_deduped_and_limited() -> None:
    analysis, diagnosis, _evidence_chain, risk_radar, _event_digest, _peer, t_strategy, regime, risk_reward, validation, _timeframe, theme = _qa_args()
    diagnosis = diagnosis.model_copy(
        update={
            "action": " 控制风险 ",
            "watch_focus": ["控制风险", " 支撑有效性 ", "", "量能变化", "事件跟踪", "仓位纪律", "额外关注"],
        }
    )
    validation = validation.model_copy(update={"summary": " 支撑有效性 "})

    actions = question_actions("未配置主题", analysis, diagnosis, risk_radar, t_strategy, regime, risk_reward, validation, theme)

    assert actions == ["控制风险", "支撑有效性", "量能变化", "事件跟踪", "仓位纪律"]


def test_risk_actions_are_cleaned_before_topic_limit() -> None:
    analysis, diagnosis, _evidence_chain, risk_radar, _event_digest, _peer, t_strategy, regime, risk_reward, validation, _timeframe, theme = _qa_args()
    non_finite_action = RiskRadarItem(name="异常项", level="高", score=96, reason="异常", action="临时动作").model_copy(
        update={"action": float("nan")}
    )
    risk_radar = risk_radar.model_copy(
        update={
            "items": [
                RiskRadarItem(name="空动作", level="高", score=100, reason="无动作", action=" "),
                RiskRadarItem(name="高风险", level="高", score=99, reason="破位", action="先降仓观察"),
                RiskRadarItem(name="重复动作", level="高", score=98, reason="重复", action=" 先降仓观察 "),
                non_finite_action,
                RiskRadarItem(name="支撑", level="中", score=95, reason="临近支撑", action="等支撑确认"),
                RiskRadarItem(name="量能", level="中", score=94, reason="量能不足", action="量能修复再说"),
                RiskRadarItem(name="收回", level="中", score=93, reason="价格偏弱", action="价格收回再说"),
                RiskRadarItem(name="仓位", level="中", score=92, reason="纪律", action="仓位纪律"),
            ]
        }
    )

    actions = question_actions("风险", analysis, diagnosis, risk_radar, t_strategy, regime, risk_reward, validation, theme)

    assert actions == ["先降仓观察", "等支撑确认", "量能修复再说", "价格收回再说"]


def test_invalidations_are_cleaned_before_source_limits() -> None:
    analysis, diagnosis, evidence_chain, risk_radar, _event_digest, _peer, t_strategy, _regime, _risk_reward, validation, _timeframe, theme = _qa_args()
    evidence_chain = evidence_chain.model_copy(
        update={
            "invalidations": [
                " ",
                "跌破支撑",
                " 跌破支撑 ",
                float("inf"),
                "放量下跌",
                "支撑失效",
                "趋势破坏",
            ]
        }
    )
    diagnosis = diagnosis.model_copy(update={"hard_risks": ["风险收益比不足"]})

    invalidations = question_invalidations(
        "未配置主题",
        analysis,
        diagnosis,
        evidence_chain,
        risk_radar,
        t_strategy,
        validation,
        theme,
    )

    assert invalidations == ["跌破支撑", "放量下跌", "支撑失效", "趋势破坏", "风险收益比不足"]


def test_question_answer_text_uses_first_three_actions() -> None:
    analysis, diagnosis, *_rest = _qa_args()

    answer = question_answer_text(
        "风险",
        analysis,
        diagnosis,
        "中等风险",
        ["动作1", "动作2", "动作3", "动作4"],
        80,
    )

    assert "动作1；动作2；动作3" in answer
    assert "动作4" not in answer


def test_answer_text_handles_empty_actions_with_beginner_summary_fallback() -> None:
    analysis, diagnosis, *_rest = _qa_args()

    answer = question_answer_text("风险", analysis, diagnosis, "中等风险", [" ", ""], 80)

    assert diagnosis.beginner_summary in answer


def test_theme_question_without_context_stays_conservative() -> None:
    args = _qa_args(include_theme=False)

    result = answer_stock_question("它有什么概念题材", *args)

    assert result.topic == "主题概念"
    assert "待确认" in result.conclusion
    assert result.actions[0] == "主题概念数据未确认前，不把题材当作买入理由。"
    assert any("暂不可用" in item for item in result.evidence)


def test_question_confidence_caps_theme_missing_data_penalty() -> None:
    analysis, diagnosis, _evidence_chain, _risk_radar, _event_digest, _peer, _t_strategy, regime, _risk_reward, validation, _timeframe, theme = _qa_args()
    theme = theme.model_copy(update={"missing_data": ["行业", "概念", "龙头", "强度"]})

    confidence = question_confidence(analysis, diagnosis, regime, validation, "主题概念", theme)

    assert confidence == 70


def test_question_confidence_combines_market_validation_and_topic_quality_penalties() -> None:
    analysis, diagnosis, _evidence_chain, _risk_radar, _event_digest, _peer, _t_strategy, regime, _risk_reward, validation, _timeframe, theme = _qa_args()
    analysis = analysis.model_copy(update={"data_quality": analysis.data_quality.model_copy(update={"score": 80})})
    regime = regime.model_copy(update={"risk_multiplier": 1.3})
    validation = validation.model_copy(update={"overall_status": "风险优先"})

    confidence = question_confidence(analysis, diagnosis, regime, validation, "事件", theme)

    assert confidence == 59


def test_question_confidence_treats_non_finite_components_conservatively() -> None:
    analysis, diagnosis, _evidence_chain, _risk_radar, _event_digest, _peer, _t_strategy, regime, _risk_reward, validation, _timeframe, theme = _qa_args()
    analysis = analysis.model_copy(
        update={"signal_snapshot": analysis.signal_snapshot.model_copy(update={"confidence": float("nan")})}
    )

    confidence = question_confidence(analysis, diagnosis, regime, validation, "风险", theme)

    assert confidence == 25


def test_answer_text_does_not_leak_non_finite_numbers() -> None:
    args = _qa_args()
    analysis, diagnosis, evidence_chain, risk_radar, event_digest, peer, t_strategy, regime, risk_reward, validation, timeframe, theme = args
    analysis = analysis.model_copy(
        update={
            "quote": analysis.quote.model_copy(update={"name": " ", "price": float("nan")}),
            "support": float("nan"),
            "resistance": float("inf"),
        }
    )
    regime = regime.model_copy(update={"market_label": None, "risk_multiplier": float("inf")})
    risk_reward = risk_reward.model_copy(
        update={
            "upside_target": float("nan"),
            "upside_pct": float("inf"),
            "downside_stop": float("-inf"),
            "downside_pct": float("nan"),
            "reward_risk_ratio": float("inf"),
            "rating": None,
        }
    )
    validation = validation.model_copy(update={"overall_status": None})

    result = answer_stock_question(
        "当前风险收益比够不够",
        analysis,
        diagnosis,
        evidence_chain,
        risk_radar,
        event_digest,
        peer,
        t_strategy,
        regime,
        risk_reward,
        validation,
        timeframe,
        theme,
    )
    answer_text = " ".join([result.conclusion, result.answer, *result.evidence, *result.actions, *result.invalidations])

    assert "nan" not in answer_text.lower()
    assert "inf" not in answer_text.lower()
    assert "none" not in answer_text.lower()
    assert "待确认" in answer_text


def _qa_args(include_theme: bool = True) -> tuple:
    analysis = _analysis()
    diagnosis = StockDiagnosis(
        symbol="600519.SH",
        updated_at="2026-06-28 10:00:00",
        headline="趋势偏弱，先看支撑确认",
        beginner_summary="先观察，不追高。",
        professional_summary="量价和风险收益未共振。",
        confirmation_signals=["放量站回20日线", "支撑位缩量企稳"],
        hard_risks=["跌破支撑", "风险收益比不足"],
        watch_focus=["支撑有效性", "量能变化"],
        action="控制风险",
        confidence=82,
    )
    evidence_chain = EvidenceChainReport(
        verdict="观察为主",
        summary="支持与反对证据并存。",
        support=["靠近支撑", "估值不极端"],
        opposition=["均线压制", "量能不足"],
        confirmations=["站稳20日线"],
        invalidations=["跌破支撑", "放量下跌"],
    )
    risk_radar = RiskRadarReport(
        overall_level="中等风险",
        summary="风险主要来自趋势和支撑。",
        items=[
            RiskRadarItem(name="趋势转弱", level="中", score=72, reason="低于均线", action="先降仓观察"),
            RiskRadarItem(name="支撑考验", level="中", score=58, reason="临近支撑", action="等支撑确认"),
        ],
        top_risks=["趋势转弱：低于均线", "支撑考验：临近支撑"],
    )
    event_digest = EventDigestReport(
        impact_label="事件影响中性偏谨慎",
        summary="近期事件没有明显改变趋势。",
        positive_events=["分红预案稳定"],
        negative_events=["需求预期偏弱"],
        watch_events=["业绩发布"],
        missing_data=["公告细节待确认"],
    )
    peer = PeerComparisonReport(
        industry="白酒",
        sample_count=6,
        valuation_position="估值处于同行中位",
        strength_position="强弱略落后龙头",
        summary="同行比较显示强弱一般。",
        metrics=["涨跌幅落后", "成交额稳定"],
        leaders=["五粮液"],
        risks=["行业整体偏弱"],
    )
    t_strategy = TStrategyAssistantReport(
        style="轻仓试错",
        suitability="不适合频繁做T",
        summary="波动不足，纪律要求高。",
        low_zone="1260-1270",
        high_zone="1310-1320",
        execution_steps=["低吸只看支撑缩量", "高抛只看压力放量"],
        stop_conditions=["跌破1260停止做T"],
    )
    regime = MarketRegimeReport(
        symbol="600519.SH",
        updated_at="2026-06-28 10:00:00",
        market_label="市场偏冷",
        industry_label="白酒偏弱",
        stock_state="支撑观察",
        risk_multiplier=1.15,
        confidence_adjustment=-5,
        suggestions=["降低仓位"],
        evidence=["市场宽度一般"],
    )
    validation = SignalValidationReport(
        symbol="600519.SH",
        updated_at="2026-06-28 10:00:00",
        overall_status="观察为主",
        summary="信号仍需二次确认。",
        items=[
            SignalValidationItem(
                name="支撑低吸",
                category="买点",
                status="等待确认",
                confidence=62,
                trigger_condition="靠近支撑",
                confirmation_condition="缩量企稳",
                invalidation_condition="跌破支撑",
                historical_reference="近60日支撑有效",
                action_hint="只做观察级低吸",
            ),
            SignalValidationItem(
                name="压力减仓",
                category="卖点",
                status="风险触发",
                confidence=70,
                trigger_condition="接近压力",
                confirmation_condition="放量突破压力",
                invalidation_condition="站稳压力",
                historical_reference="压力附近回落",
                action_hint="压力位保护利润",
            ),
        ],
    )
    risk_reward = RiskRewardReport(
        symbol="600519.SH",
        updated_at="2026-06-28 10:00:00",
        current_price=1280.0,
        upside_target=1326.0,
        downside_stop=1262.0,
        upside_pct=3.59,
        downside_pct=-1.41,
        reward_risk_ratio=1.45,
        atr14=18.0,
        atr_pct=1.4,
        volatility_pct=2.1,
        rating="性价比一般",
        summary="收益风险比处于观察区。",
        scenarios=[
            ScenarioPlan(name="上行", probability=35, trigger="放量突破", expected_move="上看压力", response="观察持有", invalidation="跌回支撑"),
            ScenarioPlan(name="震荡", probability=40, trigger="量能不足", expected_move="区间震荡", response="降低预期", invalidation="放量跌破"),
            ScenarioPlan(name="下行", probability=25, trigger="跌破支撑", expected_move="回撤扩大", response="先防守", invalidation="快速收回"),
        ],
        notes=["不追高", "先看支撑"],
    )
    timeframe = TimeframeAlignmentReport(
        symbol="600519.SH",
        updated_at="2026-06-28 10:00:00",
        alignment_score=48,
        alignment_label="短弱中性",
        conflict_level="中冲突",
        summary="短周期偏弱，中周期待确认。",
        timeframes=[],
        suggestions=["等待短周期修复"],
    )
    theme = _theme_context() if include_theme else None
    return (analysis, diagnosis, evidence_chain, risk_radar, event_digest, peer, t_strategy, regime, risk_reward, validation, timeframe, theme)


def _analysis() -> AnalysisResult:
    quote = Quote(
        code="600519",
        name="贵州茅台",
        market="SH",
        price=1280.0,
        prev_close=1290.0,
        open=1292.0,
        high=1305.0,
        low=1272.0,
        volume=10000.0,
        amount=128000000.0,
        change=-10.0,
        change_pct=-0.78,
        turnover_rate=0.4,
        pe=18.0,
        pb=6.0,
        timestamp="2026-06-28 10:00:00",
    )
    return AnalysisResult(
        quote=quote,
        action_advice=ActionAdvice(action="控制风险", confidence=80, reason="趋势偏弱"),
        data_quality=DataQuality(level="良好", source="测试", quote_time=quote.timestamp, kline_count=60, score=86),
        signal_snapshot=SignalSnapshot(score=52, label="支撑观察", confidence=84, summary="信号中性偏弱"),
        trend_score=46,
        trend_label="偏弱",
        support=1262.0,
        resistance=1326.0,
        ma5=1284.0,
        ma10=1292.0,
        ma20=1300.0,
        risk_level="中等风险",
        beginner_summary="先等支撑确认。",
        buy_points=[SignalItem(title="支撑低吸", level="观察", reason="靠近支撑但未确认")],
        sell_points=[SignalItem(title="压力减仓", level="谨慎", reason="接近压力且量能不足")],
        t_plan=[SignalItem(title="低吸高抛", level="观察", reason="区间足够但纪律要求高")],
        strength_tags=["白酒", "低波动"],
        klines=[Kline(date="2026-06-26", open=1280, close=1280, high=1300, low=1265, volume=10000)],
    )


def _theme_context() -> ThemeContextReport:
    return ThemeContextReport(
        symbol="600519.SH",
        updated_at="2026-06-28 10:00:00",
        industry="白酒",
        industry_change_pct=-0.4,
        concepts=[
            StockConceptItem(
                symbol="600519.SH",
                rank=1,
                name="白酒概念",
                change_pct=1.2,
                source="测试概念",
                updated_at="2026-06-28 10:00:00",
            )
        ],
        score=58,
        level="主题中性",
        style="防守消费",
        relative_strength="相对一般",
        summary="主题背景存在但不强。",
        evidence=["白酒概念小幅上涨"],
        opportunities=["主题回暖时再观察"],
        risks=["题材强度不足"],
        missing_data=[],
    )
