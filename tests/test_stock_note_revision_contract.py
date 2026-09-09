"""Public note mutations compare the complete persisted state inside a write transaction."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import sqlite3
import threading

import pytest

from app.db.user_mappers import STOCK_NOTE_COLUMNS
from app.models.user_data import StockNoteInput, StockNoteUpdate
from app.repositories import notes
from app.services import trading_calendar
from app.services.cache import SQLiteCache
from app.services.runtime_backup import create_runtime_backup, restore_runtime_backup
from app.services.user_data_portability import export_user_data, import_user_data
from tests.factories import make_quote
from tests.test_api_notes_routes import _client, _DataHubStub


@pytest.fixture(autouse=True)
def isolated_calendar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", tmp_path / "absent-calendar.json")


def _setup(tmp_path: Path):
    cache = SQLiteCache(tmp_path / "notes.sqlite3")
    quote = make_quote()
    note = cache.create_stock_note(quote, StockNoteInput(
        symbol="600519", content="原研究结论", price=100, trade_date="2026-09-09", color="#123456",
    ))
    return cache, _client(_DataHubStub(cache=cache, quote=quote)), note


def _rows(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT * FROM stock_note ORDER BY id").fetchall()


@pytest.mark.parametrize("stale_action", ["edit", "hide", "delete", "empty"])
def test_stale_editor_cannot_overwrite_hide_delete_or_noop_new_state_at_same_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_action: str,
) -> None:
    monkeypatch.setattr(notes, "now_text", lambda: "2026-09-09T00:00:00.000000Z")
    cache, client, initial = _setup(tmp_path)
    newer = client.patch(f"/api/stock/notes/{initial.id}", json={
        "expected_revision": initial.revision, "content": "另一个页面已保存的新结论",
    })
    assert newer.status_code == 200
    assert newer.json()["updated_at"] == initial.updated_at
    assert newer.json()["revision"] != initial.revision
    before = _rows(cache.path)
    payload = {"expected_revision": initial.revision}
    if stale_action == "edit":
        payload.update(content=initial.content, note_type="风险")
    elif stale_action == "hide":
        payload["visible"] = False
    response = client.delete(f"/api/stock/notes/{initial.id}", params=payload) if stale_action == "delete" else (
        client.patch(f"/api/stock/notes/{initial.id}", json=payload)
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert _rows(cache.path) == before
    latest = client.get(f"/api/stock/notes/{initial.id}")
    assert latest.status_code == 200 and latest.json() == newer.json()
    assert latest.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method", ["patch", "delete"])
@pytest.mark.parametrize("revision", [None, "", "a" * 63, "A" * 64, "g" * 64, "a" * 65])
def test_note_mutations_require_a_valid_explicit_state_precondition(
    tmp_path: Path, method: str, revision: str | None,
) -> None:
    cache, client, note = _setup(tmp_path)
    before = _rows(cache.path)
    supplied = {} if revision is None else {"expected_revision": revision}
    response = client.patch(f"/api/stock/notes/{note.id}", json={"content": "不应保存", **supplied}) if method == "patch" else (
        client.delete(f"/api/stock/notes/{note.id}", params=supplied)
    )
    assert response.status_code == 422
    assert _rows(cache.path) == before


@pytest.mark.parametrize(("column", "value"), [
    ("symbol", "000001.SZ"), ("code", "000001"), ("market", "SZ"), ("name", "新名称"),
    ("content", "不同内容"), ("note_type", "风险"), ("price", 101), ("trade_date", "2026-09-08"),
    ("color", "#654321"), ("visible", 0), ("created_at", "2026-09-08T00:00:00.000000Z"),
    ("updated_at", "2026-09-08T01:00:00.000000Z"),
])
def test_every_persisted_note_field_changes_revision_and_rejects_stale_delete(
    tmp_path: Path, column: str, value: object,
) -> None:
    cache, client, initial = _setup(tmp_path)
    with sqlite3.connect(cache.path) as conn:
        conn.execute(f"UPDATE stock_note SET {column} = ? WHERE id = ?", (value, initial.id))
    current = client.get(f"/api/stock/notes/{initial.id}")
    assert current.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{64}", current.json()["revision"])
    assert current.json()["revision"] != initial.revision
    before = _rows(cache.path)
    response = client.delete(f"/api/stock/notes/{initial.id}", params={"expected_revision": initial.revision})
    assert response.status_code == 409 and _rows(cache.path) == before


@pytest.mark.parametrize(("first", "second"), [("bad-one", "bad-two"), (b"bad", "bad"), (float("inf"), float("-inf"))])
def test_legacy_display_fallbacks_do_not_collapse_distinct_raw_states(
    tmp_path: Path, first: object, second: object,
) -> None:
    cache, client, note = _setup(tmp_path)
    revisions = []
    for value in (first, second):
        with sqlite3.connect(cache.path) as conn:
            conn.execute("UPDATE stock_note SET price = ? WHERE id = ?", (value, note.id))
        response = client.get(f"/api/stock/notes/{note.id}")
        assert response.status_code == 200 and response.json()["price"] is None
        revisions.append(response.json()["revision"])
    assert revisions[0] != revisions[1]
    before = _rows(cache.path)
    stale = client.patch(f"/api/stock/notes/{note.id}", json={"expected_revision": revisions[0], "visible": False})
    assert stale.status_code == 409 and _rows(cache.path) == before


def test_single_note_recovery_reaches_hidden_item_beyond_visible_list_limit(tmp_path: Path) -> None:
    cache, client, first = _setup(tmp_path)
    hidden = client.patch(f"/api/stock/notes/{first.id}", json={"expected_revision": first.revision, "visible": False})
    for index in range(10):
        cache.create_stock_note(make_quote(), StockNoteInput(symbol="600519", content=f"新笔记{index}", trade_date="2026-09-10"))
    assert first.id not in {item["id"] for item in client.get("/api/stock/notes?symbol=600519&limit=8").json()}
    recovered = client.get(f"/api/stock/notes/{first.id}")
    assert recovered.status_code == 200 and recovered.json() == hidden.json()
    assert client.get("/api/stock/notes/999999").status_code == 404


def test_empty_current_patch_keeps_revision_and_complete_stored_state(tmp_path: Path) -> None:
    cache, client, note = _setup(tmp_path)
    before = _rows(cache.path)
    response = client.patch(f"/api/stock/notes/{note.id}", json={"expected_revision": note.revision})
    assert response.status_code == 200 and response.json() == note.model_dump(mode="json")
    assert _rows(cache.path) == before


@pytest.mark.parametrize("action", ["patch", "delete"])
def test_two_independent_caches_serialize_same_revision_mutations(tmp_path: Path, action: str) -> None:
    cache, _, note = _setup(tmp_path)
    second_cache = SQLiteCache(cache.path)
    barrier = threading.Barrier(2)

    def mutate(selected_cache: SQLiteCache, label: str):
        with _client(_DataHubStub(cache=selected_cache, quote=make_quote())) as client:
            barrier.wait(timeout=5)
            if action == "delete" and label == "B":
                return label, client.delete(f"/api/stock/notes/{note.id}", params={"expected_revision": note.revision})
            return label, client.patch(f"/api/stock/notes/{note.id}", json={"expected_revision": note.revision, "content": label})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(mutate, cache, "A"), pool.submit(mutate, second_cache, "B")]
        results = [future.result(timeout=10) for future in futures]
    assert sum(response.status_code == 200 for _, response in results) == 1
    loser, = [response for _, response in results if response.status_code != 200]
    assert loser.status_code in ({404, 409} if action == "delete" else {409})
    winner, response = next(item for item in results if item[1].status_code == 200)
    current = cache.stock_note(note.id)
    if action == "delete" and winner == "B":
        assert current is None
    else:
        assert current is not None and current.content == winner and current.revision == response.json()["revision"]


@pytest.mark.parametrize("fault", [RuntimeError, KeyboardInterrupt])
def test_failed_mutation_receipt_rolls_back_state_and_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: type[BaseException],
) -> None:
    cache, _, note = _setup(tmp_path)
    before = _rows(cache.path)

    def failed_receipt(_row):
        raise fault("receipt failed")

    with monkeypatch.context() as scoped:
        scoped.setattr(notes, "row_to_stock_note", failed_receipt)
        with pytest.raises(fault):
            cache.update_stock_note(note.id, StockNoteUpdate(expected_revision=note.revision, content="不得提交"))
    assert _rows(cache.path) == before
    assert cache.stock_note(note.id).revision == note.revision


def test_derived_revision_needs_no_storage_or_export_schema_change(tmp_path: Path) -> None:
    cache, _, note = _setup(tmp_path)
    with sqlite3.connect(cache.path) as conn:
        columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(stock_note)"))
    assert columns == STOCK_NOTE_COLUMNS and "revision" not in columns
    bundle = export_user_data(cache.path)
    assert "revision" not in bundle.tables["stock_note"].columns
    restored = SQLiteCache(cache.path).stock_note(note.id)
    assert restored is not None and restored.revision == note.revision


@pytest.mark.parametrize("operation", ["replace", "restore"])
def test_restored_different_state_rejects_pre_restore_editor_and_original_backup_is_untouched(
    tmp_path: Path, operation: str,
) -> None:
    cache, client, original = _setup(tmp_path)
    bundle = export_user_data(cache.path)
    backup = create_runtime_backup(cache.path, tmp_path / "frozen-backup")
    backup_path = Path(backup.database_path)
    backup_bytes = backup_path.read_bytes()
    updated = client.patch(f"/api/stock/notes/{original.id}", json={"expected_revision": original.revision, "content": "恢复前B版本"})
    assert updated.status_code == 200
    if operation == "replace":
        assert import_user_data(cache.path, bundle, mode="replace", dry_run=False).committed
    else:
        assert restore_runtime_backup(Path(backup.backup_path), cache.path, service_stopped=True).restored
    actual = client.get(f"/api/stock/notes/{original.id}").json()
    assert actual["revision"] == original.revision and actual["content"] == original.content
    stale = client.patch(f"/api/stock/notes/{original.id}", json={"expected_revision": updated.json()["revision"], "content": "不得覆盖恢复结果"})
    assert stale.status_code == 409
    assert backup_path.read_bytes() == backup_bytes
    assert client.get(f"/api/stock/notes/{original.id}").json() == actual
    # State-based CAS deliberately allows an identical restored representation.
    same_state = client.patch(f"/api/stock/notes/{original.id}", json={"expected_revision": original.revision, "content": "明确基于原状态的修改"})
    assert same_state.status_code == 200


def test_import_preview_and_merge_preserve_current_note_state_identity(tmp_path: Path) -> None:
    cache, client, original = _setup(tmp_path)
    bundle = export_user_data(cache.path)
    changed = client.patch(f"/api/stock/notes/{original.id}", json={"expected_revision": original.revision, "content": "当前新内容"}).json()
    before = _rows(cache.path)
    preview = import_user_data(cache.path, bundle, mode="replace", dry_run=True)
    assert preview.dry_run and not preview.committed and _rows(cache.path) == before
    result = import_user_data(cache.path, bundle, mode="merge", dry_run=False)
    assert result.committed
    assert client.get(f"/api/stock/notes/{original.id}").json() == changed
    items = client.get("/api/stock/notes?symbol=600519").json()
    assert len(items) == 2
    imported, = [item for item in items if item["id"] != original.id]
    assert imported["content"] == original.content and imported["revision"] != original.revision


def test_cas_check_and_receipt_share_the_same_immediate_write_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _, note = _setup(tmp_path)
    second_cache = SQLiteCache(cache.path)
    checked = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    original = notes._note_for_revision

    def held_check(conn: sqlite3.Connection, row_id: int, revision: str):
        assert conn.in_transaction
        row = original(conn, row_id, revision)
        if not checked.is_set():
            checked.set()
            assert release.wait(timeout=5)
        return row

    def write(selected: SQLiteCache, content: str):
        if content == "B":
            second_started.set()
        with _client(_DataHubStub(cache=selected, quote=make_quote())) as client:
            return client.patch(f"/api/stock/notes/{note.id}", json={"expected_revision": note.revision, "content": content})

    monkeypatch.setattr(notes, "_note_for_revision", held_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, cache, "A")
        try:
            assert checked.wait(timeout=5)
            second = pool.submit(write, second_cache, "B")
            assert second_started.wait(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200
        assert second.result(timeout=10).status_code == 409
    current = cache.stock_note(note.id)
    assert current is not None and current.content == "A"


def test_delete_commit_failure_preserves_note_and_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_note_write_transaction import _install_storage_fault

    cache, client, note = _setup(tmp_path)
    before = _rows(cache.path)
    with monkeypatch.context() as scoped:
        _install_storage_fault(scoped, "commit")
        response = client.delete(f"/api/stock/notes/{note.id}", params={"expected_revision": note.revision})
    assert response.status_code == 503 and _rows(cache.path) == before
    retry = client.delete(f"/api/stock/notes/{note.id}", params={"expected_revision": note.revision})
    assert retry.status_code == 200 and _rows(cache.path) == []
