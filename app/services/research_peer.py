from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import AnalysisResult, FeatureSnapshot, PeerComparisonReport, PeerSampleInfo, Quote, StockInsightBundle
from app.utils.market_data import finite_float

PEER_LEADER_LIMIT = 3
HIGH_PEER_PE_PERCENTILE = 80
WEAK_STRENGTH_PERCENTILE = 35


@dataclass(frozen=True)
class PeerComparisonStats:
    peers: list[Quote]
    industry: str
    avg_change_pct: float
    avg_amount: float
    strength_percentile: float


def build_peer_comparison_report(analysis: AnalysisResult, insights: StockInsightBundle, feature: FeatureSnapshot) -> PeerComparisonReport:
    stats = _peer_comparison_stats(analysis)
    sample_status = _peer_report_sample_status(analysis, stats.peers)
    if not stats.peers:
        return _empty_peer_comparison_report(stats.industry, sample_status)
    warnings = _peer_warnings(sample_status)
    return PeerComparisonReport(
        industry=stats.industry,
        sample_count=len(stats.peers),
        valuation_position=_peer_position_label(insights.valuation.peer_pe_percentile, "估值"),
        strength_position=_peer_position_label(stats.strength_percentile, "强弱"),
        summary=_peer_summary(stats),
        metrics=_peer_metrics(analysis, insights, feature, stats),
        leaders=_peer_leaders(stats.peers),
        risks=list(dict.fromkeys([*warnings, *_peer_risks(insights, stats)])),
        sample_status=sample_status,
        warnings=warnings,
    )


def _peer_comparison_stats(analysis: AnalysisResult) -> PeerComparisonStats:
    peers = _valid_peer_quotes(analysis.peer_quotes)
    return PeerComparisonStats(
        peers=peers,
        industry=_peer_industry(analysis),
        avg_change_pct=_average_peer_change(peers),
        avg_amount=_average_peer_amount(peers),
        strength_percentile=_peer_strength_percentile(analysis.quote.change_pct, peers),
    )


def _valid_peer_quotes(peers: list[Quote]) -> list[Quote]:
    return [item for item in peers if _valid_peer_quote(item)]


def _valid_peer_quote(item: Quote) -> bool:
    price = finite_float(item.price)
    change_pct = finite_float(item.change_pct)
    amount = finite_float(item.amount) if item.amount is not None else 0
    return price is not None and price > 0 and change_pct is not None and amount is not None and amount >= 0


def _peer_industry(analysis: AnalysisResult) -> str:
    return analysis.stock_profile.industry if analysis.stock_profile and analysis.stock_profile.industry else "行业待确认"


def _average_peer_change(peers: list[Quote]) -> float:
    changes = [value for item in peers if (value := finite_float(item.change_pct)) is not None]
    return sum(changes) / len(changes) if changes else 0


def _average_peer_amount(peers: list[Quote]) -> float:
    amount_values = [value for item in peers if (value := finite_float(item.amount)) is not None and value > 0]
    return sum(amount_values) / len(amount_values) if amount_values else 0


def _peer_strength_percentile(change_pct: float, peers: list[Quote]) -> float:
    clean_change = finite_float(change_pct)
    if not peers or clean_change is None:
        return 0
    return sum(1 for item in peers if item.change_pct <= clean_change) / len(peers) * 100


def _empty_peer_comparison_report(industry: str, sample_status: PeerSampleInfo) -> PeerComparisonReport:
    warnings = _peer_warnings(sample_status)
    if sample_status.status == "unavailable":
        summary = "同行数据源暂不可用，当前仅基于个股自身历史和行业背景判断。"
    elif sample_status.status == "not_applicable":
        summary = "行业归属待确认，暂无法建立同行对比。"
    else:
        summary = "同行样本不足，暂以个股自身历史和行业涨跌背景为主。"
    return PeerComparisonReport(
        industry=industry,
        sample_count=0,
        summary=summary,
        risks=list(dict.fromkeys([*warnings, "同行报价样本不足，同行估值和强弱分位需要等待缓存积累。"])),
        sample_status=sample_status,
        warnings=warnings,
    )


def _peer_report_sample_status(analysis: AnalysisResult, valid_peers: list[Quote]) -> PeerSampleInfo:
    status = analysis.peer_sample
    if valid_peers and status.status == "not_requested":
        return PeerSampleInfo(status="available", requested_count=len(valid_peers))
    if not valid_peers and analysis.peer_quotes:
        warning = status.warning or "同行行情样本未通过有效性校验。"
        return status.model_copy(update={"status": "degraded", "warning": warning})
    return status


def _peer_warnings(status: PeerSampleInfo) -> list[str]:
    warning = " ".join(status.warning.split())[:160] if isinstance(status.warning, str) else ""
    return [warning] if warning else []


def _peer_summary(stats: PeerComparisonStats) -> str:
    return f"同行样本 {len(stats.peers)} 只，当前个股涨跌幅相对同行约处于 {stats.strength_percentile:.1f}% 分位。"


def _peer_metrics(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    stats: PeerComparisonStats,
) -> list[str]:
    change_pct = finite_float(analysis.quote.change_pct) or 0
    return [
        f"个股涨跌幅 {change_pct:.2f}%，同行均值 {stats.avg_change_pct:.2f}%。",
        _stock_amount_metric(feature),
        _peer_amount_metric(stats.avg_amount),
        _peer_pe_metric(insights),
    ]


def _stock_amount_metric(feature: FeatureSnapshot) -> str:
    return f"个股成交额 {feature.amount / 100000000:.1f} 亿。" if feature.amount else "个股成交额待确认。"


def _peer_amount_metric(avg_amount: float) -> str:
    return f"同行平均成交额 {avg_amount / 100000000:.1f} 亿。" if avg_amount else "同行成交额样本不足。"


def _peer_pe_metric(insights: StockInsightBundle) -> str:
    percentile = insights.valuation.peer_pe_percentile
    return f"同行PE分位 {percentile:.1f}%。" if percentile is not None else "同行PE分位待确认。"


def _peer_leaders(peers: list[Quote]) -> list[str]:
    leaders = sorted(peers, key=lambda item: (finite_float(item.change_pct) or 0, finite_float(item.amount) or 0), reverse=True)[
        :PEER_LEADER_LIMIT
    ]
    return [f"{item.name}{item.code}：{(finite_float(item.change_pct) or 0):.2f}%" for item in leaders]


def _peer_risks(insights: StockInsightBundle, stats: PeerComparisonStats) -> list[str]:
    risks = [
        risk
        for risk in (
            _peer_valuation_risk(insights),
            _peer_strength_risk(stats),
        )
        if risk
    ]
    return risks or ["同行对比暂未发现压倒性风险，仍需结合趋势和估值锚。"]


def _peer_valuation_risk(insights: StockInsightBundle) -> str | None:
    percentile = insights.valuation.peer_pe_percentile
    if percentile is not None and percentile >= HIGH_PEER_PE_PERCENTILE:
        return "PE相对同行偏高，追高需要更严格确认。"
    return None


def _peer_strength_risk(stats: PeerComparisonStats) -> str | None:
    if stats.strength_percentile <= WEAK_STRENGTH_PERCENTILE:
        return "涨跌幅相对同行偏弱，暂不宜急着上调评级。"
    return None


def _peer_position_label(percentile: float | None, prefix: str) -> str:
    if percentile is None:
        return f"{prefix}待确认"
    if percentile >= 75:
        return f"{prefix}相对靠前"
    if percentile <= 30:
        return f"{prefix}相对靠后"
    return f"{prefix}中等"
