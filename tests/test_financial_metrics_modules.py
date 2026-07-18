from __future__ import annotations

from math import inf, nan

from app.services.financial_metrics import format_amount_text, liquidity_view, market_cap_view, pb_view, pe_view


def test_pe_and_pb_views_keep_normal_boundaries() -> None:
    assert pe_view(11.9) == (68, "偏强", "PE 较低，可能有安全边际，也可能反映增长预期不足。")
    assert pe_view(35) == (62, "偏强", "PE 处在较容易解释的区间。")
    assert pe_view(60) == (48, "中性", "PE 偏高，需要业绩增长继续配合。")
    assert pb_view(1.19) == (66, "偏强", "PB 较低，资产价格锚相对清晰。")
    assert pb_view(8) == (46, "中性", "PB 偏高，需要盈利能力或成长性支撑。")


def test_financial_views_treat_non_finite_values_as_field_errors() -> None:
    assert pe_view(nan)[0:2] == (50, "不可用")
    assert pb_view(inf)[0:2] == (50, "不可用")
    assert market_cap_view(nan)[0:2] == (50, "不可用")


def test_market_cap_view_rejects_non_positive_values() -> None:
    assert market_cap_view(0)[0:2] == (50, "不可用")
    assert market_cap_view(-1)[0:2] == (50, "不可用")


def test_liquidity_view_scores_amount_and_turnover_rules() -> None:
    assert liquidity_view(1_000_000_000, 2.0)[0:2] == (75, "偏强")
    assert liquidity_view(300_000_000, 16.0)[0:2] == (55, "中性")
    assert liquidity_view(79_000_000, 0.2)[0:2] == (34, "偏弱")


def test_liquidity_view_rejects_invalid_amount_without_fake_turnover_bonus() -> None:
    assert liquidity_view(0, 2.0) == (50, "不可用", "成交额字段异常或缺失，交易活跃度不可用。")
    assert liquidity_view(nan, 2.0)[0:2] == (50, "不可用")


def test_format_amount_text_handles_non_finite_values() -> None:
    assert format_amount_text(None) == "--"
    assert format_amount_text(nan) == "--"
    assert format_amount_text(inf) == "--"
    assert format_amount_text(-123_000_000) == "-1.2亿"
