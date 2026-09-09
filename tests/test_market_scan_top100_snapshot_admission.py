"""TOP100 refresh keeps snapshot admission while exempting market-wide coverage."""

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MARKET_SCAN_TOP100_REFRESH_SCOPE,
    MarketScanCoverage,
    MarketScanMarketEventSpan,
    MarketScanPublicationSummary,
    MarketScanRun,
    MarketScanStaleCluster,
)
from app.services.market_scan_completion import MarketScanFinalizer, terminal_diagnostic
from app.services.market_scan_manager import MarketScanManager
from app.services.market_scan_publication_decision import completion_diagnostics, completion_status


NOW = datetime(2026, 7, 17, 16, 30)


def _run(scope=MARKET_SCAN_TOP100_REFRESH_SCOPE, *, missing=0):
    return MarketScanRun(
        id=1, status="running", trigger="retry", retry_of_run_id=2, scope=scope,
        rule_version="full-market-scan-v6:" + "a" * 64,
        as_of=NOW.isoformat(), data_date="2026-07-17", quote_date="2026-07-17",
        total_count=3, excluded_count=0, processed_count=3, success_count=3 - missing,
        missing_count=missing, skipped_count=0, retry_count=0,
        progress_pct=100, coverage_pct=(3 - missing) / 3 * 100,
        created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
    )


def _summary(*, single_market=False):
    coverages = (
        (MarketScanCoverage("ALL", 3, 3), MarketScanCoverage("SH", 3, 3))
        if single_market
        else (MarketScanCoverage("ALL", 3, 3), *(MarketScanCoverage(market, 1, 1) for market in ("SH", "SZ", "BJ")))
    )
    return MarketScanPublicationSummary(
        coverages=coverages, snapshot_contract_version="v6", expected_capture_count=3,
        capture_started_at="2026-07-17T08:20:00Z", capture_finished_at="2026-07-17T08:30:00Z",
        capture_duration_ms=600_000, capture_count=3, capture_sealed=True,
        observed_started_at="2026-07-17T08:20:00Z", observed_finished_at="2026-07-17T08:30:00Z",
        observed_span_seconds=600, observed_count=3,
    )


class _PublicationCache:
    def __init__(self, run, summary):
        self.run = run
        self.summary = summary
        self.summary_reads = 0
        self.finished = None
        self.market_scan_repo = self

    def start_market_scan_task_run(self, *_args):
        pass

    def start_market_scan_run(self, _run_id):
        return self.run

    def update_market_scan_observability(self, *_args, **_kwargs):
        pass

    def market_scan_run(self, _run_id):
        return self.run

    def market_scan_degraded_result_count(self, _run_id):
        return 0

    def publication_summary(self, _run_id):
        self.summary_reads += 1
        return self.summary

    def finish_market_scan_run(self, _run_id, status, **kwargs):
        if status in {"success", "degraded"}:
            kwargs["validate_before_commit"]()
        self.finished = (status, kwargs)


async def _no_work(*_args, **_kwargs):
    return ()


async def _unexpected_failure(_run_id, exc):
    raise AssertionError(str(exc)) from exc


def _complete_through_manager(run, summary, monkeypatch, tmp_path):
    from app.services import trading_calendar

    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", tmp_path / "absent-calendar.json")
    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "false")
    cache = _PublicationCache(run, summary)
    manager = object.__new__(MarketScanManager)
    manager.cache = cache
    manager._executor = SimpleNamespace(execute=_no_work)  # noqa: SLF001
    manager._finalizer = MarketScanFinalizer(cache)  # noqa: SLF001
    manager._now = lambda: NOW  # noqa: SLF001
    manager._track_terminal_persistence = lambda *_args: None  # noqa: SLF001
    manager._probability_capture_wakeup = asyncio.Event()  # noqa: SLF001
    manager._drain_probability_capture_outbox = _no_work  # noqa: SLF001
    manager._finish_failed = _unexpected_failure  # noqa: SLF001
    asyncio.run(manager._execute_run(run.id, asyncio.Event()))  # noqa: SLF001
    assert cache.finished is not None
    return cache


@pytest.mark.parametrize("scope", [MARKET_SCAN_FULL_MARKET_SCOPE, MARKET_SCAN_TOP100_REFRESH_SCOPE])
@pytest.mark.parametrize("seconds,status", [(1200, "success"), (1201, "failed"), (1500, "failed")])
def test_manager_preserves_snapshot_time_limit_for_top100(scope, seconds, status, monkeypatch, tmp_path):
    summary = replace(_summary(), capture_duration_ms=seconds * 1000, observed_span_seconds=seconds)
    cache = _complete_through_manager(_run(scope), summary, monkeypatch, tmp_path)
    actual_status, kwargs = cache.finished
    assert actual_status == status
    assert cache.summary_reads == 1
    codes = {item.code for item in kwargs["publication_diagnostics"].blockers}
    if status == "failed":
        assert codes == {"publication.snapshot.capture_duration_exceeded", "publication.snapshot.observed_span_exceeded"}
        assert "1200 秒门槛" in kwargs["error"]
    else:
        assert codes == set()
        assert kwargs["error"] is None


@pytest.mark.parametrize("changes,code", [
    ({"capture_sealed": False}, "capture_unsealed"),
    ({"capture_count": 2}, "capture_count_mismatch"),
    ({"missing_observed_count": 1}, "observed_timestamp_missing"),
    ({"invalid_observed_timestamps": ("bad-time",)}, "observed_timestamp_invalid"),
    ({"invalid_snapshot_timestamps": ("bad-event",)}, "invalid_timestamp"),
    ({"market_event_spans": (MarketScanMarketEventSpan("SH", span_seconds=1201),)}, "market_event_span_exceeded"),
    ({"systemic_stale_cluster": MarketScanStaleCluster("2026-07-16", 3, ("SH",), 3)}, "systemic_stale_cluster"),
])
def test_top100_manager_keeps_all_existing_snapshot_blockers(changes, code, monkeypatch, tmp_path):
    summary = replace(_summary(single_market=True), **changes)
    cache = _complete_through_manager(_run(), summary, monkeypatch, tmp_path)
    status, kwargs = cache.finished
    assert status == "failed"
    assert [item.code for item in kwargs["publication_diagnostics"].blockers] == [f"publication.snapshot.{code}"]
    assert "覆盖不足" not in kwargs["message"]
    assert "有效样本占比不足" not in kwargs["error"]


@pytest.mark.parametrize("missing,status", [(0, "success"), (1, "degraded")])
def test_single_market_top100_retains_partial_success_without_market_denominators(missing, status, monkeypatch, tmp_path):
    run = _run(missing=missing)
    summary = replace(_summary(single_market=True), coverages=(
        MarketScanCoverage("ALL", 3, 3 - missing, missing),
        MarketScanCoverage("SH", 3, 3 - missing, missing),
    ))
    cache = _complete_through_manager(run, summary, monkeypatch, tmp_path)
    actual, kwargs = cache.finished
    assert actual == status
    assert kwargs["publication_diagnostics"].blockers == []
    assert "覆盖不足" not in (kwargs["error"] or "")
    assert "有效样本占比不足" not in (kwargs["error"] or "")
    assert cache.summary_reads == 1


def test_full_market_still_requires_its_market_coverage_and_eligible_ratios():
    run = _run(MARKET_SCAN_FULL_MARKET_SCOPE)
    summary = _summary(single_market=True)
    status, message = completion_status(run, publication_summary=summary)
    diagnostics = completion_diagnostics(run, message, publication_summary=summary)
    error = terminal_diagnostic(run, status, 0, (), publication_summary=summary)
    assert status == "failed"
    assert {item.code for item in diagnostics.blockers} == {
        "publication.coverage.insufficient", "publication.eligible_ratio.insufficient",
    }
    assert "SZ 发布覆盖不足" in message and "BJ 发布覆盖不足" in message
    assert error and "有效样本占比不足" in error
