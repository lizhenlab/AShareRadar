from __future__ import annotations

import asyncio
from datetime import datetime
import threading

import pytest

from app.config import Settings
from app.services.cache import SQLiteCache
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_provider_io
from tests.factories import make_quote


NOW = datetime(2026, 9, 8, 10, 20)


class BlockingQuoteProvider:
    source_name = "合成同步行情源"

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    async def quotes(self, symbols):
        def fetch():
            self.calls += 1
            self.started.set()
            if not self.release.wait(3):
                raise RuntimeError("test release timed out")
            return [make_quote(source=self.source_name, timestamp=NOW.isoformat(" "))]
        return await run_provider_io(fetch)


@pytest.fixture
def rejoin_hub(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config_settings._default_shell_env_values", lambda: {})
    provider = BlockingQuoteProvider()
    settings = Settings(cache_path=tmp_path / "rejoin.sqlite3", quote_provider_priority=("akshare",),
                        provider_call_timeout_seconds=1, provider_failure_cooldown_seconds=0)
    monkeypatch.setattr("app.services.datahub.build_providers", lambda _settings: {"akshare": provider})
    hub = DataHub(SQLiteCache(settings=settings), settings=settings)
    hub._quote_coordinator._now = lambda: NOW
    return hub, provider


@pytest.mark.parametrize("departure", ["cancel", "timeout"])
def test_public_quote_retry_rejoins_existing_worker_but_different_key_is_busy(rejoin_hub, departure):
    async def check():
        hub, provider = rejoin_hub
        if departure == "timeout":
            hub.settings.provider_call_timeout_seconds = .05
        first = asyncio.create_task(hub.quote("600519.SH", use_cache=False))
        second = None
        try:
            assert await asyncio.to_thread(provider.started.wait, 1)
            if departure == "cancel":
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            else:
                with pytest.raises(RuntimeError, match="超过"):
                    await first
            assert hub._provider_runtime.provider_call_in_flight("akshare", "quote")
            with pytest.raises(RuntimeError, match="后台"):
                await hub.quote("000001.SZ", use_cache=False)
            hub.settings.provider_call_timeout_seconds = 1
            second = asyncio.create_task(hub.quote("600519.SH", use_cache=False))
            await asyncio.sleep(.01)
            assert not second.done()
            provider.release.set()
            quote = await second
            assert quote.code == "600519"
            assert provider.calls == 1
            assert not hub._provider_runtime.provider_call_in_flight("akshare", "quote")
        finally:
            provider.release.set()
            await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)
            await hub.aclose()
    asyncio.run(check())


def test_real_timeout_cooldown_still_blocks_same_key_retry(rejoin_hub):
    async def check():
        hub, provider = rejoin_hub
        hub.settings.provider_call_timeout_seconds = .05
        hub.settings.provider_failure_cooldown_seconds = 60
        first = asyncio.create_task(hub.quote("600519.SH", use_cache=False))
        try:
            assert await asyncio.to_thread(provider.started.wait, 1)
            with pytest.raises(RuntimeError, match="超过"):
                await first
            assert hub._provider_runtime.is_cooling("akshare", "quote")
            with pytest.raises(RuntimeError, match="冷却"):
                await hub.quote("600519.SH", use_cache=False)
            assert provider.calls == 1
        finally:
            provider.release.set()
            await asyncio.gather(first, return_exceptions=True)
            await hub.aclose()
    asyncio.run(check())
