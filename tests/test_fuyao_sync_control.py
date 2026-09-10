from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

import httpx
import pytest

from app.services.fuyao_dumps import _build_version, _finish_worker_on_cancel, read_dump_status, sync_market_dumps
from app.services.fuyao_dumps_download import _stream_file, download_dump
from app.services.fuyao_dumps_storage import dump_lease, file_digest, safe_root
from app.services.fuyao_sync_control import FuyaoSyncCancelled, FuyaoSyncControl
from tests import test_fuyao_dumps_sync as sync_fixtures
from tests.test_fuyao_dumps_sync import SigningClient
from tests.test_fuyao_dumps_validation import daily_row


bundle = sync_fixtures.bundle


class RecordingControl(FuyaoSyncControl):
    def __init__(self, cancel_stage=None, cancel_after=None):
        super().__init__()
        self.cancel_stage = cancel_stage
        self.cancel_after = cancel_after
        self.observed = []

    def checkpoint(self, stage=None, current=None, total=None, unit=None):
        selected = stage or self.snapshot()["stage"]
        if selected == self.cancel_stage and (self.cancel_after is None or current is not None and current >= self.cancel_after):
            self.cancel()
        super().checkpoint(stage, current, total, unit)
        self.observed.append(self.snapshot())


def test_control_snapshots_are_copies_and_stage_changes_reset_progress():
    control = FuyaoSyncControl()
    assert control.snapshot() == {"stage": "pending", "current": 0, "total": None, "unit": None}
    control.checkpoint("downloading_daily", 4096, 8192, "bytes")
    control.checkpoint(current=8192)
    snapshot = control.snapshot()
    snapshot["current"] = 2
    assert control.snapshot()["current"] == 8192
    control.checkpoint("validating_daily")
    assert control.snapshot() == {"stage": "validating_daily", "current": 0, "total": None, "unit": None}
    control.cancel()
    control.cancel()
    assert control.cancelled
    with pytest.raises(FuyaoSyncCancelled):
        control.checkpoint("publishing")
    assert control.snapshot()["stage"] == "validating_daily"


def test_thread_snapshot_never_mixes_stage_and_counter():
    control = FuyaoSyncControl()
    started, release = threading.Event(), threading.Event()
    def write():
        for number in range(2000):
            control.checkpoint(str(number), number, number, "rows")
            started.set()
        release.wait(5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(write)
        assert started.wait(5)
        try:
            for _ in range(2000):
                item = control.snapshot()
                assert int(item["stage"]) == item["current"] == item["total"]
        finally:
            release.set()
        worker.result(timeout=5)


def test_cancel_before_sync_does_not_create_directory_or_request(tmp_path):
    control = FuyaoSyncControl()
    control.cancel()
    client = SigningClient()
    with pytest.raises(FuyaoSyncCancelled):
        asyncio.run(sync_market_dumps(client, tmp_path / "never", "full", allowed_download_hosts=("files.example.com",), control=control))
    assert not client.calls and not (tmp_path / "never").exists()


@pytest.mark.parametrize("stage", [
    "checking_previous", "signing_daily", "downloading_daily", "signing_actions", "downloading_actions",
    "validating_actions", "seeding_daily", "validating_daily", "checking_calendar", "writing_daily",
    "hashing_daily", "writing_actions", "hashing_actions", "publishing",
])
def test_cancel_each_stage_keeps_previous_pointer_and_releases_staging(tmp_path, bundle, stage):
    previous, _ = bundle([daily_row()])
    root = tmp_path / "history"
    control = RecordingControl(cancel_stage=stage)
    with pytest.raises(FuyaoSyncCancelled):
        bundle([daily_row("2026-09-02")], mode="incremental", control=control)
    assert read_dump_status(root, verify_files=True) == previous
    assert not list(root.glob(".staging-*"))
    with dump_lease(root):
        pass


def test_progress_uses_actual_bytes_and_rows_with_stage_boundaries(bundle, monkeypatch):
    monkeypatch.setattr("app.services.fuyao_dumps_storage.BATCH_SIZE", 1)
    control = RecordingControl()
    manifest, _ = bundle([daily_row(), daily_row("2026-09-02")], control=control)
    snapshots = control.observed
    for stage, expected in [("validating_actions", 1), ("validating_daily", 2), ("writing_daily", 2), ("writing_actions", 1)]:
        values = [item for item in snapshots if item["stage"] == stage and item["total"] is not None]
        assert values[-1] == {"stage": stage, "current": expected, "total": expected, "unit": "rows"}
    for stage in ("downloading_daily", "downloading_actions", "hashing_daily", "hashing_actions"):
        values = [item for item in snapshots if item["stage"] == stage]
        assert values[-1]["current"] == values[-1]["total"] > 0
        assert values[-1]["unit"] == "bytes"
    assert control.snapshot() == {"stage": "publishing", "current": 0, "total": None, "unit": None}
    assert manifest.files[0].rows == 2


@pytest.mark.parametrize("stage", ["validating_daily", "seeding_daily", "writing_daily"])
def test_stop_mid_row_batches_does_not_finish_or_publish(tmp_path, bundle, monkeypatch, stage):
    monkeypatch.setattr("app.services.fuyao_dumps_storage.BATCH_SIZE", 1)
    previous, _ = bundle([daily_row(), daily_row("2026-09-02")])
    control = RecordingControl(cancel_stage=stage, cancel_after=1)
    with pytest.raises(FuyaoSyncCancelled):
        bundle([daily_row("2026-09-03"), daily_row("2026-09-04")], mode="incremental", control=control)
    assert read_dump_status(tmp_path / "history", verify_files=True) == previous


def test_download_stop_checks_each_chunk_and_does_not_consume_whole_stream(tmp_path):
    control = RecordingControl(cancel_stage="downloading_daily", cancel_after=1024 * 1024)
    control.checkpoint("downloading_daily")
    consumed = []
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(4):
                consumed.append(index)
                yield b"x" * (1024 * 1024)
    response = httpx.Response(200, stream=Chunks())
    with pytest.raises(FuyaoSyncCancelled):
        asyncio.run(_stream_file(response, tmp_path / "partial", 8 * 1024 * 1024, control))
    assert consumed == [0]
    assert (tmp_path / "partial").stat().st_size == 1024 * 1024
    assert control.snapshot()["total"] is None


def test_digest_checks_cancellation_during_chunks(tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b"x" * (3 * 1024 * 1024))
    control = RecordingControl(cancel_stage="checking_previous", cancel_after=1024 * 1024)
    control.checkpoint("checking_previous")
    with pytest.raises(FuyaoSyncCancelled):
        file_digest(path, control)


def test_repeated_download_cancel_drains_owned_response_close(tmp_path, monkeypatch):
    async def public(_host):
        pass
    monkeypatch.setattr("app.services.fuyao_dumps_download.require_public_dns", public)
    async def scenario():
        reading, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closed = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                reading.set()
                await asyncio.Event().wait()
                yield b"unreachable"
            async def aclose(self):
                closing.set()
                await release.wait()
                closed.append(True)
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=Stream()))
        operation = asyncio.create_task(download_dump("https://files.example.com/object", tmp_path / "partial",
            allowed_hosts=("files.example.com",), max_bytes=1024, transport=transport))
        await reading.wait()
        operation.cancel()
        await closing.wait()
        for _ in range(2):
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert closed == [True]
    asyncio.run(scenario())


def test_sqlite_merge_cancellation_is_not_reported_as_bad_data(tmp_path, monkeypatch):
    control = FuyaoSyncControl()
    started = threading.Event()
    def slow_ingest(db, _path, _kind, _control):
        started.set()
        db.execute("WITH RECURSIVE numbers(n) AS (VALUES(0) UNION ALL SELECT n + 1 FROM numbers WHERE n < 10000000) SELECT sum(n) FROM numbers").fetchone()
        raise AssertionError("cancelled SQLite scan must not finish")
    monkeypatch.setattr("app.services.fuyao_dumps.ingest_parquet", slow_ingest)
    stage = tmp_path / "stage"
    stage.mkdir()
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(_build_version, tmp_path, stage, "full", None, {}, (), datetime.now(timezone.utc), control)
        assert started.wait(5)
        control.cancel()
        with pytest.raises(FuyaoSyncCancelled):
            worker.result(timeout=5)


@pytest.mark.parametrize("cancellations", [1, 3])
def test_async_cancel_signals_worker_and_drains_cleanup_before_releasing_lease(tmp_path, cancellations):
    control = FuyaoSyncControl()
    started, stopping, release = threading.Event(), threading.Event(), threading.Event()
    root = safe_root(tmp_path / "history")
    stage = root / ".staging-test"
    stage.mkdir()
    def worker():
        started.set()
        try:
            while not control.cancelled:
                threading.Event().wait(0.001)
            control.checkpoint()
        finally:
            stopping.set()
            assert release.wait(5)
            assert stage.is_dir()
    async def operation():
        with dump_lease(root):
            try:
                await _finish_worker_on_cancel(asyncio.to_thread(worker), control)
            finally:
                stage.rmdir()
    async def scenario():
        task = asyncio.create_task(operation())
        assert await asyncio.to_thread(started.wait, 5)
        try:
            for _ in range(cancellations):
                task.cancel()
                await asyncio.sleep(0)
            assert await asyncio.to_thread(stopping.wait, 5)
            assert not task.done() and stage.is_dir()
            with pytest.raises(ValueError, match="正在运行"), dump_lease(root):
                pass
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert not stage.exists()
    with dump_lease(root):
        pass
