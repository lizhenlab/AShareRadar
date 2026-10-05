"""Refresh the listing universe without starting a scan or changing old snapshots."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from app.models.market import StockInfo
from app.services.datahub_runtime import run_cache_io
from app.services.lifecycle_cleanup import await_cleanup
from app.utils.stock_pool import diagnose_stock_pool_metadata, normalize_stock_pool_rows

if TYPE_CHECKING:
    from app.config import Settings
    from app.services.cache import SQLiteCache
    from app.services.datahub import DataHub


_StageResult = TypeVar("_StageResult")


@dataclass(frozen=True)
class StockPoolMaintenanceResult:
    status: Literal["success", "degraded"]
    message: str


@dataclass(frozen=True)
class _PoolRequirements:
    max_age_seconds: int
    minimum_total: int
    market_minimums: tuple[tuple[str, int], ...]


async def refresh_stock_pool_metadata(datahub: DataHub) -> StockPoolMaintenanceResult:
    """Skip a complete fresh cache; otherwise use the normal full-pool safeguards."""
    requirements = _requirements(datahub.settings)
    cached = await _owned_stage(run_cache_io(_fresh_complete_pool, datahub.cache, requirements))
    if cached:
        return _pool_summary(cached, "股票池缓存仍在有效期内，已跳过联网刷新")
    resolution = await _owned_stage(datahub.stock_pool_resolution(
        limit=None,
        refresh=True,
        required_markets=frozenset(dict(requirements.market_minimums)),
        minimum_market_counts=dict(requirements.market_minimums),
    ))
    rows = resolution.list_rows()
    if not resolution.resolved or not rows:
        raise RuntimeError(f"股票池刷新未取得可用结果：{resolution.reason}")
    if resolution.reason != "provider-full-pool" or not _complete_pool(rows, requirements):
        return StockPoolMaintenanceResult(
            "degraded", f"股票池刷新未取得三市场完整来源，保留已有缓存：{resolution.reason}",
        )
    cached = await _owned_stage(run_cache_io(_fresh_complete_pool, datahub.cache, requirements))
    if not cached:
        return StockPoolMaintenanceResult("degraded", "来源已返回完整股票池，但本地缓存仍未形成有效完整快照")
    return _pool_summary(cached, "已刷新本地股票池")


async def _owned_stage(work: Coroutine[Any, Any, _StageResult]) -> _StageResult:
    task = asyncio.create_task(work, name="stock-pool-metadata-maintenance-stage")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Finish the current cache/provider operation while its scheduler guard
        # is still held. Cancellation must not enter the next refresh phase.
        try:
            await await_cleanup(task)
        except Exception:
            pass
        raise


def _requirements(settings: Settings) -> _PoolRequirements:
    return _PoolRequirements(
        max_age_seconds=settings.stock_pool_cache_seconds,
        minimum_total=max(settings.stock_pool_authoritative_min_count, settings.market_scan_min_universe_count),
        market_minimums=(
            ("SH", settings.market_scan_min_sh_count),
            ("SZ", settings.market_scan_min_sz_count),
            ("BJ", settings.market_scan_min_bj_count),
        ),
    )


def _fresh_complete_pool(cache: SQLiteCache, requirements: _PoolRequirements) -> list[StockInfo]:
    rows = cache.get_stock_pool(requirements.max_age_seconds, limit=None, keyword=None)
    if len(rows) != cache.stock_pool_count() or not _complete_pool(rows, requirements):
        return []
    return rows


def _complete_pool(rows: list[StockInfo], requirements: _PoolRequirements) -> bool:
    normalized = normalize_stock_pool_rows(rows)
    counts = Counter(item.market for item in normalized)
    return (
        len(normalized) == len(rows)
        and len(normalized) >= requirements.minimum_total
        and all(counts[market] >= minimum for market, minimum in requirements.market_minimums)
    )


def _pool_summary(rows: list[StockInfo], prefix: str) -> StockPoolMaintenanceResult:
    counts = Counter(item.market for item in rows)
    diagnostic = diagnose_stock_pool_metadata(rows)
    message = f"{prefix}：{len(rows)} 只（SH {counts['SH']} / SZ {counts['SZ']} / BJ {counts['BJ']}）"
    if diagnostic.degraded:
        message += f"；{diagnostic.summary()}"
    return StockPoolMaintenanceResult("degraded" if diagnostic.degraded else "success", message)


__all__ = ["StockPoolMaintenanceResult", "refresh_stock_pool_metadata"]
