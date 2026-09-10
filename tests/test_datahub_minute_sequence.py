import asyncio
from datetime import datetime

import pytest

from app.config import Settings
from app.models.market import MinuteKline, ProviderCapability
from app.services.cache import SQLiteCache
from app.services.datahub import DataHub
from app.utils.market_time import market_local_naive

NOW = datetime(2026, 9, 8, 10, 20)


def bar(timestamp, close):
    return MinuteKline(timestamp=timestamp, open=close, close=close, high=close+1,
                       low=close-1, volume=100, interval='5m', source='合成序列源')


class SequenceProvider:
    source_name = '合成序列源'
    def __init__(self):
        self.calls = 0
        self.rows = [bar('2026-09-08T10:10:00+08:00', 10),
                     bar('2026-09-08T02:15:00Z', 11),
                     bar('2026-09-07T22:19:00-04:00', 12)]
    def capability(self):
        return ProviderCapability(name='akshare', installed=True, enabled=True, minute_kline=True, note='synthetic')
    async def minute_kline(self, symbol, interval='5m', limit=120):
        self.calls += 1
        return self.rows


@pytest.fixture
def sequence_hub(tmp_path, monkeypatch):
    provider = SequenceProvider()
    settings = Settings(cache_path=tmp_path/'minute-sequence.db', minute_provider_priority=('akshare',),
                        provider_failure_cooldown_seconds=0)
    monkeypatch.setattr('app.services.datahub.build_providers', lambda _settings: {'akshare': provider})
    hub = DataHub(SQLiteCache(settings=settings), settings=settings)
    hub._kline_coordinator._now = lambda: NOW
    return hub, provider


@pytest.mark.parametrize('limit', [1, 2, 3])
def test_sqlite_roundtrip_keeps_chronological_latest_rows(sequence_hub, limit):
    async def check():
        hub, provider = sequence_hub
        try:
            fetched = await hub.minute_kline('600519.SH', '5m', 3)
            assert [row.close for row in fetched] == [10, 11, 12]
            cached = await hub.minute_kline('600519.SH', '5m', limit)
            assert [row.close for row in cached] == [10, 11, 12][-limit:]
            assert provider.calls == 1
            assert all(row.from_cache and not row.fallback_used for row in cached)
        finally:
            await hub.aclose()
    asyncio.run(check())


def test_cache_rejects_nonlast_future_in_raw_text_order(sequence_hub):
    async def check():
        hub, provider = sequence_hub
        hub.cache.save_minute_klines('600519.SH', '5m', [
            bar('2026-09-08T10:15:00+08:00', 10),
            bar('2026-09-08T02:30:00Z', 99),
        ], provider.source_name)
        try:
            rows = await hub.minute_kline('600519.SH', '5m', 2)
            assert provider.calls == 1, 'future cache bar was accepted without refresh'
            assert all(market_local_naive(datetime.fromisoformat(row.timestamp)) <= NOW for row in rows)
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize('cached', [False, True])
def test_equivalent_instant_does_not_consume_multiple_requested_bars(sequence_hub, cached):
    async def check():
        hub, provider = sequence_hub
        provider.rows = [bar('2026-09-08T10:10:00+08:00', 10),
                         bar('2026-09-08T10:15:00+08:00', 11),
                         bar('2026-09-08T02:15:00Z', 12)]
        if cached:
            hub.cache.save_minute_klines('600519.SH', '5m', provider.rows, provider.source_name)
        try:
            rows = await hub.minute_kline('600519.SH', '5m', 2)
            assert [row.close for row in rows] == [10, 12]
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize('only_future', [False, True])
def test_provider_failure_fallback_excludes_future_bars_but_keeps_old_bars(sequence_hub, only_future):
    async def check():
        hub, provider = sequence_hub
        provider.rows = []
        cached = [bar('2026-09-08T02:30:00Z', 99)]
        if not only_future:
            cached.insert(0, bar('2026-09-07T15:00:00+08:00', 10))
        hub.cache.save_minute_klines('600519.SH', '5m', cached, provider.source_name)
        try:
            if only_future:
                with pytest.raises(RuntimeError, match='所有分钟K线数据源均不可用'):
                    await hub.minute_kline('600519.SH', '5m', 2)
            else:
                rows = await hub.minute_kline('600519.SH', '5m', 2)
                assert [row.close for row in rows] == [10]
                assert all(row.from_cache and row.fallback_used for row in rows)
            assert provider.calls == 1
        finally:
            await hub.aclose()
    asyncio.run(check())


def test_subsecond_distinct_instants_survive_sorting_and_cache_roundtrip(sequence_hub):
    async def check():
        hub, provider = sequence_hub
        provider.rows = [bar('2026-09-08T02:19:00.000002Z', 12),
                         bar('2026-09-08T10:19:00.000001+08:00', 11)]
        try:
            fetched = await hub.minute_kline('600519.SH', '5m', 2)
            cached = await hub.minute_kline('600519.SH', '5m', 1)
            assert [row.close for row in fetched] == [11, 12]
            assert [row.close for row in cached] == [12]
            assert cached[0].timestamp == provider.rows[0].timestamp
            assert provider.calls == 1
        finally:
            await hub.aclose()
    asyncio.run(check())


def test_provider_deduplicates_after_removing_invalid_observations(sequence_hub):
    async def check():
        hub, provider = sequence_hub
        provider.rows = [bar('2026-09-08T10:10:00+08:00', 10),
                         bar('2026-09-08T10:15:00+08:00', 11),
                         bar('2026-09-08T02:15:00Z', 12),
                         bar('2026-09-08T05:15:00+03:00', 99).model_copy(update={'open': 200})]
        try:
            for _ in range(2):
                rows = await hub.minute_kline('600519.SH', '5m', 2)
                assert [row.close for row in rows] == [10, 12]
            assert provider.calls == 1
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize('tie', [False, True])
def test_legacy_cache_aliases_choose_newest_observation_before_limit(sequence_hub, tie):
    async def check():
        from datetime import timedelta
        from app.utils.audit_time import audit_datetime_to_text
        from app.utils.clock import utc_now
        hub, provider = sequence_hub
        stamp = utc_now()
        first = audit_datetime_to_text(stamp - timedelta(seconds=30))
        last = first if tie else audit_datetime_to_text(stamp - timedelta(seconds=10))
        legacy_rows(hub, [
            (bar('2026-09-08T10:10:00+08:00', 10), first),
            (bar('2026-09-08T10:15:00+08:00', 11), first),
            (bar('2026-09-08T02:15:00Z', 12), last),
        ])
        try:
            rows = await hub.minute_kline('600519.SH', '5m', 2)
            assert [row.close for row in rows] == [10, 12]
            assert provider.calls == 0
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize('invalid', ['ohlc', 'nonfinite', 'timestamp'])
def test_invalid_legacy_alias_cannot_hide_valid_minute_observation(sequence_hub, invalid):
    async def check():
        from app.utils.audit_time import audit_now_text
        hub, provider = sequence_hub
        original = bar('2026-09-08T10:15:00+08:00', 11)
        damaged = bar('2026-09-08T02:15:00Z', 12)
        if invalid == 'ohlc':
            damaged = damaged.model_copy(update={'open': 200})
        elif invalid == 'nonfinite':
            damaged = damaged.model_copy(update={'amount': float('inf')})
        else:
            damaged = damaged.model_copy(update={'timestamp': '2026-09-08T10:19:00Zgarbage'})
        stamp = audit_now_text()
        legacy_rows(hub, [(original, stamp), (damaged, stamp)])
        try:
            rows = await hub.minute_kline('600519.SH', '5m', 1)
            assert [row.close for row in rows] == [11]
            assert provider.calls == 0
        finally:
            await hub.aclose()
    asyncio.run(check())


def legacy_rows(hub, observations):
    import sqlite3
    with sqlite3.connect(hub.cache.path) as conn:
        conn.executemany('''INSERT INTO kline_minute (
            symbol, interval, timestamp, open, close, high, low, volume, amount, source, fetched_at
        ) VALUES ('600519.SH', '5m', ?, ?, ?, ?, ?, ?, ?, ?, ?)''', [
            (row.timestamp, row.open, row.close, row.high, row.low, row.volume, row.amount, row.source, stamp)
            for row, stamp in observations
        ])


def test_repeated_alias_write_with_same_fetch_time_updates_warm_cache(sequence_hub):
    async def check():
        from app.utils.audit_time import audit_now_text
        hub, provider = sequence_hub
        stamp = audit_now_text()
        try:
            for timestamp, close in [('2026-09-08T10:19:00+08:00', 10),
                                     ('2026-09-08T02:19:00Z', 11),
                                     ('2026-09-08T10:19:00+08:00', 12)]:
                provider.rows = [bar(timestamp, close).model_copy(update={'fetched_at': stamp})]
                fresh = await hub.minute_kline('600519.SH', '5m', 1, use_cache=False)
                warm = await hub.minute_kline('600519.SH', '5m', 1)
                assert fresh[0].close == warm[0].close == close
                assert fresh[0].timestamp == warm[0].timestamp == timestamp
                assert warm[0].source == provider.source_name and not warm[0].fallback_used
        finally:
            await hub.aclose()
    asyncio.run(check())


def test_fallback_limit_counts_usable_rows_before_future_filter(sequence_hub):
    async def check():
        hub, provider = sequence_hub
        provider.rows = []
        hub.cache.save_minute_klines('600519.SH', '5m', [
            bar('2026-09-07T15:00:00+08:00', 10),
            bar('2026-09-08T02:30:00Z', 99),
        ], provider.source_name)
        try:
            rows = await hub.minute_kline('600519.SH', '5m', 1)
            assert [row.close for row in rows] == [10]
            assert rows[0].from_cache and rows[0].fallback_used
            assert provider.calls == 1
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize('damaged', [
    bar('2026-09-08T02:15:00Z', 12).model_copy(update={'open': 200}),
    bar('2026-09-08T02:15:00Z', 12).model_copy(update={'amount': float('inf')}),
    bar('2026-09-08T02:15:00Z', 12).model_copy(update={'timestamp': 'not-a-timestamp'}),
    bar('2026-09-08T02:15:00Z', 12).model_copy(update={'timestamp': b'not-text'}),
])
def test_all_invalid_legacy_observations_remain_unavailable(sequence_hub, damaged):
    async def check():
        from app.utils.audit_time import audit_now_text
        hub, provider = sequence_hub
        provider.rows = []
        legacy_rows(hub, [(damaged, audit_now_text())])
        try:
            assert hub.cache.get_minute_klines('600519.SH', '5m', 1, 3600) == []
            with pytest.raises(RuntimeError, match='所有分钟K线数据源均不可用'):
                await hub.minute_kline('600519.SH', '5m', 1)
            assert provider.calls == 1
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize(('low', 'expected'), [(b'1', 11), ('1', 12)])
def test_cache_rejects_numeric_blob_alias_but_accepts_real_affinity_text(sequence_hub, low, expected):
    async def check():
        from app.utils.audit_time import audit_now_text
        hub, provider = sequence_hub
        stamp = audit_now_text()
        legacy_rows(hub, [
            (bar('2026-09-08T10:15:00+08:00', 11), stamp),
            (bar('2026-09-08T02:15:00Z', 12).model_copy(update={'low': low}), stamp),
        ])
        try:
            rows = await hub.minute_kline('600519.SH', '5m', 1)
            assert [row.close for row in rows] == [expected]
            assert provider.calls == 0
            if isinstance(low, str):
                assert rows[0].low == 1.0
        finally:
            await hub.aclose()
    asyncio.run(check())
