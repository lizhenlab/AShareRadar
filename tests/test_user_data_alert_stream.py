from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from app.services.user_data_portability import export_user_data, import_user_data
import app.services.user_data_portability as portability
from tests.test_runtime_restore_alert_stream import _append_events, _cache, _stream


@pytest.mark.parametrize("empty", [False, True])
def test_replace_import_rotates_stream_atomically_even_without_events(tmp_path, empty) -> None:
    source = _cache(tmp_path / "source.sqlite3")
    target = _cache(tmp_path / "target.sqlite3")
    if not empty:
        _append_events(source, 2)
        _append_events(target, 5)
    bundle = export_user_data(source.path)
    previous = _stream(target.path)
    assert "alert_stream_state" not in bundle.tables

    result = import_user_data(target.path, bundle, mode="replace", dry_run=False)

    state = _stream(target.path)
    assert result.committed is True
    assert state.stream_id != previous.stream_id
    assert state.baseline_event_id == (0 if empty else 5)
    with closing(sqlite3.connect(target.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == (0 if empty else 2)


@pytest.mark.parametrize("mode,dry_run", [("merge", False), ("merge", True), ("replace", True)])
def test_import_without_committed_replacement_preserves_stream(tmp_path, mode, dry_run) -> None:
    source = _cache(tmp_path / "source.sqlite3")
    target = _cache(tmp_path / "target.sqlite3")
    _append_events(source, 2)
    before = _stream(target.path)
    bundle = export_user_data(source.path)
    import_user_data(target.path, bundle, mode=mode, dry_run=dry_run)
    assert _stream(target.path) == before


def test_replace_import_rotation_failure_rolls_back_business_rows_and_identity(tmp_path, monkeypatch) -> None:
    source = _cache(tmp_path / "source.sqlite3")
    target = _cache(tmp_path / "target.sqlite3")
    _append_events(source, 2)
    _append_events(target, 5)
    before = _stream(target.path)
    bundle = export_user_data(source.path)
    original_rotate = portability.rotate_alert_stream_state

    def reject_rotation(conn):
        assert original_rotate(conn).stream_id != before.stream_id
        raise RuntimeError("injected stream rotation failure")

    monkeypatch.setattr(portability, "rotate_alert_stream_state", reject_rotation, raising=False)
    with pytest.raises(RuntimeError, match="injected stream rotation"):
        import_user_data(target.path, bundle, mode="replace", dry_run=False)
    assert _stream(target.path) == before
    with closing(sqlite3.connect(target.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 5
