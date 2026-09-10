from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.models.market import MinuteKline, ProviderCapability
from app.services.cache import SQLiteCache
from app.services.datahub import DataHub
from app.services.datahub_cache import _minute_kline_cache_is_fresh


NOW = datetime(2026, 9, 8, 10, 20)
FRESH_TIMESTAMP = "2026-09-08 10:15:00"


class TimestampProvider:
    source_name = "合成分钟时间源"

    def __init__(self):
        self.timestamp = FRESH_TIMESTAMP
        self.calls = 0

    def capability(self):
        return ProviderCapability(name="akshare", installed=True, enabled=True,
                                  minute_kline=True, note="synthetic")

    async def minute_kline(self, symbol, interval="5m", limit=120):
        self.calls += 1
        return [_row(self.timestamp)]


def _row(timestamp):
    return MinuteKline(timestamp=timestamp, open=10, close=10, high=11, low=9,
                       volume=100, amount=1000, interval="5m", source=TimestampProvider.source_name)


@pytest.fixture
def timestamp_hub(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config_settings._default_shell_env_values", lambda: {})
    provider = TimestampProvider()
    settings = Settings(cache_path=tmp_path / "minute-timezone.sqlite3",
                        minute_provider_priority=("akshare",), provider_failure_cooldown_seconds=0)
    monkeypatch.setattr("app.services.datahub.build_providers", lambda _settings: {"akshare": provider})
    hub = DataHub(SQLiteCache(settings=settings), settings=settings)
    hub._kline_coordinator._now = lambda: NOW
    return hub, provider


@pytest.mark.parametrize("timestamp", [
    FRESH_TIMESTAMP,
    "2026-09-08T10:15:00",
    "2026-09-08 10:15",
    "2026-09-08T10:15:00.123456+08:00",
    "2026-09-08T02:15:00Z",
    "2026-09-08T02:15:00+00:00",
    "2026-09-08T05:15:00+03:00",
    "2026-09-07T22:15:00-04:00",
])
def test_minute_provider_and_cache_accept_same_market_instant(timestamp_hub, timestamp):
    async def check():
        hub, provider = timestamp_hub
        provider.timestamp = timestamp
        try:
            fetched = await hub.minute_kline("600519.SH", "5m", 1)
            cached = await hub.minute_kline("600519.SH", "5m", 1)
            assert fetched[0].timestamp == cached[0].timestamp == timestamp
            assert not fetched[0].from_cache and not fetched[0].fallback_used
            assert cached[0].from_cache and not cached[0].fallback_used
            assert provider.calls == 1
        finally:
            await hub.aclose()
    asyncio.run(check())


INVALID_TIMESTAMPS = [
    "2026-09-08T10:15:00+00:00",
    "2026-09-08T10:15:00-04:00",
    "2026-09-08T10:15:00+07:00",
    "2026-09-08T10:15:00+09:00",
    "2026-09-08T10:20:00.000001+08:00",
    "2026-09-08T10:15:00+invalid",
    "2026-09-08T10:15:00Zgarbage",
    "2026-09-08T10:15:00.125garbage",
    "0001-01-01T00:00:00+14:00",
]


@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
def test_minute_provider_rejects_future_stale_and_malformed_times(timestamp_hub, timestamp):
    async def check():
        hub, provider = timestamp_hub
        provider.timestamp = timestamp
        try:
            with pytest.raises(RuntimeError, match="所有分钟K线数据源均不可用"):
                await hub.minute_kline("600519.SH", "5m", 1)
            assert not hub.cache.get_minute_klines("600519.SH", "5m", 1, 3600)
            assert provider.calls == 1
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
def test_minute_cache_does_not_hide_invalid_time_behind_wall_clock_prefix(timestamp_hub, timestamp):
    async def check():
        hub, provider = timestamp_hub
        hub.cache.save_minute_klines("600519.SH", "5m", [_row(timestamp)], provider.source_name)
        try:
            result = await hub.minute_kline("600519.SH", "5m", 1)
            assert result[0].timestamp == FRESH_TIMESTAMP
            assert not result[0].from_cache and not result[0].fallback_used
            assert provider.calls == 1
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("offset", [0, 3, -4, 8])
@pytest.mark.parametrize(("now", "event", "expected"), [
    (datetime(2026, 9, 8, 9, 20), datetime(2026, 9, 8, 9, 15), True),
    (datetime(2026, 9, 8, 12), datetime(2026, 9, 8, 11, 25), True),
    (datetime(2026, 9, 8, 13, 20), datetime(2026, 9, 8, 11, 25), False),
    (datetime(2026, 9, 8, 15, 30), datetime(2026, 9, 8, 14, 55), True),
    (datetime(2026, 9, 12, 10), datetime(2026, 9, 11, 14, 55), True),
])
def test_minute_timezone_normalization_preserves_session_rules(offset, now, event, expected):
    localized = event.replace(tzinfo=timezone(timedelta(hours=8)))
    timestamp = localized.astimezone(timezone(timedelta(hours=offset))).isoformat()
    assert _minute_kline_cache_is_fresh([_row(timestamp)], "5m", now=now) is expected
