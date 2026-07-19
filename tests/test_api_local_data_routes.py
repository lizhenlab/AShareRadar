from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
import sqlite3
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.local_data_import_guard as import_guard_module
import app.services.runtime_backup as runtime_backup_module
from app.api.deps import get_datahub, get_local_data_import_previews
from app.api.routes import local_data
from app.config import Settings
from app.models.schemas import AlertRuleInput
from app.services.cache import SQLiteCache
from app.services.local_data_import_guard import LocalDataImportPreviewRegistry
from app.services.runtime_backup import create_runtime_backup, verify_runtime_backup
from app.services.user_data_portability import export_user_data
from tests.factories import make_quote


def _hold_destructive_local_data_lease(
    database_path: str,
    attempting,
    entered,
    release,
    outcomes,
) -> None:
    try:
        cache = SQLiteCache(Path(database_path))
        attempting.set()
        with cache.exclusive_local_data_operation():
            entered.set()
            if not release.wait(timeout=20):
                raise RuntimeError("lease worker release timed out")
        outcomes.put((True, ""))
    except BaseException as exc:
        outcomes.put((False, f"{type(exc).__name__}: {exc}"))


def _insert_alert_events_in_process(
    database_path: str,
    rule_id: int,
    attempting,
    finished,
    outcomes,
) -> None:
    try:
        with sqlite3.connect(database_path, timeout=20) as conn:
            conn.execute("PRAGMA busy_timeout = 20000")
            attempting.set()
            conn.executemany(
                """
                INSERT INTO alert_event (
                    rule_id, symbol, code, market, stock_name, name, condition_type,
                    event_type, message, price, change_pct, threshold, created_at
                ) VALUES (?, '600519.SH', '600519', 'SH', '贵州茅台', '并发提醒',
                          'price_above', '触发', ?, 1301, 0.1, 1300, ?)
                """,
                [
                    (rule_id, f"并发事件{index}", f"2026-07-16 11:0{index}:00")
                    for index in range(2)
                ],
            )
        outcomes.put((True, ""))
    except BaseException as exc:
        outcomes.put((False, f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _create_runtime_backup_in_process(
    database_path: str,
    attempting,
    finished,
    outcomes,
) -> None:
    try:
        attempting.set()
        result = create_runtime_backup(Path(database_path), max_backups=2)
        outcomes.put((True, result.backup_path))
    except BaseException as exc:
        outcomes.put((False, f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def test_replace_import_requires_matching_preview_and_creates_verified_backup(tmp_path: Path) -> None:
    source = SQLiteCache(tmp_path / "source.sqlite3")
    target = SQLiteCache(tmp_path / "target.sqlite3")
    source.save_watchlist_item(make_quote(), note="source")
    target.save_watchlist_item(make_quote(price=1200), note="target")
    bundle = export_user_data(source.path).model_dump(mode="json")
    registry = LocalDataImportPreviewRegistry()
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(target)
    app.dependency_overrides[get_local_data_import_previews] = lambda: registry
    client = TestClient(app)

    rejected = client.post(
        "/api/local-data/import?mode=replace&dry_run=false",
        json=bundle,
    )
    preview = client.post(
        "/api/local-data/import?mode=replace&dry_run=true",
        json=bundle,
    )
    token = preview.json()["preview_token"]
    committed = client.post(
        f"/api/local-data/import?mode=replace&dry_run=false&preview_token={token}",
        json=bundle,
    )

    assert rejected.status_code == 400
    assert "先完成服务端预览" in rejected.json()["detail"]
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    assert len(token) >= 32
    assert committed.status_code == 200
    assert committed.json()["rollback_backup_path"]
    assert committed.json()["committed"] is True
    backup_path = Path(committed.json()["rollback_backup_path"])
    assert verify_runtime_backup(backup_path).ok is True
    assert target.watchlist()[0].note == "source"


def test_import_preview_is_single_use_and_rejects_database_drift(tmp_path: Path) -> None:
    source = SQLiteCache(tmp_path / "source.sqlite3")
    target = SQLiteCache(tmp_path / "target.sqlite3")
    source.save_watchlist_item(make_quote(), note="source")
    bundle = export_user_data(source.path).model_dump(mode="json")
    registry = LocalDataImportPreviewRegistry()
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(target)
    app.dependency_overrides[get_local_data_import_previews] = lambda: registry
    client = TestClient(app)

    first_preview = client.post("/api/local-data/import?mode=merge&dry_run=true", json=bundle).json()
    target.save_watchlist_item(make_quote(price=1210), note="changed after preview")
    stale = client.post(
        f"/api/local-data/import?mode=merge&dry_run=false&preview_token={first_preview['preview_token']}",
        json=bundle,
    )

    second_preview = client.post("/api/local-data/import?mode=merge&dry_run=true", json=bundle).json()
    committed = client.post(
        f"/api/local-data/import?mode=merge&dry_run=false&preview_token={second_preview['preview_token']}",
        json=bundle,
    )
    replayed = client.post(
        f"/api/local-data/import?mode=merge&dry_run=false&preview_token={second_preview['preview_token']}",
        json=bundle,
    )

    assert stale.status_code == 400
    assert "已变化" in stale.json()["detail"]
    assert committed.status_code == 200
    assert replayed.status_code == 400
    assert "已失效" in replayed.json()["detail"]


def test_two_previews_of_same_state_cannot_both_commit(tmp_path: Path) -> None:
    source = SQLiteCache(tmp_path / "source.sqlite3")
    target = SQLiteCache(tmp_path / "target.sqlite3")
    source.save_watchlist_item(make_quote(), note="source")
    bundle = export_user_data(source.path).model_dump(mode="json")
    registry = LocalDataImportPreviewRegistry()
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(target)
    app.dependency_overrides[get_local_data_import_previews] = lambda: registry
    client = TestClient(app)

    first = client.post("/api/local-data/import?mode=merge&dry_run=true", json=bundle).json()
    second = client.post("/api/local-data/import?mode=merge&dry_run=true", json=bundle).json()
    first_commit = client.post(
        f"/api/local-data/import?mode=merge&dry_run=false&preview_token={first['preview_token']}",
        json=bundle,
    )
    stale_second = client.post(
        f"/api/local-data/import?mode=merge&dry_run=false&preview_token={second['preview_token']}",
        json=bundle,
    )

    assert first_commit.status_code == 200
    assert stale_second.status_code == 400
    assert "已变化" in stale_second.json()["detail"]


def test_backup_io_failure_is_returned_as_stable_service_error(tmp_path: Path, monkeypatch) -> None:
    source = SQLiteCache(tmp_path / "source.sqlite3")
    target = SQLiteCache(tmp_path / "target.sqlite3")
    source.save_watchlist_item(make_quote(), note="source")
    bundle = export_user_data(source.path).model_dump(mode="json")
    registry = LocalDataImportPreviewRegistry()
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(target)
    app.dependency_overrides[get_local_data_import_previews] = lambda: registry
    client = TestClient(app)
    preview = client.post("/api/local-data/import?mode=merge&dry_run=true", json=bundle).json()
    monkeypatch.setattr(
        local_data.RuntimeBackupSession,
        "create_verified_backup",
        lambda _self: (_ for _ in ()).throw(PermissionError("secret/path")),
    )

    response = client.post(
        f"/api/local-data/import?mode=merge&dry_run=false&preview_token={preview['preview_token']}",
        json=bundle,
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "本地恢复备份创建失败，请检查数据目录权限和可用空间"}
    assert target.watchlist() == []


def test_expired_server_preview_token_is_rejected(tmp_path: Path, monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(import_guard_module.time, "monotonic", lambda: clock[0])
    source = SQLiteCache(tmp_path / "source.sqlite3")
    target = SQLiteCache(tmp_path / "target.sqlite3")
    source.save_watchlist_item(make_quote(), note="source")
    bundle = export_user_data(source.path).model_dump(mode="json")
    registry = LocalDataImportPreviewRegistry(ttl_seconds=1)
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(target)
    app.dependency_overrides[get_local_data_import_previews] = lambda: registry
    client = TestClient(app)
    preview = client.post("/api/local-data/import?mode=merge&dry_run=true", json=bundle).json()

    clock[0] = 102.0
    response = client.post(
        f"/api/local-data/import?mode=merge&dry_run=false&preview_token={preview['preview_token']}",
        json=bundle,
    )

    assert response.status_code == 400
    assert "已失效" in response.json()["detail"]


def test_destructive_local_data_lease_serializes_processes(tmp_path: Path) -> None:
    target = SQLiteCache(tmp_path / "target.sqlite3")
    context = multiprocessing.get_context("spawn")
    outcomes = context.Queue()
    first_attempting = context.Event()
    first_entered = context.Event()
    first_release = context.Event()
    second_attempting = context.Event()
    second_entered = context.Event()
    second_release = context.Event()
    first = context.Process(
        target=_hold_destructive_local_data_lease,
        args=(str(target.path), first_attempting, first_entered, first_release, outcomes),
    )
    second = context.Process(
        target=_hold_destructive_local_data_lease,
        args=(str(target.path), second_attempting, second_entered, second_release, outcomes),
    )
    first.start()
    assert first_attempting.wait(timeout=10)
    assert first_entered.wait(timeout=10)
    second.start()
    assert second_attempting.wait(timeout=10)
    try:
        assert second_entered.wait(timeout=0.3) is False
        first_release.set()
        assert second_entered.wait(timeout=10)
        second_release.set()
        first.join(timeout=10)
        second.join(timeout=10)
    finally:
        first_release.set()
        second_release.set()
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert first.exitcode == second.exitcode == 0
    assert sorted(outcomes.get(timeout=5) for _process in (first, second)) == [(True, ""), (True, "")]


def test_destructive_local_data_lease_timeout_is_clean_and_redacted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "api_key=very-secret.sqlite3"
    cache = SQLiteCache(path)
    guard = runtime_backup_module.FileInstanceGuard(
        runtime_backup_module._destructive_local_data_lock_path(path)
    )
    assert guard.acquire() is True
    monkeypatch.setattr(runtime_backup_module, "BACKUP_OPERATION_LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(runtime_backup_module, "BACKUP_OPERATION_LOCK_POLL_SECONDS", 0.005)
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(cache)
    client = TestClient(app)

    try:
        response = client.get("/api/local-data/cleanup-preview")
    finally:
        guard.release()

    assert response.status_code == 503
    assert "本地数据破坏性操作繁忙" in response.json()["detail"]
    assert "very-secret" not in response.json()["detail"]
    assert str(tmp_path) not in response.json()["detail"]


class _DataHubStub:
    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache
        self.settings = cache.settings or cache.maintenance_repo.settings


def test_manual_user_history_cleanup_creates_verified_pre_cleanup_backup(tmp_path: Path) -> None:
    path = tmp_path / "target.sqlite3"
    settings = Settings(
        cache_path=path,
        scheduler_enabled=False,
        max_alert_event_rows=1,
    )
    cache = SQLiteCache(path, settings=settings)
    rule = cache.create_alert_rule(
        make_quote(),
        AlertRuleInput(symbol="600519.SH", condition_type="price_above", threshold=1300),
    )
    with sqlite3.connect(path) as conn:
        for index in range(3):
            conn.execute(
                """
                INSERT INTO alert_event (
                    rule_id, symbol, code, market, stock_name, name, condition_type,
                    event_type, message, price, change_pct, threshold, created_at
                ) VALUES (?, '600519.SH', '600519', 'SH', '贵州茅台', '测试提醒',
                          'price_above', '触发', ?, 1301, 0.1, 1300, ?)
                """,
                (rule.id, f"事件{index}", f"2026-07-16 10:0{index}:00"),
            )
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(cache)
    client = TestClient(app)

    preview = client.get("/api/local-data/cleanup-preview")
    cleaned = client.post("/api/local-data/cleanup?confirm=retention-cleanup")

    assert preview.status_code == 200
    assert preview.json()["user_history_rows"] == 2
    assert cleaned.status_code == 200
    assert cleaned.json()["tables"]["alert_event"] == 2
    backup = verify_runtime_backup(Path(cleaned.json()["rollback_backup_path"]))
    assert backup.manifest.user_table_row_counts["alert_event"] == 3
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 1


def test_cleanup_backup_failure_rolls_back_without_deleting_user_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "target.sqlite3"
    settings = Settings(cache_path=path, scheduler_enabled=False, max_alert_event_rows=1)
    cache = SQLiteCache(path, settings=settings)
    rule = cache.create_alert_rule(
        make_quote(),
        AlertRuleInput(symbol="600519.SH", condition_type="price_above", threshold=1300),
    )
    with sqlite3.connect(path) as conn:
        for index in range(3):
            conn.execute(
                """
                INSERT INTO alert_event (
                    rule_id, symbol, code, market, stock_name, name, condition_type,
                    event_type, message, price, change_pct, threshold, created_at
                ) VALUES (?, '600519.SH', '600519', 'SH', '贵州茅台', '测试提醒',
                          'price_above', '触发', ?, 1301, 0.1, 1300, ?)
                """,
                (rule.id, f"事件{index}", f"2026-07-16 10:0{index}:00"),
            )
    monkeypatch.setattr(
        local_data.RuntimeBackupSession,
        "create_verified_backup",
        lambda _self: (_ for _ in ()).throw(PermissionError("secret/path")),
    )
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(cache)
    client = TestClient(app)

    response = client.post("/api/local-data/cleanup?confirm=retention-cleanup")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地恢复备份创建失败，请检查数据目录权限和可用空间"}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 3


def test_cleanup_transaction_blocks_late_process_write_before_no_backup_decision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "target.sqlite3"
    settings = Settings(cache_path=path, scheduler_enabled=False, max_alert_event_rows=1)
    cache = SQLiteCache(path, settings=settings)
    rule = cache.create_alert_rule(
        make_quote(),
        AlertRuleInput(symbol="600519.SH", condition_type="price_above", threshold=1300),
    )
    context = multiprocessing.get_context("spawn")
    attempting = context.Event()
    finished = context.Event()
    outcomes = context.Queue()
    process = context.Process(
        target=_insert_alert_events_in_process,
        args=(str(path), rule.id, attempting, finished, outcomes),
    )
    original_cleanup = cache.cleanup_runtime_rows

    def cleanup_with_late_writer() -> dict[str, int]:
        process.start()
        assert attempting.wait(timeout=10)
        time.sleep(0.1)
        assert finished.is_set() is False
        return original_cleanup()

    monkeypatch.setattr(cache, "cleanup_runtime_rows", cleanup_with_late_writer)
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(cache)
    client = TestClient(app)

    try:
        cleaned = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
        process.join(timeout=20)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert cleaned.status_code == 200
    assert cleaned.json()["tables"]["alert_event"] == 0
    assert cleaned.json()["rollback_backup_path"] is None
    assert process.exitcode == 0
    assert outcomes.get(timeout=5) == (True, "")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 2


def test_cleanup_rollback_backup_is_protected_through_commit_from_process_rotation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "target.sqlite3"
    settings = Settings(
        cache_path=path,
        scheduler_enabled=False,
        max_alert_event_rows=1,
        max_runtime_backups=2,
    )
    cache = SQLiteCache(path, settings=settings)
    rule = cache.create_alert_rule(
        make_quote(),
        AlertRuleInput(symbol="600519.SH", condition_type="price_above", threshold=1300),
    )
    with sqlite3.connect(path) as conn:
        for index in range(3):
            conn.execute(
                """
                INSERT INTO alert_event (
                    rule_id, symbol, code, market, stock_name, name, condition_type,
                    event_type, message, price, change_pct, threshold, created_at
                ) VALUES (?, '600519.SH', '600519', 'SH', '贵州茅台', '测试提醒',
                          'price_above', '触发', ?, 1301, 0.1, 1300, ?)
                """,
                (rule.id, f"事件{index}", f"2026-07-16 10:0{index}:00"),
            )
    create_runtime_backup(path, max_backups=2)
    create_runtime_backup(path, max_backups=2)
    cleanup_applied = multiprocessing.Event()
    release_cleanup = multiprocessing.Event()
    original_cleanup = cache.cleanup_runtime_rows

    def blocked_cleanup() -> dict[str, int]:
        removed = original_cleanup()
        cleanup_applied.set()
        if not release_cleanup.wait(timeout=20):
            raise RuntimeError("cleanup release timed out")
        return removed

    monkeypatch.setattr(cache, "cleanup_runtime_rows", blocked_cleanup)
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(cache)
    client = TestClient(app)
    context = multiprocessing.get_context("spawn")
    rotation_attempting = context.Event()
    rotation_finished = context.Event()
    outcomes = context.Queue()
    rotation = context.Process(
        target=_create_runtime_backup_in_process,
        args=(str(path), rotation_attempting, rotation_finished, outcomes),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        request = executor.submit(
            client.post,
            "/api/local-data/cleanup?confirm=retention-cleanup",
        )
        assert cleanup_applied.wait(timeout=10)
        rotation.start()
        assert rotation_attempting.wait(timeout=10)
        try:
            assert rotation_finished.wait(timeout=0.3) is False
            release_cleanup.set()
            response = request.result(timeout=20)
            rotation.join(timeout=20)
        finally:
            release_cleanup.set()
            if rotation.is_alive():
                rotation.terminate()
                rotation.join(timeout=5)

    assert response.status_code == 200
    rollback_path = Path(response.json()["rollback_backup_path"])
    assert verify_runtime_backup(rollback_path).ok is True
    assert rotation.exitcode == 0
    assert outcomes.get(timeout=5)[0] is True
