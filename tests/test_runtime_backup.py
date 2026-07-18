from __future__ import annotations

import fcntl
from pathlib import Path
import sqlite3

import pytest

import app.repositories.maintenance as maintenance_module
import app.services.runtime_backup as runtime_backup_module
from app.services.cache import SQLiteCache
from app.config import Settings
from app.services.runtime_backup import (
    RuntimeBackupError,
    create_runtime_backup,
    restore_runtime_backup,
    verify_runtime_backup,
)


def test_backup_verify_restore_and_automatic_rollback_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    _insert_watchlist(target, "before")
    backup = create_runtime_backup(target, tmp_path / "backup")
    _update_watchlist(target, "after")

    verified = verify_runtime_backup(Path(backup.backup_path))
    restored = restore_runtime_backup(
        Path(backup.backup_path),
        target,
        service_stopped=True,
        rollback_destination=tmp_path / "rollback",
    )

    assert verified.ok is True
    assert _watchlist_note(target) == "before"
    assert restored.rollback_backup_path == str(tmp_path / "rollback")
    rollback = verify_runtime_backup(Path(restored.rollback_backup_path))
    assert _watchlist_note(Path(rollback.database_path)) == "after"


def test_backup_verification_rejects_tampered_database(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    backup = create_runtime_backup(target, tmp_path / "backup")

    with Path(backup.database_path).open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(RuntimeBackupError, match="SHA-256"):
        verify_runtime_backup(Path(backup.backup_path))


def test_restore_uses_private_verified_snapshot_when_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    _insert_watchlist(target, "before")
    backup = create_runtime_backup(target, tmp_path / "backup")
    _update_watchlist(target, "current")
    original_replace = runtime_backup_module._replace_from_verified_backup

    def replace_after_source_mutation(*args: object, **kwargs: object) -> None:
        _update_watchlist(Path(backup.database_path), "mutate")
        original_replace(*args, **kwargs)

    monkeypatch.setattr(runtime_backup_module, "_replace_from_verified_backup", replace_after_source_mutation)

    restored = restore_runtime_backup(Path(backup.backup_path), target, service_stopped=True)

    assert restored.restored is True
    assert _watchlist_note(Path(backup.database_path)) == "mutate"
    assert _watchlist_note(target) == "before"


def test_restore_requires_service_stopped_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    backup = create_runtime_backup(target, tmp_path / "backup")

    with pytest.raises(RuntimeBackupError, match="service_stopped=True"):
        restore_runtime_backup(Path(backup.backup_path), target)


def test_restore_rejects_held_scheduler_lock(tmp_path: Path) -> None:
    target = tmp_path / "runtime.sqlite3"
    SQLiteCache(target)
    backup = create_runtime_backup(target, tmp_path / "backup")
    lock_path = Path(f"{target}.scheduler.lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeBackupError, match="仍在使用目标数据库"):
            restore_runtime_backup(Path(backup.backup_path), target, service_stopped=True)


def test_orphaned_task_runs_are_reconciled_once(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "runtime.sqlite3")
    cache.start_task_run("first")
    cache.start_task_run("second")

    assert cache.reconcile_orphaned_task_runs() == 2
    assert cache.reconcile_orphaned_task_runs() == 0
    runs = cache.recent_task_runs(limit=5)
    assert [run.status for run in runs] == ["cancelled", "cancelled"]
    assert all(run.finished_at is not None for run in runs)
    assert all(run.duration_ms is None or run.duration_ms >= 0 for run in runs)


def test_runtime_cleanup_preview_matches_committed_removal(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_task_run_rows=1)
    cache = SQLiteCache(path, settings=settings)
    for task_name in ("first", "second", "third"):
        run_id = cache.start_task_run(task_name)
        cache.finish_task_run(run_id, "success")

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()

    assert preview["task_run"] == 2
    assert removed["task_run"] == 2
    assert len(cache.recent_task_runs(limit=10)) == 1


def test_runtime_cleanup_excludes_advice_with_review_plan_and_cleans_other_tables(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_advice_history_rows=1, max_task_run_rows=1)
    cache = SQLiteCache(path, settings=settings)
    protected_old = _insert_advice(path, "protected", "2026-07-14 10:00:00")
    removable_old = _insert_advice(path, "removable", "2026-07-15 10:00:00")
    retained_new = _insert_advice(path, "retained", "2026-07-16 10:00:00")
    _insert_review_plan(path, protected_old)
    for task_name in ("first", "second", "third"):
        run_id = cache.start_task_run(task_name)
        cache.finish_task_run(run_id, "success")

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()

    assert preview["advice_history"] == 1
    assert removed["advice_history"] == preview["advice_history"]
    assert preview["task_run"] == 2
    assert removed["task_run"] == preview["task_run"]
    with sqlite3.connect(path) as conn:
        remaining_advice = [int(row[0]) for row in conn.execute("SELECT id FROM advice_history ORDER BY id")]
        assert remaining_advice == [protected_old, retained_new]
        assert conn.execute("SELECT advice_id FROM advice_review_plan").fetchone()[0] == protected_old
        assert conn.execute("SELECT COUNT(*) FROM task_run").fetchone()[0] == 1
    assert removable_old not in remaining_advice


def test_runtime_cleanup_caps_only_deleted_symbols_unread_badges_to_remaining_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_advice_history_rows=2)
    cache = SQLiteCache(path, settings=settings)
    _insert_watchlist_state(path, "600519.SH", unread_change_count=1, last_viewed_at=None)
    _insert_watchlist_state(
        path,
        "000001.SZ",
        unread_change_count=0,
        last_viewed_at=None,
    )
    _insert_advice(path, "a-baseline", "2026-07-14 10:00:00", symbol="600519.SH", confidence=80)
    protected = _insert_advice(path, "b-protected", "2026-07-14 10:00:01", symbol="000001.SZ", confidence=70)
    viewed_through = _insert_advice(path, "b-deleted", "2026-07-14 10:00:02", symbol="000001.SZ", confidence=71)
    _insert_advice(path, "a-retained", "2026-07-14 10:00:03", symbol="600519.SH", confidence=81)
    _insert_advice(path, "b-retained", "2026-07-14 10:00:04", symbol="000001.SZ", confidence=72)
    _insert_review_plan(path, protected)
    partially_viewed = cache.mark_watchlist_viewed("000001.SZ", viewed_through_advice_id=viewed_through)
    monkeypatch.setattr(maintenance_module, "CLEANUP_DELETE_BATCH_ROWS", 1)

    with sqlite3.connect(path) as conn:
        badges_before_preview = dict(conn.execute("SELECT symbol, unread_change_count FROM watchlist"))
    preview = cache.preview_runtime_cleanup()
    with sqlite3.connect(path) as conn:
        badges_after_preview = dict(conn.execute("SELECT symbol, unread_change_count FROM watchlist"))

    removed = cache.cleanup_runtime_rows()

    with sqlite3.connect(path) as conn:
        badges = dict(conn.execute("SELECT symbol, unread_change_count FROM watchlist"))
        remaining_by_symbol = {
            symbol: [row[0] for row in conn.execute("SELECT id FROM advice_history WHERE symbol = ? ORDER BY id", (symbol,))]
            for symbol in ("600519.SH", "000001.SZ")
        }
        protected_advice = conn.execute("SELECT advice_id FROM advice_review_plan").fetchone()[0]

    assert preview["advice_history"] == 2
    assert partially_viewed is not None
    assert partially_viewed.unread_change_count == 1
    assert badges_before_preview == badges_after_preview == {"000001.SZ": 1, "600519.SH": 1}
    assert removed["advice_history"] == preview["advice_history"]
    assert badges == {"000001.SZ": 1, "600519.SH": 0}
    assert remaining_by_symbol["600519.SH"] == [4]
    assert remaining_by_symbol["000001.SZ"] == [protected, 5]
    assert protected_advice == protected


def test_regenerable_cleanup_does_not_touch_watchlist_unread_state(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, max_cache_event_rows=1, max_advice_history_rows=1)
    cache = SQLiteCache(path, settings=settings)
    _insert_watchlist_state(path, "600519.SH", unread_change_count=1, last_viewed_at=None)
    _insert_advice(path, "first", "2026-07-14 10:00:00", confidence=80)
    _insert_advice(path, "second", "2026-07-14 10:00:01", confidence=81)
    cache.log_event("runtime", "first")
    cache.log_event("runtime", "second")

    removed = cache.maintenance_repo.cleanup_regenerable_runtime_rows()

    with sqlite3.connect(path) as conn:
        unread_change_count = conn.execute(
            "SELECT unread_change_count FROM watchlist WHERE symbol = '600519.SH'"
        ).fetchone()[0]
        advice_count = conn.execute("SELECT COUNT(*) FROM advice_history").fetchone()[0]

    assert removed["cache_event"] == 1
    assert unread_change_count == 1
    assert advice_count == 2


def _insert_watchlist(path: Path, note: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (
                symbol, code, market, name, note, group_name, pinned,
                research_status, priority, unread_change_count, created_at, updated_at
            ) VALUES (
                '600519.SH', '600519', 'SH', '贵州茅台', ?, '默认', 0,
                'watching', 'medium', 0, '2026-07-16 10:00:00', '2026-07-16 10:00:00'
            )
            """,
            (note,),
        )


def _insert_watchlist_state(
    path: Path,
    symbol: str,
    *,
    unread_change_count: int,
    last_viewed_at: str | None,
) -> None:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (
                symbol, code, market, name, note, group_name, pinned,
                research_status, priority, last_viewed_at, unread_change_count, created_at, updated_at
            ) VALUES (?, ?, ?, '测试股票', NULL, '默认', 0, 'watching', 'medium', ?, ?, ?, ?)
            """,
            (symbol, code, market, last_viewed_at, unread_change_count, "2026-07-16 10:00:00", "2026-07-16 10:00:00"),
        )


def _update_watchlist(path: Path, note: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE watchlist SET note = ? WHERE symbol = '600519.SH'", (note,))


def _watchlist_note(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return str(conn.execute("SELECT note FROM watchlist WHERE symbol = '600519.SH'").fetchone()[0])


def _insert_advice(
    path: Path,
    name: str,
    created_at: str,
    *,
    symbol: str = "600519.SH",
    confidence: int = 80,
) -> int:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO advice_history (
                symbol, code, market, name, action, confidence, trend_score, trend_label,
                risk_level, price, change_pct, support, resistance, data_quality_score,
                data_quality_level, reason, summary, created_at, updated_at,
                snapshot_contract_version, conclusion_basis, rule_version, model_version,
                data_quality_source
            ) VALUES (
                ?, ?, ?, ?, '观望', ?, 60, '偏强', '中',
                100, 0, 90, 110, 90, '良好', 'reason', 'summary', ?, ?,
                'conclusion.v1', 'analysis_action_advice', 'rules.test', 'none', '测试数据'
            )
            """,
            (symbol, code, market, name, confidence, created_at, created_at),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _insert_review_plan(path: Path, advice_id: int) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO advice_review_plan (
                advice_id, symbol, snapshot_market_time, snapshot_price, hypothesis,
                trigger_condition, invalidation_condition, target_price, stop_price,
                horizon_days, evidence_refs_json, revision, created_at, updated_at
            ) VALUES (
                ?, '600519.SH', '2026-07-14 10:00:00', 100, 'hypothesis',
                'trigger', 'invalidation', 110, 90, 5, '[]', 1,
                '2026-07-14 10:00:00', '2026-07-14 10:00:00'
            )
            """,
            (advice_id,),
        )
