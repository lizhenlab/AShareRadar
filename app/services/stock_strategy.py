from __future__ import annotations

from app.models.analysis import (
    AnalysisResult,
    FundFlowAnalysis,
    OrderPressure,
    SignalItem,
    StrategyCard,
)
from app.utils.market_data import finite_float


SEVERE_QUALITY_STRATEGY_STATUS = {
    "满足": "暂停观察",
    "触发": "暂停观察",
    "接近触发": "暂停观察",
    "仅底仓适用": "暂停做T",
}
WEAK_QUALITY_STRATEGY_STATUS = {
    "满足": "等待确认",
    "触发": "等待确认",
    "接近触发": "观察",
    "仅底仓适用": "仅底仓适用（降权）",
}


def build_strategy_cards(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
) -> list[StrategyCard]:
    return [
        _trend_pullback_card(analysis),
        _breakout_confirmation_card(analysis, fund_flow),
        _support_dip_card(analysis),
        _t_range_card(analysis, order_pressure),
        _risk_stop_card(analysis),
    ]


def _trend_pullback_card(analysis: AnalysisResult) -> StrategyCard:
    support = _available_level(analysis, "support", "support_available")
    resistance = _available_level(analysis, "resistance", "resistance_available")
    signal = _first_signal(
        analysis.buy_points if support is not None and resistance is not None else [],
        title="暂无清晰买点",
        level="谨慎",
        reason="上游暂未给出有效买点，先等待趋势和数据质量恢复。",
    )
    return _strategy_from_signal(
        analysis,
        "趋势回踩策略",
        signal,
        status=_quality_strategy_status("满足" if analysis.trend_score >= 65 and analysis.quote.price >= analysis.ma5 else "等待", analysis),
        reference_price=f"5日线 {analysis.ma5:.2f}",
        invalidation=f"跌破支撑 {support:.2f}" if support is not None else "支撑位待重算，不启用结构止损条件",
        suitable_for="能接受等待确认的新手和波段观察者",
    )


def _breakout_confirmation_card(analysis: AnalysisResult, fund_flow: FundFlowAnalysis) -> StrategyCard:
    resistance = _available_level(analysis, "resistance", "resistance_available")
    fund_score = _available_fund_score(fund_flow)
    near_resistance = resistance is not None and analysis.quote.price >= resistance * 0.99
    signal_level = "积极" if near_resistance and fund_score is not None and fund_score >= 60 else "观察"
    reason = (
        f"关注 {resistance:.2f} 附近是否放量站稳，量价热度评分（衍生） {fund_score}。"
        if resistance is not None and fund_score is not None
        else "压力位或量价热度证据当前不可用，等待证据重算后再判断突破。"
    )
    return _strategy_from_signal(
        analysis,
        "突破确认策略",
        SignalItem(
            title="压力突破确认",
            level=_quality_signal_level(signal_level, analysis),
            reason=reason,
        ),
        status=_quality_strategy_status(
            (
                "接近触发"
                if resistance is not None and fund_score is not None and analysis.quote.price >= resistance * 0.985
                else "等待"
            ),
            analysis,
        ),
        reference_price=f"压力位 {resistance:.2f}" if resistance is not None else "压力位待重算",
        invalidation="突破后快速跌回压力位下方",
        suitable_for="偏右侧确认的使用者",
    )


def _support_dip_card(analysis: AnalysisResult) -> StrategyCard:
    support = _available_level(analysis, "support", "support_available")
    reason = (
        f"价格靠近 {support:.2f} 时只适合观察承接，不能越跌越加。"
        if support is not None
        else "支撑位当前不可用，等待结构价位重算后再判断承接。"
    )
    return _strategy_from_signal(
        analysis,
        "支撑低吸策略",
        SignalItem(
            title="支撑区小仓观察",
            level=_quality_signal_level("谨慎", analysis),
            reason=reason,
        ),
        status=_quality_strategy_status(
            "接近触发" if support is not None and analysis.quote.price <= support * 1.03 else "等待",
            analysis,
        ),
        reference_price=f"支撑位 {support:.2f}" if support is not None else "支撑位待重算",
        invalidation=f"有效跌破 {support:.2f}" if support is not None else "支撑位待重算，不启用结构失效条件",
        suitable_for="只做小仓位试错的使用者",
    )


def _t_range_card(analysis: AnalysisResult, order_pressure: OrderPressure) -> StrategyCard:
    support = _available_level(analysis, "support", "support_available")
    resistance = _available_level(analysis, "resistance", "resistance_available")
    range_available = support is not None and resistance is not None
    return _strategy_from_signal(
        analysis,
        "做T区间策略",
        _t_plan_signal(analysis.t_plan if range_available else []),
        status=_quality_strategy_status("仅底仓适用" if range_available else "等待", analysis),
        reference_price=f"{support:.2f} - {resistance:.2f}" if range_available else "做T区间待重算",
        invalidation="当日波动过小或盘口卖压明显增强",
        suitable_for="已有可卖底仓的使用者",
        extra_evidence=[
            (
                order_pressure.summary
                if order_pressure.data_nature != "unavailable"
                else "盘口证据不可用，不据此调整做T区间。"
            ),
            f"数据质量 {analysis.data_quality.level}，信号已自动降权。",
        ],
    )


def _risk_stop_card(analysis: AnalysisResult) -> StrategyCard:
    ma20 = _available_level(analysis, "ma20", "ma20_available")
    structural_evidence_available = (
        ma20 is not None
        and _available_level(analysis, "support", "support_available") is not None
        and _available_level(analysis, "resistance", "resistance_available") is not None
    )
    signal = _first_signal(
        analysis.sell_points if structural_evidence_available else [],
        title="暂无清晰卖点",
        level="观察",
        reason="上游暂未给出卖点，继续跟踪5日线、20日线和支撑位。",
    )
    return _strategy_from_signal(
        analysis,
        "风险止损策略",
        signal,
        status=_quality_strategy_status("触发" if analysis.risk_level in {"中等风险", "高风险"} else "备用", analysis),
        reference_price=f"20日线 {ma20:.2f}" if ma20 is not None else "20日线待重算",
        invalidation="重新站回5日线且量价热度（衍生）改善",
        suitable_for="优先控制回撤的使用者",
    )


def _first_signal(items: list[SignalItem], *, title: str, level: str, reason: str) -> SignalItem:
    if items:
        return items[0]
    return SignalItem(title=title, level=level, reason=reason)


def _t_plan_signal(items: list[SignalItem]) -> SignalItem:
    for item in items:
        if "高抛" in item.title:
            return item
    if items:
        return items[-1]
    return SignalItem(
        title="暂无做T区间",
        level="谨慎",
        reason="上游暂未形成有效做T区间，先等待日内波动和盘口信息更清晰。",
    )


def _strategy_from_signal(
    analysis: AnalysisResult,
    name: str,
    signal: SignalItem,
    *,
    status: str,
    reference_price: str,
    invalidation: str,
    suitable_for: str,
    extra_evidence: list[str] | None = None,
) -> StrategyCard:
    return StrategyCard(
        symbol=f"{analysis.quote.code}.{analysis.quote.market}",
        updated_at=analysis.quote.timestamp,
        name=name,
        status=status,
        level=signal.level,
        trigger_conditions=[signal.title],
        current_evidence=[signal.reason, *(extra_evidence or [])],
        reference_price=reference_price,
        invalidation=invalidation,
        suitable_for=suitable_for,
        risk_note="策略卡只用于个股研究辅助，不代表确定性买卖点。",
    )


def _quality_strategy_status(status: str, analysis: AnalysisResult) -> str:
    score = analysis.data_quality.score
    if score < 50:
        return SEVERE_QUALITY_STRATEGY_STATUS.get(status, "暂停")
    if score < 70:
        return WEAK_QUALITY_STRATEGY_STATUS.get(status, status)
    return status


def _quality_signal_level(level: str, analysis: AnalysisResult) -> str:
    score = analysis.data_quality.score
    if score < 50:
        return "风险" if level != "风险" else level
    if score < 70 and level in {"积极", "观察"}:
        return "谨慎"
    return level


def _available_level(analysis: AnalysisResult, value_field: str, availability_field: str) -> float | None:
    if getattr(analysis, availability_field, False) is not True:
        return None
    value = finite_float(getattr(analysis, value_field, None))
    return value if value is not None and value > 0 else None


def _available_fund_score(fund_flow: FundFlowAnalysis) -> int | None:
    fallback_nature = "derived" if fund_flow.available else "unavailable"
    if not fund_flow.available or getattr(fund_flow, "data_nature", fallback_nature) == "unavailable":
        return None
    value = finite_float(fund_flow.overall_score)
    return round(value) if value is not None and 0 <= value <= 100 else None
