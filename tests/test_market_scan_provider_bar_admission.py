"""Provider preparation must retain evidence of invalid completed observations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.models.market import Kline
from app.models.market_scan import MarketScanResultWrite
from app.services.cache import SQLiteCache
from app.services.datahub_klines import KlineCoordinator, _prepare_daily_klines
from app.services.datahub_runtime import ProviderRuntime
from app.services.market_scan_skip_contract import MARKET_SCAN_SKIP_EVIDENCE_KEY
from app.services.market_scan_manager import market_scan_rule_contract
from app.services.market_scan_score_contract import stable_score_spec_hash
from app.services.market_scan_scoring import completed_market_scan_klines
from app.utils.provider_errors import ProviderInstrumentDataError
from tests.test_market_scan_failure_isolation import _evaluator, _scan_one
from tests.test_market_scan_scoring import AS_OF, DATA_DATE, _item, _quote, _rows


@pytest.mark.parametrize("position", [0, -5, -1])
@pytest.mark.parametrize("update", [{"high": 1.0}, {"close": float("nan")}, {"volume": -1.0}])
def test_provider_preparation_rejects_invalid_completed_bar_before_filtering(
    position: int,
    update: dict[str, float],
) -> None:
    rows = _rows(DATA_DATE, 80)
    rows[position] = rows[position].model_copy(update=update)

    with pytest.raises(ProviderInstrumentDataError, match="日K.*无效"):
        _prepare_daily_klines(rows, "test-qfq", "600519.SH", 80, AS_OF)


def test_actual_provider_preparation_cannot_turn_bad_bar_into_verified_session_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _rows(DATA_DATE, 80)
    rows[-5] = rows[-5].model_copy(update={"high": 1.0})
    monkeypatch.setattr("app.services.datahub_klines._kline_now", lambda: AS_OF)
    settings = Settings(cache_path=tmp_path / "provider-bars.sqlite3", scheduler_enabled=False,
                        market_scan_retry_attempts=1, market_scan_min_history_rows=61)
    cache = SQLiteCache(settings=settings)
    runtime = ProviderRuntime(cache, settings)

    class Provider:
        source_name = "test-qfq"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return rows

    coordinator = KlineCoordinator(settings=settings, cache=cache, providers={"bad": Provider()},
                                   runtime=runtime, priority=lambda _kind: [(1, "bad")])

    class Hub:
        async def kline(self, symbol: str, **kwargs: object) -> list[Kline]:
            return await coordinator.kline(symbol, limit=80, use_cache=False)

    hub = Hub()
    hub.settings = settings

    async def scenario() -> MarketScanResultWrite:
        try:
            rule_version = f"full-market-scan-v6:{stable_score_spec_hash(market_scan_rule_contract(settings))}"
            return await _scan_one(_evaluator(hub), _item(), _quote(), rule_version)
        finally:
            assert await runtime.aclose()

    result = asyncio.run(scenario())
    assert result.status == "missing"
    assert result.score is None and result.raw_score is None
    assert MARKET_SCAN_SKIP_EVIDENCE_KEY not in result.score_details
    assert cache.get_klines("600519.SH", 80, max_age_seconds=3600) == []


@pytest.mark.parametrize("outside_date", ["2020-01-02", "2026-07-20", "2026-07-12", "bad-date", None])
def test_invalid_observation_outside_requested_completed_window_is_ignored(outside_date: str | None) -> None:
    rows = completed_market_scan_klines(_rows(DATA_DATE, 100), DATA_DATE)[-80:]
    outside = rows[-1].model_copy(update={"date": outside_date, "high": 1.0})
    baseline = _prepare_daily_klines(rows, "test-qfq", "600519.SH", 80, AS_OF)

    assert _prepare_daily_klines([outside, *reversed(rows)], "test-qfq", "600519.SH", 80, AS_OF) == baseline


def test_bad_old_observation_beyond_requested_count_does_not_reject_valid_window() -> None:
    rows = _rows(DATA_DATE, 80)
    baseline = _prepare_daily_klines(rows, "test-qfq", "600519.SH", 61, AS_OF)
    rows[0] = rows[0].model_copy(update={"high": 1.0})

    assert _prepare_daily_klines(rows, "test-qfq", "600519.SH", 61, AS_OF) == baseline


@pytest.mark.parametrize("padding_date", [DATA_DATE.isoformat(), "2026-07-12", "bad-date", "2026-07-20"])
def test_duplicate_or_noise_rows_cannot_displace_invalid_completed_session(padding_date: str) -> None:
    rows = completed_market_scan_klines(_rows(DATA_DATE, 100), DATA_DATE)[-80:]
    rows[0] = rows[0].model_copy(update={"high": 1.0})
    padding = rows[-1].model_copy(update={"date": padding_date})

    with pytest.raises(ProviderInstrumentDataError, match=rows[0].date):
        _prepare_daily_klines([*rows, *([padding] * 80)], "test-qfq", "600519.SH", 80, AS_OF)


@pytest.mark.parametrize("bad_first", [False, True])
def test_every_original_observation_within_selected_session_is_checked(bad_first: bool) -> None:
    rows = _rows(DATA_DATE, 80)
    invalid = rows[-5].model_copy(update={"high": 1.0})
    observations = [invalid, *rows] if bad_first else [*rows, invalid]

    with pytest.raises(ProviderInstrumentDataError, match=invalid.date):
        _prepare_daily_klines(observations, "test-qfq", "600519.SH", 80, AS_OF)


def test_invalid_completed_observation_uses_backup_without_global_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = _rows(DATA_DATE, 80)
    invalid = list(valid)
    invalid[-5] = invalid[-5].model_copy(update={"volume": -1.0})
    monkeypatch.setattr("app.services.datahub_klines._kline_now", lambda: AS_OF)
    settings = Settings(cache_path=tmp_path / "backup-bars.sqlite3", scheduler_enabled=False)
    cache = SQLiteCache(settings=settings)
    runtime = ProviderRuntime(cache, settings)
    called: list[str] = []

    class Provider:
        def __init__(self, name: str, observations: list[Kline]) -> None:
            self.source_name = name
            self.rows = [row.model_copy(update={"source": name}) for row in observations]

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            called.append(self.source_name)
            return self.rows

    coordinator = KlineCoordinator(
        settings=settings, cache=cache, providers={"bad": Provider("bad", invalid), "backup": Provider("backup", valid)},
        runtime=runtime, priority=lambda _kind: [(1, "bad"), (2, "backup")],
    )

    async def scenario() -> list[Kline]:
        try:
            return await coordinator.kline("600519.SH", limit=80, use_cache=False)
        finally:
            assert await runtime.aclose()

    result = asyncio.run(scenario())
    assert called == ["bad", "backup"]
    assert len(result) == 80
    assert all(row.source == "backup" and row.fallback_used for row in result)
    assert not runtime.is_cooling("bad", "kline")
    status = next(item for item in cache.provider_capability_statuses() if item.name == "bad")
    assert status.failure_count == 1 and status.success_count == 0
    assert "2026-07-13" in (status.last_error or "")
