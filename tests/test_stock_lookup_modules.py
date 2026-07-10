from __future__ import annotations

import asyncio

import pytest

from app.models.schemas import Quote
from app.utils.errors import NotFoundError
from app.workflows.stock_lookup import confirmed_stock_profile
from tests.factories import make_quote


def test_confirmed_stock_profile_falls_back_to_matching_quote_when_stock_pool_is_down() -> None:
    quote = _quote(code="600706", market="SH", name="曲江文旅", source="腾讯行情")
    hub = _LookupHub(profile_error=RuntimeError("所有股票池数据源均不可用"), quote=quote)

    profile = asyncio.run(confirmed_stock_profile(hub, "600706.SH"))

    assert profile.symbol == "600706.SH"
    assert profile.name == "曲江文旅"
    assert profile.industry is None
    assert profile.source == "腾讯行情确认"
    assert hub.profile_calls == ["600706.SH"]
    assert hub.quote_calls == ["600706.SH"]
    assert hub.cache.events == [("fallback", "股票池暂不可用，使用行情确认股票代码：600706.SH")]


def test_confirmed_stock_profile_falls_back_to_quote_when_stock_pool_is_slow() -> None:
    quote = _quote(code="600706", market="SH", name="曲江文旅", source="腾讯行情")
    hub = _LookupHub(profile_delay=1, quote=quote, optional_timeout=0.01)

    profile = asyncio.run(confirmed_stock_profile(hub, "600706.SH"))

    assert profile.symbol == "600706.SH"
    assert profile.name == "曲江文旅"
    assert hub.profile_calls == ["600706.SH"]
    assert hub.quote_calls == ["600706.SH"]
    assert hub.cache.events == [("fallback", "股票池查询超时，使用行情确认股票代码：600706.SH")]


def test_confirmed_stock_profile_ignores_log_event_failure_on_quote_confirmation() -> None:
    quote = _quote(code="600706", market="SH", name="曲江文旅", source="腾讯行情")
    hub = _LookupHub(profile_error=RuntimeError("所有股票池数据源均不可用"), quote=quote, cache=_FailingEventCache())

    profile = asyncio.run(confirmed_stock_profile(hub, "600706.SH"))

    assert profile.symbol == "600706.SH"
    assert profile.name == "曲江文旅"
    assert hub.profile_calls == ["600706.SH"]
    assert hub.quote_calls == ["600706.SH"]


def test_confirmed_stock_profile_falls_back_to_matching_quote_when_stock_pool_misses() -> None:
    hub = _LookupHub(profile=None, quote=_quote(code="600706", market="SH", name="曲江文旅"))

    profile = asyncio.run(confirmed_stock_profile(hub, "600706.SH"))

    assert profile is not None
    assert profile.symbol == "600706.SH"
    assert profile.name == "曲江文旅"
    assert hub.profile_calls == ["600706.SH"]
    assert hub.quote_calls == ["600706.SH"]
    assert hub.cache.events == [("fallback", "股票池未命中，使用行情确认股票代码：600706.SH")]


def test_confirmed_stock_profile_reports_not_found_when_pool_and_quote_miss() -> None:
    hub = _LookupHub(profile=None, quote_error=RuntimeError("quote unavailable"))

    with pytest.raises(NotFoundError, match="实时行情也无法确认"):
        asyncio.run(confirmed_stock_profile(hub, "600706.SH"))

    assert hub.profile_calls == ["600706.SH"]
    assert hub.quote_calls == ["600706.SH"]
    assert hub.cache.events == []


def test_confirmed_stock_profile_does_not_confirm_with_cached_quote() -> None:
    hub = _LookupHub(
        profile_error=RuntimeError("所有股票池数据源均不可用"),
        quote=_quote(code="600706", market="SH", name="曲江文旅").model_copy(update={"from_cache": True}),
    )

    with pytest.raises(RuntimeError, match="股票池暂不可用，无法确认股票代码：600706.SH"):
        asyncio.run(confirmed_stock_profile(hub, "600706.SH"))

    assert hub.quote_calls == ["600706.SH"]
    assert hub.quote_use_cache == [False]
    assert hub.cache.events == []


def test_confirmed_stock_profile_rejects_mismatched_quote_after_pool_failure() -> None:
    hub = _LookupHub(
        profile_error=RuntimeError("所有股票池数据源均不可用"),
        quote=_quote(code="600519", market="SH", name="贵州茅台"),
    )

    with pytest.raises(RuntimeError, match="股票池暂不可用，无法确认股票代码：600706.SH"):
        asyncio.run(confirmed_stock_profile(hub, "600706.SH"))

    assert hub.profile_calls == ["600706.SH"]
    assert hub.quote_calls == ["600706.SH"]
    assert hub.cache.events == []


def test_confirmed_stock_profile_does_not_swallow_unexpected_quote_errors() -> None:
    hub = _LookupHub(
        profile_error=RuntimeError("所有股票池数据源均不可用"),
        quote_error=ValueError("quote mapper bug"),
    )

    with pytest.raises(ValueError, match="quote mapper bug"):
        asyncio.run(confirmed_stock_profile(hub, "600706.SH"))

    assert hub.profile_calls == ["600706.SH"]
    assert hub.quote_calls == ["600706.SH"]
    assert hub.cache.events == []


class _LookupHub:
    def __init__(
        self,
        *,
        profile=None,
        profile_error: Exception | None = None,
        profile_delay: float = 0,
        quote: Quote | None = None,
        quote_error: Exception | None = None,
        optional_timeout: float = 1.5,
        cache=None,
    ) -> None:
        self.profile = profile
        self.profile_error = profile_error
        self.profile_delay = profile_delay
        self.quote_result = quote
        self.quote_error = quote_error
        self.settings = _Settings(optional_timeout)
        self.cache = cache or _EventCache()
        self.profile_calls: list[str] = []
        self.quote_calls: list[str] = []
        self.quote_use_cache: list[bool] = []

    async def stock_profile(self, symbol: str):
        self.profile_calls.append(symbol)
        if self.profile_delay:
            await asyncio.sleep(self.profile_delay)
        if self.profile_error is not None:
            raise self.profile_error
        return self.profile

    async def quote(self, symbol: str, use_cache: bool = True) -> Quote:
        self.quote_calls.append(symbol)
        self.quote_use_cache.append(use_cache)
        if self.quote_error is not None:
            raise self.quote_error
        if self.quote_result is None:
            raise RuntimeError("行情不可用")
        return self.quote_result


class _EventCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log_event(self, level: str, message: str) -> None:
        self.events.append((level, message))


class _Settings:
    def __init__(self, timeout: float) -> None:
        self.workbench_optional_timeout_seconds = timeout


class _FailingEventCache:
    def log_event(self, level: str, message: str) -> None:
        raise RuntimeError("cache log down")


def _quote(*, code: str, market: str, name: str, source: str = "测试行情") -> Quote:
    return make_quote(source=source).model_copy(update={"code": code, "market": market, "name": name})
