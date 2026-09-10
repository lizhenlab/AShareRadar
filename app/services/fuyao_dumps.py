"""Full/incremental isolated history sync with validated atomic publication."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime
from pathlib import Path
import tempfile
import sqlite3
from typing import Any, Literal, Protocol, TypeVar

import httpx

from app.models.fuyao_dumps import FuyaoDumpManifest
from app.services.fuyao_dumps_download import download_dump, signed_url
from app.services.fuyao_dumps_storage import (
    create_staging_database, dump_lease, ingest_parquet, manifest_version, publish_version, read_dump_status, safe_root, seed_verified_daily, write_parquet,
)
from app.services.fuyao_dumps_validation import FuyaoDumpError, dump_date, parquet_modules, validate_trading_dates
from app.services.lifecycle_cleanup import await_cleanup
from app.services.fuyao_sync_control import FuyaoSyncControl
from app.utils.clock import market_now


class DumpClient(Protocol):
    async def request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


T = TypeVar("T")


async def _finish_worker_on_cancel(work: Coroutine[Any, Any, T], control: FuyaoSyncControl | None = None) -> T:
    # The worker owns staging files; even repeated cancellation must drain it.
    worker = asyncio.create_task(work)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if control is not None:
            control.cancel()
        await await_cleanup(asyncio.create_task(_drain_worker(worker)))
        raise


async def _drain_worker(worker: asyncio.Task[Any]) -> None:
    await asyncio.gather(worker, return_exceptions=True)


async def sync_market_dumps(client: DumpClient, root: Path, mode: Literal["full", "incremental"], *,
                            allowed_download_hosts: tuple[str, ...], max_download_bytes: int = 2_000_000_000,
                            download_transport: httpx.AsyncBaseTransport | None = None,
                            trade_dates: tuple[str, ...] | None = None,
                            control: FuyaoSyncControl | None = None) -> FuyaoDumpManifest:
    """Two signing requests; signed downloads never receive the account API key.

    Only unadjusted research files are published. No production cache or ranking
    weights change. This operation must run as an explicit background task.
    """
    control = control or FuyaoSyncControl()
    control.checkpoint("checking_previous")
    parquet_modules()
    if mode not in ("full", "incremental") or not allowed_download_hosts:
        raise FuyaoDumpError("同步方式无效或未配置官方对象存储下载域名")
    if type(max_download_bytes) is not int or not 1 <= max_download_bytes <= 10_000_000_000:
        raise FuyaoDumpError("下载容量上限必须为 1 到 100 亿字节之间的整数")
    directory = safe_root(root)
    with dump_lease(directory), tempfile.TemporaryDirectory(prefix=".staging-", dir=directory) as temporary:
        previous = await _finish_worker_on_cancel(asyncio.to_thread(read_dump_status, directory, verify_files=True, control=control), control)
        if mode == "incremental" and previous is None:
            raise FuyaoDumpError("增量同步前必须先完成全量同步")
        stage = Path(temporary) / "version"
        stage.mkdir(mode=0o700)
        sources = await _download_sources(client, stage, mode, allowed_download_hosts, max_download_bytes, download_transport, control)
        work = asyncio.to_thread(_build_version, directory, stage, mode, previous, sources, trade_dates, market_now(), control)
        manifest = await _finish_worker_on_cancel(work, control)
        control.checkpoint("publishing")
        if previous is not None and manifest.version == previous.version:
            return previous
        publish_version(directory, stage, manifest, control)
        return manifest


async def _download_sources(client: DumpClient, stage: Path, mode: str, hosts: tuple[str, ...], max_bytes: int,
                            transport: httpx.AsyncBaseTransport | None, control: FuyaoSyncControl) -> dict[str, str]:
    endpoints = (("source-daily.parquet", "daily-k" if mode == "full" else "daily-k-10d"),
                 ("source-actions.parquet", "adjustment-factors"))
    sources = {}
    for name, endpoint in endpoints:
        kind = "daily" if name == "source-daily.parquet" else "actions"
        control.checkpoint(f"signing_{kind}")
        response = await client.request(f"/api/dump/market-dumps/{endpoint}/download-url", {})
        control.checkpoint(f"downloading_{kind}", current=0, unit="bytes")
        sources[name] = await download_dump(signed_url(response), stage / name, allowed_hosts=hosts, max_bytes=max_bytes, transport=transport, control=control)
    return sources


def _build_version(root: Path, stage: Path, mode: Literal["full", "incremental"], previous: FuyaoDumpManifest | None,
                   sources: dict[str, str], trade_dates: tuple[str, ...] | None, observed_at: datetime,
                   control: FuyaoSyncControl | None = None) -> FuyaoDumpManifest:
    control = control or FuyaoSyncControl()
    control.checkpoint()
    db = create_staging_database(stage.parent / "merge.sqlite3")
    db.set_progress_handler(lambda: int(control.cancelled), 8192)
    try:
        _, duplicate_actions = ingest_parquet(db, stage / "source-actions.parquet", "actions", control)
        if mode == "incremental" and previous is not None:
            seed_verified_daily(db, root / "versions" / previous.version / "daily.parquet", control)
        revisions, duplicates = ingest_parquet(db, stage / "source-daily.parquet", "daily", control)
        control.checkpoint("checking_calendar")
        days = [dump_date(row[0]).isoformat() for row in db.execute("SELECT DISTINCT day FROM daily ORDER BY day")]
        calendar = validate_trading_dates(days, trade_dates, control)
        symbols = db.execute("SELECT COUNT(DISTINCT symbol) FROM daily").fetchone()[0]
        files = [write_parquet(db, stage / "daily.parquet", "daily", control), write_parquet(db, stage / "actions.parquet", "actions", control)]
        if previous is not None and files == previous.files:
            return previous
        payload = dict(mode=mode, observed_at=observed_at.isoformat(), previous_version=previous.version if previous else None,
                       first_date=days[0], last_date=days[-1], symbols=symbols, trading_dates=days, files=files,
                       source_sha256=sources, revised_daily_rows=revisions, duplicate_daily_rows=duplicates, calendar_source=calendar,
                       notes=["未复权价格与企业行动分别保留；未生成前复权价格。", "全市场日期连续性已校验；个股缺席可能为停牌或覆盖不足。",
                              "不包含历史成分、披露版本和逐日交易状态证据；不得直接用于激活正式回测或评分。",
                              *_action_event_notes(db, duplicate_actions), *_share_change_notes(db)])
        model = FuyaoDumpManifest(version="0" * 64, **payload)
        control.checkpoint()
        return model.model_copy(update={"version": manifest_version(model.model_dump(mode="json"))})
    except sqlite3.OperationalError:
        control.checkpoint()
        raise
    finally:
        db.close()


def _action_event_notes(db: sqlite3.Connection, duplicates: int) -> list[str]:
    ambiguous = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM actions GROUP BY symbol, day HAVING COUNT(*) > 1)").fetchone()[0]
    return [f"企业行动仅去除 {duplicates} 条所有规范字段完全相同的重复行；保留同证券、同除权日的不同记录。",
            f"企业行动存在 {ambiguous} 组同证券、同除权日的多条不同记录，需核对事件身份及披露版本。",
            "同日多条企业行动可能为独立事件或修订记录，不可直接求和、选择最后一条或据此自动复权。"]


def _share_change_notes(db: sqlite3.Connection) -> list[str]:
    negative = db.execute("SELECT COUNT(*) FROM actions WHERE json_extract(payload, '$.per_share_bonus') < 0").fetchone()[0]
    if not negative:
        return []
    return [f"企业行动保留 {negative} 条负的 per_share_bonus 原值；该字段可承载缩股或股本减少，不解释为送股或现金分红。",
            "股本变动比例可能采用企业汇总及舍入口径，不代表每类股东按同一比例变动；未据此生成复权序列或调整持仓。"]


__all__ = ["FuyaoDumpError", "FuyaoDumpManifest", "read_dump_status", "sync_market_dumps"]
