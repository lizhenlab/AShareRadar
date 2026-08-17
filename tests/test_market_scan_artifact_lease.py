from __future__ import annotations

from pathlib import Path
import os
import multiprocessing
import sqlite3
import stat
import threading

import pytest

from app.db.market_scan_artifact_lease import (
    MarketScanArtifactLeaseError,
    market_scan_artifact_retention_lease,
    market_scan_artifact_lock_path,
    publish_market_scan_artifact,
    validated_market_scan_database_path,
    verified_market_scan_artifact_batch_publication,
)
from app.services.market_scan_probability_artifact import (
    PROBABILITY_MANAGED_DIRECTORY,
)
from app.config import Settings
from app.services.cache import SQLiteCache
from app.services.market_scan_future_range_artifact import (
    FutureRangeArtifactError,
    build_future_range_artifact,
    future_range_artifact_filename,
    write_future_range_artifact,
)
from tests.test_market_scan_future_range import _initialize
from tests.test_market_scan_future_range_store import GENERATED_AT, _payload
from tests.test_market_scan_retention import _insert_published_snapshot


def _hold_artifact_lease(database: str, acquired: object, release: object) -> None:
    with market_scan_artifact_retention_lease(database):
        acquired.set()  # type: ignore[attr-defined]
        assert release.wait(timeout=10)  # type: ignore[attr-defined]


def _observe_artifact_lease(database: str, acquired: object) -> None:
    with market_scan_artifact_retention_lease(database):
        acquired.set()  # type: ignore[attr-defined]


def test_lease_rejects_missing_symlink_and_hardlink_and_is_reentrant(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    database.write_bytes(b"database")
    with market_scan_artifact_retention_lease(database) as first:
        with market_scan_artifact_retention_lease(database) as second:
            assert first == second == database.resolve()
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(database)
    hardlink = tmp_path / "hardlink.sqlite3"
    hardlink.hardlink_to(database)
    for path in (tmp_path / "missing.sqlite3", alias, database, hardlink):
        with pytest.raises(MarketScanArtifactLeaseError):
            validated_market_scan_database_path(path)


@pytest.mark.parametrize("sidecar_kind", ["symlink", "hardlink", "fifo"])
def test_lease_rejects_untrusted_sidecar(
    tmp_path: Path,
    sidecar_kind: str,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    database.write_bytes(b"database")
    sidecar = market_scan_artifact_lock_path(database)
    source = tmp_path / "source"
    source.write_text("lock", encoding="utf-8")
    if sidecar_kind == "symlink":
        sidecar.symlink_to(source)
    elif sidecar_kind == "hardlink":
        sidecar.hardlink_to(source)
    else:
        os.mkfifo(sidecar, stat.S_IRUSR | stat.S_IWUSR)
    with pytest.raises(MarketScanArtifactLeaseError):
        with market_scan_artifact_retention_lease(database):
            pass


def test_managed_future_writer_rechecks_publication_but_external_is_offline(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    _initialize(database)
    artifact = build_future_range_artifact(_payload(), generated_at=GENERATED_AT)
    filename = future_range_artifact_filename(29, artifact)
    managed = tmp_path / "research" / "market_scan_future_range" / filename
    with pytest.raises(FutureRangeArtifactError, match="来源批次已失效"):
        write_future_range_artifact(managed, artifact, database_path=database)
    external = tmp_path / "offline" / filename
    assert write_future_range_artifact(external, artifact, database_path=database) == external


def test_cleanup_commits_before_waiting_writer_rechecks_and_rejects_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    cache = SQLiteCache(
        database,
        settings=Settings(cache_path=database, max_market_scan_runs=1),
    )
    with sqlite3.connect(database) as connection:
        run_id = _insert_published_snapshot(connection, 0)
        _insert_published_snapshot(connection, 1)
    report = _payload()
    report["run"]["run_id"] = run_id  # type: ignore[index]
    for record in report["records"]:  # type: ignore[union-attr]
        record["run_id"] = run_id
    artifact = build_future_range_artifact(report, generated_at=GENERATED_AT)
    target = tmp_path / "research" / "market_scan_future_range" / future_range_artifact_filename(run_id, artifact)
    writer_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_done = threading.Event()
    writer_error: list[BaseException] = []

    original = cache.maintenance_repo._cleanup_specs

    def cleanup_specs(specs: object) -> dict[str, int]:
        writer_started.set()
        assert release_cleanup.wait(timeout=3)
        return original(specs)  # type: ignore[arg-type]

    monkeypatch.setattr(cache.maintenance_repo, "_cleanup_specs", cleanup_specs)

    def cleanup() -> None:
        cache.cleanup_runtime_rows()
        cleanup_done.set()

    def publish() -> None:
        assert writer_started.wait(timeout=3)
        try:
            write_future_range_artifact(target, artifact, database_path=database)
        except BaseException as exc:
            writer_error.append(exc)

    cleanup_thread = threading.Thread(target=cleanup)
    writer = threading.Thread(target=publish)
    cleanup_thread.start()
    writer.start()
    assert writer_started.wait(timeout=3)
    assert not cleanup_done.is_set()
    release_cleanup.set()
    cleanup_thread.join(timeout=3)
    writer.join(timeout=3)
    assert not cleanup_thread.is_alive() and not writer.is_alive()
    assert cleanup_done.is_set()
    assert len(writer_error) == 1
    assert isinstance(writer_error[0], FutureRangeArtifactError)
    assert not target.exists()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM market_scan_run WHERE id = ?",
                (run_id,),
            ).fetchone()
            is None
        )


def test_probability_batch_holds_one_lease_across_every_file(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    cache = SQLiteCache(
        database,
        settings=Settings(cache_path=database, max_market_scan_runs=1),
    )
    with sqlite3.connect(database) as connection:
        first = _insert_published_snapshot(connection, 0)
        second = _insert_published_snapshot(connection, 1)
    targets = (
        tmp_path / PROBABILITY_MANAGED_DIRECTORY / f"run-{first}.json",
        tmp_path / PROBABILITY_MANAGED_DIRECTORY / f"run-{second}.json",
    )
    first_file_published = threading.Event()
    release_batch = threading.Event()
    cleanup_done = threading.Event()

    def publish_batch() -> None:
        with verified_market_scan_artifact_batch_publication(
            database,
            targets,
            (first, second),
            managed_directory=PROBABILITY_MANAGED_DIRECTORY,
        ):
            first_file_published.set()
            assert release_batch.wait(timeout=3)

    def cleanup() -> None:
        assert first_file_published.wait(timeout=3)
        cache.cleanup_runtime_rows()
        cleanup_done.set()

    publisher = threading.Thread(target=publish_batch)
    cleaner = threading.Thread(target=cleanup)
    publisher.start()
    cleaner.start()
    assert first_file_published.wait(timeout=3)
    assert not cleanup_done.wait(timeout=0.1)
    release_batch.set()
    publisher.join(timeout=3)
    cleaner.join(timeout=3)
    assert not publisher.is_alive() and not cleaner.is_alive()
    assert cleanup_done.is_set()


def test_failed_batch_removes_only_new_files_and_preserves_exact_repeat(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    SQLiteCache(database, settings=Settings(cache_path=database))
    with sqlite3.connect(database) as connection:
        run_id = _insert_published_snapshot(connection, 0)
    directory = tmp_path / PROBABILITY_MANAGED_DIRECTORY
    existing = directory / "existing.json"
    created = directory / "created.json"
    directory.mkdir()
    existing.write_bytes(b"same")
    with pytest.raises(RuntimeError, match="injected"):
        with verified_market_scan_artifact_batch_publication(
            database,
            (existing, created),
            (run_id,),
            managed_directory=PROBABILITY_MANAGED_DIRECTORY,
        ):
            assert publish_market_scan_artifact(existing, b"same", max_bytes=16) is False
            assert publish_market_scan_artifact(created, b"new", max_bytes=16) is True
            raise RuntimeError("injected")
    assert existing.read_bytes() == b"same"
    assert not created.exists()


def test_nested_equal_batch_transfers_new_publication_to_outer_rollback(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    SQLiteCache(database, settings=Settings(cache_path=database))
    with sqlite3.connect(database) as connection:
        run_id = _insert_published_snapshot(connection, 0)
    target = tmp_path / PROBABILITY_MANAGED_DIRECTORY / "nested.json"
    with pytest.raises(RuntimeError, match="outer"):
        with verified_market_scan_artifact_batch_publication(database, (target,), (run_id,), managed_directory=PROBABILITY_MANAGED_DIRECTORY):
            with verified_market_scan_artifact_batch_publication(database, (target,), (run_id,), managed_directory=PROBABILITY_MANAGED_DIRECTORY):
                assert publish_market_scan_artifact(target, b"nested", max_bytes=16)
            raise RuntimeError("outer")
    assert not target.exists()


def test_managed_directory_alias_is_rejected(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    database = real / "runtime.sqlite3"
    SQLiteCache(database, settings=Settings(cache_path=database))
    with sqlite3.connect(database) as connection:
        run_id = _insert_published_snapshot(connection, 0)
    target = alias / PROBABILITY_MANAGED_DIRECTORY / "alias.json"
    with pytest.raises(MarketScanArtifactLeaseError, match="别名"):
        with verified_market_scan_artifact_batch_publication(database, (target,), (run_id,), managed_directory=PROBABILITY_MANAGED_DIRECTORY):
            pass


def test_artifact_lease_serializes_across_spawned_processes(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    SQLiteCache(database, settings=Settings(cache_path=database))
    context = multiprocessing.get_context("spawn")
    first_acquired = context.Event()
    release_first = context.Event()
    second_acquired = context.Event()
    first = context.Process(
        target=_hold_artifact_lease,
        args=(str(database), first_acquired, release_first),
    )
    second = context.Process(
        target=_observe_artifact_lease,
        args=(str(database), second_acquired),
    )
    first.start()
    assert first_acquired.wait(timeout=15)
    second.start()
    assert not second_acquired.wait(timeout=0.2)
    release_first.set()
    assert second_acquired.wait(timeout=15)
    first.join(timeout=15)
    second.join(timeout=15)
    assert first.exitcode == second.exitcode == 0


def test_lease_fails_closed_if_sidecar_is_replaced_while_held(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    SQLiteCache(database, settings=Settings(cache_path=database))
    sidecar = market_scan_artifact_lock_path(database)
    with pytest.raises(MarketScanArtifactLeaseError, match="持有期间被替换"):
        with market_scan_artifact_retention_lease(database):
            sidecar.unlink()
            sidecar.write_text("replacement", encoding="utf-8")


def test_empty_run_manifest_is_allowed_only_for_exact_managed_summary(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    SQLiteCache(database, settings=Settings(cache_path=database))
    summary = tmp_path / "research/market-scan-future-range-summary.json"
    with verified_market_scan_artifact_batch_publication(
        database,
        (summary,),
        (),
        managed_directory="research/market_scan_future_range",
        managed_files=("research/market-scan-future-range-summary.json",),
    ) as batch:
        assert batch is not None and batch.run_ids == ()
    ordinary = tmp_path / PROBABILITY_MANAGED_DIRECTORY / "empty.json"
    with pytest.raises(MarketScanArtifactLeaseError, match="正整数 run_id"):
        with verified_market_scan_artifact_batch_publication(
            database,
            (ordinary,),
            (),
            managed_directory=PROBABILITY_MANAGED_DIRECTORY,
        ):
            pass
