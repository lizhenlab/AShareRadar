from __future__ import annotations

from types import SimpleNamespace

from app.services.research_breadth import MARKET_BREADTH_BANDS, _market_breadth_band, build_market_breadth_snapshot
from tests.factories import make_quote


def test_market_breadth_ignores_quotes_without_price_or_change_pct() -> None:
    snapshot = build_market_breadth_snapshot(
        [
            SimpleNamespace(price=0, change_pct=100),
            SimpleNamespace(price=10, change_pct="--"),
            make_quote(price=10.4, prev_close=10, high=10.5, low=9.9, change_pct=4.0),
        ]
    )

    assert snapshot.up_count == 1
    assert snapshot.down_count == 0
    assert snapshot.strong_count == 1
    assert snapshot.sample_count == 1
    assert snapshot.degraded is False
    assert snapshot.warnings == ()
    assert "样本 1 只" in snapshot.summary


def test_market_breadth_empty_snapshot_is_neutral_degraded_state() -> None:
    snapshot = build_market_breadth_snapshot([SimpleNamespace(price=0, change_pct=5), SimpleNamespace(price=10, change_pct=None)])

    assert snapshot.label == "市场宽度待确认"
    assert snapshot.score == 50
    assert snapshot.risk_adjustment == 0
    assert snapshot.sample_count == 0
    assert snapshot.degraded is False
    assert snapshot.summary == "市场宽度样本不足，环境判断暂以个股和行业为主。"


def test_market_breadth_source_failure_is_distinct_from_genuine_empty_sample() -> None:
    snapshot = build_market_breadth_snapshot(
        [],
        warnings=["市场宽度数据源请求失败，环境判断已降级。", "市场宽度数据源请求失败，环境判断已降级。"],
    )

    assert snapshot.label == "市场宽度数据降级"
    assert snapshot.score == 45
    assert snapshot.risk_adjustment == 0.05
    assert snapshot.sample_count == 0
    assert snapshot.degraded is True
    assert snapshot.warnings == ("市场宽度数据源请求失败，环境判断已降级。",)
    assert snapshot.summary == "市场宽度数据源暂不可用，环境判断已降级并以个股和行业为主。"


def test_market_breadth_partial_source_warning_reduces_positive_risk_credit() -> None:
    quotes = [make_quote(change_pct=4.0), make_quote(change_pct=2.0)]

    complete = build_market_breadth_snapshot(quotes)
    partial = build_market_breadth_snapshot(quotes, warnings=["市场宽度行情样本部分缺失，成功 2/3 个。"])

    assert partial.score == complete.score
    assert partial.risk_adjustment == round(complete.risk_adjustment + 0.03, 2)
    assert partial.degraded is True


def test_market_breadth_score_keeps_formula_components_explicit() -> None:
    snapshot = build_market_breadth_snapshot(
        [
            make_quote(change_pct=4.0),
            make_quote(change_pct=-4.0),
            make_quote(change_pct=1.0),
            make_quote(change_pct=-1.0),
        ]
    )

    assert snapshot.score == 45
    assert snapshot.label == "市场宽度中性"
    assert snapshot.strong_count == 1
    assert snapshot.weak_count == 1
    assert snapshot.avg_change_pct == 0


def test_market_breadth_band_boundaries_are_stable() -> None:
    assert _market_breadth_band(68).label == "市场宽度强"
    assert _market_breadth_band(56).label == "市场宽度偏暖"
    assert _market_breadth_band(44).label == "市场宽度偏冷"
    assert _market_breadth_band(32).label == "市场宽度弱"
    assert _market_breadth_band(45).label == "市场宽度中性"
    assert [band.name for band in MARKET_BREADTH_BANDS] == ["strong", "warm", "weak", "cold"]
