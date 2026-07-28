from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from app.repositories.market_scan import MarketScanResultWrite, MarketScanSeed
from app.utils.errors import NotFoundError
from tests.factories import make_stock_info
from tests.market_scan_test_support import (
    _MarketScanHub,
    _configure_clean_full_market,
    _rule_version,
    _scanner,
    _wait_for_terminal,
)


def test_market_scan_retry_finalizes_fully_processed_interrupted_run(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    run = hub.cache.create_market_scan_run(
        trigger="manual",
        rule_version=_rule_version(hub),
        as_of="2026-07-17 16:30:00",
        data_date="2026-07-17",
        scope="test",
    )
    hub.cache.start_market_scan_run(run.id)
    seeds = [
        MarketScanSeed(symbol="600001.SH", code="600001", market="SH", name="沪市样本"),
        MarketScanSeed(symbol="000001.SZ", code="000001", market="SZ", name="深市样本"),
        MarketScanSeed(symbol="920066.BJ", code="920066", market="BJ", name="北交样本"),
    ]
    hub.cache.seed_market_scan_results(run.id, seeds, excluded_count=0)
    hub.cache.save_market_scan_result_batch(
        run.id,
        [
            MarketScanResultWrite(
                symbol=seed.symbol,
                status="success",
                score=80 - index,
                trend_score=70,
                leader_score=75,
                data_quality_score=90,
                price=10.0,
                metrics={"ma20": 9.5},
                reason="测试断点结果",
                data_date="2026-07-17",
                quote_timestamp="2026-07-17 15:00:00",
                quote_source="test",
                kline_source="test",
                adjustment_mode="qfq",
            )
            for index, seed in enumerate(seeds)
        ],
    )

    async def scenario():
        scanner = _scanner(hub, now=datetime(2026, 7, 20, 10, 30))
        assert await scanner.start() == 1
        assert scanner.run(run.id).status == "interrupted"
        retried = await scanner.retry_scan(run.id)
        final = await _wait_for_terminal(scanner, retried.run.id)
        original = scanner.run(run.id)
        await scanner.stop()
        return retried, final, original

    retried, final, original = asyncio.run(scenario())

    assert retried.accepted is True
    assert retried.run.retry_of_run_id == run.id
    assert final.status == "success"
    assert final.processed_count == final.total_count == 3
    assert hub.stock_pool_calls == 0
    ranked = hub.cache.market_scan_results(
        retried.run.id,
        page=1,
        page_size=10,
        status="success",
        market=None,
        industry=None,
        is_st=None,
        is_new=None,
        min_data_quality_score=None,
        keyword=None,
        sort="rank",
        order="asc",
    )
    assert [item.rank for item in ranked.items] == [1, 2, 3]
    assert original.status == "interrupted"
    assert original.finished_at is not None


def test_market_scan_retry_refreshes_only_pending_metadata(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    _configure_clean_full_market(hub)
    run = hub.cache.create_market_scan_run(
        trigger="manual",
        rule_version=_rule_version(hub),
        as_of="2026-07-17 16:30:00",
        data_date="2026-07-17",
        scope="test",
    )
    hub.cache.start_market_scan_run(run.id)
    hub.cache.seed_market_scan_results(
        run.id,
        [
            MarketScanSeed(
                symbol="600001.SH",
                code="600001",
                market="SH",
                name="保留沪市样本",
                industry="保留行业",
                list_date="1990-01-01",
                metadata_source="legacy-clean",
            ),
            MarketScanSeed(symbol="000001.SZ", code="000001", market="SZ", name="待刷新深市样本"),
            MarketScanSeed(symbol="920066.BJ", code="920066", market="BJ", name="待刷新北交样本"),
        ],
        excluded_count=0,
    )
    hub.cache.save_market_scan_result_batch(
        run.id,
        [
            MarketScanResultWrite(
                symbol="600001.SH",
                status="success",
                score=80,
                trend_score=75,
                leader_score=80,
                data_quality_score=90,
                price=10.0,
                metrics={"ma20": 9.5},
                reason="保留干净结果",
                data_date="2026-07-17",
                quote_timestamp="2026-07-17 15:00:00",
                quote_source="test",
                kline_source="test",
                adjustment_mode="qfq",
            ),
            MarketScanResultWrite(symbol="000001.SZ", status="missing", error="上市日期未知"),
            MarketScanResultWrite(symbol="920066.BJ", status="missing", error="上市日期未知"),
        ],
    )
    hub.cache.finish_market_scan_run(run.id, "degraded", message="等待重试")

    async def scenario():
        scanner = _scanner(hub)
        await scanner.start()
        retried = await scanner.retry_scan(run.id)
        final = await _wait_for_terminal(scanner, retried.run.id)
        page = scanner.results(
            final.id,
            page=1,
            page_size=10,
            status=None,
            market=None,
            industry=None,
            is_st=None,
            is_new=None,
            min_data_quality_score=None,
            keyword=None,
            sort="rank",
            order="asc",
        )
        await scanner.stop()
        return final, page

    final, page = asyncio.run(scenario())
    by_symbol = {item.symbol: item for item in page.items}

    assert final.total_count == 3
    assert final.success_count == 3
    assert hub.stock_pool_calls == 1
    assert by_symbol["600001.SH"].name == "保留沪市样本"
    assert by_symbol["600001.SH"].industry == "保留行业"
    assert by_symbol["600001.SH"].metadata_source == "legacy-clean"
    assert by_symbol["000001.SZ"].name == "深市样本"
    assert by_symbol["000001.SZ"].list_date == "1991-04-03"
    assert "上市日期未知" not in by_symbol["000001.SZ"].tags
    assert by_symbol["920066.BJ"].name == "北交样本"
    assert by_symbol["920066.BJ"].list_date == "2020-01-01"


def test_market_scan_retry_fails_when_validated_pool_omits_pending_symbol(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    hub.rows = [
        make_stock_info("600002", "SH"),
        make_stock_info("000001", "SZ"),
        make_stock_info("920066", "BJ"),
    ]
    run = hub.cache.create_market_scan_run(
        trigger="manual",
        rule_version=_rule_version(hub),
        as_of="2026-07-17 16:30:00",
        data_date="2026-07-17",
        scope="test",
    )
    hub.cache.start_market_scan_run(run.id)
    hub.cache.seed_market_scan_results(
        run.id,
        [
            MarketScanSeed(symbol="600001.SH", code="600001", market="SH", name="原沪市样本"),
            MarketScanSeed(symbol="000001.SZ", code="000001", market="SZ", name="深市样本"),
            MarketScanSeed(symbol="920066.BJ", code="920066", market="BJ", name="北交样本"),
        ],
        excluded_count=0,
    )
    hub.cache.finish_market_scan_run(run.id, "failed", message="等待重试")

    async def scenario():
        scanner = _scanner(hub)
        await scanner.start()
        retried = await scanner.retry_scan(run.id)
        final = await _wait_for_terminal(scanner, retried.run.id)
        await scanner.stop()
        return final

    final = asyncio.run(scenario())

    assert final.status == "failed"
    assert "重试股票池缺少 1 只待计算股票" in (final.last_error or "")
    assert "600001.SH" in (final.last_error or "")
    assert hub.stock_pool_calls == 1
    assert hub.kline_calls == {}


def test_market_scan_retry_rejects_changed_scoring_contract(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        run = hub.cache.create_market_scan_run(
            trigger="manual",
            rule_version=_rule_version(hub),
            as_of="2026-07-17 16:30:00",
            data_date="2026-07-17",
            scope="test",
        )
        hub.cache.start_market_scan_run(run.id)
        hub.cache.finish_market_scan_run(run.id, "failed", message="等待重试")
        hub.settings = hub.settings.model_copy(update={"market_scan_min_data_quality_score": hub.settings.market_scan_min_data_quality_score + 1})
        scanner = _scanner(hub)
        with pytest.raises(ValueError, match="规则/评分配置已变更.*新建扫描"):
            await scanner.retry_scan(run.id)
        current = scanner.run(run.id)
        await scanner.stop()
        return hub, current

    hub, current = asyncio.run(scenario())

    assert current.retry_count == 0
    assert hub.cache.market_scan_runs(page=1, page_size=10).total == 1


def test_market_scan_rejects_stale_retry_that_requires_new_market_data(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    run = hub.cache.create_market_scan_run(
        trigger="manual",
        rule_version=_rule_version(hub),
        as_of="2020-01-02 16:30:00",
        data_date="2020-01-02",
        scope="test",
    )
    hub.cache.start_market_scan_run(run.id)
    hub.cache.finish_market_scan_run(run.id, "failed", message="模拟旧批次失败")

    async def scenario():
        scanner = _scanner(hub)
        with pytest.raises(ValueError, match="已过期.*请新建扫描"):
            await scanner.retry_scan(run.id)
        current = scanner.run(run.id)
        await scanner.stop()
        return current

    current = asyncio.run(scenario())

    assert current.status == "failed"
    assert current.retry_count == 0
    assert hub.cache.market_scan_runs(page=1, page_size=100).total == 1


def test_market_scan_retry_rejects_intraday_snapshot_before_daily_bars_are_complete(
    tmp_path: Path,
) -> None:
    hub = _MarketScanHub(tmp_path)
    run = hub.cache.create_market_scan_run(
        trigger="manual",
        rule_version=_rule_version(hub),
        as_of="2026-07-16 16:30:00",
        data_date="2026-07-16",
        scope="test",
    )
    hub.cache.start_market_scan_run(run.id)
    hub.cache.finish_market_scan_run(run.id, "failed", message="等待次日重试")

    async def scenario():
        scanner = _scanner(hub, now=datetime(2026, 7, 17, 10, 30))
        with pytest.raises(ValueError, match="15:15"):
            await scanner.retry_scan(run.id)
        current = scanner.run(run.id)
        await scanner.stop()
        return current

    current = asyncio.run(scenario())

    assert current.status == "failed"
    assert current.retry_count == 0
    assert hub.cache.market_scan_runs(page=1, page_size=100).total == 1
    assert hub.stock_pool_calls == 0


def test_market_scan_rejects_stale_retry_when_all_successes_used_fallback_data(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    run = hub.cache.create_market_scan_run(
        trigger="manual",
        rule_version=_rule_version(hub),
        as_of="2020-01-02 16:30:00",
        data_date="2020-01-02",
        scope="test",
    )
    hub.cache.start_market_scan_run(run.id)
    hub.cache.seed_market_scan_results(
        run.id,
        [MarketScanSeed(symbol="600001.SH", code="600001", market="SH", name="旧批次")],
        excluded_count=0,
    )
    hub.cache.save_market_scan_result_batch(
        run.id,
        [
            MarketScanResultWrite(
                symbol="600001.SH",
                status="success",
                score=80,
                trend_score=75,
                leader_score=80,
                data_quality_score=85,
                price=10,
                metrics={"ma20": 9.5},
                reason="旧批次降级结果",
                data_date="2020-01-02",
                quote_timestamp="2020-01-02 15:00:00",
                quote_source="fallback",
                kline_source="test",
                adjustment_mode="qfq",
                quote_fallback_used=True,
                degradation_reasons=("quote_fallback",),
            )
        ],
    )
    hub.cache.finish_market_scan_run(run.id, "degraded", message="全部成功但使用备用行情")

    async def scenario():
        scanner = _scanner(hub)
        with pytest.raises(ValueError, match="已过期.*请新建扫描"):
            await scanner.retry_scan(run.id)
        await scanner.stop()

    asyncio.run(scenario())

    assert hub.cache.market_scan_retry_plan(run.id).pending_count == 1
    assert hub.cache.market_scan_runs(page=1, page_size=100).total == 1


def test_market_scan_retry_validates_requested_run_before_returning_active_run(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        scanner = _scanner(hub)
        await scanner.start()
        active = hub.cache.create_market_scan_run(
            trigger="manual",
            rule_version=_rule_version(hub),
            as_of="2026-07-17 16:30:00",
            data_date="2026-07-17",
            scope="test",
        )
        hub.cache.start_market_scan_run(active.id)
        with pytest.raises(NotFoundError, match="999"):
            await scanner.retry_scan(999)
        current = scanner.run(active.id)
        hub.cache.finish_market_scan_run(active.id, "failed", message="测试收尾")
        await scanner.stop()
        return current

    current = asyncio.run(scenario())

    assert current.status == "running"
