from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from app.services.fuyao_dumps import FuyaoDumpError, read_dump_status, sync_market_dumps
from app.services.fuyao_dumps_export import export_dump_research
from app.services.fuyao_dumps_storage import dump_lease, safe_root
from tests.test_fuyao_dumps_validation import action_row, daily_row


class SigningClient:
    def __init__(self):
        self.calls = []

    async def request(self, path, params=None):
        self.calls.append(path)
        return {"code": 0, "data": {"presigned_url": f"https://files.example.com/{len(self.calls)}?secret=hidden",
                                    "presigned_url_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat()}}


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    arrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    async def public(_host):
        return None
    monkeypatch.setattr("app.services.fuyao_dumps_download.require_public_dns", public)
    def run(rows, *, actions=None, mode="full", root=None, calendar=None, control=None):
        raw = []
        for index, content in enumerate((rows, [action_row()] if actions is None else actions)):
            path = tmp_path / f"sample-{index}.parquet"
            parquet.write_table(arrow.Table.from_pylist(content), path)
            raw.append(path.read_bytes())
        client = SigningClient()
        result = asyncio.run(sync_market_dumps(client, root or tmp_path / "history", mode,
            allowed_download_hosts=("files.example.com",), download_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw[int(request.url.path[1:]) - 1])),
            trade_dates=calendar or ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"), control=control))
        return result, client
    return run


def test_full_real_parquet_normalization_export_and_idempotent_increment(tmp_path, bundle):
    rows = [daily_row("2026-09-02"), daily_row(), daily_row()]
    first, client = bundle(rows)
    assert first.files[0].rows == 2 and first.duplicate_daily_rows == 1
    assert first.adjusted == "none" and not first.point_in_time_verified and not first.production_cache_updated
    assert len(client.calls) == 2 and "daily-k/download-url" in client.calls[0]
    assert read_dump_status(tmp_path / "history", verify_files=True) == first
    second, client = bundle([daily_row(), daily_row("2026-09-02")], mode="incremental")
    assert second.version == first.version and "daily-k-10d" in client.calls[0]
    output = export_dump_research(tmp_path / "history", tmp_path / "export")
    export = json.loads(output.read_bytes())
    assert not export["official_execution_admitted"] and not export["point_in_time_verified"]
    assert "none" in (output.parent / "daily-none.csv").read_text()
    assert len(list((tmp_path / "history" / "versions").iterdir())) == 1
    assert not list((tmp_path / "history").glob(".staging-*"))


def test_incremental_merges_revisions_and_new_dates(tmp_path, bundle):
    first, _ = bundle([daily_row(), daily_row("2026-09-02")])
    changed = daily_row("2026-09-02", close_price=10.5)
    second, _ = bundle([changed, daily_row("2026-09-03")], mode="incremental")
    assert second.previous_version == first.version and second.revised_daily_rows == 1
    assert second.files[0].rows == 3 and second.last_date == "2026-09-03"
    assert (tmp_path / "history" / "versions" / first.version).is_dir()


def test_signed_share_change_survives_archive_with_explicit_semantics(tmp_path, bundle):
    parquet = pytest.importorskip("pyarrow.parquet")
    manifest, _ = bundle([daily_row()], actions=[action_row(per_share_bonus=-0.87581, dividend_per_share=0)])
    canonical = tmp_path / "history" / "versions" / manifest.version / "actions.parquet"
    assert parquet.read_table(canonical).to_pylist()[0]["per_share_bonus"] == -0.87581
    assert any("1 条负的 per_share_bonus" in note for note in manifest.notes)
    assert any("不代表每类股东" in note for note in manifest.notes)
    assert not manifest.production_cache_updated and not manifest.point_in_time_verified
    assert manifest.adjusted == "none"
    assert read_dump_status(tmp_path / "history", verify_files=True) == manifest


def test_same_day_actions_preserve_distinct_records_and_deduplicate_across_batches(tmp_path, bundle, monkeypatch):
    parquet = pytest.importorskip("pyarrow.parquet")
    from app.artifacts.io import canonical_json_bytes
    from app.services.fuyao_dumps_validation import normalize_row

    monkeypatch.setattr("app.services.fuyao_dumps_storage.BATCH_SIZE", 1)
    distinct = [
        action_row(thscode="000812.SZ", ticker="000812", ex_date_ms=906393600000,
                   dividend_per_share=0.2, per_share_bonus=0),
        action_row(thscode="000812.SZ", ticker="000812", ex_date_ms=906393600000,
                   dividend_per_share=0, per_share_bonus=0.1),
        action_row(thscode="603883.SH", ticker="603883", ex_date_ms=1719417600000,
                   dividend_per_share=0.16, per_share_bonus=0),
        action_row(thscode="603883.SH", ticker="603883", ex_date_ms=1719417600000,
                   dividend_per_share=0.5, per_share_bonus=0.3),
    ]
    actions = [*distinct, distinct[0], distinct[-1]]
    first, _ = bundle([daily_row()], actions=actions)
    canonical = tmp_path / "history" / "versions" / first.version / "actions.parquet"
    records = parquet.read_table(canonical).to_pylist()
    expected = [normalize_row(row, "actions") for row in distinct]
    expected.sort(key=lambda row: (row["thscode"], row["ex_date_ms"], canonical_json_bytes(row)))
    assert records == expected
    assert first.files[1].rows == 4 and first.revised_daily_rows == 0
    assert any("仅去除 2 条" in note for note in first.notes)
    assert any("存在 2 组" in note and "披露版本" in note for note in first.notes)
    assert any("不可直接求和、选择最后一条或据此自动复权" in note for note in first.notes)
    reordered, _ = bundle([daily_row()], actions=list(reversed(actions)), root=tmp_path / "reordered")
    assert reordered.files == first.files
    second, _ = bundle([daily_row()], actions=list(reversed(distinct)), mode="incremental")
    assert second.version == first.version
    assert read_dump_status(tmp_path / "history", verify_files=True) == first
    exported = export_dump_research(tmp_path / "history", tmp_path / "export-actions")
    with (exported.parent / "corporate-actions.csv").open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 4
    assert [float(row["dividend_per_share"]) for row in rows] == [row["dividend_per_share"] for row in expected]


def test_invalid_action_is_rejected_before_daily_ingestion(tmp_path, bundle, monkeypatch):
    from app.services.fuyao_dumps_storage import ingest_parquet

    calls = []
    def traced_ingest(db, path, kind, control=None):
        calls.append(kind)
        return ingest_parquet(db, path, kind, control)
    monkeypatch.setattr("app.services.fuyao_dumps.ingest_parquet", traced_ingest)
    with pytest.raises(FuyaoDumpError):
        bundle([daily_row()], actions=[action_row(dividend_per_share=-1)])
    assert calls == ["actions"]
    assert read_dump_status(tmp_path / "history") is None


def test_conflicting_duplicate_after_batch_boundary_cannot_publish(tmp_path, bundle, monkeypatch):
    monkeypatch.setattr("app.services.fuyao_dumps_storage.BATCH_SIZE", 1)
    with pytest.raises(FuyaoDumpError, match="冲突"):
        bundle([daily_row(), daily_row(close_price=10.5)])
    assert read_dump_status(tmp_path / "history") is None
    assert not list((tmp_path / "history").glob(".staging-*"))


def test_gap_or_invalid_actions_leave_previous_version_intact(tmp_path, bundle):
    previous, _ = bundle([daily_row()])
    for kwargs in [{"rows": [daily_row("2026-09-03")]},
                   {"rows": [daily_row("2026-09-02")], "actions": [action_row(dividend_per_share=-1)]}]:
        with pytest.raises(FuyaoDumpError):
            bundle(mode="incremental", **kwargs)
        assert read_dump_status(tmp_path / "history", verify_files=True) == previous
    assert len(list((tmp_path / "history" / "versions").iterdir())) == 1


def test_incremental_without_seed_and_missing_dependency_do_not_spend_requests(tmp_path, monkeypatch):
    client = SigningClient()
    with pytest.raises(FuyaoDumpError, match="全量"):
        asyncio.run(sync_market_dumps(client, tmp_path / "history", "incremental", allowed_download_hosts=("files.example.com",)))
    assert not client.calls
    def missing():
        raise FuyaoDumpError("missing pyarrow")
    monkeypatch.setattr("app.services.fuyao_dumps.parquet_modules", missing)
    with pytest.raises(FuyaoDumpError, match="pyarrow"):
        asyncio.run(sync_market_dumps(client, tmp_path / "other", "full", allowed_download_hosts=("files.example.com",)))
    assert not client.calls


def test_corrupted_archive_or_pointer_and_symlinks_are_rejected(tmp_path, bundle):
    manifest, _ = bundle([daily_row()])
    root = tmp_path / "history"
    path = root / "versions" / manifest.version / "daily.parquet"
    path.write_bytes(b"tampered")
    with pytest.raises(FuyaoDumpError):
        read_dump_status(root, verify_files=True)
    with pytest.raises(FuyaoDumpError):
        export_dump_research(root, tmp_path / "export")
    (root / "current.json").write_text('{"version":"../../private"}')
    with pytest.raises(FuyaoDumpError):
        read_dump_status(root)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(FuyaoDumpError, match="符号链接"):
        safe_root(alias)


@pytest.mark.parametrize("pointer", [None, [], {}, {"version": 3}, {"version": "0" * 63},
                                    {"version": "G" * 64}, {"version": "a" * 64, "extra": "ignored"}])
def test_current_pointer_requires_exact_shape_and_digest_name(tmp_path, pointer):
    root = safe_root(tmp_path / "history")
    (root / "current.json").write_text(json.dumps(pointer))
    with pytest.raises(FuyaoDumpError, match="研究数据版本校验失败"):
        read_dump_status(root)


@pytest.mark.parametrize("change", ["identity", "content", "files"])
def test_manifest_requires_bound_identity_digest_and_file_sequence(tmp_path, bundle, change):
    from app.services.fuyao_dumps_storage import manifest_version

    manifest, _ = bundle([daily_row()])
    root = tmp_path / "history"
    directory = root / "versions" / manifest.version
    payload = manifest.model_dump(mode="json")
    if change == "identity":
        payload["version"] = "f" * 64
    elif change == "content":
        payload["notes"].append("tampered content")
    else:
        payload["files"].reverse()
        payload["version"] = manifest_version(payload)
        target = directory.with_name(payload["version"])
        directory.rename(target)
        directory = target
        (root / "current.json").write_text(json.dumps({"version": payload["version"]}))
    (directory / "manifest.json").write_text(json.dumps(payload))
    with pytest.raises(FuyaoDumpError, match="研究数据版本校验失败"):
        read_dump_status(root)


def test_sync_process_lock_rejects_second_writer(tmp_path):
    root = safe_root(tmp_path / "history")
    with dump_lease(root), pytest.raises(FuyaoDumpError, match="正在运行"), dump_lease(root):
        pass


def test_failed_pointer_publication_preserves_complete_previous_version(tmp_path, bundle, monkeypatch):
    first, _ = bundle([daily_row()])
    def fail(*_args):
        raise OSError("simulated publication failure")
    monkeypatch.setattr("app.services.fuyao_dumps_storage._atomic_pointer", fail)
    with pytest.raises(OSError):
        bundle([daily_row("2026-09-02")], mode="incremental")
    assert read_dump_status(tmp_path / "history", verify_files=True) == first
    assert not list((tmp_path / "history").glob(".staging-*"))


@pytest.mark.parametrize("cancellations", [1, 3])
def test_cancelled_worker_finishes_before_staging_cleanup(cancellations):
    from app.services.fuyao_dumps import _finish_worker_on_cancel
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        finished = []
        async def worker():
            started.set()
            await release.wait()
            finished.append(True)
        operation = asyncio.create_task(_finish_worker_on_cancel(worker()))
        await started.wait()
        for _ in range(cancellations):
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert finished == [True]
    asyncio.run(scenario())


def test_empty_unknown_mode_or_unbounded_download_refused_before_network(tmp_path):
    client = SigningClient()
    for values in [{"mode": "unknown", "allowed_download_hosts": ("files.example.com",)},
                   {"mode": "full", "allowed_download_hosts": ()},
                   {"mode": "full", "allowed_download_hosts": ("files.example.com",), "max_download_bytes": 0}]:
        with pytest.raises(FuyaoDumpError):
            asyncio.run(sync_market_dumps(client, tmp_path / "history", **values))
    assert not client.calls


def test_export_does_not_replace_existing_directory(tmp_path, bundle):
    bundle([daily_row()])
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("user content")
    with pytest.raises(FuyaoDumpError):
        export_dump_research(tmp_path / "history", target)
    assert (target / "keep.txt").read_text() == "user content"
