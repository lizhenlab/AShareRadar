from __future__ import annotations

from app.services.datahub_metadata_stock_pool import (
    STOCK_POOL_MIN_BASELINE_RETAIN_RATIO,
    StockPoolResolution,
    _stock_pool_shrinkage_diagnostic,
)
from app.utils.stock_pool import (
    diagnose_stock_pool_metadata,
    normalize_stock_industry_text,
    normalize_stock_pool_rows,
)
from tests.factories import make_stock_info


def _market_rows(market: str, count: int):
    prefixes = {"SH": 600000, "SZ": 1, "BJ": 920001}
    return [make_stock_info(code=f"{prefixes[market] + index:06d}", market=market) for index in range(count)]


def test_stock_pool_metadata_diagnostic_reports_market_level_industry_gap_without_blocking() -> None:
    rows = [
        *(item.model_copy(update={"industry": None}) for item in _market_rows("SH", 100)),
        *_market_rows("SZ", 100),
        *_market_rows("BJ", 100),
    ]

    resolution = StockPoolResolution.hit(rows, "provider-full-pool")

    assert resolution.resolved is True
    assert len(resolution.list_rows()) == 300
    assert resolution.metadata_diagnostic is not None
    diagnostic = resolution.metadata_diagnostic
    assert diagnostic.degraded is True
    sh = next(item for item in diagnostic.markets if item.scope == "SH")
    assert sh.industry_count == 0
    assert sh.total_count == 100
    assert "SH 行业完整率 0/100" in diagnostic.summary()


def test_stock_pool_metadata_normalization_removes_placeholder_fields() -> None:
    rows = normalize_stock_pool_rows(
        [
            make_stock_info().model_copy(
                update={
                    "industry": " 　未知 ",
                    "list_date": " -- ",
                }
            )
        ]
    )

    assert len(rows) == 1
    assert rows[0].industry is None
    assert rows[0].list_date is None
    diagnostic = diagnose_stock_pool_metadata(rows)
    assert diagnostic.overall.industry_count == 0
    assert diagnostic.overall.list_date_count == 0
    assert diagnostic.degraded is True


def test_stock_pool_industry_normalization_removes_exchange_classification_prefixes() -> None:
    assert normalize_stock_industry_text("C制造业") == "制造业"
    assert normalize_stock_industry_text("J66 货币金融服务") == "货币金融服务"
    assert normalize_stock_industry_text("AI软件服务") == "AI软件服务"


def test_stock_pool_metadata_diagnostic_treats_invalid_date_as_incomplete() -> None:
    row = make_stock_info().model_copy(update={"list_date": "2026-02-30"})

    diagnostic = diagnose_stock_pool_metadata([row])

    assert diagnostic.overall.list_date_count == 0
    assert any("上市日期完整率" in issue for issue in diagnostic.issues)


def test_stock_pool_shrinkage_guard_applies_98_percent_per_market() -> None:
    baseline = [*_market_rows("SH", 100), *_market_rows("SZ", 100), *_market_rows("BJ", 100)]
    candidate = [*baseline[3:100], *baseline[100:]]

    diagnostic = _stock_pool_shrinkage_diagnostic(
        candidate,
        baseline,
        authoritative_min_count=100,
        minimum_market_counts=(("BJ", 1), ("SH", 1), ("SZ", 1)),
    )

    assert STOCK_POOL_MIN_BASELINE_RETAIN_RATIO == 0.98
    assert diagnostic is not None
    assert "SH 97/100" in diagnostic
    assert "总量" not in diagnostic


def test_stock_pool_shrinkage_guard_accepts_exact_98_percent_boundary() -> None:
    baseline = [*_market_rows("SH", 100), *_market_rows("SZ", 100), *_market_rows("BJ", 100)]
    candidate = [*baseline[2:100], *baseline[100:]]

    diagnostic = _stock_pool_shrinkage_diagnostic(
        candidate,
        baseline,
        authoritative_min_count=100,
        minimum_market_counts=(("BJ", 1), ("SH", 1), ("SZ", 1)),
    )

    assert diagnostic is None


def test_stock_pool_shrinkage_guard_keeps_fixed_floor_behavior_without_baseline() -> None:
    candidate = [*_market_rows("SH", 10), *_market_rows("SZ", 10), *_market_rows("BJ", 10)]

    diagnostic = _stock_pool_shrinkage_diagnostic(
        candidate,
        [],
        authoritative_min_count=100,
        minimum_market_counts=(("BJ", 1), ("SH", 1), ("SZ", 1)),
    )

    assert diagnostic is None
