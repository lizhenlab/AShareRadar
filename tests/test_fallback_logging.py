from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from app.config import Settings
from app.services.cache import SQLiteCache
from app.services.scheduler import LocalDataScheduler
from app.utils.fallback_logging import report_persistence_failure


def test_persistence_fallback_reports_safe_category_without_raw_error(capsys) -> None:
    report_persistence_failure(
        "test persistence failed",
        sqlite3.OperationalError("database is locked; api_key=do-not-print"),
    )

    output = capsys.readouterr().err
    assert "test persistence failed: OperationalError: database is locked" in output
    assert "do-not-print" not in output
    assert "api_key" not in output


def test_cache_event_failure_is_visible_without_raising(tmp_path: Path, capsys) -> None:
    cache = SQLiteCache(settings=Settings(cache_path=tmp_path / "cache.sqlite3", scheduler_enabled=False))

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database; token=secret")

    cache.runtime_event_repo.log_event = fail  # type: ignore[method-assign]
    cache.log_event("test", "message")

    output = capsys.readouterr().err
    assert "cache event persistence failed: OperationalError: readonly database" in output
    assert "secret" not in output


def test_scheduler_monitor_failure_is_visible_without_raising(capsys) -> None:
    settings = Settings(scheduler_enabled=False)

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full; authorization=secret")

    hub = SimpleNamespace(
        settings=settings,
        cache=SimpleNamespace(save_monitor_event=fail),
    )
    scheduler = LocalDataScheduler(hub)  # type: ignore[arg-type]

    asyncio.run(scheduler._save_monitor_event("error", "test", "message"))  # noqa: SLF001

    output = capsys.readouterr().err
    assert "scheduler monitor persistence failed: OperationalError: database or disk is full" in output
    assert "secret" not in output
