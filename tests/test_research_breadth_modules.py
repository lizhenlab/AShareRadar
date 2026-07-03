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
    assert "样本 1 只" in snapshot.summary


def test_market_breadth_empty_snapshot_is_neutral_degraded_state() -> None:
    snapshot = build_market_breadth_snapshot([SimpleNamespace(price=0, change_pct=5), SimpleNamespace(price=10, change_pct=None)])

    assert snapshot.label == "市场宽度待确认"
    assert snapshot.score == 50
    assert snapshot.risk_adjustment == 0
    assert snapshot.summary == "市场宽度样本不足，环境判断暂以个股和行业为主。"


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
