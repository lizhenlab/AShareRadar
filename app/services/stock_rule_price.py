from __future__ import annotations

from app.models.schemas import AnalysisResult, RuleMatch
from app.services.stock_rule_contracts import (
    LEVEL_POSITIVE,
    LEVEL_RISK,
    LEVEL_WATCH,
    RULE_PARAMETERS_BY_ID,
    STATUS_MATCHED,
    BreakMa20State,
    VolumeBreakoutState,
    _rule_match_fields,
)
from app.services.stock_rule_values import (
    _non_negative_score,
    _positive_metric,
    _positive_price_level,
    _price_level_evidence,
    _rule_confidence,
    _score_evidence,
    _status_from_flags,
)


def _rule_volume_breakout(analysis: AnalysisResult, latest_high_20: float, volume_ratio: float | None) -> RuleMatch:
    quote = analysis.quote
    state = _volume_breakout_state(quote.price, latest_high_20, volume_ratio)
    status = _volume_breakout_status(state)
    evidence = _volume_breakout_evidence(quote.price, latest_high_20, volume_ratio)
    return RuleMatch(
        **_rule_match_fields("volume_breakout_20d"),
        status=status,
        level=_volume_breakout_level(status),
        confidence=_rule_confidence("volume_breakout_20d", status),
        reason="；".join(evidence),
        actions=[
            "只把站稳压力位后的回踩作为确认点。",
            "突破当日若放量过猛，次日承接更关键。",
        ],
        invalidation=f"跌回压力位 {analysis.resistance:.2f} 下方或量能快速萎缩。",
        evidence=evidence,
        missing_data=_volume_breakout_missing_data(quote.price, latest_high_20, volume_ratio),
    )


def _volume_breakout_state(price: float, latest_high_20: float, volume_ratio: float | None) -> VolumeBreakoutState:
    config = RULE_PARAMETERS_BY_ID["volume_breakout_20d"]
    current_price = _positive_price_level(price)
    high_threshold = _positive_price_level(latest_high_20)
    clean_volume_ratio = _positive_metric(volume_ratio)
    return VolumeBreakoutState(
        near_breakout=current_price is not None
        and high_threshold is not None
        and current_price >= high_threshold * float(config["near_breakout_pct"]),
        enough_volume=clean_volume_ratio is not None and clean_volume_ratio >= float(config["volume_ratio"]),
    )


def _volume_breakout_status(state: VolumeBreakoutState) -> str:
    return _status_from_flags(state.near_breakout and state.enough_volume, state.near_breakout or state.enough_volume)


def _volume_breakout_level(status: str) -> str:
    return LEVEL_POSITIVE if status == STATUS_MATCHED else LEVEL_WATCH


def _volume_breakout_evidence(price: float, latest_high_20: float, volume_ratio: float | None) -> list[str]:
    evidence = [f"{_price_level_evidence('现价', price)} / {_price_level_evidence('20日高点', latest_high_20)}"]
    clean_volume_ratio = _positive_metric(volume_ratio)
    if clean_volume_ratio is not None:
        evidence.append(f"量比估算 {clean_volume_ratio:.2f}")
    elif volume_ratio is not None:
        evidence.append("量比估算 缺失")
    return evidence


def _volume_breakout_missing_data(price: float, latest_high_20: float, volume_ratio: float | None) -> list[str]:
    missing_data = []
    if _positive_price_level(price) is None:
        missing_data.append("现价")
    if _positive_price_level(latest_high_20) is None:
        missing_data.append("20日高点")
    if _positive_metric(volume_ratio) is None:
        missing_data.append("近5日成交量")
    return missing_data


def _rule_break_ma20(analysis: AnalysisResult) -> RuleMatch:
    status = _break_ma20_status(_break_ma20_state(analysis))
    evidence = _break_ma20_evidence(analysis)
    return RuleMatch(
        **_rule_match_fields("break_ma20_risk"),
        status=status,
        level=_break_ma20_level(status),
        confidence=_break_ma20_confidence(status),
        reason="，".join(evidence) + "。",
        actions=[
            "跌破后先观察能否快速收回20日线。",
            "若同时跌破支撑位，当前建议需要降级。",
        ],
        invalidation=_break_ma20_invalidation(analysis),
        evidence=evidence,
        missing_data=_break_ma20_missing_data(analysis),
    )


def _break_ma20_state(analysis: AnalysisResult) -> BreakMa20State:
    config = RULE_PARAMETERS_BY_ID["break_ma20_risk"]
    price = _positive_price_level(analysis.quote.price)
    ma20 = _positive_price_level(analysis.ma20)
    trend_score = _non_negative_score(analysis.trend_score)
    return BreakMa20State(
        broken=price is not None
        and ma20 is not None
        and trend_score is not None
        and price < ma20
        and trend_score < float(config["trend_score"]),
        close=price is not None and ma20 is not None and price < ma20 * float(config["near_ma20_pct"]),
    )


def _break_ma20_status(state: BreakMa20State) -> str:
    return _status_from_flags(state.broken, state.close)


def _break_ma20_level(status: str) -> str:
    return LEVEL_RISK if status == STATUS_MATCHED else LEVEL_WATCH


def _break_ma20_confidence(status: str) -> int:
    return _rule_confidence("break_ma20_risk", status)


def _break_ma20_evidence(analysis: AnalysisResult) -> list[str]:
    return [
        _price_level_evidence("现价", analysis.quote.price),
        _price_level_evidence("20日线", analysis.ma20),
        _score_evidence("趋势评分", analysis.trend_score),
    ]


def _break_ma20_invalidation(analysis: AnalysisResult) -> str:
    ma20 = _positive_price_level(analysis.ma20)
    if ma20 is None:
        return "缺少有效20日线时不启用该风控信号。"
    return f"重新站上20日线 {ma20:.2f} 且趋势评分回到50以上。"


def _break_ma20_missing_data(analysis: AnalysisResult) -> list[str]:
    missing_data = []
    if _positive_price_level(analysis.quote.price) is None:
        missing_data.append("现价")
    if _positive_price_level(analysis.ma20) is None:
        missing_data.append("20日线")
    if _non_negative_score(analysis.trend_score) is None:
        missing_data.append("趋势评分")
    return missing_data
