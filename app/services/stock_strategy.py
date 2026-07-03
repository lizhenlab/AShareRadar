from __future__ import annotations

from app.models.schemas import AnalysisResult, FundFlowAnalysis, OrderPressure, SignalItem, StrategyCard


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
    signal = _first_signal(
        analysis.buy_points,
        title="暂无清晰买点",
        level="谨慎",
        reason="上游暂未给出有效买点，先等待趋势和数据质量恢复。",
    )
    return _strategy_from_signal(
        "趋势回踩策略",
        signal,
        status=_quality_strategy_status("满足" if analysis.trend_score >= 65 and analysis.quote.price >= analysis.ma5 else "等待", analysis),
        reference_price=f"5日线 {analysis.ma5:.2f}",
        invalidation=f"跌破支撑 {analysis.support:.2f}",
        suitable_for="能接受等待确认的新手和波段观察者",
    )


def _breakout_confirmation_card(analysis: AnalysisResult, fund_flow: FundFlowAnalysis) -> StrategyCard:
    signal_level = "积极" if analysis.quote.price >= analysis.resistance * 0.99 and fund_flow.overall_score >= 60 else "观察"
    return _strategy_from_signal(
        "突破确认策略",
        SignalItem(
            title="压力突破确认",
            level=_quality_signal_level(signal_level, analysis),
            reason=f"关注 {analysis.resistance:.2f} 附近是否放量站稳，资金面评分 {fund_flow.overall_score}。",
        ),
        status=_quality_strategy_status("接近触发" if analysis.quote.price >= analysis.resistance * 0.985 else "等待", analysis),
        reference_price=f"压力位 {analysis.resistance:.2f}",
        invalidation="突破后快速跌回压力位下方",
        suitable_for="偏右侧确认的使用者",
    )


def _support_dip_card(analysis: AnalysisResult) -> StrategyCard:
    return _strategy_from_signal(
        "支撑低吸策略",
        SignalItem(
            title="支撑区小仓观察",
            level=_quality_signal_level("谨慎", analysis),
            reason=f"价格靠近 {analysis.support:.2f} 时只适合观察承接，不能越跌越加。",
        ),
        status=_quality_strategy_status("接近触发" if analysis.support and analysis.quote.price <= analysis.support * 1.03 else "等待", analysis),
        reference_price=f"支撑位 {analysis.support:.2f}",
        invalidation=f"有效跌破 {analysis.support:.2f}",
        suitable_for="只做小仓位试错的使用者",
    )


def _t_range_card(analysis: AnalysisResult, order_pressure: OrderPressure) -> StrategyCard:
    return _strategy_from_signal(
        "做T区间策略",
        _t_plan_signal(analysis.t_plan),
        status=_quality_strategy_status("仅底仓适用", analysis),
        reference_price=f"{analysis.support:.2f} - {analysis.resistance:.2f}",
        invalidation="当日波动过小或盘口卖压明显增强",
        suitable_for="已有可卖底仓的使用者",
        extra_evidence=[order_pressure.summary, f"数据质量 {analysis.data_quality.level}，信号已自动降权。"],
    )


def _risk_stop_card(analysis: AnalysisResult) -> StrategyCard:
    signal = _first_signal(
        analysis.sell_points,
        title="暂无清晰卖点",
        level="观察",
        reason="上游暂未给出卖点，继续跟踪5日线、20日线和支撑位。",
    )
    return _strategy_from_signal(
        "风险止损策略",
        signal,
        status=_quality_strategy_status("触发" if analysis.risk_level in {"中等风险", "高风险"} else "备用", analysis),
        reference_price=f"20日线 {analysis.ma20:.2f}",
        invalidation="重新站回5日线且资金面改善",
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
