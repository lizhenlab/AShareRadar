from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.models.market_scan import MARKET_SCAN_TOP100_REFRESH_SCOPE, MarketScanRun
from app.services import market_scan_probability_capture as capture
from app.services.market_scan_probability_source import ProbabilitySourceError, list_probability_source_snapshots
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from tests.market_scan_test_support import (
    SCAN_AS_OF,
    _MarketScanHub,
    _configure_clean_full_market,
    _scanner,
    _wait_for_terminal,
)


def test_capture_reads_complete_canonical_run_and_uses_research_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(71)
    cache = _FakeCache(tmp_path, run, candidates=[_run(70), run], items=[object(), object()])
    observed: dict[str, object] = {}

    def project(source_run, results, *, canonical_published):
        observed.update(run=source_run, results=results, canonical=canonical_published)
        return {"run": {"run_id": 71}, "records": [{"symbol": "600001.SH"}]}

    def persist(directory, *, run, records, captured_at):
        observed.update(directory=directory, projected_run=run, records=records, captured_at=captured_at)
        return _archive_info(71)

    monkeypatch.setattr(capture, "project_probability_source_capture", project)
    monkeypatch.setattr(capture, "capture_source_snapshot", persist)

    info = capture.capture_market_scan_probability_source(
        cache,
        71,
        captured_at="2026-08-11 16:10:00",
    )

    assert info["run_id"] == 71
    assert observed["run"] is run
    assert observed["results"] == cache.items
    assert observed["canonical"] is True
    assert observed["directory"] == tmp_path / "research" / "market_scan_probability_source"
    assert observed["captured_at"] == "2026-08-11T16:10:00+08:00"
    assert cache.result_query["status"] == "success"
    assert cache.result_query["page_size"] == run.success_count
    assert cache.result_query["sort"] == "rank"


def test_capture_rejects_superseded_same_date_cohort_before_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _run(70, finished_at="2026-08-11 16:05:00")
    canonical = _run(71, finished_at="2026-08-11 16:06:00")
    cache = _FakeCache(tmp_path, source, candidates=[source, canonical], items=[object(), object()])
    called = False

    def project(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(capture, "project_probability_source_capture", project)

    with pytest.raises(capture.ProbabilitySourceCaptureError, match="run 71 替代"):
        capture.capture_market_scan_probability_source(cache, 70)

    assert called is False


def test_capture_canonical_order_prefers_later_scan_as_of_before_run_id(tmp_path: Path) -> None:
    later_scan = _run(70, as_of="2026-08-11 16:10:00")
    higher_id_but_earlier_scan = _run(71, as_of="2026-08-11 16:00:00")
    cache = _FakeCache(
        tmp_path,
        higher_id_but_earlier_scan,
        candidates=[later_scan, higher_id_but_earlier_scan],
        items=[object(), object()],
    )

    with pytest.raises(capture.ProbabilitySourceCaptureError, match="run 70 替代"):
        capture.capture_market_scan_probability_source(cache, 71)


def test_best_effort_capture_audits_failure_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(71)
    cache = _FakeCache(tmp_path, run, candidates=[run], items=[object(), object()])

    def fail(*_args, **_kwargs):
        raise RuntimeError("token=private-value archive unavailable")

    monkeypatch.setattr(capture, "capture_market_scan_probability_source", fail)
    outcome = asyncio.run(
        capture.capture_market_scan_probability_source_best_effort(
            cache,
            71,
            sensitive_values=("private-value",),
        )
    )

    assert outcome["status"] == "failed"
    assert cache.events and cache.events[-1][0:2] == ("warning", "research")
    assert "<redacted>" in cache.events[-1][2]
    assert "private-value" not in cache.events[-1][2]


@pytest.mark.parametrize(
    "updates",
    (
        {"mode": "intraday"},
        {"mode": "preopen"},
        {"scope": MARKET_SCAN_TOP100_REFRESH_SCOPE},
        {"status": "failed"},
    ),
)
def test_best_effort_capture_silently_skips_non_official_full_runs(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    run = _run(71).model_copy(update=updates)
    cache = _FakeCache(tmp_path, run, candidates=[run], items=[object(), object()])

    outcome = asyncio.run(capture.capture_market_scan_probability_source_best_effort(cache, 71))

    assert outcome["status"] == "skipped"
    assert cache.events == []


def test_completed_official_full_scan_captures_restart_loadable_source(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        _configure_clean_full_market(hub)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        for _attempt in range(200):
            if any("PIT样本归档完成" in event.message for event in hub.cache.recent_monitor_events(limit=20)):
                break
            await asyncio.sleep(0.01)
        await scanner.stop()
        return hub, final

    hub, final = asyncio.run(scenario())
    archive_dir = Path(hub.cache.path).parent / "research" / "market_scan_probability_source"
    archives = list_probability_source_snapshots(archive_dir, run_id=final.id)

    assert final.status == "success"
    assert len(archives) == 1
    assert archives[0]["run_id"] == final.id
    assert archives[0]["quality"]["record_count"] == final.success_count
    assert any("PIT样本归档完成" in event.message for event in hub.cache.recent_monitor_events(limit=20))
    with sqlite3.connect(hub.cache.path) as conn:
        outbox = conn.execute(
            """
            SELECT status, attempt_count, archive_digest, completed_at
            FROM market_scan_probability_capture_outbox
            WHERE run_id = ?
            """,
            (final.id,),
        ).fetchone()
    assert outbox is not None
    assert outbox[0] == "succeeded"
    assert outbox[1] == 1
    assert outbox[2] == archives[0]["digest"]
    assert outbox[3]


def test_capture_outbox_retries_after_failure_and_recovers_expired_lease_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_publish = capture.capture_source_snapshot

    def unavailable(*_args, **_kwargs):
        raise OSError("archive temporarily unavailable")

    monkeypatch.setattr(capture, "capture_source_snapshot", unavailable)

    async def first_attempt():
        hub = _MarketScanHub(tmp_path)
        _configure_clean_full_market(hub)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        for _attempt in range(200):
            if any("PIT样本归档失败" in event.message for event in hub.cache.recent_monitor_events(limit=20)):
                break
            await asyncio.sleep(0.01)
        await scanner.stop()
        return hub, final

    hub, final = asyncio.run(first_attempt())
    with sqlite3.connect(hub.cache.path) as conn:
        failed = conn.execute(
            """
            SELECT status, attempt_count, last_error
            FROM market_scan_probability_capture_outbox WHERE run_id = ?
            """,
            (final.id,),
        ).fetchone()
        assert failed is not None
        assert failed[0] == "pending"
        assert failed[1] == 1
        assert "temporarily unavailable" in failed[2]
        conn.execute(
            """
            UPDATE market_scan_probability_capture_outbox
            SET status = 'processing', lease_owner = 'dead-process',
                lease_expires_at = '2099-01-01T00:00:00Z'
            WHERE run_id = ?
            """,
            (final.id,),
        )

    monkeypatch.setattr(capture, "capture_source_snapshot", original_publish)

    async def recovered_attempt() -> None:
        scanner = _scanner(hub)
        await scanner.start()
        for _attempt in range(300):
            with sqlite3.connect(hub.cache.path) as conn:
                status = conn.execute(
                    """
                    SELECT status FROM market_scan_probability_capture_outbox
                    WHERE run_id = ?
                    """,
                    (final.id,),
                ).fetchone()[0]
            if status == "succeeded":
                break
            await asyncio.sleep(0.01)
        await scanner.stop()

    asyncio.run(recovered_attempt())
    archives = list_probability_source_snapshots(
        Path(hub.cache.path).parent / "research" / "market_scan_probability_source",
        run_id=final.id,
    )
    with sqlite3.connect(hub.cache.path) as conn:
        recovered = conn.execute(
            """
            SELECT status, attempt_count, lease_owner, lease_expires_at
            FROM market_scan_probability_capture_outbox WHERE run_id = ?
            """,
            (final.id,),
        ).fetchone()
    assert len(archives) == 1
    assert recovered == ("succeeded", 2, None, None)


def test_public_capture_rejects_symlink_output_root_without_writing_target(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path / "runtime")
        _configure_clean_full_market(hub)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        await scanner.stop()
        return hub, final

    hub, final = asyncio.run(scenario())
    real_directory = tmp_path / "outside"
    real_directory.mkdir()
    root_link = tmp_path / "archive-root-link"
    root_link.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ProbabilitySourceError, match="输出目录必须是真实目录"):
        capture.capture_market_scan_probability_source(
            hub.cache,
            final.id,
            directory=root_link,
            captured_at="2026-08-11T16:01:00+08:00",
        )

    assert list(real_directory.iterdir()) == []


class _FakeCache:
    def __init__(
        self,
        tmp_path: Path,
        run: MarketScanRun,
        *,
        candidates: list[MarketScanRun],
        items: list[object],
    ) -> None:
        self.path = tmp_path / "runtime.sqlite3"
        self.run = run
        self.candidates = candidates
        self.items = items
        self.result_query: dict[str, object] = {}
        self.events: list[tuple[str, str, str, str | None]] = []

    def market_scan_run(self, run_id: int) -> MarketScanRun:
        assert run_id == self.run.id
        return self.run

    def market_scan_runs(self, **_query):
        return SimpleNamespace(items=self.candidates)

    def market_scan_results(self, run_id: int, **query):
        assert run_id == self.run.id
        self.result_query = query
        return SimpleNamespace(run=self.run, total=len(self.items), items=self.items)

    def save_monitor_event(
        self,
        level: str,
        category: str,
        message: str,
        symbol: str | None = None,
    ) -> None:
        self.events.append((level, category, message, symbol))


def _run(
    run_id: int,
    *,
    finished_at: str = "2026-08-11 16:05:00",
    as_of: str = "2026-08-11 16:00:00",
) -> MarketScanRun:
    return MarketScanRun(
        id=run_id,
        status="success",
        trigger="manual",
        mode="official",
        rule_version="full-market-scan-v6:test",
        as_of=as_of,
        data_date="2026-08-11",
        quote_date="2026-08-11",
        scope=FULL_MARKET_SCOPE,
        total_count=2,
        excluded_count=0,
        processed_count=2,
        success_count=2,
        missing_count=0,
        skipped_count=0,
        retry_count=0,
        progress_pct=100,
        coverage_pct=100,
        created_at="2026-08-11 15:55:00",
        updated_at=finished_at,
        finished_at=finished_at,
    )


def _archive_info(run_id: int) -> dict[str, object]:
    return {
        "run_id": run_id,
        "quote_date": "2026-08-11",
        "digest": "a" * 64,
        "quality": {"record_count": 2},
    }
