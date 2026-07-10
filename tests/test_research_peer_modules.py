from __future__ import annotations

from datetime import datetime

from app.models.schemas import PeerSampleInfo, Quote
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_features import build_feature_snapshot
from app.services.research_peer import build_peer_comparison_report
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote, make_stock_info


def test_peer_comparison_returns_safe_report_without_valid_peer_quotes() -> None:
    analysis, bundle, feature = _peer_inputs(
        peer_quotes=[_peer_quote("600001", "无效同行", price=0, change_pct=5.0)],
        industry="白酒",
    )

    report = build_peer_comparison_report(analysis, bundle, feature)

    assert report.industry == "白酒"
    assert report.sample_count == 0
    assert report.summary == "同行样本不足，暂以个股自身历史和行业涨跌背景为主。"
    assert report.sample_status.status == "degraded"
    assert report.warnings == ["同行行情样本未通过有效性校验。"]
    assert report.risks == [
        "同行行情样本未通过有效性校验。",
        "同行报价样本不足，同行估值和强弱分位需要等待缓存积累。",
    ]


def test_peer_comparison_distinguishes_source_failure_from_small_peer_group() -> None:
    warning = "白酒同行股票池暂不可用。"
    analysis, bundle, feature = _peer_inputs(
        peer_sample=PeerSampleInfo(status="unavailable", warning=warning),
        industry="白酒",
    )

    report = build_peer_comparison_report(analysis, bundle, feature)

    assert report.sample_count == 0
    assert report.sample_status.status == "unavailable"
    assert report.summary == "同行数据源暂不可用，当前仅基于个股自身历史和行业背景判断。"
    assert report.warnings == [warning]
    assert report.risks[0] == warning


def test_peer_comparison_keeps_partial_quotes_and_surfaces_degradation() -> None:
    warning = "白酒同行行情样本部分缺失，成功 2/3 个。"
    analysis, bundle, feature = _peer_inputs(
        peer_quotes=[_peer_quote("600001", "同行A"), _peer_quote("600002", "同行B")],
        peer_sample=PeerSampleInfo(status="degraded", requested_count=3, missing_count=1, warning=warning),
        industry="白酒",
    )

    report = build_peer_comparison_report(analysis, bundle, feature)

    assert report.sample_count == 2
    assert report.sample_status.status == "degraded"
    assert report.warnings == [warning]
    assert report.risks[0] == warning


def test_peer_comparison_calculates_strength_metrics_and_leaders() -> None:
    analysis, bundle, feature = _peer_inputs(
        change_pct=1.0,
        amount=3_000_000_000,
        peer_quotes=[
            _peer_quote("600001", "同行A", change_pct=-1.0, amount=200_000_000),
            _peer_quote("600002", "同行B", change_pct=0.5, amount=0),
            _peer_quote("600003", "同行C", change_pct=2.0, amount=100_000_000),
            _peer_quote("600004", "无效同行", price=0, change_pct=9.0, amount=900_000_000),
        ],
        peer_pe_percentile=55,
    )

    report = build_peer_comparison_report(analysis, bundle, feature)

    assert report.sample_count == 3
    assert report.strength_position == "强弱中等"
    assert "66.7% 分位" in report.summary
    assert report.metrics == [
        "个股涨跌幅 1.00%，同行均值 0.50%。",
        "个股成交额 30.0 亿。",
        "同行平均成交额 1.5 亿。",
        "同行PE分位 55.0%。",
    ]
    assert report.leaders[0] == "同行C600003：2.00%"


def test_peer_comparison_reports_high_valuation_and_weak_relative_strength_risks() -> None:
    analysis, bundle, feature = _peer_inputs(
        change_pct=-2.0,
        peer_quotes=[
            _peer_quote("600001", "同行A", change_pct=-1.0),
            _peer_quote("600002", "同行B", change_pct=0.5),
            _peer_quote("600003", "同行C", change_pct=1.0),
        ],
        peer_pe_percentile=85,
    )

    report = build_peer_comparison_report(analysis, bundle, feature)

    assert report.valuation_position == "估值相对靠前"
    assert report.strength_position == "强弱相对靠后"
    assert report.risks == [
        "PE相对同行偏高，追高需要更严格确认。",
        "涨跌幅相对同行偏弱，暂不宜急着上调评级。",
    ]


def test_peer_comparison_uses_default_risk_when_no_peer_warning_triggers() -> None:
    analysis, bundle, feature = _peer_inputs(
        change_pct=2.0,
        peer_quotes=[
            _peer_quote("600001", "同行A", change_pct=-1.0),
            _peer_quote("600002", "同行B", change_pct=0.5),
        ],
        peer_pe_percentile=35,
    )

    report = build_peer_comparison_report(analysis, bundle, feature)

    assert report.valuation_position == "估值中等"
    assert report.strength_position == "强弱相对靠前"
    assert report.risks == ["同行对比暂未发现压倒性风险，仍需结合趋势和估值锚。"]


def _peer_inputs(
    *,
    change_pct: float = 1.0,
    amount: float = 1_300_000_000,
    peer_quotes: list[Quote] | None = None,
    peer_sample: PeerSampleInfo | None = None,
    industry: str = "测试行业",
    peer_pe_percentile: float | None = None,
):
    quote = make_quote(change_pct=change_pct, turnover_rate=4.0).model_copy(update={"amount": amount})
    klines = [
        make_kline(
            date=f"2026-05-{index + 1:02d}",
            close=100 + index * 0.5,
            high=101 + index * 0.5,
            low=99 + index * 0.5,
            volume=1800 + index * 20,
        )
        for index in range(40)
    ]
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
    analysis = build_analysis(
        quote,
        klines,
        stock_profile=make_stock_info().model_copy(update={"industry": industry}),
        peer_quotes=peer_quotes or [],
        peer_sample=peer_sample,
        data_quality=quality,
    )
    bundle = build_stock_insight_bundle(analysis)
    if peer_pe_percentile is not None:
        bundle = bundle.model_copy(update={"valuation": bundle.valuation.model_copy(update={"peer_pe_percentile": peer_pe_percentile})})
    feature = build_feature_snapshot(analysis, bundle).model_copy(update={"amount": amount})
    return analysis, bundle, feature


def _peer_quote(
    code: str,
    name: str,
    *,
    price: float = 10.0,
    change_pct: float = 0.0,
    amount: float | None = 100_000_000,
) -> Quote:
    return Quote(
        code=code,
        name=name,
        market="SH",
        price=price,
        prev_close=price - 0.2 if price else 0,
        open=price - 0.1 if price else 0,
        high=price + 0.2 if price else 0,
        low=price - 0.3 if price else 0,
        volume=100000,
        amount=amount,
        change=0.2,
        change_pct=change_pct,
        turnover_rate=1.5,
        timestamp="2026-05-13 10:00:00",
        source="测试行情",
    )
