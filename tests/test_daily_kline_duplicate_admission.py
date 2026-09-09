"""A provider/cache transition cannot choose a same-session price by row order."""

from pathlib import Path
import asyncio

import pytest

from app.config import Settings
from app.services.cache import SQLiteCache
from app.services.datahub_klines import KlineCoordinator, _prepare_daily_klines
from app.services.datahub_runtime import ProviderRuntime
from app.services.market_scan_scoring import completed_market_scan_klines
from app.utils.provider_errors import ProviderInstrumentDataError
from app.utils.daily_kline_identity import deduplicate_daily_klines
from tests.test_market_scan_input_admission import _score
from tests.test_market_scan_scoring import AS_OF, DATA_DATE, _rows


def _sessions():
    return completed_market_scan_klines(_rows(DATA_DATE, 110), DATA_DATE)[-80:]


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_date_limit_cannot_accidentally_mean_unbounded_history(limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        deduplicate_daily_klines(_sessions(), limit=limit)


def test_identical_daily_duplicates_do_not_consume_requested_history_slots() -> None:
    rows = _sessions()
    expected = _prepare_daily_klines(rows, "test-qfq", "600519.SH", 80, AS_OF)
    actual = _prepare_daily_klines([*rows, *([rows[-1]] * 80)], "test-qfq", "600519.SH", 80, AS_OF)

    assert actual == expected
    assert _score(rows=actual) == _score(rows=expected)


@pytest.mark.parametrize("update", [{"volume": 90_000_000}, {"fallback_used": True}, {"session_status": "suspended"}])
@pytest.mark.parametrize("reverse", [False, True])
def test_provider_conflicting_daily_evidence_is_rejected_before_cache_write(update, reverse) -> None:
    rows = _sessions()
    changed = rows[-5].model_copy(update=update)
    observations = [changed, *rows] if reverse else [*rows, changed]

    with pytest.raises(ProviderInstrumentDataError, match="冲突日K"):
        _prepare_daily_klines(observations, "test-qfq", "600519.SH", 80, AS_OF)


def test_identical_provider_duplicates_are_collapsed_before_truncation_at_boundary() -> None:
    rows = _sessions()
    expected = _prepare_daily_klines(rows, "test-qfq", "600519.SH", 61, AS_OF)
    actual = _prepare_daily_klines([*reversed(rows), *rows[-20:]], "test-qfq", "600519.SH", 61, AS_OF)
    assert actual == expected


@pytest.mark.parametrize("reverse", [False, True])
def test_cache_rejects_conflicting_daily_batch_atomically_without_choosing_a_winner(tmp_path: Path, reverse: bool) -> None:
    cache = SQLiteCache(settings=Settings(cache_path=tmp_path / "synthetic.sqlite3", scheduler_enabled=False))
    rows = _sessions()
    cache.save_klines("600519.SH", rows, "test-qfq")
    before = cache.get_klines("600519.SH", 80, 10**9)
    changed = rows[-5].model_copy(update={"volume": rows[-5].volume * 9})
    observations = [changed, *rows] if reverse else [*rows, changed]

    with pytest.raises(ValueError, match="冲突日K"):
        cache.save_klines("600519.SH", observations, "test-qfq")

    assert cache.get_klines("600519.SH", 80, 10**9) == before
    assert _score(rows=before).raw_score == 88.3484


def test_cache_identical_duplicate_batch_preserves_all_distinct_sessions(tmp_path: Path) -> None:
    cache = SQLiteCache(settings=Settings(cache_path=tmp_path / "synthetic.sqlite3", scheduler_enabled=False))
    rows = _sessions()
    cache.save_klines("600519.SH", [*rows, *rows[-20:]], "test-qfq")
    actual = cache.get_klines("600519.SH", 80, 10**9)
    assert [row.date for row in actual] == [row.date for row in rows]
    assert _score(rows=actual).raw_score == _score(rows=rows).raw_score


def test_out_of_window_duplicate_conflict_cannot_change_a_current_window() -> None:
    rows = _sessions()
    old_conflict = rows[0].model_copy(update={"volume": rows[0].volume * 9})
    expected = _prepare_daily_klines(rows, "test-qfq", "600519.SH", 61, AS_OF)
    assert _prepare_daily_klines([old_conflict, *rows], "test-qfq", "600519.SH", 61, AS_OF) == expected


def test_conflicting_provider_uses_backup_and_cold_warm_scores_remain_equal(tmp_path: Path) -> None:
    rows = _sessions()
    conflict = rows[-5].model_copy(update={"volume": rows[-5].volume * 9})
    settings = Settings(cache_path=tmp_path / "synthetic.sqlite3", scheduler_enabled=False)
    cache = SQLiteCache(settings=settings)
    runtime = ProviderRuntime(cache, settings)
    calls = []

    class Provider:
        def __init__(self, source, values):
            self.source_name = source
            self.rows = [row.model_copy(update={"source": source}) for row in values]

        async def kline(self, symbol, limit):
            calls.append(self.source_name)
            return self.rows

    coordinator = KlineCoordinator(
        settings=settings, cache=cache, runtime=runtime, now=lambda: AS_OF,
        providers={"bad": Provider("bad", [*rows, conflict]), "backup": Provider("backup", rows)},
        priority=lambda _: [(1, "bad"), (2, "backup")],
    )

    async def scenario():
        try:
            cold = await coordinator.kline("600519.SH", 80, use_cache=False)
            warm = await coordinator.kline("600519.SH", 80)
            return cold, warm
        finally:
            assert await runtime.aclose()

    cold, warm = asyncio.run(scenario())
    assert calls == ["bad", "backup"]
    assert len(cold) == len(warm) == 80
    assert all(row.source == "backup" and row.fallback_used for row in [*cold, *warm])
    assert _score(rows=cold).raw_score == _score(rows=warm).raw_score
    assert not runtime.is_cooling("bad", "kline")
