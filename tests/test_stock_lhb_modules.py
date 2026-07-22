from __future__ import annotations

from datetime import datetime

from app.models.schemas import AbnormalEventItem, AbnormalEventSummary
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.stock_lhb import build_lhb_summary
from tests.factories import make_kline, make_quote


def test_lhb_summary_uses_default_reason_and_action_when_no_candidate_signal() -> None:
    analysis = _analysis(change_pct=0.5, turnover_rate=3.0, amount=100_000_000)

    summary = build_lhb_summary(analysis)

    assert summary.available is False
    assert summary.capability_status == "unavailable"
    assert summary.score == 0
    assert summary.level == "不可用"
    assert "不会根据行情推断上榜事实" in summary.summary
    assert summary.reasons == []
    assert summary.action_items == ["当前未检测到需要额外核查的强量价异动。"]
    assert summary.missing_data == ["龙虎榜上榜日期", "买入席位", "卖出席位", "净买入额", "游资/机构标签"]


def test_lhb_summary_collects_move_turnover_and_amount_actions() -> None:
    analysis = _analysis(change_pct=8.2, turnover_rate=13.5, amount=1_500_000_000)

    summary = build_lhb_summary(analysis)

    assert summary.score == 0
    assert "仅为异动核查建议" in summary.summary
    assert "不构成龙虎榜证据" in summary.summary
    assert summary.reasons == [
        "量价异动：当日涨跌幅 8.20%。",
        "量价异动：换手率 13.50%，成交活跃。",
    ]
    assert summary.action_items == [
        "异动核查建议：如需判断是否上榜，请以交易所正式龙虎榜为准。",
        "异动核查建议：若交易所确认上榜，再核对买卖席位、净买入额及机构/游资方向。",
        "异动核查建议：若正式榜单可查，再比较净买入额与成交额，避免只看绝对金额。",
    ]


def test_lhb_summary_adds_strong_move_bonus_and_weak_trend_action() -> None:
    analysis = _analysis(change_pct=-9.5, turnover_rate=4.0, amount=2_000_000_000, trend_score=40)

    summary = build_lhb_summary(analysis)

    assert summary.score == 0
    assert summary.reasons == ["量价异动：当日涨跌幅 -9.50%。"]
    assert summary.action_items[-1] == "异动核查建议：趋势偏弱时，优先判断量价变化是短暂修复还是抛压释放。"


def test_lhb_summary_includes_top_three_abnormal_events_as_reasons() -> None:
    analysis = _analysis(change_pct=0.5, turnover_rate=3.0, amount=100_000_000)
    abnormal = AbnormalEventSummary(
        symbol="600519.SH",
        updated_at="2026-05-13 10:00:00",
        score=70,
        level="偏强",
        main_signal="测试异动",
        events=[_event(f"异动{index}") for index in range(4)],
    )

    summary = build_lhb_summary(analysis, abnormal)

    assert summary.score == 0
    assert summary.reasons == ["行情异动：异动0。", "行情异动：异动1。", "行情异动：异动2。"]
    assert all("异动3" not in item for item in summary.reasons)


def _analysis(*, change_pct: float, turnover_rate: float, amount: float, trend_score: int | None = None):
    quote = make_quote(change_pct=change_pct, turnover_rate=turnover_rate).model_copy(update={"amount": amount})
    klines = [
        make_kline(
            date=f"2026-05-{index + 1:02d}",
            close=100 + index * 0.2,
            high=101 + index * 0.2,
            low=99 + index * 0.2,
            volume=1000 + index * 10,
        )
        for index in range(40)
    ]
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
    analysis = build_analysis(quote, klines, data_quality=quality)
    if trend_score is not None:
        analysis = analysis.model_copy(update={"trend_score": trend_score})
    return analysis


def _event(title: str) -> AbnormalEventItem:
    return AbnormalEventItem(
        date="2026-05-13",
        title=title,
        level="观察",
        direction="中性",
        description="测试异动",
    )
