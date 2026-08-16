from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.models.market_scan import (
    MARKET_SCAN_TOP100_REFRESH_SCOPE,
    MarketScanProductionScoreContract,
    MarketScanRun,
)
from app.services import market_scan_probability_capture as capture
from app.services import market_scan_probability_source as probability_source
from app.services.cache import SQLiteCache
from app.services.market_scan_manager import MarketScanManager
from app.services.market_scan_probability_source import ProbabilitySourceError, list_probability_source_snapshots
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.market_scan_scoring import (
    FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION,
    FULL_MARKET_SCORE_RULE_VERSION,
    market_scan_score_spec,
    market_scan_score_spec_v4,
    stable_score_spec_hash,
)
from tests.market_scan_test_support import (
    action_pass_publication_diagnostics,
)


@pytest.fixture
def compact_full_market_population_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use a compact but real action-eligible 100-result publication cohort."""
    monkeypatch.setattr(
        probability_source,
        "PROBABILITY_SOURCE_MINIMUM_POPULATION",
        {"ALL": 100, "SH": 34, "SZ": 33, "BJ": 33},
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

    def persist(
        directory,
        *,
        run,
        records,
        captured_at,
        projection_receipt,
        before_publish,
        database_path,
    ):
        before_publish()
        observed.update(
            directory=directory,
            projected_run=run,
            records=records,
            captured_at=captured_at,
            projection_receipt=projection_receipt,
            database_path=database_path,
        )
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
    assert observed["projection_receipt"] == {
        "run": {"run_id": 71},
        "records": [{"symbol": "600001.SH"}],
    }
    assert observed["directory"] == tmp_path / "research" / "market_scan_probability_source"
    assert observed["captured_at"] == "2026-08-11T16:10:00+08:00"
    assert cache.result_query["status"] == "success"
    assert cache.result_query["page_size"] == run.success_count
    assert cache.result_query["sort"] == "rank"
    assert cache.action_source_calls == [run.id]


def test_manual_capture_rejects_missing_unified_action_source_before_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(71)
    cache = _FakeCache(
        tmp_path,
        run,
        candidates=[run],
        items=[object(), object()],
        action_source_digest=None,
    )
    projected = False

    def project(*_args, **_kwargs):
        nonlocal projected
        projected = True
        return {}

    monkeypatch.setattr(capture, "project_probability_source_capture", project)

    with pytest.raises(
        capture.ProbabilitySourceCaptureIneligible,
        match="缺少统一动作源回执或跳过证据无效",
    ):
        capture.capture_market_scan_probability_source(cache, run.id)

    assert cache.action_source_calls == [run.id]
    assert cache.result_query == {}
    assert projected is False


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


def test_previous_v4_capture_claim_finishes_skipped_instead_of_retrying(
    tmp_path: Path,
) -> None:
    run = _run(71)
    v4_contract = MarketScanProductionScoreContract(
        FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION,
        stable_score_spec_hash(market_scan_score_spec_v4(min_data_quality_score=50)),
        run.success_count,
    )
    cache = _OutboxFakeCache(
        tmp_path,
        run,
        candidates=[run],
        items=[object(), object()],
        score_contract=v4_contract,
    )

    summary = asyncio.run(
        capture.process_market_scan_probability_capture_outbox(
            cache,
            owner="v4-skip-test",
            limit=1,
        )
    )

    assert summary == {"captured": 0, "skipped": 1, "failed": 0}
    assert cache.finished == [{
        "run_id": run.id,
        "owner": "v4-skip-test",
        "status": "skipped",
        "message": (
            f"run {run.id} 使用历史只读评分合同 "
            f"{FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION}，不创建新PIT归档"
        ),
    }]
    assert cache.retried == []


def test_immutable_projection_error_finishes_terminal_instead_of_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(71)
    cache = _OutboxFakeCache(
        tmp_path,
        run,
        candidates=[run],
        items=[object(), object()],
    )

    def invalid_projection(*_args, **_kwargs):
        raise ProbabilitySourceError("sealed feature evidence mismatch")

    monkeypatch.setattr(
        capture,
        "project_probability_source_capture",
        invalid_projection,
    )

    summary = asyncio.run(
        capture.process_market_scan_probability_capture_outbox(
            cache,
            owner="permanent-projection-test",
            limit=1,
        )
    )

    assert summary == {"captured": 0, "skipped": 1, "failed": 0}
    assert cache.finished[0]["status"] == "skipped"
    assert "无法确定性投影" in str(cache.finished[0]["message"])
    assert cache.retried == []


def test_transient_capture_failure_stops_after_bounded_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(71)
    cache = _OutboxFakeCache(
        tmp_path,
        run,
        candidates=[run],
        items=[object(), object()],
        attempt_count=capture.PROBABILITY_SOURCE_CAPTURE_MAX_ATTEMPTS,
    )

    def unavailable(*_args, **_kwargs):
        raise OSError("archive filesystem unavailable")

    monkeypatch.setattr(
        capture,
        "capture_market_scan_probability_source",
        unavailable,
    )

    summary = asyncio.run(
        capture.process_market_scan_probability_capture_outbox(
            cache,
            owner="bounded-retry-test",
            limit=1,
        )
    )

    assert summary == {"captured": 0, "skipped": 0, "failed": 1}
    assert cache.finished[0]["status"] == "skipped"
    assert "自动重试上限" in str(cache.finished[0]["message"])
    assert cache.retried == []


def test_completed_official_full_scan_captures_restart_loadable_source(
    tmp_path: Path,
    compact_full_market_population_contract: None,
) -> None:
    cache, final = _canonical_action_source_cache(tmp_path)

    summary = asyncio.run(
        capture.process_market_scan_probability_capture_outbox(
            cache,
            owner="canonical-capture-test",
            limit=1,
        )
    )
    archive_dir = Path(cache.path).parent / "research" / "market_scan_probability_source"
    archives = list_probability_source_snapshots(archive_dir, run_id=final.id)

    assert summary == {"captured": 1, "skipped": 0, "failed": 0}
    assert cache.market_scan_action_source_digest(final.id) == final.snapshot_digest
    assert final.status == "degraded"
    assert final.success_count == 100
    assert len(archives) == 1
    assert archives[0]["run_id"] == final.id
    assert archives[0]["quality"]["record_count"] == final.success_count
    with sqlite3.connect(cache.path) as conn:
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
    assert cache.probability_source_capture_status(final.id) == {
        "status": "succeeded",
        "archive_digest": archives[0]["digest"],
        "last_error": None,
    }
    assert cache.probability_source_capture_status(final.id + 1000) is None
    with pytest.raises(ValueError, match="正整数"):
        cache.probability_source_capture_status(0)


def test_probability_capture_adapter_rejects_published_v5_run_missing_replay_receipt(
    tmp_path: Path,
    compact_full_market_population_contract: None,
) -> None:
    import json

    from app.db.market_scan_integrity import market_scan_snapshot_digest
    from app.repositories.market_scan_action_gate_replay import (
        MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE,
    )
    from tests.test_market_scan_skip_contract import (
        _disable_market_scan_immutability,
    )

    cache, final = _canonical_action_source_cache(tmp_path)
    with sqlite3.connect(cache.path) as conn:
        conn.row_factory = sqlite3.Row
        _disable_market_scan_immutability(conn)
        raw = conn.execute(
            "SELECT publication_diagnostics_json FROM market_scan_run WHERE id = ?",
            (final.id,),
        ).fetchone()[0]
        diagnostics = json.loads(raw)
        diagnostics["passed_gates"] = [
            item
            for item in diagnostics["passed_gates"]
            if item["code"] != MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE
        ]
        conn.execute(
            "UPDATE market_scan_run SET publication_diagnostics_json = ? WHERE id = ?",
            (json.dumps(diagnostics, separators=(",", ":"), sort_keys=True), final.id),
        )
        conn.execute(
            "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
            (market_scan_snapshot_digest(conn, final.id), final.id),
        )

    assert cache.market_scan_action_source_digest(final.id) is None
    with pytest.raises(
        capture.ProbabilitySourceCaptureIneligible,
        match="缺少统一动作源回执",
    ):
        capture.capture_market_scan_probability_source(cache, final.id)


def test_capture_outbox_retries_after_failure_and_recovers_expired_lease_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compact_full_market_population_contract: None,
) -> None:
    original_publish = capture.capture_source_snapshot

    def unavailable(*_args, **_kwargs):
        raise OSError("archive temporarily unavailable")

    monkeypatch.setattr(capture, "capture_source_snapshot", unavailable)

    cache, final = _canonical_action_source_cache(tmp_path)
    summary = asyncio.run(
        capture.process_market_scan_probability_capture_outbox(
            cache,
            owner="failed-capture-test",
            limit=1,
        )
    )
    assert summary == {"captured": 0, "skipped": 0, "failed": 1}
    with sqlite3.connect(cache.path) as conn:
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
        assert cache.probability_source_capture_status(final.id) == {
            "status": "pending",
            "archive_digest": None,
            "last_error": failed[2],
        }
        conn.execute(
            """
            UPDATE market_scan_probability_capture_outbox
            SET status = 'processing', lease_owner = 'dead-process',
                lease_expires_at = '2099-01-01T00:00:00Z'
            WHERE run_id = ?
            """,
            (final.id,),
        )
    assert cache.probability_source_capture_status(final.id) == {
        "status": "processing",
        "archive_digest": None,
        "last_error": failed[2],
    }

    monkeypatch.setattr(capture, "capture_source_snapshot", original_publish)

    restarted = SQLiteCache(settings=cache.settings)
    restarted.reconcile_probability_source_capture_outbox()
    recovered_summary = asyncio.run(
        capture.process_market_scan_probability_capture_outbox(
            restarted,
            owner="restarted-capture-test",
            limit=1,
        )
    )
    assert recovered_summary == {"captured": 1, "skipped": 0, "failed": 0}
    archives = list_probability_source_snapshots(
        Path(cache.path).parent / "research" / "market_scan_probability_source",
        run_id=final.id,
    )
    with sqlite3.connect(cache.path) as conn:
        recovered = conn.execute(
            """
            SELECT status, attempt_count, lease_owner, lease_expires_at
            FROM market_scan_probability_capture_outbox WHERE run_id = ?
            """,
            (final.id,),
        ).fetchone()
    assert len(archives) == 1
    assert recovered == ("succeeded", 2, None, None)
    assert restarted.probability_source_capture_status(final.id) == {
        "status": "succeeded",
        "archive_digest": archives[0]["digest"],
        "last_error": None,
    }


def test_capture_publish_refreshes_compact_index_before_the_next_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = object.__new__(MarketScanManager)
    manager._probability_capture_lock = asyncio.Lock()  # noqa: SLF001
    manager._probability_capture_owner = "test-owner"  # noqa: SLF001
    manager._sensitive_values = ()  # noqa: SLF001
    manager.cache = object()
    manager._lifecycle = SimpleNamespace(owns_instance_guard=lambda: True)  # noqa: SLF001
    refresh_finished = False

    async def processed(*_args, **_kwargs):
        return {"captured": 1, "skipped": 0, "failed": 0}

    async def refresh():
        nonlocal refresh_finished
        refresh_finished = True
        return 1

    monkeypatch.setattr(
        "app.services.market_scan_manager.process_market_scan_probability_capture_outbox",
        processed,
    )
    manager.refresh_probability_research_cache = refresh  # type: ignore[method-assign]

    summary = asyncio.run(manager._drain_probability_capture_outbox())  # noqa: SLF001

    assert summary["captured"] == 1
    assert refresh_finished is True


def test_public_capture_rejects_symlink_output_root_without_writing_target(
    tmp_path: Path,
    compact_full_market_population_contract: None,
) -> None:
    cache, final = _canonical_action_source_cache(tmp_path / "runtime")
    real_directory = tmp_path / "outside"
    real_directory.mkdir()
    root_link = tmp_path / "archive-root-link"
    root_link.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ProbabilitySourceError, match="输出目录必须是真实目录"):
        capture.capture_market_scan_probability_source(
            cache,
            final.id,
            directory=root_link,
            captured_at="2026-08-11T16:01:00+08:00",
        )

    assert list(real_directory.iterdir()) == []


def _canonical_action_source_cache(tmp_path: Path) -> tuple[SQLiteCache, MarketScanRun]:
    # This shared fixture executes the real v5 scorer over 100 varied successes,
    # verifies one canonical production skip, and lets finish_run seal the replay
    # receipt. Probability tests must not self-declare the action gate.
    from tests.test_market_scan_skip_contract import _valid_action_source_run

    _repo, settings, run_id, _skip_symbol, _diagnostics = _valid_action_source_run(
        tmp_path
    )
    cache = SQLiteCache(settings=settings)
    return cache, cache.market_scan_run(run_id)


class _FakeCache:
    def __init__(
        self,
        tmp_path: Path,
        run: MarketScanRun,
        *,
        candidates: list[MarketScanRun],
        items: list[object],
        score_contract: MarketScanProductionScoreContract | None = None,
        action_source_digest: str | None = "a" * 64,
    ) -> None:
        self.path = tmp_path / "runtime.sqlite3"
        self.run = run
        self.candidates = candidates
        self.items = items
        self.result_query: dict[str, object] = {}
        self.events: list[tuple[str, str, str, str | None]] = []
        self.score_contract = score_contract
        self.action_source_digest = action_source_digest
        self.action_source_calls: list[int] = []

    def market_scan_run(self, run_id: int) -> MarketScanRun:
        assert run_id == self.run.id
        return self.run

    def market_scan_action_source_digest(self, run_id: int) -> str | None:
        assert run_id == self.run.id
        self.action_source_calls.append(run_id)
        return self.action_source_digest

    def market_scan_runs(self, **_query):
        return SimpleNamespace(items=self.candidates)

    def market_scan_results(self, run_id: int, **query):
        assert run_id == self.run.id
        self.result_query = query
        return SimpleNamespace(run=self.run, total=len(self.items), items=self.items)

    def market_scan_success_score_contract(
        self,
        run_id: int,
    ) -> MarketScanProductionScoreContract:
        assert run_id == self.run.id
        return self.score_contract or MarketScanProductionScoreContract(
            FULL_MARKET_SCORE_RULE_VERSION,
            stable_score_spec_hash(market_scan_score_spec(min_data_quality_score=50)),
            self.run.success_count,
        )

    def save_monitor_event(
        self,
        level: str,
        category: str,
        message: str,
        symbol: str | None = None,
    ) -> None:
        self.events.append((level, category, message, symbol))


class _OutboxFakeCache(_FakeCache):
    def __init__(self, *args, attempt_count: int = 1, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.claimed = False
        self.attempt_count = attempt_count
        self.finished: list[dict[str, object]] = []
        self.retried: list[dict[str, object]] = []

    def claim_probability_source_capture(self, **_kwargs) -> dict[str, object] | None:
        if self.claimed:
            return None
        self.claimed = True
        return {
            "run_id": self.run.id,
            "attempt_count": self.attempt_count,
            "captured_at": "2026-08-11T16:10:00+08:00",
        }

    def finish_probability_source_capture(self, run_id: int, **kwargs) -> None:
        self.finished.append({"run_id": run_id, **kwargs})

    def retry_probability_source_capture(self, run_id: int, **kwargs) -> None:
        self.retried.append({"run_id": run_id, **kwargs})


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
        snapshot_digest="a" * 64,
        snapshot_seal_origin="publication",
        snapshot_sealed_at=finished_at,
        publication_diagnostics=action_pass_publication_diagnostics(),
    )


def _archive_info(run_id: int) -> dict[str, object]:
    return {
        "run_id": run_id,
        "quote_date": "2026-08-11",
        "digest": "a" * 64,
        "quality": {"record_count": 2},
    }
