from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from app.services.cache import SQLiteCache


RAW_ERROR = "GET https://alice:secret@example.test/quote?token=raw-token failed"


def test_provider_status_repository_sanitizes_general_and_capability_errors_before_write() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)

        cache.update_provider_failure("general", 1, RAW_ERROR)
        cache.update_provider_capability_failure("capable", "quote", 2, RAW_ERROR)

        with sqlite3.connect(path) as conn:
            stored_errors = [
                conn.execute("SELECT last_error FROM provider_status WHERE name = 'general'").fetchone()[0],
                conn.execute("SELECT last_error FROM provider_status WHERE name = 'capable'").fetchone()[0],
                conn.execute(
                    "SELECT last_error FROM provider_capability_status WHERE name = 'capable' AND kind = 'quote'"
                ).fetchone()[0],
            ]

    _assert_errors_sanitized(stored_errors)


def test_provider_status_repository_sanitizes_historical_dirty_errors_when_mapping() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.ensure_provider("general", 1)
        cache.ensure_provider_capability("capable", "quote", 2)

        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE provider_status SET healthy = 0, last_error = ? WHERE name = 'general'", (RAW_ERROR,))
            conn.execute(
                "UPDATE provider_capability_status SET healthy = 0, last_error = ? WHERE name = 'capable' AND kind = 'quote'",
                (RAW_ERROR,),
            )

        provider_error = next(item.last_error for item in cache.provider_statuses() if item.name == "general")
        capability_error = next(
            item.last_error
            for item in cache.provider_capability_statuses()
            if item.name == "capable" and item.kind == "quote"
        )

    _assert_errors_sanitized([provider_error, capability_error])


def _assert_errors_sanitized(errors: list[str | None]) -> None:
    assert all(error is not None and "alice" not in error for error in errors)
    assert all(error is not None and "secret" not in error for error in errors)
    assert all(error is not None and "raw-token" not in error for error in errors)
    assert all(error is not None and "<redacted>" in error for error in errors)
