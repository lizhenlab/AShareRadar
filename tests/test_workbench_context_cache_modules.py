from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import gc
import time
from types import SimpleNamespace

import pytest

import app.services.workbench_context as workbench_context_service
from app.config import Settings
from app.services.cache import SQLiteCache
from app.services.workbench_context import (
    WorkbenchContext,
    WorkbenchContextCache,
    WorkbenchContextIntegrityError,
)
from app.services.datahub import DataHub
from app.workflows.individual import stock_strategy_cards


def test_datahub_instances_own_separate_workbench_context_caches(tmp_path) -> None:
    first_settings = Settings(cache_path=tmp_path / "first.sqlite3", scheduler_enabled=False)
    second_settings = Settings(cache_path=tmp_path / "second.sqlite3", scheduler_enabled=False)
    first = DataHub(cache=SQLiteCache(settings=first_settings), settings=first_settings)
    second = DataHub(cache=SQLiteCache(settings=second_settings), settings=second_settings)

    assert first.workbench_contexts is not second.workbench_contexts


def test_workbench_cache_rejects_cross_symbol_builder_result_without_caching() -> None:
    async def run_check():
        cache = WorkbenchContextCache()

        async def build(_symbol: str):
            return _bound_context("000001.SZ")

        with pytest.raises(WorkbenchContextIntegrityError):
            await cache.get("600519.SH", build)
        return dict(cache.entries)

    assert asyncio.run(run_check()) == {}


def _bound_context(symbol: str) -> WorkbenchContext:
    code, market = symbol.split(".")
    quote_time = "2026-08-13 05:59:00"

    def research_child() -> SimpleNamespace:
        return SimpleNamespace(symbol=symbol, updated_at=quote_time)

    insights = SimpleNamespace(
        overview=research_child(),
        fund_flow=research_child(),
        order_pressure=research_child(),
        events=research_child(),
        financial_health=research_child(),
        valuation=research_child(),
        lhb=research_child(),
        abnormal_events=research_child(),
        rule_matches=research_child(),
        strategy_cards=[research_child(), research_child()],
    )
    context = object.__new__(WorkbenchContext)
    context.analysis = SimpleNamespace(
        quote=SimpleNamespace(code=code, market=market, timestamp=quote_time),
        stock_profile=None,
        review=None,
    )
    context.insights = insights
    for field in (
        "feature_snapshot", "factor_lab", "market_regime", "signal_validation",
        "risk_reward", "timeframe_alignment", "alpha_evidence", "diagnosis",
        "evidence_chain", "qa_report", "event_digest", "peer_comparison",
        "t_strategy", "risk_radar", "chip_analysis", "leadership",
        "theme_context", "replay",
    ):
        setattr(context, field, research_child())
    context.requested_symbol = symbol
    context.observed_symbol = symbol
    context.context_generated_at = "2026-08-13 06:00:00"
    context.signal_date = "2026-08-13"
    context.daily_bar_cutoff = "2026-08-12"
    context.quote_event_time = quote_time
    context.cache_cohort_key = "test"
    return context


def test_valid_bound_workbench_context_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_check() -> tuple[WorkbenchContext, int]:
        cache = WorkbenchContextCache()
        build_count = 0

        async def build(_symbol: str) -> WorkbenchContext:
            nonlocal build_count
            build_count += 1
            return _bound_context("600519.SH")

        await cache.get("600519", build)
        second = await cache.get("600519.SH", build)
        return second, build_count

    monkeypatch.setattr(
        workbench_context_service,
        "workbench_cache_cohort_key",
        lambda: "test",
    )
    context, build_count = asyncio.run(run_check())

    assert context.requested_symbol == "600519.SH"
    assert build_count == 1


@pytest.mark.parametrize(
    "field",
    [
        "evidence_chain",
        "qa_report",
        "event_digest",
        "peer_comparison",
        "t_strategy",
        "risk_radar",
        "insights.strategy_cards[1]",
    ],
)
def test_cached_workbench_context_rejects_every_swapped_support_owner(
    field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_check() -> dict[str, tuple[float, WorkbenchContext]]:
        cache = WorkbenchContextCache()
        context = _bound_context("600519.SH")
        child = (
            context.insights.strategy_cards[1]
            if field == "insights.strategy_cards[1]"
            else getattr(context, field)
        )
        child.symbol = "000001.SZ"
        cache.entries["600519.SH"] = (time.monotonic(), context)

        async def should_not_build(_symbol: str) -> WorkbenchContext:
            raise AssertionError("poisoned fresh cache entry must fail closed")

        with pytest.raises(WorkbenchContextIntegrityError, match="身份绑定"):
            await cache.get("600519.SH", should_not_build)
        return dict(cache.entries)

    monkeypatch.setattr(
        workbench_context_service,
        "workbench_cache_cohort_key",
        lambda: "test",
    )

    assert asyncio.run(run_check()) == {}


def test_direct_strategy_cards_route_rejects_future_cached_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_check() -> dict[str, tuple[float, WorkbenchContext]]:
        cache = WorkbenchContextCache()
        context = _bound_context("600519.SH")
        context.insights.strategy_cards[0].updated_at = "2099-01-01 09:59:00"
        cache.entries["600519.SH"] = (time.monotonic(), context)
        datahub = SimpleNamespace(workbench_contexts=cache)

        with pytest.raises(WorkbenchContextIntegrityError, match="晚于研究决策时点"):
            await stock_strategy_cards(datahub, "600519")
        return dict(cache.entries)

    monkeypatch.setattr(
        workbench_context_service,
        "workbench_cache_cohort_key",
        lambda: "test",
    )

    assert asyncio.run(run_check()) == {}


def test_expired_workbench_context_is_pruned_and_rebuilt() -> None:
    async def run_check():
        cache = WorkbenchContextCache(ttl_seconds=0.01)
        cache.entries["600519.SH"] = (time.monotonic() - 1, "stale")  # type: ignore[assignment]
        build_count = 0

        async def build(symbol: str):
            nonlocal build_count
            build_count += 1
            return f"fresh:{symbol}"

        result = await cache.get("600519", build)
        return result, build_count, cache.entries["600519.SH"][1]

    result, build_count, cached = asyncio.run(run_check())

    assert result == "fresh:600519.SH"
    assert cached == "fresh:600519.SH"
    assert build_count == 1


def test_cancelled_inflight_task_does_not_poison_future_gets() -> None:
    async def run_check():
        cache = WorkbenchContextCache()

        async def cancelled_build():
            await asyncio.sleep(10)
            return "cancelled"

        task = asyncio.create_task(cancelled_build())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        cache._inflight["600519.SH"] = task  # noqa: SLF001

        async def build(symbol: str):
            return f"fresh:{symbol}"

        result = await cache.get("600519", build)
        return result, list(cache._inflight)  # noqa: SLF001

    result, inflight_keys = asyncio.run(run_check())

    assert result == "fresh:600519.SH"
    assert inflight_keys == []


def test_clear_during_inflight_build_prevents_stale_cache_writeback() -> None:
    async def run_check():
        cache = WorkbenchContextCache()
        started = asyncio.Event()
        release = asyncio.Event()

        async def build(symbol: str):
            started.set()
            await release.wait()
            return f"fresh:{symbol}"

        pending = asyncio.create_task(cache.get("600519", build))
        await started.wait()
        cache.clear()
        release.set()
        result = await pending
        return result, dict(cache.entries)

    result, entries = asyncio.run(run_check())

    assert result == "fresh:600519.SH"
    assert entries == {}


def test_concurrent_workbench_context_requests_share_inflight_build() -> None:
    async def run_check():
        cache = WorkbenchContextCache()
        build_count = 0

        async def build(symbol: str):
            nonlocal build_count
            build_count += 1
            await asyncio.sleep(0)
            return f"fresh:{symbol}"

        first, second = await asyncio.gather(cache.get("600519", build), cache.get("600519.SH", build))
        return first, second, build_count

    first, second, build_count = asyncio.run(run_check())

    assert first == "fresh:600519.SH"
    assert second == "fresh:600519.SH"
    assert build_count == 1


def test_cancelled_waiter_does_not_cancel_shared_workbench_build() -> None:
    async def run_check():
        cache = WorkbenchContextCache()
        started = asyncio.Event()
        release = asyncio.Event()
        build_count = 0
        build_cancelled = False

        async def build(symbol: str):
            nonlocal build_count, build_cancelled
            build_count += 1
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                build_cancelled = True
                raise
            return f"fresh:{symbol}"

        cancelled_waiter = asyncio.create_task(cache.get("600519", build))
        surviving_waiter = asyncio.create_task(cache.get("600519.SH", build))
        await started.wait()
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        try:
            await cancelled_waiter
        except asyncio.CancelledError:
            pass
        release.set()
        result = await surviving_waiter
        await asyncio.sleep(0)
        return result, build_count, build_cancelled, dict(cache.entries), dict(cache._inflight)  # noqa: SLF001

    result, build_count, build_cancelled, entries, inflight = asyncio.run(run_check())

    assert result == "fresh:600519.SH"
    assert build_count == 1
    assert build_cancelled is False
    assert entries["600519.SH"][1] == result
    assert inflight == {}


def test_workbench_cache_close_cancels_and_awaits_inflight_builds() -> None:
    async def run_check():
        cache = WorkbenchContextCache()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def build(symbol: str):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return symbol  # pragma: no cover

        waiter = asyncio.create_task(cache.get("600519", build))
        await started.wait()
        await cache.aclose()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        return cancelled.is_set(), dict(cache.entries), dict(cache._inflight)  # noqa: SLF001

    cancelled, entries, inflight = asyncio.run(run_check())

    assert cancelled is True
    assert entries == {}
    assert inflight == {}


def test_workbench_cache_close_is_bounded_and_consumes_late_failure() -> None:
    async def run_check() -> tuple[float, bool, list[dict]]:
        cache = WorkbenchContextCache(shutdown_timeout_seconds=0.01)
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop_errors: list[dict] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def stubborn_build():
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            finally:
                finished.set()
            raise RuntimeError("late workbench close failure")

        task = asyncio.create_task(stubborn_build(), name="stubborn-workbench-build")
        cache._inflight["600519.SH"] = task  # noqa: SLF001
        await started.wait()
        fallback_release = loop.call_later(0.5, release.set)
        began = loop.time()
        await cache.aclose()
        elapsed = loop.time() - began
        release.set()
        fallback_release.cancel()
        await finished.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        del task
        gc.collect()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
        assert cache.entries == {}
        assert cache._inflight == {}  # noqa: SLF001
        return elapsed, cancelled.is_set(), loop_errors

    elapsed, cancelled, loop_errors = asyncio.run(run_check())

    assert elapsed < 0.25
    assert cancelled is True
    assert loop_errors == []


def test_cleared_orphaned_build_exception_is_consumed() -> None:
    async def run_check():
        cache = WorkbenchContextCache()
        started = asyncio.Event()
        release = asyncio.Event()
        loop_errors: list[dict] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def build(_symbol: str):
            started.set()
            await release.wait()
            raise RuntimeError("orphaned build failed")

        waiter = asyncio.create_task(cache.get("600519", build))
        await started.wait()
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        cache.clear()
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
        return loop_errors

    assert asyncio.run(run_check()) == []


def test_restore_and_trim_keep_only_newest_cache_entries() -> None:
    cache = WorkbenchContextCache(max_size=1)
    older = _bound_context("600519.SH")
    newer = _bound_context("000001.SZ")

    cache.restore_entries(
        {
            "600519.SH": (1.0, older),
            "000001.SZ": (2.0, newer),
        }
    )
    cache.trim()

    assert cache.entries == {"000001.SZ": (2.0, newer)}


def test_use_cache_false_bypasses_a_fresh_cached_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_check() -> tuple[object, int]:
        cache = WorkbenchContextCache()
        cache.entries["600519.SH"] = (time.monotonic(), _bound_context("600519.SH"))
        build_count = 0

        async def build(symbol: str) -> str:
            nonlocal build_count
            build_count += 1
            return f"rebuilt:{symbol}"

        result = await cache.get("600519", build, use_cache=False)  # type: ignore[arg-type]
        return result, build_count

    monkeypatch.setattr(workbench_context_service, "workbench_cache_cohort_key", lambda: "test")

    assert asyncio.run(run_check()) == ("rebuilt:600519.SH", 1)


def test_stale_cache_cohort_is_pruned_and_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_check() -> tuple[WorkbenchContext, int]:
        cache = WorkbenchContextCache()
        stale = _bound_context("600519.SH")
        stale.cache_cohort_key = "previous-session"
        cache.entries["600519.SH"] = (time.monotonic(), stale)
        build_count = 0

        async def build(_symbol: str) -> WorkbenchContext:
            nonlocal build_count
            build_count += 1
            return _bound_context("600519.SH")

        return await cache.get("600519", build), build_count

    monkeypatch.setattr(workbench_context_service, "workbench_cache_cohort_key", lambda: "test")

    rebuilt, build_count = asyncio.run(run_check())
    assert rebuilt.cache_cohort_key == "test"
    assert build_count == 1


def test_builder_result_from_stale_cohort_is_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_check() -> dict[str, tuple[float, WorkbenchContext]]:
        cache = WorkbenchContextCache()

        async def build(_symbol: str) -> WorkbenchContext:
            context = _bound_context("600519.SH")
            context.cache_cohort_key = "previous-session"
            return context

        with pytest.raises(WorkbenchContextIntegrityError, match="时段已切换"):
            await cache.get("600519", build)
        return dict(cache.entries)

    monkeypatch.setattr(workbench_context_service, "workbench_cache_cohort_key", lambda: "test")

    assert asyncio.run(run_check()) == {}


def test_builder_failure_clears_inflight_and_does_not_cache() -> None:
    async def run_check() -> tuple[dict, dict]:
        cache = WorkbenchContextCache()

        async def build(_symbol: str) -> WorkbenchContext:
            raise RuntimeError("deterministic builder failure")

        with pytest.raises(RuntimeError, match="builder failure"):
            await cache.get("600519", build)
        return dict(cache.entries), dict(cache._inflight)  # noqa: SLF001

    assert asyncio.run(run_check()) == ({}, {})


def test_invalid_requested_identity_is_rejected_and_cache_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_check() -> dict[str, tuple[float, WorkbenchContext]]:
        cache = WorkbenchContextCache()
        context = _bound_context("600519.SH")
        context.requested_symbol = None  # type: ignore[assignment]
        cache.entries["600519.SH"] = (time.monotonic(), context)

        async def should_not_build(_symbol: str) -> WorkbenchContext:
            raise AssertionError("invalid fresh cache entry must fail closed")

        with pytest.raises(WorkbenchContextIntegrityError, match="请求身份字段无效"):
            await cache.get("600519", should_not_build)
        return dict(cache.entries)

    monkeypatch.setattr(workbench_context_service, "workbench_cache_cohort_key", lambda: "test")

    assert asyncio.run(run_check()) == {}


def test_profile_and_review_owners_are_part_of_context_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _bound_context("600519.SH")
    context.analysis.stock_profile = SimpleNamespace(symbol="600519.SH")
    context.analysis.review = SimpleNamespace(symbol="000001.SZ")

    monkeypatch.setattr(workbench_context_service, "workbench_cache_cohort_key", lambda: "test")

    with pytest.raises(WorkbenchContextIntegrityError, match="股票身份绑定不一致"):
        workbench_context_service._require_context_binding(context, "600519.SH")  # noqa: SLF001


def test_malformed_child_owner_is_rejected_as_invalid_identity() -> None:
    context = _bound_context("600519.SH")
    context.factor_lab.symbol = object()

    with pytest.raises(WorkbenchContextIntegrityError, match="股票身份字段无效"):
        workbench_context_service._require_context_binding(context, "600519.SH")  # noqa: SLF001


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: setattr(value, "context_generated_at", "not-a-time"), "研究时点字段无效"),
        (lambda value: setattr(value, "context_generated_at", "2099-01-01 06:00:00"), "决策时点不能位于未来"),
        (lambda value: setattr(value, "quote_event_time", "2026-08-13 05:58:00"), "行情时点与研究决策不一致"),
        (lambda value: setattr(value, "signal_date", "2026-08-12"), "行情交易日与研究批次不一致"),
        (lambda value: setattr(value.factor_lab, "updated_at", "not-a-time"), "factor_lab 研究时点无效"),
        (lambda value: setattr(value.factor_lab, "updated_at", "2026-08-12 05:59:00"), "factor_lab 不属于行情交易日"),
    ],
)
def test_context_time_metadata_attacks_fail_closed(
    mutation,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _bound_context("600519.SH")
    mutation(context)
    monkeypatch.setattr(
        workbench_context_service,
        "utc_now",
        lambda: datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(WorkbenchContextIntegrityError, match=message):
        workbench_context_service._require_context_binding(context, "600519.SH")  # noqa: SLF001


def test_calendar_resolution_failure_marks_context_cohort_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _bound_context("600519.SH")
    monkeypatch.setattr(
        workbench_context_service,
        "workbench_cache_cohort_key",
        lambda: (_ for _ in ()).throw(RuntimeError("calendar unavailable")),
    )

    assert workbench_context_service._context_cache_cohort_is_current(context) is False  # noqa: SLF001


@pytest.mark.parametrize("value", [None, "bad", 0, -1, float("inf")])
def test_invalid_shutdown_timeout_uses_bounded_default(value: object) -> None:
    cache = WorkbenchContextCache(shutdown_timeout_seconds=value)  # type: ignore[arg-type]

    assert cache.shutdown_timeout_seconds == 5.0


def test_completed_cache_task_preserves_context_and_normalized_name() -> None:
    async def run_check() -> tuple[WorkbenchContext, str]:
        context = _bound_context("600519.SH")
        task = workbench_context_service._completed_context_task(  # noqa: SLF001
            context,
            "600519.SH",
        )
        return await task, task.get_name()

    context, name = asyncio.run(run_check())

    assert context.requested_symbol == "600519.SH"
    assert name == "stock-workbench-cached-600519.SH"


def test_close_without_inflight_work_is_a_noop() -> None:
    cache = WorkbenchContextCache()

    asyncio.run(cache.aclose())

    assert cache.entries == {}


@pytest.mark.parametrize("appears_on_call", [2, 3])
def test_cache_entry_appearing_during_locked_double_check_is_reused(
    appears_on_call: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_check() -> tuple[WorkbenchContext, int, int]:
        cache = WorkbenchContextCache()
        context = _bound_context("600519.SH")
        fresh_calls = 0
        build_calls = 0

        def entry_appears(_symbol: str) -> WorkbenchContext | None:
            nonlocal fresh_calls
            fresh_calls += 1
            return context if fresh_calls >= appears_on_call else None

        async def must_not_build(_symbol: str) -> WorkbenchContext:
            nonlocal build_calls
            build_calls += 1
            raise AssertionError("cache entry appeared before build creation")

        monkeypatch.setattr(cache, "_fresh_entry", entry_appears)
        result = await cache.get("600519", must_not_build)
        return result, fresh_calls, build_calls

    monkeypatch.setattr(workbench_context_service, "workbench_cache_cohort_key", lambda: "test")

    result, fresh_calls, build_calls = asyncio.run(run_check())
    assert result.requested_symbol == "600519.SH"
    assert fresh_calls == appears_on_call
    assert build_calls == 0


def test_workbench_cohort_key_binds_phase_quote_and_bar_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workbench_context_service, "market_session_phase", lambda: "after_close")
    monkeypatch.setattr(workbench_context_service, "expected_quote_date", lambda: date(2026, 8, 13))
    monkeypatch.setattr(
        workbench_context_service,
        "latest_expected_daily_kline_date",
        lambda: date(2026, 8, 12),
    )

    assert workbench_context_service.workbench_cache_cohort_key() == (
        "after_close:2026-08-13:2026-08-12"
    )


def test_requested_symbol_cannot_disagree_with_observed_owners() -> None:
    context = _bound_context("600519.SH")
    context.requested_symbol = "000001.SZ"

    with pytest.raises(WorkbenchContextIntegrityError, match="请求身份绑定不一致"):
        workbench_context_service._require_context_binding(context, "600519.SH")  # noqa: SLF001


def test_compact_but_noncanonical_signal_date_is_rejected() -> None:
    context = _bound_context("600519.SH")
    context.signal_date = "20260813"

    with pytest.raises(WorkbenchContextIntegrityError, match="研究时点字段无效"):
        workbench_context_service._require_context_binding(context, "600519.SH")  # noqa: SLF001


def test_cleanup_consumes_cancelled_error_from_future_exception_reader() -> None:
    class CancelledDuringRead:
        @staticmethod
        def cancelled() -> bool:
            return False

        @staticmethod
        def exception() -> None:
            raise asyncio.CancelledError

    workbench_context_service._consume_task_exception(CancelledDuringRead())  # type: ignore[arg-type]  # noqa: SLF001
