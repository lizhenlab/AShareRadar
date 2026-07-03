from __future__ import annotations

from app.models.schemas import (
    AnalysisResult,
    MarketRegimeReport,
    RiskRewardReport,
    StockDiagnosis,
    StockConceptItem,
    StockQaItem,
    StockQaReport,
    ThemeContextReport,
    TStrategyAssistantReport,
)
from app.services.research_qa_utils import clean_text, dedupe, first_clean_items
from app.utils.market_data import finite_float

_DEFAULT_EVIDENCE_FALLBACK = "关键证据待确认。"


def build_stock_qa_report(
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
    t_strategy: "TStrategyAssistantReport | None" = None,
    theme_context: ThemeContextReport | None = None,
) -> StockQaReport:
    items = _normalize_items([
        _direct_buy_item(diagnosis, market_regime, risk_reward),
        _risk_reward_item(risk_reward),
        _diagnosis_reason_item(diagnosis),
        _next_session_focus_item(analysis, diagnosis),
        _t_strategy_item(analysis, t_strategy),
        _theme_context_item(theme_context),
    ])
    return StockQaReport(summary="围绕单只股票的常见问题，回答均引用当前分析结果。", items=items)


def _direct_buy_item(
    diagnosis: StockDiagnosis,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
) -> StockQaItem:
    action = clean_text(getattr(diagnosis, "action", None), fallback="观察")
    answer = (
        f"当前系统建议是「{action}」。即使处在积极关注，也只适合按确认信号分步观察，不应在贴近压力或量能未确认时追高。"
        if action == "积极关注"
        else f"当前系统建议是「{action}」。不是「积极关注」时，不应把单日涨跌当成买点，先等确认信号。"
    )
    return StockQaItem(
        question="现在能不能直接买？",
        answer=answer,
        evidence=_evidence(
            [
                getattr(diagnosis, "headline", None),
                getattr(risk_reward, "summary", None),
                getattr(market_regime, "market_label", None),
            ],
            fallback="系统建议、风险收益和市场环境证据待确认。",
        ),
    )


def _risk_reward_item(risk_reward: RiskRewardReport) -> StockQaItem:
    rating = clean_text(getattr(risk_reward, "rating", None), fallback="待确认")
    return StockQaItem(
        question="当前风险收益比够不够？",
        answer=(
            f"当前评级「{rating}」，{_risk_reward_ratio_text(risk_reward)}。"
            "只有上方空间、下方防守和验证状态同时匹配时，才把它视为可观察机会。"
        ),
        evidence=_evidence([
            getattr(risk_reward, "summary", None),
            _risk_reward_level_evidence(risk_reward),
            *_scenario_triggers(risk_reward),
        ]),
    )


def _risk_reward_ratio_text(risk_reward: RiskRewardReport) -> str:
    ratio = finite_float(getattr(risk_reward, "reward_risk_ratio", None))
    if ratio is not None and ratio > 0 and _risk_reward_levels_are_usable(risk_reward):
        return f"收益风险比 {ratio:.2f}"
    return "收益风险比待确认"


def _risk_reward_level_evidence(risk_reward: RiskRewardReport) -> str:
    return (
        f"上方目标 {_upside_target_text(risk_reward)}，"
        f"下方防守 {_downside_stop_text(risk_reward)}。"
    )


def _upside_target_text(risk_reward: RiskRewardReport) -> str:
    price, target, _stop = _risk_reward_level_values(risk_reward)
    if price is not None and target is not None and target > price:
        return f"{target:.2f}"
    return "待确认"


def _downside_stop_text(risk_reward: RiskRewardReport) -> str:
    price, _target, stop = _risk_reward_level_values(risk_reward)
    if price is not None and stop is not None and 0 < stop < price:
        return f"{stop:.2f}"
    return "待确认"


def _risk_reward_levels_are_usable(risk_reward: RiskRewardReport) -> bool:
    price, target, stop = _risk_reward_level_values(risk_reward)
    return bool(price is not None and target is not None and stop is not None and target > price and 0 < stop < price)


def _risk_reward_level_values(risk_reward: RiskRewardReport) -> tuple[float | None, float | None, float | None]:
    price = _positive_number_or_none(getattr(risk_reward, "current_price", None))
    target = _positive_number_or_none(getattr(risk_reward, "upside_target", None))
    stop = _positive_number_or_none(getattr(risk_reward, "downside_stop", None))
    return price, target, stop


def _positive_number_or_none(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _diagnosis_reason_item(diagnosis: StockDiagnosis) -> StockQaItem:
    return StockQaItem(
        question="为什么是这个结论？",
        answer="结论同时看趋势、资金、估值、环境、验证闭环和风险收益比；任一硬风险触发都会优先降级。",
        evidence=_evidence(
            [
                clean_text(getattr(diagnosis, "professional_summary", None), max_length=160),
                *first_clean_items(getattr(diagnosis, "hard_risks", None), 2),
            ],
            fallback="诊断依据待确认。",
        ),
    )


def _next_session_focus_item(analysis: AnalysisResult, diagnosis: StockDiagnosis) -> StockQaItem:
    return StockQaItem(
        question="明天重点看什么？",
        answer=(
            f"先看支撑 {_support_level_text(analysis)} 是否守住，再看压力 {_resistance_level_text(analysis)} 能否放量突破；"
            "若确认信号不齐，不急着给方向结论。"
        ),
        evidence=_evidence(
            [
                *first_clean_items(getattr(diagnosis, "watch_focus", None), 2),
                *first_clean_items(getattr(diagnosis, "confirmation_signals", None), 3),
            ],
            fallback="等待支撑、压力和量能确认。",
        ),
    )


def _t_strategy_item(analysis: AnalysisResult, t_strategy: TStrategyAssistantReport | None) -> StockQaItem:
    fallback = "做T只适用于已有可卖底仓，先看区间是否足够。"
    return StockQaItem(
        question="适不适合做T？",
        answer=clean_text(getattr(t_strategy, "summary", None), fallback=fallback),
        evidence=_evidence(_t_plan_reasons(analysis), fallback="T计划待确认。"),
    )


def _theme_context_item(theme_context: ThemeContextReport | None) -> StockQaItem:
    if not theme_context:
        return StockQaItem(
            question="概念题材能不能支撑走势？",
            answer="主题概念暂未确认，不应把题材当成独立买卖依据。",
            evidence=["概念归属成分待补。"],
        )
    return StockQaItem(
        question="概念题材能不能支撑走势？",
        answer=(
            f"当前为「{clean_text(getattr(theme_context, 'level', None), fallback='待确认')} / "
            f"{clean_text(getattr(theme_context, 'style', None), fallback='待确认')}」。"
            "主题只用于解释背景，具体动作仍服从支撑、压力、量能和风险收益比。"
        ),
        evidence=_evidence(
            [
                getattr(theme_context, "summary", None),
                *_concept_evidence(getattr(theme_context, "concepts", None)),
                *first_clean_items(getattr(theme_context, "risks", None), 2),
            ],
            fallback="主题概念证据待确认。",
        ),
    )


def _scenario_triggers(risk_reward: RiskRewardReport) -> list[str]:
    return [getattr(item, "trigger", None) for item in _first_items(getattr(risk_reward, "scenarios", None), 2)]


def _support_level_text(analysis: AnalysisResult) -> str:
    support = _positive_number_or_none(getattr(analysis, "support", None))
    resistance = _positive_number_or_none(getattr(analysis, "resistance", None))
    price = _positive_number_or_none(getattr(getattr(analysis, "quote", None), "price", None))
    if support is None or (price is not None and support >= price) or (resistance is not None and support >= resistance):
        return "待确认"
    return f"{support:.2f}"


def _resistance_level_text(analysis: AnalysisResult) -> str:
    resistance = _positive_number_or_none(getattr(analysis, "resistance", None))
    support = _positive_number_or_none(getattr(analysis, "support", None))
    price = _positive_number_or_none(getattr(getattr(analysis, "quote", None), "price", None))
    if resistance is None or (price is not None and resistance <= price) or (support is not None and resistance <= support):
        return "待确认"
    return f"{resistance:.2f}"


def _t_plan_reasons(analysis: AnalysisResult) -> list[str]:
    return [getattr(item, "reason", None) for item in _first_items(getattr(analysis, "t_plan", None), 3)]


def _concept_evidence(concepts: object) -> list[str]:
    result: list[str] = []
    for item in _first_items(concepts, 3):
        name = clean_text(getattr(item, "name", None))
        if not name:
            continue
        result.append(f"{name}：{_concept_change_text(item)}")
    return result


def _concept_change_text(item: StockConceptItem) -> str:
    change_pct = finite_float(getattr(item, "change_pct", None))
    return f"{change_pct:.2f}%" if change_pct is not None else "涨跌幅待确认"


def _evidence(items: object, *, fallback: str = _DEFAULT_EVIDENCE_FALLBACK) -> list[str]:
    return dedupe(items) or [fallback]


def _first_items(items: object, limit: int) -> list[object]:
    if not isinstance(items, list | tuple):
        return []
    return list(items[: max(0, limit)])


def _normalize_items(items: object) -> list[StockQaItem]:
    return [_normalize_item(item) for item in _first_items(items, 20)]


def _normalize_item(item: StockQaItem) -> StockQaItem:
    return StockQaItem(
        question=clean_text(getattr(item, "question", None), fallback="常见问题"),
        answer=clean_text(getattr(item, "answer", None), fallback="结论待确认。"),
        evidence=_evidence(getattr(item, "evidence", None)),
    )


__all__ = ["build_stock_qa_report"]
