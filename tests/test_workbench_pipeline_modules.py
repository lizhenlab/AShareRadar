from __future__ import annotations

import asyncio
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from app.models.schemas import ProviderCapability
from app.models.workbench import (
    _validate_workbench_child_symbols,
    _validate_workbench_child_times,
    _validate_workbench_metadata,
    _validate_workbench_quote_binding,
)
from app.workflows.workbench_pipeline import (
    _bound_concepts,
    _bound_order_book,
    _market_breadth_sample_or_empty,
    _order_book_or_error,
    _stock_concepts_or_error,
    _time_aligned_breadth,
)
from tests.factories import make_quote


def test_backend_v2_rejects_swapped_support_panel_owner() -> None:
    workbench = _workbench_contract_stub()
    workbench.peer_comparison.symbol = "000001.SZ"

    with pytest.raises(ValueError, match="peer_comparison symbol mismatch"):
        _validate_workbench_child_symbols(workbench, "600519.SH")


def test_backend_v2_rejects_swapped_strategy_card_owner() -> None:
    workbench = _workbench_contract_stub()
    workbench.insights.strategy_cards[0].symbol = "000001.SZ"

    with pytest.raises(ValueError, match="insights.strategy_card symbol mismatch"):
        _validate_workbench_child_symbols(workbench, "600519.SH")


@pytest.mark.parametrize(
    ("field", "updated_at", "message"),
    [
        ("event_digest", "2026-08-12 15:00:00", "update must match signal date"),
        ("risk_radar", "2026-08-13 10:01:00", "update cannot follow decision time"),
    ],
)
def test_backend_v2_rejects_old_or_post_decision_research_child(
    field: str,
    updated_at: str,
    message: str,
) -> None:
    workbench = _workbench_contract_stub()
    getattr(workbench, field).updated_at = updated_at

    with pytest.raises(ValueError, match=message):
        _validate_workbench_child_times(workbench)


@pytest.mark.parametrize(
    "dates",
    [
        ["2026-08-12", "2026-08-11"],
        ["2026-08-11", "2026-08-11"],
    ],
)
def test_backend_v2_rejects_non_increasing_daily_bars(dates: list[str]) -> None:
    workbench = _workbench_contract_stub()
    workbench.analysis.klines = [SimpleNamespace(date=value) for value in dates]

    with pytest.raises(ValueError, match="strictly increasing"):
        _validate_workbench_quote_binding(workbench, "600519.SH")


def test_backend_v2_rejects_daily_bar_after_declared_cutoff() -> None:
    workbench = _workbench_contract_stub()
    workbench.analysis.klines.append(SimpleNamespace(date="2026-08-13"))

    with pytest.raises(ValueError, match="daily bar cannot follow cutoff"):
        _validate_workbench_quote_binding(workbench, "600519.SH")


def test_backend_v2_rejects_future_decision_and_generated_times() -> None:
    workbench = _workbench_contract_stub()
    workbench.generated_at = "2099-01-01T00:00:01Z"
    workbench.context_generated_at = "2099-01-01T00:00:00Z"
    workbench.research_cohort.decision_time = "2099-01-01T00:00:00Z"

    with pytest.raises(ValueError, match="cannot be in the future"):
        _validate_workbench_metadata(workbench, "600519.SH")


def _workbench_contract_stub() -> SimpleNamespace:
    symbol = "600519.SH"
    child = SimpleNamespace(symbol=symbol, updated_at="2026-08-13 09:59:00")
    workbench = SimpleNamespace(
        symbol=symbol,
        generated_at="2026-08-13 10:00:00",
        context_generated_at="2026-08-13 10:00:00",
        research_mode="interactive_shadow",
        production_effect="none",
        research_cohort=SimpleNamespace(
            requested_symbol=symbol,
            observed_symbol=symbol,
            mode="interactive_shadow",
            decision_time="2026-08-13 10:00:00",
            quote_event_time="2026-08-13 09:59:00",
            signal_date="2026-08-13",
            daily_bar_cutoff="2026-08-12",
            production_effect="none",
        ),
        analysis=SimpleNamespace(
            quote=SimpleNamespace(code="600519", market="SH", timestamp="2026-08-13 09:59:00"),
            klines=[SimpleNamespace(date="2026-08-11"), SimpleNamespace(date="2026-08-12")],
            stock_profile=None,
            review=None,
        ),
        insights=SimpleNamespace(**{
            field: deepcopy(child)
            for field in (
                "overview", "fund_flow", "order_pressure", "events", "financial_health",
                "valuation", "lhb", "abnormal_events", "rule_matches",
            )
        }, strategy_cards=[deepcopy(child)]),
        alert_rules=[],
        alert_events=[],
        notes=[],
    )
    for field in (
        "feature_snapshot", "factor_lab", "market_regime", "signal_validation",
        "risk_reward", "timeframe_alignment", "alpha_evidence", "diagnosis",
        "evidence_chain", "qa_report", "event_digest", "peer_comparison",
        "t_strategy", "risk_radar", "chip_analysis", "leadership", "theme_context", "replay",
    ):
        setattr(workbench, field, deepcopy(child))
    workbench.chart_marks = SimpleNamespace(symbol=symbol)
    return workbench


def test_order_book_or_error_reports_disabled_futu_without_provider_call() -> None:
    class DisabledFutuProvider:
        def capability(self) -> ProviderCapability:
            return _capability(enabled=False)

    class DataHubStub:
        providers = {"futu": DisabledFutuProvider()}

        async def order_book(self, symbol: str):
            raise AssertionError("disabled Futu should skip order_book")

    order_book, error = asyncio.run(_order_book_or_error(DataHubStub(), "600519"))  # type: ignore[arg-type]

    assert order_book is None
    assert error == "Futu OpenAPI 未启用，盘口压力使用行情区间估算。"


def test_order_book_or_error_uses_readable_error_for_empty_exception() -> None:
    class EnabledFutuProvider:
        def capability(self) -> ProviderCapability:
            return _capability(enabled=True)

    class DataHubStub:
        providers = {"futu": EnabledFutuProvider()}

        async def order_book(self, symbol: str):
            raise TimeoutError()

    order_book, error = asyncio.run(_order_book_or_error(DataHubStub(), "600519"))  # type: ignore[arg-type]

    assert order_book is None
    assert error == "TimeoutError: 数据源响应超时"


def test_order_book_or_error_handles_futu_capability_failure_as_degraded_order_book() -> None:
    class BrokenCapabilityProvider:
        def capability(self) -> ProviderCapability:
            raise RuntimeError("capability probe down")

    class DataHubStub:
        providers = {"futu": BrokenCapabilityProvider()}

        async def order_book(self, symbol: str):
            raise AssertionError("capability failure should skip order_book")

    order_book, error = asyncio.run(_order_book_or_error(DataHubStub(), "600519"))  # type: ignore[arg-type]

    assert order_book is None
    assert error == "capability probe down"


def test_stock_concepts_or_error_keeps_workbench_renderable_on_source_failure() -> None:
    class DataHubStub:
        async def cached_stock_concepts_result(self, symbol: str, limit: int = 8):
            raise RuntimeError("概念归属不可用：600706.SH；akshare: concept down")

    concepts, error = asyncio.run(_stock_concepts_or_error(DataHubStub(), "600706"))  # type: ignore[arg-type]

    assert concepts == []
    assert error == "概念归属不可用：600706.SH；akshare: concept down"


def test_stock_concepts_or_error_times_out_as_optional_data() -> None:
    class SettingsStub:
        workbench_optional_timeout_seconds = 0.01

    class DataHubStub:
        settings = SettingsStub()

        async def cached_stock_concepts_result(self, symbol: str, limit: int = 8):
            await asyncio.sleep(1)
            return SimpleNamespace(rows=[], used_fallback_cache=False)

    concepts, error = asyncio.run(_stock_concepts_or_error(DataHubStub(), "600706"))  # type: ignore[arg-type]

    assert concepts == []
    assert error == "TimeoutError: 数据源响应超时"


def test_workbench_concepts_use_cache_only_loader_without_provider_fallback() -> None:
    calls: list[str] = []

    class DataHubStub:
        async def cached_stock_concepts_result(self, symbol: str, limit: int = 8):
            calls.append("cache")
            raise RuntimeError("概念归属新鲜非静态缓存不可用：600706.SH；交互式研究不发起在线概念扫描")

        async def stock_concepts_result(self, symbol: str, limit: int = 8):
            calls.append("provider")
            raise AssertionError("workbench must not start a concept provider call")

    concepts, error = asyncio.run(_stock_concepts_or_error(DataHubStub(), "600706"))  # type: ignore[arg-type]

    assert concepts == []
    assert error == "概念归属新鲜非静态缓存不可用：600706.SH；交互式研究不发起在线概念扫描"
    assert calls == ["cache"]


def test_workbench_legacy_hub_without_cache_only_api_never_uses_provider_capable_fallbacks() -> None:
    calls: list[str] = []

    class LegacyDataHubStub:
        async def stock_concepts_result(self, symbol: str, limit: int = 8):
            calls.append("provider-result")
            return SimpleNamespace(rows=[object()], used_fallback_cache=False)

        async def stock_concepts(self, symbol: str, limit: int = 8):
            calls.append("provider")
            return [object()]

    concepts, error = asyncio.run(_stock_concepts_or_error(LegacyDataHubStub(), "600706"))  # type: ignore[arg-type]

    assert concepts == []
    assert "cached_stock_concepts_result" in str(error)
    assert calls == []


def test_workbench_cache_only_concepts_still_reject_fallback_rows() -> None:
    class DataHubStub:
        async def cached_stock_concepts_result(self, symbol: str, limit: int = 8):
            return SimpleNamespace(rows=[object()], used_fallback_cache=True)

    concepts, error = asyncio.run(_stock_concepts_or_error(DataHubStub(), "600706"))  # type: ignore[arg-type]

    assert concepts == []
    assert error == "概念数据源不可用，过期缓存不参与主题与龙头强度评分。"


def test_stale_concept_cache_is_withheld_from_theme_and_leader_scoring() -> None:
    class DataHubStub:
        async def cached_stock_concepts_result(self, symbol: str, limit: int = 8):
            return SimpleNamespace(rows=[object()], used_fallback_cache=True)

    concepts, error = asyncio.run(_stock_concepts_or_error(DataHubStub(), "600706"))  # type: ignore[arg-type]

    assert concepts == []
    assert error == "概念数据源不可用，过期缓存不参与主题与龙头强度评分。"


def test_cross_session_optional_evidence_is_withheld_from_research_scores() -> None:
    analysis = SimpleNamespace(quote=make_quote().model_copy(update={"timestamp": "2026-08-12 10:00:00"}))
    aligned = make_quote().model_copy(update={"timestamp": "2026-08-12 09:59:00"})
    stale = make_quote().model_copy(update={"timestamp": "2026-08-11 15:00:00"})

    breadth, warnings = _time_aligned_breadth(analysis, [aligned, stale], ())  # type: ignore[arg-type]

    assert breadth == [aligned]
    assert warnings == ("市场宽度含不同交易日或降级行情，已从本次环境评分剔除。",)


def test_same_session_future_optional_evidence_is_withheld() -> None:
    analysis = SimpleNamespace(quote=make_quote().model_copy(update={"timestamp": "2026-08-12 10:00:00"}))
    future = make_quote().model_copy(update={"timestamp": "2026-08-12 14:00:00"})
    order_book = SimpleNamespace(symbol="600519.SH", updated_at="2026-08-12 14:00:00")
    concepts = [
        SimpleNamespace(symbol="600519.SH", updated_at="2026-08-12 14:00:00", fallback_used=False)
    ]

    breadth, _warnings = _time_aligned_breadth(analysis, [future], ())  # type: ignore[arg-type]
    bound_book, _book_error = _bound_order_book(analysis, order_book, None)  # type: ignore[arg-type]
    bound_concepts, _concept_error = _bound_concepts(analysis, concepts, None)  # type: ignore[arg-type]

    assert breadth == []
    assert bound_book is None
    assert bound_concepts == []


def test_cross_symbol_order_book_and_concepts_fail_closed() -> None:
    analysis = SimpleNamespace(quote=make_quote().model_copy(update={"timestamp": "2026-08-12 10:00:00"}))
    order_book = SimpleNamespace(symbol="000001.SZ", updated_at="2026-08-12 10:00:00")
    concepts = [
        SimpleNamespace(
            symbol="000001.SZ",
            updated_at="2026-08-12 10:00:00",
            fallback_used=False,
        )
    ]

    bound_book, book_error = _bound_order_book(analysis, order_book, None)  # type: ignore[arg-type]
    bound_concepts, concept_error = _bound_concepts(analysis, concepts, None)  # type: ignore[arg-type]

    assert bound_book is None
    assert "股票身份" in str(book_error)
    assert bound_concepts == []
    assert "不同股票" in str(concept_error)


def test_market_breadth_sample_preserves_all_quote_failure_as_warning() -> None:
    cache = _EventCache()

    class SettingsStub:
        seed_symbols = ("600519.SH",)
        workbench_optional_timeout_seconds = 0.5

    class DataHubStub:
        settings = SettingsStub()

        async def stock_pool(self, limit: int, refresh: bool):
            return []

        async def quotes(self, symbols):
            raise RuntimeError("provider secret detail")

    hub = DataHubStub()
    hub.cache = cache

    result = asyncio.run(_market_breadth_sample_or_empty(hub))  # type: ignore[arg-type]

    assert result.quotes == ()
    assert result.quote_sample.requested_count == 1
    assert result.quote_sample.unavailable is True
    assert result.warnings == ("市场宽度行情样本暂不可用，环境判断已降级。",)
    assert "provider secret detail" not in " ".join(result.warnings)


def test_market_breadth_sample_timeout_returns_stable_user_warning() -> None:
    cache = _EventCache()

    class SettingsStub:
        seed_symbols = ("600519.SH",)
        workbench_optional_timeout_seconds = 0.01

    class DataHubStub:
        settings = SettingsStub()

        async def stock_pool(self, limit: int, refresh: bool):
            await asyncio.sleep(1)
            return []

    hub = DataHubStub()
    hub.cache = cache

    async def run_check():
        event_loop_thread = threading.get_ident()
        result = await _market_breadth_sample_or_empty(hub)  # type: ignore[arg-type]
        return result, event_loop_thread

    result, event_loop_thread = asyncio.run(run_check())

    assert result.quotes == ()
    assert result.warnings == ("市场宽度数据源请求失败，环境判断已降级。",)
    assert any("TimeoutError" in message for _, message in cache.events)
    assert cache.io_threads
    assert all(thread_id != event_loop_thread for thread_id in cache.io_threads)


def test_market_breadth_cancellation_propagates_without_fallback_log() -> None:
    cache = _EventCache()

    class SettingsStub:
        seed_symbols = ("600519.SH",)
        workbench_optional_timeout_seconds = 0.5

    class DataHubStub:
        settings = SettingsStub()

        async def stock_pool(self, limit: int, refresh: bool):
            raise asyncio.CancelledError()

    hub = DataHubStub()
    hub.cache = cache

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_market_breadth_sample_or_empty(hub))  # type: ignore[arg-type]

    assert cache.events == []


def _capability(*, enabled: bool) -> ProviderCapability:
    return ProviderCapability(
        name="futu",
        installed=True,
        enabled=enabled,
        order_book=enabled,
        note="测试盘口能力",
    )


class _EventCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.io_threads: list[int] = []

    def log_event(self, category: str, message: str) -> None:
        self.io_threads.append(threading.get_ident())
        self.events.append((category, message))
