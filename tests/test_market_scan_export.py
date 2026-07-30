from __future__ import annotations

from datetime import datetime
from io import BytesIO

from openpyxl import load_workbook
import pytest

from app.models.market_scan import MarketScanResultItem, MarketScanResultPage, MarketScanRun
from app.services.market_scan_export import MarketScanExportFilters, build_market_scan_workbook
from app.services.market_scan_manager import MarketScanManager


EXPORTED_AT = datetime.fromisoformat("2026-07-29T16:20:30+08:00")


def test_workbook_contains_complete_ranked_snapshot_and_audit_metadata() -> None:
    page = _page([_item()])
    filters = MarketScanExportFilters(
        market=("SZ", "SH"),
        industry=("银行", "半导体"),
        is_st=False,
        is_new=False,
        min_score=60,
        max_score=95,
        min_trend_score=50,
        max_trend_score=90,
        min_change_pct=-2.5,
        max_change_pct=9.5,
        min_turnover_rate=1,
        max_turnover_rate=30,
        min_amount=1_000_000,
        max_amount=500_000_000,
        min_data_quality_score=80,
        max_data_quality_score=99,
        keyword="000001",
        sort=("amount", "score", "symbol"),
        order=("desc", "desc", "asc"),
    )

    exported = build_market_scan_workbook(page, filters, exported_at=EXPORTED_AT)
    workbook = load_workbook(BytesIO(exported.content), data_only=False)
    results = workbook["榜单"]
    info = {row[0].value: row[1].value for row in workbook["导出信息"].iter_rows(min_row=2)}

    assert workbook.sheetnames == ["榜单", "评分明细", "导出信息"]
    assert exported.filename == "AShareRadar-market-scan-2026-07-29-official-run-12.xlsx"
    assert exported.row_count == 1
    assert results.freeze_panes == "A2"
    assert results.auto_filter.ref == "A1:AD2"
    assert results["A1"].value == "排名"
    assert results["B2"].value == "000001"
    assert results["B2"].number_format == "@"
    assert results["D2"].value == "'=HYPERLINK(\"https://example.invalid\")"
    assert results["F2"].value == "'+银行"
    assert results["V2"].value == "'@趋势和量价配合"
    assert results["W2"].value == "错误文本"
    assert results["AC2"].value == "'-provider_reason"
    assert all(cell.data_type != "f" for row in results.iter_rows() for cell in row)
    details = workbook["评分明细"]
    assert details["B2"].value == "000001"
    assert details["B2"].number_format == "@"
    assert details["D2"].value == 92
    assert details["E2"].value == pytest.approx(91.123456)
    assert details["K2"].value == '{"amount":2.5}'
    assert details["Q2"].value == '[["raw_score","desc"],["symbol","asc"]]'
    assert all(cell.data_type != "f" for row in details.iter_rows() for cell in row)
    assert info["批次 ID"] == 12
    assert info["榜单类型"] == "盘后正式"
    assert info["筛选市场"] == "SZ、SH"
    assert info["筛选行业"] == "银行、半导体"
    assert info["ST 条件"] == "排除 ST"
    assert info["强势分范围"] == "60 ～ 95"
    assert info["趋势分范围"] == "50 ～ 90"
    assert info["涨跌幅范围"] == "'-2.5 ～ 9.5"
    assert info["换手率范围"] == "1 ～ 30"
    assert info["成交额范围"] == "1000000 ～ 500000000"
    assert info["数据质量范围"] == "80 ～ 99"
    assert info["排序"] == "成交额（降序） → 短线强势分（降序） → 股票代码（升序）"
    assert info["导出条数"] == 1
    assert info["数据说明"] == "仅导出已持久化榜单快照，不会重新获取行情或重新计算。"


def test_workbook_supports_an_empty_filtered_result_without_an_invalid_table() -> None:
    page = _page([])

    exported = build_market_scan_workbook(page, MarketScanExportFilters(), exported_at=EXPORTED_AT)
    workbook = load_workbook(BytesIO(exported.content))

    assert exported.row_count == 0
    assert workbook["榜单"].max_row == 1
    assert workbook["榜单"].tables == {}
    assert workbook["榜单"].auto_filter.ref == "A1:AD1"


def test_workbook_rejects_a_truncated_result_page() -> None:
    page = _page([_item()]).model_copy(update={"total": 2})

    with pytest.raises(ValueError, match="读取不完整"):
        build_market_scan_workbook(page, MarketScanExportFilters(), exported_at=EXPORTED_AT)


def test_manager_exports_only_published_runs_and_forwards_every_filter() -> None:
    page = _page([_item()])
    filters = MarketScanExportFilters(
        status=None,
        market=("SZ", "SH"),
        industry=("  银行   服务 ", "电力"),
        is_st=False,
        min_score=60,
        max_score=98,
        min_trend_score=50,
        max_trend_score=95,
        min_change_pct=-3,
        max_change_pct=10,
        min_turnover_rate=1,
        max_turnover_rate=25,
        min_amount=1_000_000,
        max_amount=900_000_000,
        min_data_quality_score=70,
        max_data_quality_score=100,
        keyword=" 000001   平安 ",
        sort=("score", "amount", "symbol"),
        order=("desc", "desc", "asc"),
    )
    manager = object.__new__(MarketScanManager)
    calls: list[tuple[int, dict[str, object]]] = []
    manager.run = lambda run_id: page.run  # type: ignore[method-assign]
    manager.results = lambda run_id, **kwargs: calls.append((run_id, kwargs)) or page  # type: ignore[method-assign]
    manager._now = lambda: EXPORTED_AT

    exported = manager.export_results(page.run.id, filters=filters)

    assert exported.row_count == 1
    assert calls == [
        (
            page.run.id,
            {
                "page": 1,
                "page_size": 1,
                "status": None,
                "market": ("SZ", "SH"),
                "industry": ("银行 服务", "电力"),
                "is_st": False,
                "is_new": None,
                "min_score": 60,
                "max_score": 98,
                "min_trend_score": 50,
                "max_trend_score": 95,
                "min_change_pct": -3,
                "max_change_pct": 10,
                "min_turnover_rate": 1,
                "max_turnover_rate": 25,
                "min_amount": 1_000_000,
                "max_amount": 900_000_000,
                "min_data_quality_score": 70,
                "max_data_quality_score": 100,
                "keyword": "000001 平安",
                "sort": ("score", "amount", "symbol"),
                "order": ("desc", "desc", "asc"),
            },
        )
    ]

    manager.run = lambda run_id: page.run.model_copy(update={"status": "running"})  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="只有已发布"):
        manager.export_results(page.run.id, filters=filters)


def _page(items: list[MarketScanResultItem]) -> MarketScanResultPage:
    return MarketScanResultPage(
        run=_run(),
        items=items,
        total=len(items),
        page=1,
        page_size=max(1, len(items)),
        page_count=1 if items else 0,
    )


def _run() -> MarketScanRun:
    return MarketScanRun(
        id=12,
        status="success",
        trigger="manual",
        mode="official",
        rule_version="full-market-scan-v3:abcdef12",
        as_of="2026-07-29 16:00:00",
        data_date="2026-07-29",
        quote_date="2026-07-29",
        scope="SH/SZ/BJ listed A-shares",
        stock_pool_source="akshare",
        total_count=1,
        excluded_count=0,
        processed_count=1,
        success_count=1,
        missing_count=0,
        skipped_count=0,
        retry_count=0,
        progress_pct=100,
        coverage_pct=100,
        created_at="2026-07-29 16:00:00",
        updated_at="2026-07-29 16:10:00",
        started_at="2026-07-29 16:00:01",
        finished_at="2026-07-29 16:10:00",
        duration_ms=599_000,
    )


def _item() -> MarketScanResultItem:
    return MarketScanResultItem(
        run_id=12,
        symbol="SZ000001",
        code="000001",
        market="SZ",
        name='=HYPERLINK("https://example.invalid")',
        industry="+银行",
        list_date="1991-04-03",
        status="success",
        rank=1,
        score=92,
        raw_score=91.123456,
        trend_score=88,
        leader_score=77,
        data_quality_score=96,
        price=12.34,
        change_pct=1.25,
        turnover_rate=2.5,
        volume_ratio=1.1,
        amount=1_234_567_890.12,
        tags=["放量", "突破"],
        reason="@趋势和量价配合",
        error="错误\x00文本",
        data_date="2026-07-29",
        quote_timestamp="2026-07-29 15:00:00",
        quote_source="tencent",
        kline_source="akshare",
        adjustment_mode="qfq",
        degradation_reasons=["-provider_reason"],
        updated_at="2026-07-29 16:08:00",
        score_details={
            "run_rule_version": "full-market-scan-v3:abcdef12",
            "score_spec_hash": "abcdef123456",
            "components": {
                "leader_score": {"base": 50, "trend_delta": 4, "rule_deltas": {"amount": 2.5}},
                "final_score": {"quality_penalty": 0.6, "base": 91.4, "rank_discount": 0.276544},
                "rank_refinement": {"score": 0.445, "weighted_terms": {"ma_alignment": 0.2}},
            },
            "ranking": {
                "tie_break": [["raw_score", "desc"], ["symbol", "asc"]],
                "tie_break_values": {"raw_score": 91.123456, "symbol": "000001.SZ"},
            },
        },
    )
