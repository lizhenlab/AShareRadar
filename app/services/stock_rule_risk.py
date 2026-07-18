from __future__ import annotations

from app.models.schemas import AbnormalEventItem, AbnormalEventSummary, AnalysisResult, RuleMatch, ValuationAnalysis
from app.services.stock_rule_contracts import (
    LEVEL_RISK,
    LEVEL_WATCH,
    RULE_PARAMETERS_BY_ID,
    STATUS_MATCHED,
    HighValuationChaseState,
    _rule_match_fields,
)
from app.services.stock_rule_values import (
    _missing_data_items,
    _non_negative_score,
    _positive_score,
    _rule_confidence,
    _score_evidence,
    _status_from_flags,
)


def _rule_high_valuation_chase(analysis: AnalysisResult, valuation: ValuationAnalysis) -> RuleMatch:
    state = _high_valuation_chase_state(analysis.trend_score, valuation.score)
    status = _high_valuation_chase_status(state)
    return RuleMatch(
        **_rule_match_fields("high_valuation_chase_risk"),
        status=status,
        level=_high_valuation_chase_level(status),
        confidence=_high_valuation_chase_confidence(status),
        reason=_high_valuation_chase_reason(analysis, valuation),
        actions=[
            "强趋势里更重视失效价，不只用估值逆势判断顶部。",
            "若放量滞涨或跌破5日线，及时降低信号等级。",
        ],
        invalidation="估值评分改善，或趋势从急涨进入健康整理后重新评估。",
        evidence=_high_valuation_chase_evidence(analysis, valuation),
        missing_data=_high_valuation_chase_missing_data(valuation),
    )


def _high_valuation_chase_state(trend_score: int, valuation_score: int) -> HighValuationChaseState:
    config = RULE_PARAMETERS_BY_ID["high_valuation_chase_risk"]
    clean_trend_score = _non_negative_score(trend_score)
    clean_valuation_score = _positive_score(valuation_score)
    if clean_trend_score is None or clean_valuation_score is None:
        return HighValuationChaseState(False, False)
    return HighValuationChaseState(
        hit=clean_trend_score >= float(config["trend_hit"]) and clean_valuation_score < float(config["valuation_hit"]),
        close=clean_trend_score >= float(config["trend_close"]) and clean_valuation_score < float(config["valuation_close"]),
    )


def _high_valuation_chase_status(state: HighValuationChaseState) -> str:
    return _status_from_flags(state.hit, state.close)


def _high_valuation_chase_level(status: str) -> str:
    return LEVEL_RISK if status == STATUS_MATCHED else LEVEL_WATCH


def _high_valuation_chase_confidence(status: str) -> int:
    return _rule_confidence("high_valuation_chase_risk", status)


def _high_valuation_chase_reason(analysis: AnalysisResult, valuation: ValuationAnalysis) -> str:
    return f"{_score_evidence('趋势评分', analysis.trend_score)}，{_score_evidence('估值评分', valuation.score, positive=True)}。{valuation.summary}"


def _high_valuation_chase_evidence(analysis: AnalysisResult, valuation: ValuationAnalysis) -> list[str]:
    return [
        _score_evidence("趋势评分", analysis.trend_score),
        _score_evidence("估值评分", valuation.score, positive=True),
        valuation.summary,
    ]


def _high_valuation_chase_missing_data(valuation: ValuationAnalysis) -> list[str]:
    missing_data = list(valuation.missing_data)
    if _positive_score(valuation.score) is None:
        missing_data.append("估值评分")
    return _missing_data_items(missing_data)


def _rule_abnormal_risk(analysis: AnalysisResult, abnormal_events: AbnormalEventSummary) -> RuleMatch:
    risk_events = _abnormal_risk_events(abnormal_events)
    status = _abnormal_risk_status(risk_events, abnormal_events)
    evidence = _abnormal_risk_evidence(risk_events, abnormal_events)
    return RuleMatch(
        **_rule_match_fields("abnormal_risk_event"),
        status=status,
        level=LEVEL_RISK if risk_events else LEVEL_WATCH,
        confidence=_abnormal_risk_confidence(status),
        reason=f"{'；'.join(evidence)}。当前风险等级：{analysis.risk_level}。",
        actions=[
            "先解释异动来源，再看关键价位是否失守。",
            "风险异动叠加数据质量异常时，建议结论自动降权。",
        ],
        invalidation="风险异动后的2到3个交易日内重新站稳短期均线且量能恢复正常。",
        evidence=evidence,
    )


def _abnormal_risk_events(abnormal_events: AbnormalEventSummary) -> list[AbnormalEventItem]:
    return [item for item in abnormal_events.events if item.level == LEVEL_RISK]


def _abnormal_risk_status(risk_events: list[AbnormalEventItem], abnormal_events: AbnormalEventSummary) -> str:
    return _status_from_flags(bool(risk_events), bool(abnormal_events.events))


def _abnormal_risk_evidence(risk_events: list[AbnormalEventItem], abnormal_events: AbnormalEventSummary) -> list[str]:
    return [item.title for item in risk_events[:3]] or [abnormal_events.main_signal]


def _abnormal_risk_confidence(status: str) -> int:
    return _rule_confidence("abnormal_risk_event", status)
