from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.repositories.market_scan import (
    MarketScanRepository,
    MarketScanResultWrite,
    MarketScanSeed,
)
from app.services.cache import SQLiteCache


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
    assert _symbols(_results(repo, run.id, keyword="  600002  ")) == ["600002.SH"]
    assert _symbols(_results(repo, run.id, keyword="北交")) == ["920066.BJ"]
    assert _symbols(_results(repo, run.id, keyword="%", status=None)) == []
    assert _symbols(_results(repo, run.id, keyword="_", status=None)) == []


def test_retry_derives_new_run_and_keeps_original_snapshot_immutable(tmp_path: Path) -> None:
    repo, _path = _repository(tmp_path)
    seeds = _sample_seeds()[:3]
    run = _seed_running_run(repo, seeds)
    writes = [
        _write("600001.SH", status="success", score=88, quality=95),
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
    assert by_symbol["600001.SH"].rank is None
    assert by_symbol["000001.SZ"].status == "pending"
    assert by_symbol["000001.SZ"].error is None
    assert by_symbol["600002.SH"].status == "pending"
    assert by_symbol["600002.SH"].reason is None
    assert repo.run(run.id) == original_before
    assert _results(repo, run.id, status=None).items == original_items_before
    assert original_items_before[0].rank == 1


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
    retried = repo.prepare_retry(run.id)

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
):
    run = repo.create_run(**_run_values(as_of=as_of))
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
        MarketScanSeed("600003.SH", "600003", "SH", "*ST停牌", "电力", "20020101", True),
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
        reason=reason or ("测试评分依据" if status == "success" else None),
        error=error,
        data_date="2026-07-17" if status == "success" else None,
        quote_timestamp="2026-07-17 15:00:00" if status == "success" else None,
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
    market: str | None = None,
    industry: str | None = None,
    is_st: bool | None = None,
    is_new: bool | None = None,
    min_data_quality_score: int | None = None,
    keyword: str | None = None,
    sort: str = "rank",
    order: str = "asc",
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
        min_data_quality_score=min_data_quality_score,
        keyword=keyword,
        sort=sort,  # type: ignore[arg-type]
        order=order,  # type: ignore[arg-type]
    )


def _symbols(page) -> list[str]:
    return [item.symbol for item in page.items]
