from __future__ import annotations

import asyncio
import ast
from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models.market_scan import MarketScanResultItem
from app.models.schemas import Kline, Quote, StockInfo
from app.repositories.market_scan import MarketScanResultWrite, MarketScanSeed
from app.services.cache import SQLiteCache
from app.services.datahub_metadata import StockPoolResolution
from app.services.instance_guard import FileInstanceGuard
from app.services.market_scan_manager import MarketScanManager, market_scan_rule_version
from app.services.market_scan_execution import MarketScanExecutor
from app.services.market_scan_recovery import ProviderWaitBudget
from app.services.provider_errors import ProviderChainUnavailable
from app.utils.errors import NotFoundError
from tests.factories import make_kline, make_quote, make_stock_info


SCAN_AS_OF = datetime(2026, 7, 17, 16, 30)
SCAN_DATA_DATE = date(2026, 7, 17)


class _MarketScanHub:
    def __init__(
        self,
        tmp_path: Path,
        *,
        block_klines: asyncio.Event | None = None,
    ) -> None:
        self.settings = Settings(
            cache_path=tmp_path / "market-scan.sqlite3",
            scheduler_enabled=False,
            market_scan_min_universe_count=1,
            market_scan_min_sh_count=1,
            market_scan_min_sz_count=1,
            market_scan_min_bj_count=1,
            market_scan_batch_size=2,
            market_scan_concurrency=2,
            market_scan_symbol_timeout_seconds=2,
            market_scan_retry_attempts=1,
            market_scan_provider_wait_budget_seconds=0,
        )
        self.cache = SQLiteCache(settings=self.settings)
        self.block_klines = block_klines
        self.stock_pool_calls = 0
        self.kline_calls: dict[str, int] = {}
        self.active_klines = 0
        self.max_active_klines = 0
        self.rows = [
            make_stock_info("600001", "SH").model_copy(update={"name": "*ST测试", "list_date": "20260601"}),
            make_stock_info("000001", "SZ").model_copy(update={"name": "停牌样本"}),
            make_stock_info("920066", "BJ").model_copy(update={"name": "北交样本"}),
            make_stock_info("600099", "SH").model_copy(update={"name": "退市样本"}),
        ]
        self.quotes_by_symbol = {
            "600001.SH": _quote_for("600001", "SH", "*ST测试", change_pct=3.2),
            "000001.SZ": _quote_for("000001", "SZ", "停牌样本", change_pct=0.0),
        }
        current_rows = _daily_rows(SCAN_DATA_DATE, 80)
        self.klines_by_symbol = {
            "600001.SH": current_rows,
            "000001.SZ": _daily_rows(date(2026, 7, 10), 80),
            "920066.BJ": current_rows,
        }

    async def stock_pool(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets=None,
        minimum_market_counts=None,
    ) -> list[StockInfo]:
        del keyword, limit, refresh, required_markets, minimum_market_counts
        self.stock_pool_calls += 1
        return self.rows

    async def quotes(self, symbols, use_cache: bool = True) -> list[Quote]:
        del use_cache
        return [self.quotes_by_symbol[symbol] for symbol in symbols if symbol in self.quotes_by_symbol]

    async def partial_quotes(self, symbols, use_cache: bool = True) -> list[Quote]:
        return await self.quotes(symbols, use_cache=use_cache)

    async def partial_quotes_with_errors(
        self,
        symbols,
        use_cache: bool = True,
    ) -> tuple[list[Quote], tuple[str, ...]]:
        quotes = await self.partial_quotes(symbols, use_cache=use_cache)
        returned = {f"{quote.code}.{quote.market}" for quote in quotes}
        errors = ("测试行情源部分缺失",) if set(symbols) - returned else ()
        return quotes, errors

    async def kline(
        self,
        symbol: str,
        limit: int = 120,
        use_cache: bool = True,
        *,
        allow_stale: bool = False,
        require_provider_response: bool = False,
    ) -> list[Kline]:
        del use_cache, allow_stale, require_provider_response
        self.kline_calls[symbol] = self.kline_calls.get(symbol, 0) + 1
        self.active_klines += 1
        self.max_active_klines = max(self.max_active_klines, self.active_klines)
        try:
            if self.block_klines is not None:
                await self.block_klines.wait()
            await asyncio.sleep(0)
            return self.klines_by_symbol[symbol][-limit:]
        finally:
            self.active_klines -= 1


class _ResolutionMarketScanHub(_MarketScanHub):
    def __init__(self, tmp_path: Path, *, stock_pool_reason: str) -> None:
        super().__init__(tmp_path)
        self.stock_pool_reason = stock_pool_reason

    async def stock_pool_resolution(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets=None,
        minimum_market_counts=None,
    ) -> StockPoolResolution:
        del keyword, limit, refresh, required_markets, minimum_market_counts
        self.stock_pool_calls += 1
        return StockPoolResolution.hit(self.rows, self.stock_pool_reason)


class _BlockingTaskRunCache(SQLiteCache):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings=settings)
        self.start_entered = threading.Event()
        self.allow_start = threading.Event()

    def start_market_scan_task_run(self, run_id: int, task_name: str) -> int:
        task_run_id = super().start_market_scan_task_run(run_id, task_name)
        self.start_entered.set()
        self.allow_start.wait(timeout=5)
        return task_run_id


def test_full_market_scan_persists_every_symbol_and_ranks_only_valid_rows(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        all_results = scanner.results(
            final.id,
            page=1,
            page_size=100,
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
        return hub, started, final, all_results

    hub, started, final, page = asyncio.run(scenario())

    assert started.accepted is True
    assert started.run.status == "queued"
    assert started.run.rule_version == _rule_version(hub)
    assert "kline_limit=" in started.run.rule_version
    assert final.status == "degraded"
    assert final.total_count == 3
    assert final.excluded_count == 1
    assert final.processed_count == 3
    assert final.success_count == 1
    assert final.missing_count == 2
    assert final.skipped_count == 0
    assert final.coverage_pct == 33.33
    assert {item.symbol for item in page.items} == {"600001.SH", "000001.SZ", "920066.BJ"}
    by_symbol = {item.symbol: item for item in page.items}
    assert by_symbol["600001.SH"].status == "success"
    assert by_symbol["600001.SH"].rank == 1
    assert by_symbol["600001.SH"].is_st is True
    assert by_symbol["600001.SH"].is_new is True
    assert by_symbol["600001.SH"].metadata_source == hub.rows[0].source
    assert {"ST", "新股"}.issubset(by_symbol["600001.SH"].tags)
    assert by_symbol["000001.SZ"].status == "missing"
    assert "当日报价存在有效成交" in (by_symbol["000001.SZ"].error or "")
    assert by_symbol["920066.BJ"].status == "missing"
    assert "行情" in (by_symbol["920066.BJ"].error or "")
    assert "测试行情源部分缺失" in (by_symbol["920066.BJ"].error or "")
    assert hub.max_active_klines <= hub.settings.market_scan_concurrency


def test_market_scan_stops_without_persisting_false_missing_rows_when_provider_chain_is_cooling(
    tmp_path: Path,
) -> None:
    class CoolingMarketScanHub(_MarketScanHub):
        async def kline(
            self,
            symbol: str,
            limit: int = 120,
            use_cache: bool = True,
            *,
            allow_stale: bool = False,
            require_provider_response: bool = False,
        ) -> list[Kline]:
            del symbol, limit, use_cache, allow_stale, require_provider_response
            raise ProviderChainUnavailable("所有日K数据源当前均在冷却")

    async def scenario():
        hub = CoolingMarketScanHub(tmp_path)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        retry_plan = hub.cache.market_scan_retry_plan(final.id)
        await scanner.stop()
        return final, retry_plan

    final, retry_plan = asyncio.run(scenario())

    assert final.status == "failed"
    assert final.processed_count == 0
    assert final.missing_count == 0
    assert "均在冷却" in (final.last_error or "")
    assert retry_plan.pending_count == final.total_count


def test_market_scan_quote_chain_failure_preserves_every_pending_symbol(tmp_path: Path) -> None:
    class FailingQuoteHub(_MarketScanHub):
        async def partial_quotes_with_errors(self, symbols, use_cache: bool = True):
            del symbols, use_cache
            raise TimeoutError("报价源超时")

    async def scenario():
        hub = FailingQuoteHub(tmp_path)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        retry_plan = hub.cache.market_scan_retry_plan(final.id)
        await scanner.stop()
        return final, retry_plan

    final, retry_plan = asyncio.run(scenario())

    assert final.status == "failed"
    assert final.processed_count == 0
    assert final.missing_count == 0
    assert "批量行情请求超过" in (final.last_error or "")
    assert retry_plan.pending_count == final.total_count


def test_market_scan_permanent_quote_chain_failure_keeps_unresolved_rows_pending(
    tmp_path: Path,
) -> None:
    class PermanentQuoteHub(_MarketScanHub):
        def provider_chain_state(self, kind: str):
            assert kind == "quote"
            return SimpleNamespace(
                status="permanent_unavailable",
                retry_after_seconds=None,
            )

    async def scenario():
        hub = PermanentQuoteHub(tmp_path)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        retry_plan = hub.cache.market_scan_retry_plan(final.id)
        await scanner.stop()
        return final, retry_plan

    final, retry_plan = asyncio.run(scenario())

    assert final.status == "failed"
    assert final.processed_count == 0
    assert final.missing_count == 0
    assert retry_plan.pending_count == final.total_count


@pytest.mark.parametrize("returned_count", [0, 1])
def test_market_scan_rejects_severely_truncated_bulk_quotes(
    tmp_path: Path,
    returned_count: int,
) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        rows: list[StockInfo] = []
        quotes: dict[str, Quote] = {}
        markets = ("SH", "SZ", "BJ")
        for index in range(12):
            market = markets[index % len(markets)]
            code = (
                f"600{index:03d}"
                if market == "SH"
                else f"000{index:03d}"
                if market == "SZ"
                else f"920{index:03d}"
            )
            info = make_stock_info(code, market).model_copy(
                update={"name": f"样本{index}", "list_date": "20000101"}
            )
            rows.append(info)
            if index < returned_count:
                quotes[info.symbol] = _quote_for(code, market, info.name, change_pct=1.0)
        hub.rows = rows
        hub.quotes_by_symbol = quotes
        hub.settings = hub.settings.model_copy(
            update={
                "market_scan_batch_size": 12,
                "market_scan_batch_retry_attempts": 1,
            }
        )
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        retry_plan = hub.cache.market_scan_retry_plan(final.id)
        await scanner.stop()
        return final, retry_plan

    final, retry_plan = asyncio.run(scenario())

    assert final.status == "failed"
    assert final.processed_count == 0
    assert final.missing_count == 0
    assert f"批量行情覆盖率异常：{returned_count}/12" in (final.last_error or "")
    assert retry_plan.pending_count == 12


def test_market_scan_retries_only_unavailable_rows_after_provider_recovers(
    tmp_path: Path,
) -> None:
    class RecoveringHub(_MarketScanHub):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.failures_remaining = 2
            self.settings = self.settings.model_copy(
                update={
                    "market_scan_batch_retry_attempts": 2,
                    "market_scan_provider_wait_budget_seconds": 1,
                    "market_scan_retry_backoff_seconds": 0,
                }
            )

        async def kline(
            self,
            symbol: str,
            limit: int = 120,
            use_cache: bool = True,
            *,
            allow_stale: bool = False,
            require_provider_response: bool = False,
        ) -> list[Kline]:
            del use_cache, allow_stale, require_provider_response
            self.kline_calls[symbol] = self.kline_calls.get(symbol, 0) + 1
            self.active_klines += 1
            self.max_active_klines = max(self.max_active_klines, self.active_klines)
            try:
                await asyncio.sleep(0)
                if self.failures_remaining > 0:
                    self.failures_remaining -= 1
                    raise ProviderChainUnavailable("日K源短暂不可用")
                return self.klines_by_symbol[symbol][-limit:]
            finally:
                self.active_klines -= 1

    async def scenario():
        hub = RecoveringHub(tmp_path)
        _configure_clean_full_market(hub)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        await scanner.stop()
        return hub, final

    hub, final = asyncio.run(scenario())

    assert final.status == "success"
    assert final.processed_count == final.total_count == 3
    assert final.missing_count == 0
    assert sorted(hub.kline_calls.values()) == [1, 2, 2]
    assert hub.active_klines == 0


def test_symbol_fetch_delegates_chain_outage_to_the_batch_without_local_sleep(
    tmp_path: Path,
) -> None:
    class UnavailableHub(_MarketScanHub):
        async def kline(
            self,
            symbol: str,
            limit: int = 120,
            use_cache: bool = True,
            *,
            allow_stale: bool = False,
            require_provider_response: bool = False,
        ) -> list[Kline]:
            del symbol, limit, use_cache, allow_stale, require_provider_response
            self.kline_calls["chain"] = self.kline_calls.get("chain", 0) + 1
            raise ProviderChainUnavailable("整条日K链路不可用", retry_after_seconds=5)

    async def scenario() -> int:
        hub = UnavailableHub(tmp_path)
        hub.settings = hub.settings.model_copy(
            update={
                "market_scan_retry_attempts": 3,
                "market_scan_retry_backoff_seconds": 5,
                "market_scan_provider_wait_budget_seconds": 0,
            }
        )
        executor = MarketScanExecutor(hub)  # type: ignore[arg-type]
        with pytest.raises(ProviderChainUnavailable, match="整条日K链路不可用"):
            await executor._fetch_kline("600001.SH", asyncio.Event())  # noqa: SLF001
        return hub.kline_calls["chain"]

    assert asyncio.run(scenario()) == 1


def test_missing_quote_with_current_zero_volume_bar_is_possible_suspension(
    tmp_path: Path,
) -> None:
    hub = _MarketScanHub(tmp_path)
    executor = MarketScanExecutor(hub)  # type: ignore[arg-type]
    rows = _daily_rows(SCAN_DATA_DATE, 80)
    rows[-1] = rows[-1].model_copy(update={"volume": 0.0})
    item = MarketScanResultItem(
        run_id=1,
        symbol="600001.SH",
        code="600001",
        market="SH",
        name="停牌样本",
        status="pending",
        updated_at="2026-07-17 16:30:00",
    )

    result = executor._missing_quote_result(  # noqa: SLF001
        item,
        rows,
        cutoff=SCAN_DATA_DATE,
        expected_data_date=SCAN_DATA_DATE,
        quote_error="报价源未覆盖",
    )

    assert result.status == "skipped"
    assert "可能停牌" in (result.reason or "")
    assert result.error is None
    assert result.data_date == SCAN_DATA_DATE.isoformat()


def test_provider_recovery_wait_rejects_a_delay_larger_than_remaining_budget(
    tmp_path: Path,
) -> None:
    async def scenario() -> float:
        hub = _MarketScanHub(tmp_path)
        executor = MarketScanExecutor(hub)  # type: ignore[arg-type]
        budget = ProviderWaitBudget(remaining_seconds=0.1)
        error = ProviderChainUnavailable("仍在冷却", retry_after_seconds=2)
        with pytest.raises(ProviderChainUnavailable, match="仍在冷却"):
            await executor._wait_for_provider_recovery(  # noqa: SLF001
                (error,),
                kind="kline",
                attempt=1,
                max_attempts=3,
                wait_budget=budget,
                cancel_event=asyncio.Event(),
            )
        return budget.remaining_seconds

    remaining = asyncio.run(scenario())

    assert remaining == pytest.approx(0.1)


def test_provider_recovery_wait_wakes_when_the_chain_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecoveringStateHub(_MarketScanHub):
        chain_status = "temporary_unavailable"

        def provider_chain_state(self, kind: str):
            assert kind == "kline"
            return SimpleNamespace(
                status=self.chain_status,
                retry_after_seconds=1,
            )

    async def scenario() -> tuple[float, float]:
        hub = RecoveringStateHub(tmp_path)
        executor = MarketScanExecutor(hub)  # type: ignore[arg-type]
        budget = ProviderWaitBudget(remaining_seconds=2)

        async def recover() -> None:
            await asyncio.sleep(0.04)
            hub.chain_status = "ready"

        changer = asyncio.create_task(recover())
        loop = asyncio.get_running_loop()
        started = loop.time()
        await executor._wait_for_provider_recovery(  # noqa: SLF001
            (ProviderChainUnavailable("短暂故障", retry_after_seconds=1),),
            kind="kline",
            attempt=1,
            max_attempts=3,
            wait_budget=budget,
            cancel_event=asyncio.Event(),
        )
        elapsed = loop.time() - started
        await changer
        return elapsed, budget.remaining_seconds

    monkeypatch.setattr(
        "app.services.market_scan_recovery.PROVIDER_RECOVERY_POLL_SECONDS",
        0.01,
    )
    elapsed, remaining = asyncio.run(scenario())

    assert 0.03 <= elapsed < 0.3
    assert 1.7 < remaining < 2


def test_full_market_scan_with_all_scores_still_degrades_for_fallback_data(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        hub.rows = [
            make_stock_info("600001", "SH").model_copy(update={"name": "沪市样本", "list_date": "20000101"}),
            make_stock_info("000001", "SZ").model_copy(update={"name": "深市样本", "list_date": "19910403"}),
            make_stock_info("920066", "BJ").model_copy(update={"name": "北交样本", "list_date": "20200101"}),
        ]
        hub.quotes_by_symbol["920066.BJ"] = _quote_for(
            "920066",
            "BJ",
            "北交样本",
            change_pct=1.2,
        )
        hub.klines_by_symbol["600001.SH"] = [row.model_copy(update={"fallback_used": True}) for row in hub.klines_by_symbol["600001.SH"]]
        hub.klines_by_symbol["000001.SZ"] = _daily_rows(SCAN_DATA_DATE, 80)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        results = scanner.results(
            final.id,
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
        await scanner.stop()
        return final, results

    final, results = asyncio.run(scenario())

    assert final.status == "degraded"
    assert final.success_count == final.total_count == 3
    assert "降级结果 1" in (final.message or "")
    assert "1 只结果使用备用数据" in (final.last_error or "")
    by_symbol = {item.symbol: item for item in results.items}
    assert "兜底K线" in by_symbol["600001.SH"].tags


def test_stale_stock_pool_keeps_initial_and_retry_runs_degraded(tmp_path: Path) -> None:
    async def scenario():
        hub = _ResolutionMarketScanHub(tmp_path, stock_pool_reason="stale-fallback")
        _configure_clean_full_market(hub)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        first = await _wait_for_terminal(scanner, started.run.id)
        retried = await scanner.retry_scan(first.id)
        second = await _wait_for_terminal(scanner, retried.run.id)
        await scanner.stop()
        return hub, first, second

    hub, first, second = asyncio.run(scenario())

    assert first.status == second.status == "degraded"
    assert first.success_count == first.total_count == 3
    assert second.success_count == second.total_count == 3
    assert first.stock_pool_source == second.stock_pool_source == "stale-fallback"
    assert "股票池使用本地缓存" in (first.message or "")
    assert "股票池使用本地缓存" in (second.message or "")
    assert "stale-fallback" in (first.last_error or "")
    assert "stale-fallback" in (second.last_error or "")
    assert hub.stock_pool_calls == 2


def test_market_scan_deduplicates_active_start_and_can_cancel_then_resume(tmp_path: Path) -> None:
    async def scenario():
        gate = asyncio.Event()
        hub = _MarketScanHub(tmp_path, block_klines=gate)
        scanner = _scanner(hub)
        await scanner.start()
        first = await scanner.create_scan(as_of=SCAN_AS_OF)
        await _wait_for_status(scanner, first.run.id, {"running"})
        duplicate = await scanner.create_scan(as_of=SCAN_AS_OF)
        cancelled = await scanner.cancel_scan(first.run.id)
        gate.set()
        retried = await scanner.retry_scan(first.run.id)
        final = await _wait_for_terminal(scanner, retried.run.id)
        original = scanner.run(first.run.id)
        await scanner.stop()
        return first, duplicate, cancelled, retried, final, original

    first, duplicate, cancelled, retried, final, original = asyncio.run(scenario())

    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.deduplicated is True
    assert duplicate.run.id == first.run.id
    assert cancelled.status == "cancelled"
    assert retried.accepted is True
    assert retried.run.id != first.run.id
    assert retried.run.retry_of_run_id == first.run.id
    assert retried.run.retry_count == 1
    assert final.status == "degraded"
    assert final.processed_count == final.total_count
    assert original.status == "cancelled"


def test_market_scan_cancellation_closes_atomically_linked_task_returned_late(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        cache = _BlockingTaskRunCache(hub.settings)
        hub.cache = cache
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        assert await asyncio.to_thread(cache.start_entered.wait, 1)
        cancellation = asyncio.create_task(scanner.cancel_scan(started.run.id))
        await _wait_for_status(scanner, started.run.id, {"cancelling", "cancelled"})
        cache.allow_start.set()
        cancelled = await cancellation
        task_runs = cache.recent_task_runs(limit=10)
        await scanner.stop()
        return cancelled, task_runs

    cancelled, task_runs = asyncio.run(scenario())

    assert cancelled.status == "cancelled"
    assert len(task_runs) == 1
    assert task_runs[0].task_name == "full_market_scan"
    assert task_runs[0].status == "cancelled"
    assert "已取消" in (task_runs[0].message or "")


def test_market_scan_task_attach_failure_rolls_back_task_and_finishes_scan(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        with sqlite3.connect(hub.cache.path) as conn:
            conn.execute(
                """
                CREATE TRIGGER reject_market_scan_task_attach
                BEFORE UPDATE OF task_run_id ON market_scan_run
                WHEN NEW.task_run_id IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'simulated task attach failure');
                END
                """
            )
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        failed = await _wait_for_terminal(scanner, started.run.id)
        task_runs = hub.cache.recent_task_runs(limit=10)
        await scanner.stop()
        return failed, task_runs

    failed, task_runs = asyncio.run(scenario())

    assert failed.status == "failed"
    assert failed.task_run_id is None
    assert "simulated task attach failure" in (failed.last_error or "")
    assert task_runs == []


def test_market_scan_graceful_shutdown_marks_run_interrupted_not_user_cancelled(tmp_path: Path) -> None:
    async def scenario():
        gate = asyncio.Event()
        hub = _MarketScanHub(tmp_path, block_klines=gate)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        await _wait_for_status(scanner, started.run.id, {"running"})
        await scanner.stop()
        return scanner.run(started.run.id), hub.cache.recent_task_runs(limit=10)

    interrupted, task_runs = asyncio.run(scenario())

    assert interrupted.status == "interrupted"
    assert interrupted.last_error == "应用关闭时终止后台扫描任务"
    linked = next(item for item in task_runs if item.task_name == "full_market_scan")
    assert linked.status == "cancelled"
    assert "应用关闭中断" in (linked.message or "")


@pytest.mark.parametrize(
    ("finish_method", "expected_status", "persistence_error"),
    [
        ("_finish_cancelled", "cancelled", "attempt to write a readonly database"),
        ("_finish_interrupted", "interrupted", "database or disk is full"),
        ("_finish_failed", "failed", "database is locked"),
    ],
)
def test_market_scan_terminal_persistence_failure_is_visible_and_sanitized(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    finish_method: str,
    expected_status: str,
    persistence_error: str,
) -> None:
    secret = "fake-sensitive-value-for-redaction-test"
    hub = _MarketScanHub(tmp_path)
    hub.settings = hub.settings.model_copy(update={"llm_api_key": secret})

    def fail_terminal_write(*args, **kwargs):
        del args, kwargs
        raise sqlite3.OperationalError(f"{persistence_error}; context={secret}; " "https://db.example/write?token=private-token&mode=full")

    hub.cache.finish_market_scan_run = fail_terminal_write  # type: ignore[method-assign]
    scanner = _scanner(hub)

    async def scenario() -> None:
        finish = getattr(scanner, finish_method)
        if finish_method == "_finish_failed":
            await finish(42, RuntimeError("扫描执行失败"))
        else:
            await finish(42)

    asyncio.run(scenario())
    stderr = capsys.readouterr().err

    assert "terminal persistence failed" in stderr
    assert "run_id=42" in stderr
    assert f"target_status={expected_status}" in stderr
    assert persistence_error in stderr
    assert "OperationalError" in stderr
    assert "https://db.example/write" in stderr
    assert secret not in stderr
    assert "private-token" not in stderr
    assert "token=" not in stderr
    assert "mode=" not in stderr


def test_market_scan_retries_transient_terminal_write_and_commits_linked_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        _configure_clean_full_market(hub)
        original_finish = hub.cache.finish_market_scan_run
        finish_calls = 0

        def fail_once(*args, **kwargs):
            nonlocal finish_calls
            finish_calls += 1
            if finish_calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return original_finish(*args, **kwargs)

        hub.cache.finish_market_scan_run = fail_once  # type: ignore[method-assign]
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        task_runs = hub.cache.recent_task_runs(limit=10)
        await scanner.stop()
        return final, finish_calls, task_runs

    final, finish_calls, task_runs = asyncio.run(scenario())

    assert final.status == "success"
    assert finish_calls == 2
    assert len(task_runs) == 1
    assert task_runs[0].status == "success"
    assert "terminal persistence failed" not in capsys.readouterr().err


def test_market_scan_permanent_terminal_failure_recovers_on_next_owned_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        _configure_clean_full_market(hub)
        original_finish = hub.cache.finish_market_scan_run

        def fail_terminal_write(*args, **kwargs):
            del args, kwargs
            raise sqlite3.OperationalError("database is locked")

        hub.cache.finish_market_scan_run = fail_terminal_write  # type: ignore[method-assign]
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        for _attempt in range(200):
            if started.run.id not in scanner._lifecycle.active_run_ids:
                break
            await asyncio.sleep(0.01)
        assert started.run.id not in scanner._lifecycle.active_run_ids
        assert scanner._lifecycle.cancel_local(started.run.id) is None
        current = scanner.run(started.run.id)
        assert current.status == "running"

        hub.cache.finish_market_scan_run = original_finish  # type: ignore[method-assign]
        recovered = scanner.run(started.run.id)
        task_runs = hub.cache.recent_task_runs(limit=10)
        await scanner.stop()
        return started.run.id, recovered, task_runs

    run_id, recovered, task_runs = asyncio.run(scenario())
    stderr = capsys.readouterr().err

    assert recovered.status == "interrupted"
    assert recovered.message == "本地扫描任务已退出，终态写入失败后自动中断；可从断点重试"
    assert recovered.last_error == "本地后台扫描已退出，但原终态未能持久化"
    linked = next(item for item in task_runs if item.task_name == "full_market_scan")
    assert linked.status == "cancelled"
    assert f"run_id={run_id}" in stderr
    assert "target_status=success" in stderr
    assert "database is locked" in stderr


def test_market_scan_support_modules_keep_domain_boundary_and_bounded_size() -> None:
    root = Path(__file__).resolve().parents[1]
    modules = [
        root / "app/services/market_scan_execution.py",
        root / "app/services/market_scan_completion.py",
        root / "app/services/market_scan_lifecycle.py",
    ]

    for path in modules:
        source = path.read_text(encoding="utf-8")
        assert len(source.splitlines()) < 500, path.name
        tree = ast.parse(source)
        repository_imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("app.repositories")
        ]
        assert repository_imports == [], f"{path.name} must not depend on repository DTOs"


def test_market_scan_start_reconciles_orphaned_runs(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    run = hub.cache.create_market_scan_run(
        trigger="manual",
        rule_version=_rule_version(hub),
        as_of="2026-07-18 16:30:00",
        data_date="2026-07-17",
        scope="test",
    )
    task_run_id = hub.cache.start_task_run("full_market_scan")
    hub.cache.attach_market_scan_task_run(run.id, task_run_id)
    hub.cache.start_market_scan_run(run.id)

    async def scenario():
        scanner = _scanner(hub)
        reconciled = await scanner.start()
        current = scanner.run(run.id)
        await scanner.stop()
        return reconciled, current, hub.cache.recent_task_runs(limit=10)

    reconciled, current, task_runs = asyncio.run(scenario())

    assert reconciled == 1
    assert current.status == "interrupted"
    assert "断点重试" in (current.message or "")
    linked = next(item for item in task_runs if item.id == task_run_id)
    assert linked.status == "cancelled"
    assert linked.finished_at is not None
    assert linked.message == "应用重启时终止遗留全市场扫描记录"


def test_market_scan_lock_blocks_non_owner_mutations_and_reconciliation(tmp_path: Path) -> None:
    async def scenario():
        owner_hub = _MarketScanHub(tmp_path)
        standby_hub = _MarketScanHub(tmp_path)
        owner = _scanner(owner_hub)
        standby = _scanner(standby_hub)
        assert await owner.start() == 0
        retryable = owner_hub.cache.create_market_scan_run(
            trigger="manual",
            rule_version=_rule_version(owner_hub),
            as_of="2026-07-17 16:30:00",
            data_date="2026-07-17",
            scope="test",
        )
        owner_hub.cache.start_market_scan_run(retryable.id)
        owner_hub.cache.finish_market_scan_run(retryable.id, "failed", message="可重试")
        active = owner_hub.cache.create_market_scan_run(
            trigger="manual",
            rule_version=_rule_version(owner_hub),
            as_of="2026-07-17 16:30:00",
            data_date="2026-07-17",
            scope="test",
        )
        owner_hub.cache.start_market_scan_run(active.id)

        assert await standby.start() == 0
        assert standby.run(active.id).status == "running"
        with pytest.raises(RuntimeError, match="其他进程"):
            await standby.create_scan(as_of=SCAN_AS_OF)
        with pytest.raises(RuntimeError, match="其他进程"):
            await standby.retry_scan(retryable.id)
        with pytest.raises(RuntimeError, match="其他进程"):
            await standby.cancel_scan(active.id)
        assert standby.run(active.id).status == "running"
        assert standby_hub.cache.market_scan_runs(page=1, page_size=20).total == 2

        await owner.stop()
        reconciled = await standby.start()
        interrupted = standby.run(active.id)
        await standby.stop()
        return reconciled, interrupted

    reconciled, interrupted = asyncio.run(scenario())

    assert reconciled == 1
    assert interrupted.status == "interrupted"
    assert (tmp_path / "market-scan.sqlite3.market-scan.lock").exists()


def test_market_scan_status_recovery_never_interrupts_another_leader_run(tmp_path: Path) -> None:
    async def scenario():
        owner_hub = _MarketScanHub(tmp_path)
        standby_hub = _MarketScanHub(tmp_path)
        owner = _scanner(owner_hub)
        standby = _scanner(standby_hub)
        assert await owner.start() == 0
        active = owner_hub.cache.create_market_scan_run(
            trigger="manual",
            rule_version=_rule_version(owner_hub),
            as_of="2026-07-17 16:30:00",
            data_date="2026-07-17",
            scope="test",
        )
        owner_hub.cache.start_market_scan_run(active.id)
        assert await standby.start() == 0

        standby._track_terminal_persistence(active.id, False)
        observed = standby.run(active.id)

        owner_hub.cache.finish_market_scan_run(active.id, "failed", message="测试收尾")
        await standby.stop()
        await owner.stop()
        return observed

    observed = asyncio.run(scenario())

    assert observed.status == "running"


def test_market_scan_crash_takeover_reconciles_once_before_creating(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path, block_klines=asyncio.Event())
        lock = FileInstanceGuard(Path(f"{hub.cache.path}.market-scan.lock"))
        assert lock.acquire() is True
        orphaned = hub.cache.create_market_scan_run(
            trigger="manual",
            rule_version=_rule_version(hub),
            as_of="2026-07-17 16:30:00",
            data_date="2026-07-17",
            scope="test",
        )
        hub.cache.start_market_scan_run(orphaned.id)
        reconcile_calls = 0
        original_reconcile = hub.cache.reconcile_incomplete_market_scans

        def reconcile() -> int:
            nonlocal reconcile_calls
            reconcile_calls += 1
            return original_reconcile()

        hub.cache.reconcile_incomplete_market_scans = reconcile  # type: ignore[method-assign]
        standby = _scanner(hub)
        assert await standby.start() == 0
        assert reconcile_calls == 0

        lock.release()
        created = await standby.create_scan(as_of=SCAN_AS_OF)
        duplicate = await standby.create_scan(as_of=SCAN_AS_OF)
        old_run = standby.run(orphaned.id)
        await standby.stop()
        return reconcile_calls, created, duplicate, old_run

    reconcile_calls, created, duplicate, old_run = asyncio.run(scenario())

    assert reconcile_calls == 1
    assert old_run.status == "interrupted"
    assert created.accepted is True
    assert created.run.id != old_run.id
    assert duplicate.deduplicated is True
    assert duplicate.run.id == created.run.id


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
        scanner = _scanner(hub)
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


def test_market_scan_rejects_pool_missing_required_market(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        hub.rows = [item for item in hub.rows if item.market != "BJ"]
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        await scanner.stop()
        return final

    final = asyncio.run(scenario())

    assert final.status == "failed"
    assert final.total_count == 0
    assert "BJ" in (final.last_error or "")


def test_market_scan_rejects_truncated_individual_market_pool(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        hub.settings = hub.settings.model_copy(update={"market_scan_min_sh_count": 2})
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        await scanner.stop()
        return final

    final = asyncio.run(scenario())

    assert final.status == "failed"
    assert "SH 1/2" in (final.last_error or "")


def test_market_scan_scheduler_respects_publish_floor_and_does_not_repeat_failed_auto_run(
    tmp_path: Path,
) -> None:
    hub = _MarketScanHub(tmp_path)
    hub.settings = hub.settings.model_copy(
        update={
            "market_scan_auto_enabled": True,
            "market_scan_schedule_hour": 14,
            "market_scan_schedule_minute": 0,
        }
    )
    scanner = _scanner(hub)

    async def scenario():
        early = await scanner.scheduled_tick(datetime(2026, 7, 17, 14, 30))
        run = hub.cache.create_market_scan_run(
            trigger="scheduled",
            rule_version=_rule_version(hub),
            as_of="2026-07-17 15:20:00",
            data_date="2026-07-17",
            scope="test",
        )
        hub.cache.start_market_scan_run(run.id)
        hub.cache.finish_market_scan_run(run.id, "failed", message="模拟自动扫描失败")
        repeated = await scanner.scheduled_tick(datetime(2026, 7, 17, 16, 30))
        await scanner.stop()
        return early, repeated

    early, repeated = asyncio.run(scenario())

    assert early is None
    assert repeated is None
    assert scanner.latest_run().id == 1


def test_market_scan_scheduler_respects_same_day_manual_cancellation(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    hub.settings = hub.settings.model_copy(
        update={
            "market_scan_auto_enabled": True,
            "market_scan_schedule_hour": 16,
            "market_scan_schedule_minute": 0,
        }
    )
    run = hub.cache.create_market_scan_run(
        trigger="manual",
        rule_version=_rule_version(hub),
        as_of="2026-07-17 15:20:00",
        data_date="2026-07-17",
        scope="test",
    )
    hub.cache.request_market_scan_cancel(run.id)
    hub.cache.finish_market_scan_run(run.id, "cancelled", message="用户取消")
    scanner = _scanner(hub)

    async def scenario():
        repeated = await scanner.scheduled_tick(datetime(2026, 7, 17, 16, 30))
        await scanner.stop()
        return repeated

    repeated = asyncio.run(scenario())

    assert repeated is None
    assert scanner.latest_run().id == run.id  # type: ignore[union-attr]


def test_market_scan_rejects_intraday_snapshot_before_daily_bars_are_complete(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    scanner = _scanner(hub)

    with pytest.raises(ValueError, match="15:15"):
        asyncio.run(scanner.create_scan(as_of=datetime(2026, 7, 17, 10, 30)))

    assert scanner.latest_run() is None


def test_market_scan_rejects_historical_as_of_before_any_side_effect(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        scanner = _scanner(hub, now=datetime(2026, 7, 20, 16, 30))
        with pytest.raises(ValueError, match="当前快照.*已持久化快照"):
            await scanner.create_scan(as_of=SCAN_AS_OF)
        latest = scanner.latest_run()
        await scanner.stop()
        return hub, latest

    hub, latest = asyncio.run(scenario())

    assert latest is None
    assert hub.stock_pool_calls == 0
    assert hub.kline_calls == {}
    assert Path(f"{hub.cache.path}.market-scan.lock").exists() is False


async def _wait_for_terminal(scanner: MarketScanManager, run_id: int):
    return await _wait_for_status(
        scanner,
        run_id,
        {"success", "degraded", "failed", "cancelled", "interrupted"},
    )


def _scanner(
    hub: _MarketScanHub,
    *,
    now: datetime = SCAN_AS_OF,
) -> MarketScanManager:
    return MarketScanManager(hub, now=lambda: now)  # type: ignore[arg-type]


def _rule_version(hub: _MarketScanHub) -> str:
    return market_scan_rule_version(hub.settings)


def _configure_clean_full_market(hub: _MarketScanHub) -> None:
    hub.rows = [
        make_stock_info("600001", "SH").model_copy(update={"name": "沪市样本", "list_date": "20000101"}),
        make_stock_info("000001", "SZ").model_copy(update={"name": "深市样本", "list_date": "19910403"}),
        make_stock_info("920066", "BJ").model_copy(update={"name": "北交样本", "list_date": "20200101"}),
    ]
    hub.quotes_by_symbol = {
        "600001.SH": _quote_for("600001", "SH", "沪市样本", change_pct=1.0),
        "000001.SZ": _quote_for("000001", "SZ", "深市样本", change_pct=1.1),
        "920066.BJ": _quote_for("920066", "BJ", "北交样本", change_pct=1.2),
    }
    hub.klines_by_symbol = {symbol: _daily_rows(SCAN_DATA_DATE, 80) for symbol in hub.quotes_by_symbol}


async def _wait_for_status(scanner: MarketScanManager, run_id: int, statuses: set[str]):
    for _attempt in range(200):
        run = scanner.run(run_id)
        if run.status in statuses:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"scan {run_id} did not reach {statuses}")


def _quote_for(code: str, market: str, name: str, *, change_pct: float) -> Quote:
    return make_quote(
        price=10.3,
        prev_close=10.0,
        high=10.5,
        low=9.9,
        change_pct=change_pct,
        turnover_rate=4.2,
        timestamp="2026-07-17 15:00:00",
    ).model_copy(
        update={
            "code": code,
            "market": market,
            "name": name,
            "amount": 800_000_000,
            "change": 0.3,
        }
    )


def _daily_rows(latest: date, count: int) -> list[Kline]:
    days: list[date] = []
    cursor = latest
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    return [
        make_kline(
            date=day.isoformat(),
            close=10 + index * 0.03,
            volume=1_000_000 + index * 10_000,
            source="测试前复权日K",
            as_of=latest.isoformat(),
            data_version=f"test|qfq|{latest.isoformat()}",
        )
        for index, day in enumerate(days)
    ]
