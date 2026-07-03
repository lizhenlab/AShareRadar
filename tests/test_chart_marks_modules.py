from __future__ import annotations

import math
from types import SimpleNamespace

from app.models.schemas import AbnormalEventItem, AbnormalEventSummary, StockEventItem, StockEventSummary
from app.services.analysis import build_analysis
from app.services.chart_marks import build_chart_marks_from_context
from app.services.data_quality import build_data_quality
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote


def test_chart_mark_categories_follow_visible_marks_after_limit() -> None:
    bundle = _bundle(
        abnormal_events=[
            AbnormalEventItem(date="2026-05-13", title="放量异动", level="积极", direction="up", description="成交放大")
        ],
        events=[
            StockEventItem(date="2026-05-12", title="行业走强", category="行业", level="积极", description="行业上涨", source="测试")
        ],
    )

    summary = build_chart_marks_from_context(_datahub(), "600519.SH", bundle, limit=1)

    assert [item.category for item in summary.marks] == ["异动"]
    assert summary.categories == ["异动"]


def test_chart_marks_filter_regular_events_before_capping_sample() -> None:
    mixed_events = [
        StockEventItem(date=f"2026-05-{day:02d}", title=f"异动{day}", category="异动", level="观察", description="重复异动", source="测试")
        for day in range(1, 7)
    ]
    mixed_events.append(
        StockEventItem(date="2026-05-07", title="行业轮动", category="行业", level="积极", description="非异动事件", source="测试")
    )
    bundle = _bundle(events=mixed_events)

    summary = build_chart_marks_from_context(_datahub(), "600519.SH", bundle, limit=10)

    assert [item.label for item in summary.marks] == ["行业轮动"]
    assert summary.categories == ["行业"]


def test_chart_marks_treat_negative_internal_limit_as_empty() -> None:
    bundle = _bundle(
        abnormal_events=[
            AbnormalEventItem(date="2026-05-13", title="放量异动", level="积极", direction="up", description="成交放大")
        ]
    )

    summary = build_chart_marks_from_context(_datahub(), "600519.SH", bundle, limit=-1)

    assert summary.marks == []
    assert summary.categories == []


def test_chart_marks_filter_invisible_dirty_events_before_limit() -> None:
    bundle = _bundle(
        abnormal_events=[
            AbnormalEventItem(date="bad-date", title="nan", level="积极", direction="up", description="nan"),
            AbnormalEventItem(date="2026-05-13", title="放量异动", level="积极", direction="up", description="成交放大"),
        ]
    )

    summary = build_chart_marks_from_context(_datahub(), "600519.SH", bundle, limit=1)

    assert [item.label for item in summary.marks] == ["放量异动"]
    assert summary.categories == ["异动"]
    assert summary.marks[0].visible is True


def test_chart_marks_sanitize_note_text_price_and_visibility_before_summary() -> None:
    dirty_note = SimpleNamespace(
        trade_date="bad-date",
        created_at="2026-05-13 10:00:00",
        price=math.inf,
        note_type="nan",
        content="nan",
        color=" ",
        visible=True,
    )
    valid_note = SimpleNamespace(
        trade_date="2026-05-13",
        created_at="2026-05-13 10:00:00",
        price=1288.5,
        note_type=" 复盘 ",
        content=" 关注支撑 ",
        color="#111827",
        visible=True,
    )

    summary = build_chart_marks_from_context(_datahub([dirty_note, valid_note]), "600519.SH", _bundle(), limit=1)

    assert len(summary.marks) == 1
    assert summary.marks[0].label == "复盘"
    assert summary.marks[0].description == "关注支撑"
    assert summary.marks[0].price == 1288.5
    assert summary.marks[0].anchor_price_type == "manual"
    assert summary.categories == ["笔记"]


def _bundle(
    *,
    abnormal_events: list[AbnormalEventItem] | None = None,
    events: list[StockEventItem] | None = None,
):
    klines = [make_kline(date=f"2026-05-{index + 1:02d}", close=100 + index, volume=1000 + index) for index in range(30)]
    quote = make_quote(price=130, prev_close=128, change_pct=1.56)
    analysis = build_analysis(quote, klines, data_quality=build_data_quality(quote, klines))
    bundle = build_stock_insight_bundle(analysis)
    return bundle.model_copy(
        update={
            "abnormal_events": AbnormalEventSummary(
                symbol="600519.SH",
                updated_at="2026-05-13 15:00:00",
                score=50,
                level="观察",
                main_signal="测试",
                events=abnormal_events or [],
            ),
            "events": StockEventSummary(
                symbol="600519.SH",
                updated_at="2026-05-13 15:00:00",
                events=events or [],
                notes=[],
            ),
        }
    )


def _datahub(notes=None):
    return SimpleNamespace(cache=SimpleNamespace(stock_notes=lambda *_args, **_kwargs: notes or []))
