from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

import pytest

from app.config import Settings
from app.models.schemas import Kline, StockInfo
from app.services.datahub_runtime import ProviderCallBusyError, ProviderCallTimeoutError
from app.services.market_scan_execution import MarketScanExecutor
from app.services.market_scan_pressure import MarketScanPressureController
from app.services.market_scan_recovery import ProviderWaitBudget
from app.services.market_scan_scoring import MarketScanDataMissing, MarketScanSkipped
from app.utils.provider_errors import ProviderChainUnavailable, ProviderCoverageMiss
from tests.factories import make_stock_info
from tests.market_scan_test_support import (
    SCAN_AS_OF,
    SCAN_DATA_DATE,
    _MarketScanHub,
    _configure_clean_full_market,
    _daily_rows,
    _quote_for,
    _scanner,
    _wait_for_terminal,
)


def test_busy_pressure_halves_concurrency_and_healthy_batches_restore_one() -> None:
    controller = MarketScanPressureController(5, retry_backoff_seconds=1)
    error = _chain_error(
        ProviderCallBusyError("provider busy", retry_after_seconds=3),
    )

    decision = controller.observe_failures((error,), attempted_count=5)

    assert decision.pressured is True
    assert decision.minimum_delay_seconds == 3
    assert controller.current_concurrency == 2
    assert controller.snapshot().last_signal == "busy+retry_after"

    controller.record_backoff(1.25)
    terminal_warnings = controller.terminal_warnings(["普通供应商告警"])
    assert terminal_warnings[0] == ("扫描压力控制：触发 1 次，结束并发 2/5，" "最后信号 busy+retry_after，累计退避 1.25 秒")
    assert terminal_warnings[1] == "普通供应商告警"

    controller.complete_batch()
    controller.complete_batch()
    assert controller.current_concurrency == 3
    controller.complete_batch()
    assert controller.current_concurrency == 4
    controller.complete_batch()
    controller.complete_batch()
    assert controller.current_concurrency == 5


def test_systemic_ratio_slows_down_but_isolated_chain_failure_does_not() -> None:
    controller = MarketScanPressureController(5, retry_backoff_seconds=0)
    isolated = ProviderChainUnavailable("isolated chain failure")

    decision = controller.observe_failures(
        (isolated,),
        attempted_count=10,
        unavailable_count=1,
    )

    assert decision.pressured is False
    assert decision.provider_failure_observed is True
    assert controller.current_concurrency == 5

    systemic = controller.observe_failures(
        (ProviderChainUnavailable("quote chain unavailable"),),
        attempted_count=10,
        unavailable_count=10,
    )

    assert systemic.pressured is True
    assert controller.current_concurrency == 2
    assert controller.snapshot().last_unavailable_ratio == 1
    assert controller.snapshot().last_signal == "systemic_unavailable"


def test_timeout_immediately_reduces_concurrency_even_below_systemic_ratio() -> None:
    controller = MarketScanPressureController(8, retry_backoff_seconds=0)
    error = _chain_error(ProviderCallTimeoutError("provider timed out"))

    decision = controller.observe_failures(
        (error,),
        attempted_count=20,
        unavailable_count=1,
    )

    assert decision.pressured is True
    assert controller.current_concurrency == 4
    assert controller.snapshot().last_unavailable_ratio == pytest.approx(0.05)
    assert controller.snapshot().last_signal == "timeout"


@pytest.mark.parametrize(
    "error",
    [
        MarketScanSkipped("suspended"),
        MarketScanDataMissing("short history"),
        ProviderCoverageMiss("instrument not covered"),
    ],
)
def test_instrument_level_outcomes_do_not_count_as_pressure(error: BaseException) -> None:
    controller = MarketScanPressureController(5, retry_backoff_seconds=1)
    observed = _chain_error(error) if isinstance(error, ProviderCoverageMiss) else error

    decision = controller.observe_failures(
        (observed,),
        attempted_count=1,
        unavailable_count=1,
    )

    assert decision.pressured is False
    assert decision.provider_failure_observed is False
    assert controller.current_concurrency == 5
    assert controller.snapshot().pressure_events == 0


def test_backoff_jitter_is_bounded_deterministic_and_retry_after_is_a_floor() -> None:
    first = MarketScanPressureController(5, retry_backoff_seconds=2)
    second = MarketScanPressureController(5, retry_backoff_seconds=2)
    outage = ProviderChainUnavailable("systemic outage")

    first_delay = first.observe_failures(
        (outage,),
        attempted_count=4,
        unavailable_count=4,
    ).minimum_delay_seconds
    second_delay = second.observe_failures(
        (ProviderChainUnavailable("same class of outage"),),
        attempted_count=4,
        unavailable_count=4,
    ).minimum_delay_seconds

    assert first_delay == second_delay == pytest.approx(2.2)
    assert 1.8 <= first_delay <= 2.2

    retry_after = first.observe_failures(
        (_chain_error(ProviderCallBusyError("busy", retry_after_seconds=9)),),
        attempted_count=4,
    ).minimum_delay_seconds
    assert retry_after == 9
    assert first.current_concurrency == 1


def test_busy_retry_after_cannot_exceed_the_whole_provider_wait_budget(tmp_path: Path) -> None:
    async def scenario() -> tuple[float, float]:
        hub = _MarketScanHub(tmp_path)
        executor = MarketScanExecutor(hub)
        error = _chain_error(ProviderCallBusyError("busy", retry_after_seconds=2))
        decision = executor._pressure.observe_failures((error,), attempted_count=2)  # noqa: SLF001
        budget = ProviderWaitBudget(remaining_seconds=1)

        with pytest.raises(ProviderChainUnavailable, match="provider unavailable"):
            await executor._wait_for_provider_recovery(  # noqa: SLF001
                (error,),
                kind="kline",
                attempt=1,
                max_attempts=3,
                wait_budget=budget,
                cancel_event=asyncio.Event(),
                minimum_delay_seconds=decision.minimum_delay_seconds,
            )
        return budget.remaining_seconds, executor.pressure_snapshot.last_retry_after_seconds

    remaining, retry_after = asyncio.run(scenario())

    assert remaining == 1
    assert retry_after == 2


def test_adaptive_concurrency_changes_later_batch_inflight_limits(tmp_path: Path) -> None:
    class AdaptiveHub(_MarketScanHub):
        settings: Settings

        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.settings = self.settings.model_copy(
                update={
                    "market_scan_batch_size": 4,
                    "market_scan_concurrency": 5,
                    "market_scan_batch_retry_attempts": 2,
                    "market_scan_retry_backoff_seconds": 0,
                    "market_scan_provider_wait_budget_seconds": 0,
                }
            )
            self.rows = _stock_rows(16)
            ordered = sorted(self.rows, key=lambda row: (row.market, row.code, row.symbol))
            self.batch_by_symbol = {row.symbol: index // 4 for index, row in enumerate(ordered)}
            self.busy_symbol = ordered[0].symbol
            self.busy_raised = False
            self.active_by_batch: defaultdict[int, int] = defaultdict(int)
            self.max_active_by_batch: defaultdict[int, int] = defaultdict(int)
            self.quotes_by_symbol = {row.symbol: _quote_for(row.code, row.market, row.name, change_pct=1.0) for row in self.rows}
            self.klines_by_symbol = {row.symbol: _daily_rows(SCAN_DATA_DATE, 80) for row in self.rows}

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
            batch = self.batch_by_symbol[symbol]
            self.kline_calls[symbol] = self.kline_calls.get(symbol, 0) + 1
            self.active_by_batch[batch] += 1
            self.max_active_by_batch[batch] = max(
                self.max_active_by_batch[batch],
                self.active_by_batch[batch],
            )
            try:
                if symbol == self.busy_symbol and not self.busy_raised:
                    self.busy_raised = True
                    raise ProviderCallBusyError("provider busy", retry_after_seconds=0)
                await asyncio.sleep(0.01)
                return self.klines_by_symbol[symbol][-limit:]
            finally:
                self.active_by_batch[batch] -= 1

    async def scenario():
        hub = AdaptiveHub(tmp_path)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        snapshot = scanner._executor.pressure_snapshot  # noqa: SLF001
        await scanner.stop()
        return hub, final, snapshot

    hub, final, snapshot = asyncio.run(scenario())

    assert final.status == "success"
    assert final.success_count == final.total_count == 16
    assert dict(hub.max_active_by_batch) == {0: 3, 1: 2, 2: 3, 3: 4}
    assert snapshot.max_concurrency == 5
    assert snapshot.current_concurrency == 5
    assert snapshot.completed_batches == 4
    assert snapshot.pressure_events == 1
    assert "busy" in snapshot.last_signal
    assert snapshot.total_backoff_seconds == 0
    assert (final.last_error or "").startswith("扫描压力控制：触发 1 次")
    assert not any(row.symbol in (final.last_error or "") for row in hub.rows)


def test_exhausted_provider_pressure_is_retained_on_failed_run(tmp_path: Path) -> None:
    class FailingHub(_MarketScanHub):
        async def kline(self, *args, **kwargs):
            del args, kwargs
            raise ProviderCallBusyError("provider busy", retry_after_seconds=0)

    async def scenario():
        hub = FailingHub(tmp_path)
        _configure_clean_full_market(hub)
        hub.settings = hub.settings.model_copy(
            update={
                "market_scan_batch_retry_attempts": 1,
                "market_scan_provider_wait_budget_seconds": 0,
            }
        )
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        await scanner.stop()
        return final

    final = asyncio.run(scenario())

    assert final.status == "failed"
    assert "扫描压力控制：触发 1 次" in (final.last_error or "")


def _chain_error(cause: BaseException) -> ProviderChainUnavailable:
    error = ProviderChainUnavailable("provider unavailable")
    error.__cause__ = cause
    return error


def _stock_rows(count: int) -> list[StockInfo]:
    markets = ("SH", "SZ", "BJ")
    rows = []
    for index in range(count):
        market = markets[index % len(markets)]
        prefix = "600" if market == "SH" else "000" if market == "SZ" else "920"
        rows.append(
            make_stock_info(f"{prefix}{index:03d}", market).model_copy(
                update={"name": f"样本{index:02d}", "list_date": "20000101"},
            )
        )
    return rows
