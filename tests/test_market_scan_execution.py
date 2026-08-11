from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.market_scan import MARKET_SCAN_TOP100_REFRESH_SCOPE, MarketScanResultItem
from app.models.schemas import Kline, Quote, StockInfo
from app.services.market_scan_execution import MarketScanExecutor
from app.services.market_scan_manager import MarketScanManager
from app.services.market_scan_recovery import ProviderWaitBudget
from app.services.market_scan_stock_evaluation import MarketScanStockEvaluator
from app.services.provider_errors import ProviderChainUnavailable
from app.utils.audit_time import parse_audit_time
from app.utils.market_time import market_local_naive
from tests.factories import make_stock_info
from tests.market_scan_test_support import (
    SCAN_AS_OF,
    SCAN_DATA_DATE,
    _MarketScanHub,
    _ResolutionMarketScanHub,
    _configure_clean_full_market,
    _daily_rows,
    _quote_for,
    _rule_version,
    _scanner,
    _wait_for_terminal,
)


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
    assert started.run.rule_version.startswith("full-market-scan-v6:")
    assert len(started.run.rule_version.rsplit(":", 1)[1]) == 64
    assert final.status == "failed"
    assert final.total_count == 3
    assert final.excluded_count == 1
    assert final.processed_count == 3
    assert final.success_count == 1
    assert final.missing_count == 2
    assert final.skipped_count == 0
    assert final.coverage_pct == 33.33
    assert final.current_stage is None
    assert {"stock_pool", "bulk_quotes", "klines", "scoring", "persistence", "publication"}.issubset(
        final.stage_metrics
    )
    assert final.stage_metrics["bulk_quotes"].calls >= 1
    assert final.stage_metrics["klines"].items >= 3
    assert final.stage_metrics["scoring"].items == 3
    assert {item.market for item in final.market_progress} == {"SH", "SZ", "BJ"}
    assert sum(item.processed_count for item in final.market_progress) == 3
    assert final.elapsed_seconds is not None
    assert {item.symbol for item in page.items} == {"600001.SH", "000001.SZ", "920066.BJ"}
    by_symbol = {item.symbol: item for item in page.items}
    assert by_symbol["600001.SH"].status == "success"
    assert by_symbol["600001.SH"].rank is None
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


def test_top100_refresh_reuses_ranked_symbols_but_fetches_fresh_data_and_reranks(
    tmp_path: Path,
) -> None:
    class RefreshHub(_MarketScanHub):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.quote_cache_flags: list[bool] = []
            self.kline_provider_flags: list[bool] = []

        async def partial_quotes_with_errors(self, symbols, use_cache: bool = True):
            self.quote_cache_flags.append(use_cache)
            return await super().partial_quotes_with_errors(symbols, use_cache=use_cache)

        async def kline(
            self,
            symbol: str,
            limit: int = 120,
            use_cache: bool = True,
            *,
            allow_stale: bool = False,
            require_provider_response: bool = False,
        ):
            self.kline_provider_flags.append(require_provider_response)
            return await super().kline(
                symbol,
                limit,
                use_cache,
                allow_stale=allow_stale,
                require_provider_response=require_provider_response,
            )

    async def scenario():
        hub = RefreshHub(tmp_path)
        _configure_clean_full_market(hub)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        source = await _wait_for_terminal(scanner, started.run.id)
        source_page = scanner.results(
            source.id,
            page=1,
            page_size=100,
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
        pool_calls = hub.stock_pool_calls
        hub.quote_cache_flags.clear()
        hub.kline_provider_flags.clear()

        response = await scanner.refresh_top100_scores(source.id)
        refreshed = await _wait_for_terminal(scanner, response.run.id)
        refreshed_page = scanner.results(
            refreshed.id,
            page=1,
            page_size=100,
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
        source_after = scanner.results(
            source.id,
            page=1,
            page_size=100,
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
        return hub, source, source_page, source_after, response, refreshed, refreshed_page, pool_calls

    hub, source, source_page, source_after, response, refreshed, refreshed_page, pool_calls = asyncio.run(scenario())

    assert response.accepted is True
    assert refreshed.status == "success"
    assert refreshed.scope == MARKET_SCAN_TOP100_REFRESH_SCOPE
    assert refreshed.retry_of_run_id == source.id
    assert refreshed.total_count == refreshed.success_count == source.success_count == 3
    assert refreshed.finished_at is not None
    assert refreshed.duration_ms is not None
    assert "stock_pool" not in refreshed.stage_metrics
    assert hub.stock_pool_calls == pool_calls
    assert hub.quote_cache_flags and set(hub.quote_cache_flags) == {False}
    assert hub.kline_provider_flags and all(hub.kline_provider_flags)
    assert [item.symbol for item in refreshed_page.items] == [
        item.symbol for item in source_page.items
    ]
    assert [item.rank for item in refreshed_page.items] == [1, 2, 3]
    assert all(item.run_id == refreshed.id and item.score is not None for item in refreshed_page.items)
    assert source_after.items == source_page.items


def test_market_scan_prefetches_each_batch_but_still_refreshes_every_symbol(
    tmp_path: Path,
) -> None:
    class PrefetchHub(_MarketScanHub):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.prefetch_batches: list[tuple[str, ...]] = []
            self.provider_refreshes: list[tuple[str, bool, bool]] = []

        async def prefetch_market_scan_klines(
            self,
            symbols: list[str],
            *,
            limit: int,
        ) -> dict[str, list[Kline]]:
            self.prefetch_batches.append(tuple(symbols))
            return {
                symbol: self.klines_by_symbol.get(symbol, [])[-limit:]
                for symbol in symbols
            }

        async def market_scan_kline_from_prefetch(
            self,
            symbol: str,
            prefetched_cache: list[Kline],
            *,
            limit: int,
            allow_stale: bool,
            require_provider_response: bool,
        ) -> list[Kline]:
            self.provider_refreshes.append(
                (symbol, allow_stale, require_provider_response)
            )
            assert prefetched_cache == self.klines_by_symbol.get(symbol, [])[-limit:]
            return await super().kline(
                symbol,
                limit=limit,
                allow_stale=allow_stale,
                require_provider_response=require_provider_response,
            )

    async def scenario() -> PrefetchHub:
        hub = PrefetchHub(tmp_path)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        await _wait_for_terminal(scanner, started.run.id)
        await scanner.stop()
        return hub

    hub = asyncio.run(scenario())

    assert [len(batch) for batch in hub.prefetch_batches] == [2, 1]
    assert len(hub.provider_refreshes) == 3
    assert all(allow_stale and required for _, allow_stale, required in hub.provider_refreshes)


def test_scoring_as_of_is_bound_to_frozen_quote_observation_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AdvancingClock:
        def __init__(self) -> None:
            self.current = SCAN_AS_OF
            self.returned: list[datetime] = []

        def __call__(self):
            value = self.current
            self.returned.append(value)
            self.current += timedelta(minutes=1)
            return value

    recorded: dict[str, datetime] = {}
    original = MarketScanStockEvaluator.scan_one

    async def recording_scan_one(self, item, quote, **kwargs):
        recorded[item.symbol] = kwargs["as_of"]
        return await original(self, item, quote, **kwargs)

    async def scenario():
        hub = _MarketScanHub(tmp_path)
        _configure_clean_full_market(hub)
        clock = AdvancingClock()
        scanner = MarketScanManager(hub, now=clock)  # type: ignore[arg-type]
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        page = scanner.results(
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
        return clock, final, page

    monkeypatch.setattr(MarketScanStockEvaluator, "scan_one", recording_scan_one)
    clock, final, page = asyncio.run(scenario())

    assert final.status == "success"
    assert len(recorded) == len(page.items) == 3
    for item in page.items:
        assert item.quote_observed_at is not None
        observed_local = market_local_naive(parse_audit_time(item.quote_observed_at))
        assert recorded[item.symbol] == max(SCAN_AS_OF, observed_local)
    assert clock.returned[-1] > max(recorded.values())


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


def test_kline_retry_reuses_the_frozen_quote_batches_without_refetching_quotes(
    tmp_path: Path,
) -> None:
    class RetryKlineHub(_MarketScanHub):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.quote_calls = 0
            self.kline_attempts: dict[str, int] = {}
            self.settings.market_scan_batch_retry_attempts = 2
            self.settings.market_scan_provider_wait_budget_seconds = 5
            self.settings.market_scan_retry_backoff_seconds = 0

        async def partial_quotes_with_errors(self, symbols, use_cache: bool = True):
            self.quote_calls += 1
            return await super().partial_quotes_with_errors(symbols, use_cache=use_cache)

        async def kline(
            self,
            symbol: str,
            limit: int = 120,
            use_cache: bool = True,
            *,
            allow_stale: bool = False,
            require_provider_response: bool = False,
        ) -> list[Kline]:
            self.kline_attempts[symbol] = self.kline_attempts.get(symbol, 0) + 1
            if self.kline_attempts[symbol] == 1:
                raise ProviderChainUnavailable("日K瞬时失败", retry_after_seconds=0)
            return await super().kline(
                symbol,
                limit,
                use_cache,
                allow_stale=allow_stale,
                require_provider_response=require_provider_response,
            )

    async def scenario():
        hub = RetryKlineHub(tmp_path)
        _configure_clean_full_market(hub)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        await scanner.stop()
        return hub, final

    hub, final = asyncio.run(scenario())

    assert final.status == "success"
    assert hub.quote_calls == 2
    assert set(hub.kline_attempts.values()) == {2}


def test_v6_snapshot_rejects_cached_quotes_even_when_the_hub_returns_them(
    tmp_path: Path,
) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        _configure_clean_full_market(hub)
        hub.quotes_by_symbol = {
            symbol: quote.model_copy(update={"from_cache": True, "fallback_used": True})
            for symbol, quote in hub.quotes_by_symbol.items()
        }
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
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

    assert final.status == "failed"
    assert final.processed_count == final.total_count == 3
    assert final.success_count == 0
    assert final.missing_count == 3
    assert final.quote_capture_started_at is not None
    assert final.quote_capture_finished_at is not None
    assert all(
        item.status == "missing"
        and item.score is None
        and item.rank is None
        and "缓存报价" in (item.error or "")
        for item in page.items
    )


def test_realtime_fallback_provider_quotes_remain_eligible_but_degrade_the_run(
    tmp_path: Path,
) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        _configure_clean_full_market(hub)
        hub.quotes_by_symbol = {
            symbol: quote.model_copy(update={"from_cache": False, "fallback_used": True})
            for symbol, quote in hub.quotes_by_symbol.items()
        }
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        page = scanner.results(
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
        return final, page

    final, page = asyncio.run(scenario())

    assert final.status == "degraded"
    assert final.success_count == final.total_count == 3
    assert all(item.quote_fallback_used and "兜底行情" in item.tags for item in page.items)


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
        hub.klines_by_symbol["000001.SZ"] = _daily_rows(
            SCAN_DATA_DATE,
            80,
            last_close=hub.quotes_by_symbol["000001.SZ"].price,
        )
        hub.klines_by_symbol["920066.BJ"] = _daily_rows(
            SCAN_DATA_DATE,
            80,
            last_close=hub.quotes_by_symbol["920066.BJ"].price,
        )
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
    assert "1 只股票使用备用数据或元数据不完整" in (final.last_error or "")
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
