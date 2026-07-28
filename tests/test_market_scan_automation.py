from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from app.models.market_scan import (
    MarketScanCoverage,
    MarketScanPublicationSummary,
    MarketScanResultWrite,
    MarketScanRetryPlan,
    MarketScanRun,
    MarketScanSeed,
)
from app.models.system import TaskRun
from app.services.market_scan_automation import (
    MarketScanAutomaticAction,
    automatic_retry_decision,
)
from app.services.market_scan_preflight import (
    run_market_scan_preflight,
)
from app.services.market_scan_preflight_state import (
    MARKET_SCAN_PREFLIGHT_MONITOR_CATEGORY,
    market_scan_preflight_task_name,
    preflight_attempt_decision,
)
from tests.market_scan_test_support import (
    SCAN_AS_OF,
    _MarketScanHub,
    _configure_clean_full_market,
    _rule_version,
    _scanner,
    _wait_for_terminal,
)


class _RecordingPreflightHub(_MarketScanHub):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.stock_pool_refreshes: list[bool] = []
        self.quote_cache_flags: list[bool] = []
        self.kline_options: list[tuple[str, int, bool, bool, bool]] = []
        self.quote_delay_seconds = 0.0
        self.quote_failure: Exception | None = None

    async def stock_pool(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets=None,
        minimum_market_counts=None,
    ):
        self.stock_pool_refreshes.append(refresh)
        return await super().stock_pool(
            keyword,
            limit,
            refresh,
            required_markets,
            minimum_market_counts,
        )

    async def partial_quotes_with_errors(self, symbols, use_cache: bool = True):
        self.quote_cache_flags.append(use_cache)
        if self.quote_delay_seconds:
            await asyncio.sleep(self.quote_delay_seconds)
        if self.quote_failure is not None:
            raise self.quote_failure
        return await super().partial_quotes_with_errors(symbols, use_cache=use_cache)

    async def kline(
        self,
        symbol: str,
        limit: int = 120,
        use_cache: bool = True,
        *,
        allow_stale: bool = False,
        require_provider_response: bool = False,
    ):
        self.kline_options.append(
            (symbol, limit, use_cache, allow_stale, require_provider_response)
        )
        return await super().kline(
            symbol,
            limit,
            use_cache,
            allow_stale=allow_stale,
            require_provider_response=require_provider_response,
        )


def test_preflight_refreshes_three_market_pool_and_probes_fresh_quote_and_klines(
    tmp_path: Path,
) -> None:
    hub = _RecordingPreflightHub(tmp_path)
    _configure_clean_full_market(hub)

    report = asyncio.run(
        run_market_scan_preflight(
            hub,  # type: ignore[arg-type]
            current=SCAN_AS_OF,
            timeout_seconds=1,
        )
    )

    assert report.ok is True
    assert [check.capability for check in report.checks] == [
        "stock_pool",
        "quote",
        "kline.BJ",
        "kline.SH",
        "kline.SZ",
    ]
    assert hub.stock_pool_refreshes == [True]
    assert hub.quote_cache_flags == [False]
    assert {option[0] for option in hub.kline_options} == {
        "600001.SH",
        "000001.SZ",
        "920066.BJ",
    }
    assert all(option[1:] == (5, False, False, True) for option in hub.kline_options)


def test_preflight_total_timeout_keeps_per_capability_results(tmp_path: Path) -> None:
    hub = _RecordingPreflightHub(tmp_path)
    _configure_clean_full_market(hub)
    hub.quote_delay_seconds = 1

    report = asyncio.run(
        run_market_scan_preflight(
            hub,  # type: ignore[arg-type]
            current=SCAN_AS_OF,
            timeout_seconds=0.02,
        )
    )
    checks = {check.capability: check for check in report.checks}

    assert report.ok is False
    assert checks["stock_pool"].ok is True
    assert checks["quote"].ok is False
    assert "总预算" in checks["quote"].detail
    assert all(checks[f"kline.{market}"].ok for market in ("SH", "SZ", "BJ"))


def test_preflight_stops_before_market_probes_when_refreshed_pool_lacks_market(
    tmp_path: Path,
) -> None:
    hub = _RecordingPreflightHub(tmp_path)
    _configure_clean_full_market(hub)
    hub.rows = [row for row in hub.rows if row.market != "BJ"]

    report = asyncio.run(
        run_market_scan_preflight(
            hub,  # type: ignore[arg-type]
            current=SCAN_AS_OF,
            timeout_seconds=1,
        )
    )

    assert report.ok is False
    assert report.checks[0].capability == "stock_pool"
    assert report.checks[0].ok is False
    assert hub.quote_cache_flags == []
    assert hub.kline_options == []


def test_preflight_sanitizes_provider_errors(tmp_path: Path) -> None:
    hub = _RecordingPreflightHub(tmp_path)
    _configure_clean_full_market(hub)
    secret = "do-not-persist-this-secret"
    hub.quote_failure = RuntimeError(
        f"api_key={secret} https://example.invalid/quote?token={secret}"
    )

    report = asyncio.run(
        run_market_scan_preflight(
            hub,  # type: ignore[arg-type]
            current=SCAN_AS_OF,
            timeout_seconds=1,
            sensitive_values=(secret,),
        )
    )
    quote_check = next(check for check in report.checks if check.capability == "quote")

    assert quote_check.ok is False
    assert secret not in quote_check.detail
    assert "<redacted>" in quote_check.detail


def test_preflight_failure_persists_diagnostics_without_creating_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = _MarketScanHub(tmp_path)
    _configure_clean_full_market(hub)
    hub.quotes_by_symbol.pop("920066.BJ")
    _enable_automation_settings(hub)
    scanner = _scanner(hub)
    task_runs_for_name = hub.cache.task_runs_for_name
    named_lookups: list[tuple[str, int]] = []

    def tracked_task_runs_for_name(task_name: str, limit: int = 20):
        named_lookups.append((task_name, limit))
        return task_runs_for_name(task_name, limit)

    monkeypatch.setattr(hub.cache, "task_runs_for_name", tracked_task_runs_for_name)

    async def scenario():
        response = await scanner.scheduled_tick(SCAN_AS_OF)
        repeated = await scanner.scheduled_tick(SCAN_AS_OF)
        memory_deferred = await scanner.scheduled_tick(SCAN_AS_OF)
        await scanner.stop()
        return response, repeated, memory_deferred

    response, repeated, memory_deferred = asyncio.run(scenario())
    task_runs = hub.cache.recent_task_runs(limit=20)
    events = hub.cache.recent_monitor_events(limit=20)

    assert response is None
    assert repeated is None
    assert memory_deferred is None
    assert scanner.latest_run() is None
    assert len(task_runs) == 1
    assert [limit for _task_name, limit in named_lookups] == [4, 4]
    assert all(task_name.startswith("full_market_scan_preflight|") for task_name, _limit in named_lookups)
    assert task_runs[0].status == "failed"
    assert task_runs[0].task_name.startswith("full_market_scan_preflight|")
    assert "quote=失败" in (task_runs[0].message or "")
    assert all(
        f"kline.{market}=通过" in (task_runs[0].message or "")
        for market in ("SH", "SZ", "BJ")
    )
    assert any(
        event.category == MARKET_SCAN_PREFLIGHT_MONITOR_CATEGORY
        and event.level == "warning"
        for event in events
    )


def test_preflight_attempt_cadence_is_persisted_and_bounded() -> None:
    action = MarketScanAutomaticAction("scheduled", "2026-07-17")
    task_name = market_scan_preflight_task_name(action)
    failed = _task_run(1, task_name, "failed", "2026-07-17 16:30:00")

    waiting = preflight_attempt_decision(
        [failed],
        task_name=task_name,
        current=datetime(2026, 7, 17, 16, 39),
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )
    due = preflight_attempt_decision(
        [failed],
        task_name=task_name,
        current=datetime(2026, 7, 17, 16, 40),
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )
    exhausted = preflight_attempt_decision(
        [
            _task_run(index, task_name, "failed", f"2026-07-17 {15 + index:02d}:30:00")
            for index in range(1, 5)
        ],
        task_name=task_name,
        current=datetime(2026, 7, 17, 20, 0),
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )

    assert waiting.allowed is False
    assert waiting.due_at == datetime(2026, 7, 17, 16, 40)
    assert due.allowed is True
    assert due.attempt_number == 2
    assert exhausted.allowed is False
    assert exhausted.exhausted is True


def test_auto_retry_decision_uses_structured_state_and_excludes_individual_failures() -> None:
    current = datetime(2026, 7, 17, 16, 40)
    empty_summary = MarketScanPublicationSummary(coverages=())
    incomplete = _market_scan_run(processed_count=40, success_count=40)
    incomplete_plan = _retry_plan(incomplete, pending_count=60)

    retryable = automatic_retry_decision(
        incomplete,
        incomplete_plan,
        empty_summary,
        current=current,
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )
    individual = automatic_retry_decision(
        _market_scan_run(
            processed_count=100,
            success_count=99,
            missing_count=1,
            last_error="timeout provider down",
        ),
        _retry_plan(incomplete, pending_count=1),
        _publication_summary(all_success=99, sh_success=39, sz_success=55, bj_success=5),
        current=current,
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )
    manual = automatic_retry_decision(
        incomplete.model_copy(update={"trigger": "manual"}),
        incomplete_plan,
        empty_summary,
        current=current,
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )
    degraded = automatic_retry_decision(
        incomplete.model_copy(update={"status": "degraded"}),
        incomplete_plan,
        empty_summary,
        current=current,
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )
    interrupted_run = incomplete.model_copy(
        update={"status": "interrupted", "processed_count": 100, "success_count": 100}
    )
    interrupted = automatic_retry_decision(
        interrupted_run,
        _retry_plan(interrupted_run, pending_count=0),
        empty_summary,
        current=current,
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )
    cancelled = automatic_retry_decision(
        incomplete.model_copy(update={"status": "cancelled"}),
        incomplete_plan,
        empty_summary,
        current=current,
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )
    assert retryable.is_due(current) is True
    assert retryable.due_at == current
    assert individual.eligible is False
    assert manual.eligible is False
    assert degraded.eligible is False
    assert interrupted.is_due(current) is True
    assert cancelled.eligible is False


@pytest.mark.parametrize("low_scope", ("ALL", "SH", "SZ", "BJ"))
def test_auto_retry_uses_each_publication_coverage_floor(low_scope: str) -> None:
    current = datetime(2026, 7, 17, 16, 40)
    run = _market_scan_run(processed_count=100, success_count=99, missing_count=1)
    success_counts = {"ALL": 100, "SH": 40, "SZ": 55, "BJ": 5}
    success_counts[low_scope] = {"ALL": 94, "SH": 37, "SZ": 52, "BJ": 4}[low_scope]

    decision = automatic_retry_decision(
        run,
        _retry_plan(run, pending_count=1),
        _publication_summary(
            all_success=success_counts["ALL"],
            sh_success=success_counts["SH"],
            sz_success=success_counts["SZ"],
            bj_success=success_counts["BJ"],
        ),
        current=current,
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )

    assert decision.is_due(current) is True


def test_auto_retry_excludes_degraded_single_stock_missing() -> None:
    current = datetime(2026, 7, 17, 16, 40)
    run = _market_scan_run(
        status="degraded",
        processed_count=100,
        success_count=99,
        missing_count=1,
    )

    decision = automatic_retry_decision(
        run,
        _retry_plan(run, pending_count=1),
        _publication_summary(all_success=99, sh_success=39, sz_success=55, bj_success=5),
        current=current,
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )

    assert decision.eligible is False


def test_auto_retry_excludes_completed_run_with_only_deterministic_skips() -> None:
    current = datetime(2026, 7, 17, 16, 40)
    run = _market_scan_run(
        total_count=332,
        processed_count=332,
        success_count=311,
        skipped_count=21,
    )
    summary = MarketScanPublicationSummary(
        coverages=(
            MarketScanCoverage("ALL", 311, 311, skipped_count=21),
            MarketScanCoverage("SH", 1, 1),
            MarketScanCoverage("SZ", 1, 1),
            MarketScanCoverage("BJ", 309, 309, skipped_count=21),
        ),
        snapshot_span_seconds=50,
    )

    decision = automatic_retry_decision(
        run,
        MarketScanRetryPlan(
            source_run_id=run.id,
            result_count=332,
            preserved_success_count=0,
            pending_count=332,
            needs_market_data=True,
            rule_version=run.rule_version,
        ),
        summary,
        current=current,
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )

    assert decision.eligible is False
    assert decision.reason == "individual-or-non-retryable-failure"


@pytest.mark.parametrize(
    ("retry_count", "expected_due"),
    [
        (0, datetime(2026, 7, 17, 16, 40)),
        (1, datetime(2026, 7, 17, 17, 0)),
        (2, datetime(2026, 7, 17, 17, 30)),
        (3, None),
    ],
)
def test_auto_retry_delays_are_derived_from_persisted_retry_count(
    retry_count: int,
    expected_due: datetime | None,
) -> None:
    run = _market_scan_run(
        trigger="scheduled" if retry_count == 0 else "retry",
        retry_count=retry_count,
        processed_count=40,
        success_count=40,
    )

    decision = automatic_retry_decision(
        run,
        _retry_plan(run, pending_count=60),
        MarketScanPublicationSummary(coverages=()),
        current=datetime(2026, 7, 17, 18, 0),
        delays_seconds=(600, 1800, 3600),
        max_retry_attempts=3,
    )

    assert decision.due_at == expected_due
    assert decision.eligible is (expected_due is not None)


def test_scheduled_retry_resumes_pending_rows_once_after_restart_derived_delay(
    tmp_path: Path,
) -> None:
    hub = _MarketScanHub(tmp_path)
    _configure_clean_full_market(hub)
    _enable_automation_settings(hub)
    source = _seed_incomplete_scheduled_run(hub)
    scanner = _scanner(hub)

    async def scenario():
        await scanner.start()
        early = await scanner.scheduled_tick(datetime(2026, 7, 17, 16, 39))
        started = await scanner.scheduled_tick(datetime(2026, 7, 17, 16, 40))
        assert started is not None
        terminal = await _wait_for_terminal(scanner, started.run.id)
        duplicate = await scanner.scheduled_tick(datetime(2026, 7, 17, 16, 41))
        await scanner.stop()
        return early, started, terminal, duplicate

    early, started, terminal, duplicate = asyncio.run(scenario())
    runs = hub.cache.market_scan_runs(page=1, page_size=20)

    assert early is None
    assert started.run.retry_of_run_id == source.id
    assert started.run.retry_count == 1
    assert terminal.status == "success"
    assert duplicate is None
    assert runs.total == 2


def _enable_automation_settings(hub: _MarketScanHub) -> None:
    hub.settings = hub.settings.model_copy(
        update={
            "scheduler_enabled": True,
            "market_scan_auto_enabled": True,
            "market_scan_schedule_hour": 16,
            "market_scan_schedule_minute": 30,
            "market_scan_preflight_enabled": True,
            "market_scan_preflight_timeout_seconds": 1.0,
            "market_scan_auto_retry_delays_seconds": (600, 1800, 3600),
            "market_scan_auto_retry_max_attempts": 3,
        }
    )


def _task_run(run_id: int, task_name: str, status: str, finished_at: str) -> TaskRun:
    return TaskRun(
        id=run_id,
        task_name=task_name,
        status=status,
        started_at=finished_at,
        finished_at=finished_at,
    )


def _market_scan_run(**updates) -> MarketScanRun:
    values = {
        "id": 1,
        "status": "failed",
        "trigger": "scheduled",
        "rule_version": "test-rule",
        "as_of": "2026-07-17 16:30:00",
        "data_date": "2026-07-17",
        "scope": "test",
        "total_count": 100,
        "excluded_count": 0,
        "processed_count": 0,
        "success_count": 0,
        "missing_count": 0,
        "skipped_count": 0,
        "retry_count": 0,
        "progress_pct": 0,
        "coverage_pct": 0,
        "created_at": "2026-07-17 16:20:00",
        "updated_at": "2026-07-17 16:30:00",
        "finished_at": "2026-07-17 16:30:00",
    }
    values.update(updates)
    return MarketScanRun(**values)


def _retry_plan(run: MarketScanRun, *, pending_count: int) -> MarketScanRetryPlan:
    return MarketScanRetryPlan(
        source_run_id=run.id,
        result_count=run.total_count,
        preserved_success_count=run.total_count - pending_count,
        pending_count=pending_count,
        needs_market_data=pending_count > 0,
        rule_version=run.rule_version,
    )


def _publication_summary(
    *,
    all_success: int,
    sh_success: int,
    sz_success: int,
    bj_success: int,
) -> MarketScanPublicationSummary:
    return MarketScanPublicationSummary(
        coverages=(
            MarketScanCoverage("ALL", 100, all_success),
            MarketScanCoverage("SH", 40, sh_success),
            MarketScanCoverage("SZ", 55, sz_success),
            MarketScanCoverage("BJ", 5, bj_success),
        )
    )


def _seed_incomplete_scheduled_run(hub: _MarketScanHub) -> MarketScanRun:
    run = hub.cache.create_market_scan_run(
        trigger="scheduled",
        rule_version=_rule_version(hub),
        as_of="2026-07-17 16:30:00",
        data_date="2026-07-17",
        scope="test",
    )
    hub.cache.start_market_scan_run(run.id)
    seeds = [
        MarketScanSeed(
            symbol=row.symbol,
            code=row.code,
            market=row.market,
            name=row.name,
            list_date=row.list_date,
            metadata_source=row.source,
        )
        for row in hub.rows
    ]
    hub.cache.seed_market_scan_results(run.id, seeds, excluded_count=0)
    hub.cache.save_market_scan_result_batch(
        run.id,
        [
            MarketScanResultWrite(
                symbol="600001.SH",
                status="success",
                score=60,
                trend_score=60,
                leader_score=60,
                data_quality_score=90,
                price=10,
                change_pct=1,
                turnover_rate=2,
                volume_ratio=1,
                amount=1_000_000,
                metrics={"ma20": 9.5},
                data_date="2026-07-17",
                quote_timestamp="2026-07-17 15:00:00",
                quote_source="test",
                kline_source="test",
                adjustment_mode="qfq",
                reason="test score",
            )
        ],
    )
    hub.cache.finish_market_scan_run(run.id, "failed", message="structured test failure")
    with sqlite3.connect(hub.cache.path) as conn:
        conn.execute(
            "UPDATE market_scan_run SET finished_at = ?, updated_at = ? WHERE id = ?",
            ("2026-07-17 16:30:00", "2026-07-17 16:30:00", run.id),
        )
    return hub.cache.market_scan_run(run.id)
