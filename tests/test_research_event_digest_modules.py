from __future__ import annotations

from datetime import datetime

from app.models.schemas import AbnormalEventItem, AbnormalEventSummary, StockEventItem, StockEventSummary
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_events import DEFAULT_WATCH_EVENT, build_event_digest_report
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote


def test_event_digest_risk_events_win_over_positive_events() -> None:
    insights = _insights_with_events(
        abnormal_events=[
            _abnormal_event(title="放量上涨", level="积极", direction="利好", description="量能放大。"),
            _abnormal_event(title="向下跳空", level="风险", direction="利空", description="跳空低开。"),
        ],
        stock_events=[
            _stock_event(title="分红预案", level="积极", description="分红预案稳定。"),
        ],
    )

    report = build_event_digest_report(insights)

    assert report.impact_label == "事件偏风险"
    assert any("向下跳空" in item for item in report.negative_events)
    assert any("放量上涨" in item for item in report.positive_events)


def test_event_digest_positive_label_requires_no_negative_events() -> None:
    insights = _insights_with_events(
        abnormal_events=[_abnormal_event(title="放量上涨", level="积极", direction="利好", description="量价配合。")],
        stock_events=[_stock_event(title="行业催化", level="积极", description="行业事件偏正面。")],
    )

    report = build_event_digest_report(insights)

    assert report.impact_label == "事件偏积极"
    assert report.negative_events == []
    assert len(report.positive_events) == 2


def test_event_digest_uses_default_watch_text_when_no_event_changes_conclusion() -> None:
    report = build_event_digest_report(_insights_with_events())

    assert report.impact_label == "事件待确认"
    assert report.watch_events == [DEFAULT_WATCH_EVENT]


def test_event_digest_missing_data_is_deduped_and_capped() -> None:
    insights = _insights_with_events(
        event_notes=["公告源", "研报源", "公告源", "融资融券", "交易所问询", "行业新闻"],
        lhb_missing=["龙虎榜席位", "龙虎榜席位"],
        abnormal_notes=["逐笔成交", "盘后公告"],
    )

    report = build_event_digest_report(insights)

    assert report.missing_data == ["公告源", "研报源", "融资融券", "交易所问询", "行业新闻", "龙虎榜席位"]


def _insights_with_events(
    *,
    abnormal_events: list[AbnormalEventItem] | None = None,
    stock_events: list[StockEventItem] | None = None,
    event_notes: list[str] | None = None,
    lhb_missing: list[str] | None = None,
    abnormal_notes: list[str] | None = None,
):
    quote = make_quote(change_pct=0.8)
    klines = [make_kline(date=f"2026-05-{index + 1:02d}", close=100 + index * 0.2, volume=1000 + index * 20) for index in range(40)]
    analysis = build_analysis(quote, klines, data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)))
    insights = build_stock_insight_bundle(analysis)
    return insights.model_copy(
        update={
            "abnormal_events": AbnormalEventSummary(
                symbol="600519.SH",
                updated_at="2026-05-13 10:00:00",
                score=50,
                level="中性",
                main_signal="测试事件",
                events=abnormal_events or [],
                notes=abnormal_notes or [],
            ),
            "events": StockEventSummary(
                symbol="600519.SH",
                updated_at="2026-05-13 10:00:00",
                events=stock_events or [],
                notes=event_notes or [],
                missing_sources=[],
                next_steps=[],
            ),
            "lhb": insights.lhb.model_copy(update={"missing_data": lhb_missing or []}),
        }
    )


def _abnormal_event(title: str, level: str, direction: str, description: str) -> AbnormalEventItem:
    return AbnormalEventItem(
        date="2026-05-13 10:00:00",
        title=title,
        level=level,
        direction=direction,
        description=description,
    )


def _stock_event(title: str, level: str, description: str) -> StockEventItem:
    return StockEventItem(
        date="2026-05-13",
        title=title,
        category="测试",
        level=level,
        description=description,
        source="测试源",
    )
