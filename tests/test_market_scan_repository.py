from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.models.market_scan import (
    MARKET_SCAN_TOP100_REFRESH_SCOPE,
    MarketScanPublicationDiagnostic,
    MarketScanPublicationDiagnostics,
)
from app.repositories.market_scan import (
    MarketScanRepository,
    MarketScanResultWrite,
    MarketScanSeed,
)
from app.services.cache import SQLiteCache
from app.services.market_scan_universe import FULL_MARKET_SCOPE


def test_results_are_ranked_stably_paginated_and_filtered(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, _sample_seeds())
    repo.save_result_batch(run.id, _sample_results())

    final = repo.finish_run(run.id, "degraded", message="含缺失与跳过结果")
    first_page = _results(repo, run.id, page=1, page_size=2)
    second_page = _results(repo, run.id, page=2, page_size=2)

    assert final.total_count == 6
    assert final.processed_count == 6
    assert final.success_count == 4
    assert final.missing_count == 1
    assert final.skipped_count == 1
    assert first_page.total == 4
    assert first_page.page_count == 2
    assert [item.symbol for item in first_page.items] == ["600001.SH", "000001.SZ"]
    assert [item.symbol for item in second_page.items] == ["600002.SH", "920066.BJ"]
    assert [item.rank for item in first_page.items + second_page.items] == [1, 2, 3, 4]

    assert _symbols(_results(repo, run.id, market="SH")) == ["600001.SH", "600002.SH"]
    assert _symbols(_results(repo, run.id, industry="电力", status=None)) == [
        "600001.SH",
        "600002.SH",
        "600003.SH",
    ]
    assert _symbols(_results(repo, run.id, industry="%", status=None)) == []
    assert _symbols(_results(repo, run.id, industry="_", status=None)) == []
    assert _symbols(_results(repo, run.id, is_st=True, status=None)) == [
        "000001.SZ",
        "600003.SH",
    ]
    assert _symbols(_results(repo, run.id, is_new=True, status=None)) == [
        "920066.BJ",
        "300001.SZ",
    ]
    assert _symbols(_results(repo, run.id, status="missing")) == ["300001.SZ"]
    assert _symbols(_results(repo, run.id, min_data_quality_score=90, status=None)) == [
        "600001.SH",
        "000001.SZ",
    ]
    assert _symbols(
        _results(
            repo,
            run.id,
            min_confidence=90,
            max_risk=20,
            min_tradability=70,
            sort=("risk", "confidence", "symbol"),
            order=("asc", "desc", "asc"),
        )
    ) == ["600001.SH", "000001.SZ"]
    assert _symbols(
        _results(
            repo,
            run.id,
            market=("SZ", "BJ"),
            industry=("银行", "高端装备"),
            min_score=70,
            max_score=80,
            min_trend_score=60,
            max_trend_score=70,
            min_change_pct=2,
            max_change_pct=3,
            min_turnover_rate=2,
            max_turnover_rate=4,
            min_amount=150,
            max_amount=350,
            min_data_quality_score=70,
            max_data_quality_score=95,
            sort=("amount", "score", "symbol"),
            order=("desc", "desc", "asc"),
        )
    ) == ["920066.BJ", "000001.SZ"]
    assert _symbols(_results(repo, run.id, keyword="  600002  ")) == ["600002.SH"]
    assert _symbols(_results(repo, run.id, keyword="北交")) == ["920066.BJ"]
    assert _symbols(_results(repo, run.id, keyword="%", status=None)) == []
    assert _symbols(_results(repo, run.id, keyword="_", status=None)) == []
    probability_scoped = _results(repo, run.id, symbols=("600002.SH", "920066.BJ"))
    assert [(item.symbol, item.rank) for item in probability_scoped.items] == [
        ("600002.SH", 3),
        ("920066.BJ", 4),
    ]  # A Shadow probability scope filters rows without reranking the persisted production ranks.
    assert _symbols(_results(repo, run.id, symbols=())) == []


def test_success_raw_scores_reads_only_the_complete_success_multiset(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, _sample_seeds())
    repo.save_result_batch(run.id, _sample_results())

    assert repo.success_raw_scores(run.id) == (80, 90, 80, 70)


def test_run_queries_keep_preopen_official_and_intraday_cohorts_isolated(
    tmp_path: Path,
) -> None:
    repo, _path = _repository(tmp_path)
    runs = {}
    for index, mode in enumerate(("official", "intraday", "preopen"), start=1):
        run = _seed_running_run(
            repo,
            _sample_seeds()[:1],
            as_of=f"2026-07-17 0{index}:00:00",
            mode=mode,
        )
        repo.save_result_batch(
            run.id,
            _sample_results()[:1],
        )
        runs[mode] = repo.finish_run(run.id, "success", message=f"{mode} complete")

    preopen_page = repo.list_runs(page=1, page_size=20, mode="preopen")

    assert [item.id for item in preopen_page.items] == [runs["preopen"].id]
    assert repo.latest_run(mode="preopen").id == runs["preopen"].id  # type: ignore[union-attr]
    assert repo.latest_published_run(mode="official").id == runs["official"].id  # type: ignore[union-attr]
    assert repo.latest_full_run(mode="intraday").id == runs["intraday"].id  # type: ignore[union-attr]


def test_publication_diagnostics_round_trip_and_legacy_terminal_rows_remain_nullable(
    tmp_path: Path,
) -> None:
    repo, path = _repository(tmp_path)
    run = _seed_running_run(repo, _sample_seeds())
    repo.save_result_batch(run.id, _sample_results())
    diagnostics = MarketScanPublicationDiagnostics(
        headline="盘后正式扫描未达到发布可信度：发布阻断：SH 发布覆盖不足",
        blockers=[
            MarketScanPublicationDiagnostic(
                code="publication.coverage.insufficient",
                label="SH 发布覆盖不足",
                detail="SH 发布覆盖不足：1/2（50.00%，门槛 95.00%）",
                severity="error",
            )
        ],
        passed_gates=[
            MarketScanPublicationDiagnostic(
                code="score_distribution.pass",
                label="评分分布",
                detail="raw-score-distribution-v2：raw_score样本 100/100",
                severity="info",
            )
        ],
    )

    final = repo.finish_run(
        run.id,
        "degraded",
        message="结构化诊断持久化",
        publication_diagnostics=diagnostics,
    )
    legacy = repo.create_run(
        trigger="manual",
        rule_version="test-rule-v1",
        as_of="2026-07-17 16:40:00",
        data_date="2026-07-17",
        scope="test",
    )
    repo.start_run(legacy.id)
    repo.finish_run(legacy.id, "failed", message="旧式终态")

    assert final.publication_diagnostics == diagnostics
    assert repo.run(run.id).publication_diagnostics == diagnostics
    listed = {item.id: item for item in repo.list_runs(page=1, page_size=10).items}
    assert listed[run.id].publication_diagnostics == diagnostics
    assert repo.run(legacy.id).publication_diagnostics is None
    with sqlite3.connect(path) as conn:
        stored = conn.execute(
            "SELECT publication_diagnostics_json FROM market_scan_run WHERE id = ?",
            (run.id,),
        ).fetchone()[0]
    assert '"schema_version":"market-scan-publication-diagnostics-v1"' in stored


def test_retry_derives_new_run_and_keeps_original_snapshot_immutable(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    seeds = _sample_seeds()[:3]
    run = _seed_running_run(repo, seeds)
    writes = [
        _write("600001.SH", status="success", score=88, raw_score=88.4, quality=95),
        _write("000001.SZ", status="missing", error="行情缺失"),
        _write("600002.SH", status="skipped", reason="停牌"),
    ]
    repo.save_result_batch(run.id, writes)
    repo.finish_run(run.id, "degraded", message="降级完成")
    original_before = repo.run(run.id)
    original_items_before = _results(repo, run.id, status=None).items

    retried = repo.prepare_retry(run.id)
    pending = repo.pending_items(retried.id)
    all_items = _results(repo, retried.id, status=None).items
    by_symbol = {item.symbol: item for item in all_items}

    assert retried.id != run.id
    assert retried.retry_of_run_id == run.id
    assert retried.status == "queued"
    assert retried.trigger == "retry"
    assert retried.retry_count == 1
    assert retried.processed_count == 1
    assert retried.success_count == 1
    assert retried.missing_count == 0
    assert retried.skipped_count == 0
    assert [item.symbol for item in pending] == ["600002.SH", "000001.SZ"]
    assert by_symbol["600001.SH"].status == "success"
    assert by_symbol["600001.SH"].score == 88
    assert by_symbol["600001.SH"].raw_score == pytest.approx(88.4)
    assert by_symbol["600001.SH"].rank is None
    assert by_symbol["000001.SZ"].status == "pending"
    assert by_symbol["000001.SZ"].error is None
    assert by_symbol["600002.SH"].status == "pending"
    assert by_symbol["600002.SH"].reason is None
    assert repo.run(run.id) == original_before
    assert _results(repo, run.id, status=None).items == original_items_before
    assert original_items_before[0].rank == 1


def test_top100_refresh_derives_only_source_leaders_and_preserves_full_snapshot(
    tmp_path: Path,
) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, _sample_seeds()[:4])
    repo.save_result_batch(
        run.id,
        [
            _write("600001.SH", status="success", score=70, raw_score=70.1, quality=95),
            _write("000001.SZ", status="success", score=90, raw_score=90.1, quality=95),
            _write("600002.SH", status="success", score=80, raw_score=80.1, quality=95),
            _write("920066.BJ", status="success", score=60, raw_score=60.1, quality=95),
        ],
    )
    source = repo.finish_run(run.id, "success", message="源榜单已发布")
    source_items = _results(repo, source.id, status=None).items

    refreshed = repo.prepare_top100_refresh(
        source.id,
        rule_version=source.rule_version,
        as_of="2026-07-17 16:35:00",
        data_date=source.data_date,
        quote_date=source.quote_date,
        limit=3,
    )
    pending = repo.pending_items(refreshed.id)

    assert refreshed.retry_of_run_id == source.id
    assert refreshed.status == "queued"
    assert refreshed.trigger == "retry"
    assert refreshed.scope == MARKET_SCAN_TOP100_REFRESH_SCOPE
    assert refreshed.stock_pool_source == f"top100-source-run:{source.id}"
    assert refreshed.total_count == 3
    assert refreshed.processed_count == refreshed.success_count == 0
    assert {item.symbol for item in pending} == {"000001.SZ", "600002.SH", "600001.SH"}
    assert all(item.status == "pending" and item.score is None and item.rank is None for item in pending)
    assert repo.latest_run().id == refreshed.id
    assert repo.latest_full_run().id == source.id
    assert repo.run(source.id) == source
    assert _results(repo, source.id, status=None).items == source_items


def test_failed_retry_recomputes_every_result_instead_of_mixing_snapshots(
    tmp_path: Path,
) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, _sample_seeds()[:2])
    repo.save_result_batch(
        run.id,
        [
            _write("600001.SH", status="success", score=88, quality=95),
            _write("000001.SZ", status="missing", error="行情缺失"),
        ],
    )
    repo.finish_run(run.id, "failed", message="发布可信度不足")

    plan = repo.retry_plan(run.id)
    retried = repo.prepare_retry(run.id, plan, as_of="2026-07-17 16:45:00")
    items = _results(repo, retried.id, status=None).items

    assert plan.preserved_success_count == 0
    assert plan.pending_count == 2
    assert retried.processed_count == 0
    assert retried.success_count == 0
    assert retried.as_of == "2026-07-17 16:45:00"
    assert repo.run(run.id).as_of == "2026-07-17 16:30:00"
    assert retried.message == "等待完整重算"
    assert [item.status for item in items] == ["pending", "pending"]
    assert all(item.score is None and item.quote_timestamp is None for item in items)


@pytest.mark.parametrize("source_status", ("cancelled", "interrupted"))
def test_cancelled_and_interrupted_retries_keep_clean_successes(
    tmp_path: Path,
    source_status: str,
) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, _sample_seeds()[:2])
    repo.save_result_batch(
        run.id,
        [_write("600001.SH", status="success", score=88, quality=95)],
    )
    repo.finish_run(run.id, source_status, message="扫描中止")  # type: ignore[arg-type]

    plan = repo.retry_plan(run.id)
    retried = repo.prepare_retry(run.id, plan)

    assert plan.preserved_success_count == 1
    assert plan.pending_count == 1
    assert retried.processed_count == 1
    assert retried.success_count == 1
    assert retried.message == "等待断点续跑"
    assert retried.as_of == run.as_of
    assert [item.symbol for item in repo.pending_items(retried.id)] == ["000001.SZ"]


@pytest.mark.parametrize("source_status", ("degraded", "interrupted"))
def test_v6_retries_recompute_every_symbol_without_copying_snapshot_provenance(
    tmp_path: Path,
    source_status: str,
) -> None:
    repo, _path = _repository(tmp_path)
    values = _run_values()
    values["rule_version"] = "full-market-scan-v6:test"
    run = repo.create_run(**values)
    repo.start_run(run.id)
    repo.seed_results(run.id, _sample_seeds()[:2], excluded_count=0)
    writes = [_write("600001.SH", status="success", score=88, quality=95)]
    if source_status == "degraded":
        writes.append(_write("000001.SZ", status="missing", error="行情缺失"))
    repo.save_result_batch(run.id, writes)
    repo.finish_run(run.id, source_status, message="v6 等待完整重算")  # type: ignore[arg-type]

    plan = repo.retry_plan(run.id)
    retried = repo.prepare_retry(run.id, plan, as_of="2026-07-17 16:45:00")
    items = _results(repo, retried.id, status=None).items

    assert plan.preserved_success_count == 0
    assert plan.pending_count == plan.result_count == retried.total_count == 2
    assert retried.processed_count == retried.success_count == 0
    assert retried.as_of == "2026-07-17 16:45:00"
    assert repo.run(run.id).as_of == "2026-07-17 16:30:00"
    assert retried.quote_capture_started_at is None
    assert retried.quote_capture_finished_at is None
    assert retried.quote_capture_duration_ms is None
    assert retried.quote_capture_count == 0
    assert retried.message == "等待完整重算"
    assert all(item.status == "pending" for item in items)
    assert all(
        item.score is None
        and item.quote_timestamp is None
        and item.quote_observed_at is None
        and item.quote_source is None
        and item.kline_source is None
        for item in items
    )


def test_retry_plan_guard_and_result_copy_commit_atomically(tmp_path: Path) -> None:
    repo, path = _repository(tmp_path)
    run = _seed_running_run(repo, _sample_seeds()[:2])
    repo.save_result_batch(
        run.id,
        [
            _write("600001.SH", status="success", score=88, quality=95),
            _write("000001.SZ", status="missing", error="行情缺失"),
        ],
    )
    repo.finish_run(run.id, "degraded", message="等待重试")
    plan = repo.retry_plan(run.id)

    with pytest.raises(ValueError, match="发生变化"):
        repo.prepare_retry(run.id, replace(plan, pending_count=plan.pending_count + 1))
    assert repo.list_runs(page=1, page_size=10).total == 1

    with sqlite3.connect(path) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER reject_retry_result_copy
            BEFORE INSERT ON market_scan_result
            WHEN NEW.run_id <> {run.id}
            BEGIN
                SELECT RAISE(ABORT, 'simulated retry copy failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated retry copy failure"):
        repo.prepare_retry(run.id, plan)
    assert repo.list_runs(page=1, page_size=10).total == 1

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER reject_retry_result_copy")
    retried = repo.prepare_retry(run.id, plan)

    assert retried.retry_of_run_id == run.id
    assert retried.processed_count == plan.preserved_success_count
    assert len(repo.pending_items(retried.id)) == plan.pending_count


def test_quote_capture_envelope_begin_and_seal_are_cross_process_atomic(
    tmp_path: Path,
) -> None:
    repo, path = _repository(tmp_path)
    peer = SQLiteCache(
        settings=Settings(cache_path=path, scheduler_enabled=False)
    ).market_scan_repo
    run = _seed_running_run(repo, _sample_seeds()[:2])

    def race(call):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        outcomes: list[str] = []

        def worker(repository):
            barrier.wait(timeout=2)
            try:
                call(repository)
            except (RuntimeError, ValueError) as exc:
                outcome = f"error:{exc}"
            else:
                outcome = "ok"
            with lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(repository,))
            for repository in (repo, peer)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        assert all(not thread.is_alive() for thread in threads)
        return outcomes

    begin_outcomes = race(
        lambda repository: repository.begin_quote_capture(
            run.id,
            "2026-07-17T07:00:00Z",
        )
    )
    seal_outcomes = race(
        lambda repository: repository.seal_quote_capture(
            run.id,
            finished_at="2026-07-17T07:00:02Z",
            duration_ms=2_000,
            count=2,
        )
    )
    current = repo.run(run.id)

    assert begin_outcomes.count("ok") == 1
    assert sum(outcome.startswith("error:") for outcome in begin_outcomes) == 1
    assert seal_outcomes.count("ok") == 1
    assert sum(outcome.startswith("error:") for outcome in seal_outcomes) == 1
    assert current.quote_capture_started_at == "2026-07-17T07:00:00.000000Z"
    assert current.quote_capture_finished_at == "2026-07-17T07:00:02.000000Z"
    assert current.quote_capture_duration_ms == 2_000
    assert current.quote_capture_count == 2


def test_retry_pending_metadata_can_be_refreshed_without_mutating_clean_results(
    tmp_path: Path,
) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(
        repo,
        [
            _sample_seeds()[0],
            MarketScanSeed(
                "000001.SZ",
                "000001",
                "SZ",
                "平安银行",
                "银行",
                None,
            ),
        ],
    )
    repo.save_result_batch(
        run.id,
        [
            _write("600001.SH", status="success", score=88, quality=95),
            MarketScanResultWrite(
                symbol="000001.SZ",
                status="success",
                score=70,
                trend_score=70,
                leader_score=70,
                data_quality_score=80,
                price=10,
                tags=("上市日期未知",),
                metrics={"ma20": 9.5},
                reason="测试评分依据",
                data_date="2026-07-17",
                quote_timestamp="2026-07-17 15:00:00",
                quote_source="test",
                kline_source="test",
                adjustment_mode="qfq",
                metadata_degraded=True,
                degradation_reasons=("metadata_incomplete",),
            ),
        ],
    )
    repo.finish_run(run.id, "degraded", message="上市日期缺失")
    retried = repo.prepare_retry(run.id)
    repo.start_run(retried.id)

    refreshed = repo.refresh_pending_metadata(
        retried.id,
        [
            MarketScanSeed(
                "600001.SH",
                "600001",
                "SH",
                "不应覆盖干净结果",
                list_date="2000-01-01",
            ),
            MarketScanSeed(
                "000001.SZ",
                "000001",
                "SZ",
                "平安银行",
                "银行",
                "1991-04-03",
                metadata_source="fresh-stock-pool",
            ),
        ],
    )
    clean = _results(repo, retried.id, status="success").items[0]
    pending = repo.pending_items(retried.id)[0]

    assert refreshed == 1
    assert clean.name == "沪电一号"
    assert pending.name == "平安银行"
    assert pending.industry == "银行"
    assert pending.list_date == "1991-04-03"
    assert pending.metadata_source == "fresh-stock-pool"


def test_cancel_transitions_active_run_and_rejects_terminal_cancel(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    run = repo.create_run(**_run_values())

    cancelling = repo.request_cancel(run.id)
    cancelled = repo.finish_run(run.id, "cancelled", message="用户取消")

    assert cancelling.status == "cancelling"
    assert cancelling.cancel_requested_at is not None
    assert cancelled.status == "cancelled"
    assert cancelled.finished_at is not None
    with pytest.raises(ValueError, match="已结束，不能取消"):
        repo.request_cancel(run.id)


def test_result_batch_rechecks_cancellation_inside_write_transaction(tmp_path: Path) -> None:
    repo, path = _repository(tmp_path)
    independent_repo = MarketScanRepository(path, threading.RLock())
    run = _seed_running_run(repo, _sample_seeds()[:2])
    errors: list[Exception] = []
    started = threading.Event()
    finished = threading.Event()

    def save_after_cancellation_starts() -> None:
        started.set()
        try:
            independent_repo.save_result_batch(
                run.id,
                [_write("600001.SH", status="success", score=88, quality=95)],
            )
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    cancelling_conn = sqlite3.connect(path, timeout=5)
    worker = threading.Thread(target=save_after_cancellation_starts, daemon=True)
    try:
        cancelling_conn.execute("BEGIN IMMEDIATE")
        cancelling_conn.execute(
            "UPDATE market_scan_run SET status = 'cancelling' WHERE id = ?",
            (run.id,),
        )
        worker.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.1)
        cancelling_conn.commit()
    finally:
        if cancelling_conn.in_transaction:
            cancelling_conn.rollback()
        cancelling_conn.close()

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "cancelling" in str(errors[0])

    cancelled = repo.finish_run(run.id, "cancelled", message="用户取消")
    with pytest.raises(ValueError, match="cancelled"):
        repo.save_result_batch(
            run.id,
            [_write("000001.SZ", status="success", score=80, quality=90)],
        )

    assert cancelled.status == "cancelled"
    assert cancelled.processed_count == 0
    assert cancelled.success_count == 0
    pending = _results(repo, run.id, status="pending").items
    assert [item.symbol for item in pending] == ["000001.SZ", "600001.SH"]
    assert all(item.rank is None and item.score is None for item in pending)


def test_historical_snapshots_are_isolated_and_terminal_finish_is_idempotent(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    first = _seed_running_run(repo, [_sample_seeds()[0]])
    repo.save_result_batch(
        first.id,
        [_write("600001.SH", status="success", score=91, quality=96)],
    )
    first_final = repo.finish_run(first.id, "success", message="首轮完成")
    repeated_finish = repo.finish_run(first.id, "failed", message="不应覆盖", error="不应写入")

    with pytest.raises(ValueError, match="当前状态不能写入结果"):
        repo.save_result_batch(
            first.id,
            [_write("600001.SH", status="success", score=1, quality=1)],
        )

    second = _seed_running_run(repo, [_sample_seeds()[0]], as_of="2026-07-18 16:30:00")
    repo.save_result_batch(
        second.id,
        [_write("600001.SH", status="success", score=42, quality=75)],
    )
    repo.finish_run(second.id, "success", message="次轮完成")

    first_snapshot = _results(repo, first.id).items[0]
    second_snapshot = _results(repo, second.id).items[0]
    history = repo.list_runs(page=1, page_size=10)

    assert repeated_finish.status == "success"
    assert repeated_finish.message == first_final.message == "首轮完成"
    assert repeated_finish.finished_at == first_final.finished_at
    assert repeated_finish.last_error is None
    assert first_snapshot.score == 91
    assert second_snapshot.score == 42
    assert repo.latest_run() is not None
    assert repo.latest_run().id == second.id  # type: ignore[union-attr]
    assert [item.id for item in history.items] == [second.id, first.id]
    assert history.total == 2


def test_published_full_run_and_capture_outbox_commit_atomically(tmp_path: Path) -> None:
    repo, path = _repository(tmp_path)
    values = _run_values()
    values["scope"] = FULL_MARKET_SCOPE
    run = repo.create_run(
        **values,
        mode="official",
    )
    repo.start_run(run.id)
    repo.seed_results(run.id, [_sample_seeds()[0]], excluded_count=0)
    repo.save_result_batch(
        run.id,
        [_write("600001.SH", status="success", score=91, quality=96)],
    )

    def reject_publication() -> None:
        raise RuntimeError("publication fence rejected")

    with pytest.raises(RuntimeError, match="publication fence rejected"):
        repo.finish_run(
            run.id,
            "success",
            message="不应提交",
            validate_before_commit=reject_publication,
        )

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT status FROM market_scan_run WHERE id = ?", (run.id,)
        ).fetchone()[0] == "running"
        assert conn.execute(
            "SELECT COUNT(*) FROM market_scan_probability_capture_outbox"
        ).fetchone()[0] == 0

    final = repo.finish_run(run.id, "success", message="正式发布")
    repeated = repo.finish_run(run.id, "failed", message="幂等重复终态")
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            """
            SELECT status, attempt_count
            FROM market_scan_probability_capture_outbox
            WHERE run_id = ?
            """,
            (run.id,),
        ).fetchone()

    assert final.status == repeated.status == "success"
    assert row == ("pending", 0)


@pytest.mark.parametrize(
    ("mode", "scope"),
    (
        ("intraday", FULL_MARKET_SCOPE),
        ("preopen", FULL_MARKET_SCOPE),
        ("official", MARKET_SCAN_TOP100_REFRESH_SCOPE),
    ),
)
def test_non_official_or_top100_publication_does_not_enqueue_probability_capture(
    tmp_path: Path,
    mode: str,
    scope: str,
) -> None:
    repo, path = _repository(tmp_path)
    values = _run_values()
    values["scope"] = scope
    run = repo.create_run(**values, mode=mode)  # type: ignore[arg-type]
    repo.start_run(run.id)
    repo.seed_results(run.id, [_sample_seeds()[0]], excluded_count=0)
    repo.save_result_batch(
        run.id,
        [_write("600001.SH", status="success", score=91, quality=96)],
    )
    repo.finish_run(run.id, "success", message="published")

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM market_scan_probability_capture_outbox"
        ).fetchone()[0] == 0


def test_latest_published_run_excludes_unpublished_and_uses_stable_recency_order(
    tmp_path: Path,
) -> None:
    repo, path = _repository(tmp_path)
    assert repo.latest_published_run() is None

    older_data = _seed_running_run(
        repo,
        [_sample_seeds()[0]],
        as_of="2026-07-16 16:30:00",
    )
    repo.save_result_batch(
        older_data.id,
        [_write("600001.SH", status="success", score=80, quality=90)],
    )
    repo.finish_run(older_data.id, "success", message="较早交易日")

    later_finish = _seed_running_run(
        repo,
        _sample_seeds()[:2],
        as_of="2026-07-17 16:30:00",
    )
    repo.save_result_batch(
        later_finish.id,
        [
            _write("600001.SH", status="success", score=81, quality=90),
            _write("000001.SZ", status="skipped", reason="历史数据不足"),
        ],
    )
    repo.finish_run(later_finish.id, "degraded", message="同日较晚完成")

    higher_id = _seed_running_run(
        repo,
        [_sample_seeds()[0]],
        as_of="2026-07-17 17:00:00",
    )
    repo.save_result_batch(
        higher_id.id,
        [_write("600001.SH", status="success", score=82, quality=90)],
    )
    repo.finish_run(higher_id.id, "success", message="同交易日更高 ID")

    failed = _seed_running_run(
        repo,
        [_sample_seeds()[0]],
        as_of="2026-07-18 16:30:00",
    )
    repo.finish_run(failed.id, "failed", message="未发布")

    with sqlite3.connect(path) as conn:
        conn.executemany(
            "UPDATE market_scan_run SET finished_at = ? WHERE id = ?",
            (
                ("2026-07-30 16:00:00", older_data.id),
                ("2026-07-18 17:00:00", later_finish.id),
                ("2026-07-18 16:00:00", higher_id.id),
                ("2026-07-31 16:00:00", failed.id),
            ),
        )

    latest = repo.latest_published_run()
    assert latest is not None
    assert latest.id == later_finish.id
    assert latest.status == "degraded"

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE market_scan_run SET finished_at = ? WHERE id = ?",
            ("2026-07-18 17:00:00", higher_id.id),
        )

    tied = repo.latest_published_run()
    assert tied is not None
    assert tied.id == higher_id.id


def test_run_queries_filter_mode_status_and_data_date_without_changing_legacy_defaults(
    tmp_path: Path,
) -> None:
    repo, _path = _repository(tmp_path)
    official = _seed_running_run(repo, [_sample_seeds()[0]], as_of="2026-07-17 16:30:00")
    repo.save_result_batch(official.id, [_write("600001.SH", status="success", score=80, quality=90)])
    repo.finish_run(official.id, "success", message="正式榜单")

    intraday = _seed_running_run(
        repo,
        _sample_seeds()[:2],
        as_of="2026-07-18 10:30:00",
        mode="intraday",
    )
    repo.save_result_batch(
        intraday.id,
        [
            _write("600001.SH", status="success", score=82, quality=90),
            _write("000001.SZ", status="skipped", reason="盘中不可交易"),
        ],
    )
    repo.finish_run(intraday.id, "degraded", message="盘中榜单")

    assert repo.latest_run().id == intraday.id  # type: ignore[union-attr]
    assert repo.latest_run(mode="official").id == official.id  # type: ignore[union-attr]
    assert repo.latest_published_run(mode="intraday").id == intraday.id  # type: ignore[union-attr]
    assert repo.list_runs(page=1, page_size=10).total == 2
    official_history = repo.list_runs(
        page=1,
        page_size=10,
        mode="official",
        status="published",
        data_date="2026-07-17",
    )
    assert [run.id for run in official_history.items] == [official.id]
    assert repo.list_runs(page=1, page_size=10, status="success").total == 1
    assert repo.list_runs(page=1, page_size=10, status="failed").total == 0


def test_reconcile_orphaned_run_and_terminal_finish_are_idempotent(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, [_sample_seeds()[0]])

    first_reconcile = repo.reconcile_incomplete_runs()
    interrupted = repo.run(run.id)
    second_reconcile = repo.reconcile_incomplete_runs()
    repeated_finish = repo.finish_run(run.id, "failed", message="不应覆盖")

    assert first_reconcile == 1
    assert second_reconcile == 0
    assert interrupted.status == "interrupted"
    assert interrupted.finished_at is not None
    assert interrupted.last_error == "应用重启时终止遗留扫描任务"
    assert "断点重试" in (interrupted.message or "")
    assert repeated_finish.status == "interrupted"
    assert repeated_finish.finished_at == interrupted.finished_at
    assert repeated_finish.message == interrupted.message


def test_sqlite_allows_only_one_active_scan_across_repository_instances(tmp_path: Path) -> None:
    repo, path = _repository(tmp_path)
    independent_repo = MarketScanRepository(path, threading.RLock())
    first = repo.create_run(**_run_values())

    with pytest.raises(sqlite3.IntegrityError):
        independent_repo.create_run(**_run_values(as_of="2026-07-18 16:30:00"))

    repo.finish_run(first.id, "failed", message="释放活动约束")
    second = independent_repo.create_run(**_run_values(as_of="2026-07-18 16:30:00"))

    assert second.id > first.id
    assert second.status == "queued"


def test_result_batch_rejects_unknown_duplicate_and_invalid_payloads(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, [_sample_seeds()[0]])
    valid = _write("600001.SH", status="success", score=88, quality=95)

    with pytest.raises(ValueError, match="重复股票"):
        repo.save_result_batch(run.id, [valid, valid])
    with pytest.raises(ValueError, match="不属于待处理股票"):
        repo.save_result_batch(
            run.id,
            [_write("600099.SH", status="missing", error="无此股票")],
        )
    with pytest.raises(ValueError, match="非有限数值"):
        repo.save_result_batch(
            run.id,
            [
                MarketScanResultWrite(
                    symbol="600001.SH",
                    status="missing",
                    amount=float("inf"),
                    error="异常",
                )
            ],
        )
    with pytest.raises(ValueError, match="缺少评分"):
        repo.save_result_batch(
            run.id,
            [MarketScanResultWrite(symbol="600001.SH", status="success", score=80)],
        )
    with pytest.raises(ValueError, match="数据来源或评分依据"):
        repo.save_result_batch(
            run.id,
            [
                MarketScanResultWrite(
                    symbol="600001.SH",
                    status="success",
                    score=80,
                    trend_score=80,
                    leader_score=80,
                    data_quality_score=80,
                    price=10,
                    data_date="2026-07-17",
                    metrics={"ma20": 9.5},
                    adjustment_mode="qfq",
                )
            ],
        )
    with pytest.raises(ValueError, match="必须记录错误原因"):
        repo.save_result_batch(
            run.id,
            [MarketScanResultWrite(symbol="600001.SH", status="missing")],
        )

    assert repo.run(run.id).processed_count == 0


def test_terminal_success_and_degraded_states_require_complete_consistent_results(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, _sample_seeds()[:2])

    repo.save_result_batch(
        run.id,
        [_write("600001.SH", status="success", score=88, quality=95)],
    )
    with pytest.raises(ValueError, match="完成全部"):
        repo.finish_run(run.id, "success", message="误报完成")

    repo.save_result_batch(
        run.id,
        [_write("000001.SZ", status="missing", error="行情缺失")],
    )
    with pytest.raises(ValueError, match="不得包含缺失"):
        repo.finish_run(run.id, "success", message="误报成功")

    final = repo.finish_run(run.id, "degraded", message="降级完成")
    assert final.status == "degraded"


def test_all_successful_fallback_results_must_finish_as_degraded(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, [_sample_seeds()[0]])
    fallback = _write("600001.SH", status="success", score=88, quality=95)
    fallback = MarketScanResultWrite(
        **{
            **fallback.__dict__,
            "tags": ("展示文案可以改变",),
            "kline_fallback_used": True,
            "degradation_reasons": ("kline_fallback",),
        }
    )
    repo.save_result_batch(run.id, [fallback])

    assert repo.degraded_result_count(run.id) == 1
    with pytest.raises(ValueError, match="必须标记为降级"):
        repo.finish_run(run.id, "success", message="误报成功")

    final = repo.finish_run(run.id, "degraded", message="备用数据降级完成")
    assert final.status == "degraded"

    retried = repo.prepare_retry(run.id)
    assert retried.processed_count == 0
    assert retried.success_count == 0
    assert [item.symbol for item in repo.pending_items(retried.id)] == ["600001.SH"]
    assert repo.run(run.id) == final


def test_display_tags_do_not_control_degradation_or_retry(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, [_sample_seeds()[0]])
    clean = _write("600001.SH", status="success", score=88, quality=95)
    clean = MarketScanResultWrite(**{**clean.__dict__, "tags": ("兜底K线", "任意展示文案")})
    repo.save_result_batch(run.id, [clean])

    assert repo.degraded_result_count(run.id) == 0
    assert "兜底K线" not in _results(repo, run.id).items[0].tags
    final = repo.finish_run(run.id, "success", message="结构化状态为干净结果")
    plan = repo.retry_plan(final.id)

    assert plan.preserved_success_count == 1
    assert plan.pending_count == 0
    assert plan.needs_market_data is False


def test_fallback_stock_pool_source_is_persisted_and_requires_degraded_status(
    tmp_path: Path,
) -> None:
    repo, _path = _repository(tmp_path)
    run = _seed_running_run(repo, [_sample_seeds()[0]])
    sourced = repo.record_stock_pool_source(run.id, "  stale-fallback  ")
    repo.save_result_batch(
        run.id,
        [_write("600001.SH", status="success", score=88, quality=95)],
    )

    assert sourced.stock_pool_source == "stale-fallback"
    with pytest.raises(ValueError, match="必须标记为降级"):
        repo.finish_run(run.id, "success", message="误报成功")

    final = repo.finish_run(run.id, "degraded", message="股票池缓存兜底")
    retried = repo.prepare_retry(run.id, as_of="2026-07-17 16:45:00")

    assert final.stock_pool_source == "stale-fallback"
    assert retried.stock_pool_source == "stale-fallback"
    assert repo.retry_plan(final.id).pending_count == 1


def test_task_run_creation_and_scan_attachment_roll_back_together(tmp_path: Path) -> None:
    repo, path = _repository(tmp_path)
    run = repo.create_run(**_run_values())
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_market_scan_task_attach
            BEFORE UPDATE OF task_run_id ON market_scan_run
            WHEN NEW.task_run_id IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'simulated task attach failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated task attach failure"):
        repo.create_and_attach_task_run(run.id, "full_market_scan")

    current = repo.run(run.id)
    with sqlite3.connect(path) as conn:
        task_rows = conn.execute("SELECT id, status FROM task_run").fetchall()

    assert current.status == "queued"
    assert current.task_run_id is None
    assert task_rows == []


def test_scan_and_linked_task_terminal_state_commit_atomically(tmp_path: Path) -> None:
    repo, path = _repository(tmp_path)
    run = _seed_running_run(repo, [_sample_seeds()[0]])
    repo.save_result_batch(run.id, [_write("600001.SH", status="success", score=88, quality=95)])
    with sqlite3.connect(path) as conn:
        task_run_id = int(
            conn.execute(
                "INSERT INTO task_run (task_name, status, started_at) VALUES ('full_market_scan', 'running', '2026-07-17 16:30:00')"
            ).lastrowid
        )
    repo.attach_task_run(run.id, task_run_id)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_task_terminal
            BEFORE UPDATE OF status ON task_run
            WHEN NEW.status <> 'running'
            BEGIN
                SELECT RAISE(ABORT, 'simulated task persistence failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated task persistence failure"):
        repo.finish_run(run.id, "success", message="应整体回滚")

    assert repo.run(run.id).status == "running"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM task_run WHERE id = ?", (task_run_id,)).fetchone()[0] == "running"
        conn.execute("DROP TRIGGER reject_task_terminal")

    final = repo.finish_run(run.id, "success", message="原子完成")
    with sqlite3.connect(path) as conn:
        task = conn.execute(
            "SELECT status, finished_at, message FROM task_run WHERE id = ?",
            (task_run_id,),
        ).fetchone()

    assert final.status == "success"
    assert task == ("success", final.finished_at, "原子完成")


def test_reconcile_repairs_terminal_scan_with_stale_running_task(tmp_path: Path) -> None:
    repo, path = _repository(tmp_path)
    run = _seed_running_run(repo, [_sample_seeds()[0]])
    repo.save_result_batch(run.id, [_write("600001.SH", status="success", score=88, quality=95)])
    with sqlite3.connect(path) as conn:
        task_run_id = int(
            conn.execute(
                "INSERT INTO task_run (task_name, status, started_at) VALUES ('full_market_scan', 'running', '2026-07-17 16:30:00')"
            ).lastrowid
        )
    repo.attach_task_run(run.id, task_run_id)
    final = repo.finish_run(run.id, "success", message="扫描已完成")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE task_run SET status = 'running', finished_at = NULL, duration_ms = NULL WHERE id = ?",
            (task_run_id,),
        )

    assert repo.reconcile_incomplete_runs() == 0
    with sqlite3.connect(path) as conn:
        task = conn.execute("SELECT status, finished_at FROM task_run WHERE id = ?", (task_run_id,)).fetchone()

    assert task == ("success", final.finished_at)


def _repository(tmp_path: Path) -> tuple[MarketScanRepository, Path]:
    path = tmp_path / "market-scan-repository.sqlite3"
    settings = Settings(cache_path=path, scheduler_enabled=False)
    cache = SQLiteCache(settings=settings)
    return cache.market_scan_repo, path


def _seed_running_run(
    repo: MarketScanRepository,
    seeds: list[MarketScanSeed],
    *,
    as_of: str = "2026-07-17 16:30:00",
    mode: str = "official",
):
    run = repo.create_run(**_run_values(as_of=as_of), mode=mode)
    repo.start_run(run.id)
    repo.seed_results(run.id, seeds, excluded_count=2)
    return repo.run(run.id)


def _run_values(*, as_of: str = "2026-07-17 16:30:00") -> dict[str, str]:
    return {
        "trigger": "manual",
        "rule_version": "full-market-score-v1",
        "as_of": as_of,
        "data_date": as_of[:10],
        "scope": "SH/SZ/BJ listed A-shares",
    }


def _sample_seeds() -> list[MarketScanSeed]:
    return [
        MarketScanSeed("600001.SH", "600001", "SH", "沪电一号", "电力", "20000101"),
        MarketScanSeed("000001.SZ", "000001", "SZ", "*ST银行", "银行", "19910403", True),
        MarketScanSeed("600002.SH", "600002", "SH", "沪电二号", "电力", "20010101"),
        MarketScanSeed("920066.BJ", "920066", "BJ", "北交新星", "高端装备", "20260701", False, True),
        MarketScanSeed("300001.SZ", "300001", "SZ", "新材料", "材料", "20260702", False, True),
        MarketScanSeed("600003.SH", "600003", "SH", "*ST停牌", "新能源电力", "20020101", True),
    ]


def _sample_results() -> list[MarketScanResultWrite]:
    return [
        _write("600001.SH", status="success", score=90, trend=80, change=1, amount=100, quality=95),
        _write("000001.SZ", status="success", score=80, trend=70, change=2, amount=200, quality=90),
        _write("600002.SH", status="success", score=80, trend=70, change=2, amount=200, quality=85),
        _write("920066.BJ", status="success", score=70, trend=60, change=3, amount=300, quality=75),
        _write("300001.SZ", status="missing", error="行情缺失"),
        _write("600003.SH", status="skipped", reason="停牌"),
    ]


def _write(
    symbol: str,
    *,
    status: str,
    score: int | None = None,
    raw_score: float | None = None,
    trend: int | None = None,
    change: float | None = None,
    amount: float | None = None,
    quality: int | None = None,
    reason: str | None = None,
    error: str | None = None,
) -> MarketScanResultWrite:
    return MarketScanResultWrite(
        symbol=symbol,
        status=status,  # type: ignore[arg-type]
        score=score,
        raw_score=raw_score,
        trend_score=trend if trend is not None else score,
        leader_score=score,
        data_quality_score=quality,
        price=10.0 if status == "success" else None,
        change_pct=change,
        turnover_rate=3.0 if status == "success" else None,
        volume_ratio=1.2 if status == "success" else None,
        amount=amount,
        tags=("测试",) if status == "success" else (),
        metrics={"ma20": 9.5} if status == "success" else {},
        score_details=(
            {
                "components": {
                    "score_dimensions": {
                        "scores": {
                            "alpha_5d": float(score or 0),
                            "confidence": float(quality or 0),
                            "risk": float(100 - (score or 0)),
                            "tradability": {100: 90.0, 200: 70.0, 300: 50.0}.get(amount, 40.0),
                        }
                    }
                }
            }
            if status == "success"
            else {}
        ),
        reason=reason or ("测试评分依据" if status == "success" else None),
        error=error,
        data_date="2026-07-17" if status == "success" else None,
        quote_timestamp="2026-07-17 15:00:00" if status == "success" else None,
        quote_observed_at="2026-07-17T07:00:00Z" if status == "success" else None,
        quote_source="test" if status == "success" else None,
        kline_source="test" if status == "success" else None,
        adjustment_mode="qfq" if status == "success" else None,
    )


def _results(
    repo: MarketScanRepository,
    run_id: int,
    *,
    page: int = 1,
    page_size: int = 100,
    status: str | None = "success",
    market: str | tuple[str, ...] | None = None,
    industry: str | tuple[str, ...] | None = None,
    is_st: bool | None = None,
    is_new: bool | None = None,
    min_score: int | None = None,
    max_score: int | None = None,
    min_trend_score: int | None = None,
    max_trend_score: int | None = None,
    min_change_pct: float | None = None,
    max_change_pct: float | None = None,
    min_turnover_rate: float | None = None,
    max_turnover_rate: float | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    min_data_quality_score: int | None = None,
    max_data_quality_score: int | None = None,
    min_confidence: float | None = None,
    max_risk: float | None = None,
    min_tradability: float | None = None,
    symbols: tuple[str, ...] | None = None,
    keyword: str | None = None,
    sort: str | tuple[str, ...] = "rank",
    order: str | tuple[str, ...] = "asc",
):
    return repo.results_page(
        run_id,
        page=page,
        page_size=page_size,
        status=status,  # type: ignore[arg-type]
        market=market,
        industry=industry,
        is_st=is_st,
        is_new=is_new,
        min_score=min_score,
        max_score=max_score,
        min_trend_score=min_trend_score,
        max_trend_score=max_trend_score,
        min_change_pct=min_change_pct,
        max_change_pct=max_change_pct,
        min_turnover_rate=min_turnover_rate,
        max_turnover_rate=max_turnover_rate,
        min_amount=min_amount,
        max_amount=max_amount,
        min_data_quality_score=min_data_quality_score,
        max_data_quality_score=max_data_quality_score,
        min_confidence=min_confidence,
        max_risk=max_risk,
        min_tradability=min_tradability,
        keyword=keyword,
        symbols=symbols,
        sort=sort,  # type: ignore[arg-type]
        order=order,  # type: ignore[arg-type]
    )


def _symbols(page) -> list[str]:
    return [item.symbol for item in page.items]
