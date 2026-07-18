from __future__ import annotations

from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.local_data_import_guard as import_guard_module
from app.api.deps import get_datahub, get_local_data_import_previews
from app.api.routes import local_data
from app.config import Settings
from app.models.schemas import AlertRuleInput
from app.services.cache import SQLiteCache
from app.services.local_data_import_guard import LocalDataImportPreviewRegistry
from app.services.runtime_backup import verify_runtime_backup
from app.services.user_data_portability import export_user_data
from tests.factories import make_quote


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
    monkeypatch.setattr(local_data, "create_runtime_backup", lambda _path: (_ for _ in ()).throw(PermissionError("secret/path")))

    response = client.post(
        f"/api/local-data/import?mode=merge&dry_run=false&preview_token={preview['preview_token']}",
        json=bundle,
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "本地恢复备份创建失败，请检查数据目录权限和可用空间"}


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


class _DataHubStub:
    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache


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
