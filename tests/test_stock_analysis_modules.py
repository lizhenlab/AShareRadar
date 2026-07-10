from __future__ import annotations

import asyncio

from app.workflows.stock_analysis import _peer_quote_sample_or_fallback, _peer_sample_info
from tests.factories import make_stock_info


def test_peer_sample_stock_pool_failure_reaches_analysis_contract() -> None:
    hub = _PeerHub(stock_pool_delay=0)
    hub.fail_stock_pool = True
    profile = make_stock_info().model_copy(update={"industry": "白酒"})

    sample = asyncio.run(_peer_quote_sample_or_fallback(hub, profile, "600519.SH"))  # type: ignore[arg-type]
    info = _peer_sample_info(sample)

    assert info.status == "unavailable"
    assert info.warning == "白酒同行股票池暂不可用。"
    assert any("白酒同行股票池不可用" in message for _, message in hub.cache.events)


def test_peer_sample_timeout_uses_stable_warning_without_internal_error_text() -> None:
    hub = _PeerHub(stock_pool_delay=1)
    hub.settings.workbench_optional_timeout_seconds = 0.01
    profile = make_stock_info().model_copy(update={"industry": "白酒"})

    sample = asyncio.run(_peer_quote_sample_or_fallback(hub, profile, "600519.SH"))  # type: ignore[arg-type]
    info = _peer_sample_info(sample)

    assert info.status == "unavailable"
    assert info.warning == "同行样本请求失败，当前仅使用个股历史和行业背景。"
    assert "TimeoutError" not in info.warning
    assert any("TimeoutError" in message for _, message in hub.cache.events)


class _Settings:
    seed_symbols: tuple[str, ...] = ()
    workbench_optional_timeout_seconds = 0.5


class _EventCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log_event(self, category: str, message: str) -> None:
        self.events.append((category, message))


class _PeerHub:
    def __init__(self, *, stock_pool_delay: float) -> None:
        self.settings = _Settings()
        self.cache = _EventCache()
        self.stock_pool_delay = stock_pool_delay
        self.fail_stock_pool = False

    async def stock_pool(self, limit: int = 1200, refresh: bool = False):
        if self.stock_pool_delay:
            await asyncio.sleep(self.stock_pool_delay)
        if self.fail_stock_pool:
            raise RuntimeError("private provider detail")
        return []
