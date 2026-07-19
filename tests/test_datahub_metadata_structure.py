from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.models.schemas import StockInfo
from app.services import datahub_metadata as facade
from app.services.datahub_metadata_coordinator import MetadataCoordinator, PlateRankResult
from app.services.datahub_metadata_mapping import _profile_with_local_industry
from app.services.datahub_metadata_stock_pool import (
    StockPoolRequest,
    StockPoolResolution,
    StockPoolResolver,
    _stock_pool_markets,
)
from app.services.datahub_runtime import ProviderAttempt, TimedProviderCall
from tests.factories import make_stock_info


SERVICES_DIR = Path(__file__).parents[1] / "app" / "services"
METADATA_MODULES = tuple(sorted(SERVICES_DIR.glob("datahub_metadata*.py")))


def test_datahub_metadata_facade_preserves_public_contract() -> None:
    assert facade.__all__ == [
        "MetadataCoordinator",
        "PlateRankResult",
        "STOCK_POOL_BASELINE_COMPARISON_MIN_COUNT",
        "STOCK_POOL_FALLBACK_SECONDS",
        "STOCK_POOL_MARKETS",
        "STOCK_POOL_MIN_BASELINE_RETAIN_RATIO",
        "StockConceptResult",
        "StockPoolRequest",
        "StockPoolResolution",
        "StockPoolResolver",
    ]
    assert facade.MetadataCoordinator is MetadataCoordinator
    assert facade.PlateRankResult is PlateRankResult
    assert facade.StockPoolRequest is StockPoolRequest
    assert facade.StockPoolResolution is StockPoolResolution
    assert facade.StockPoolResolver is StockPoolResolver
    assert facade._profile_with_local_industry is _profile_with_local_industry
    assert facade._stock_pool_markets is _stock_pool_markets
    assert MetadataCoordinator.plate_rank_result.__module__.endswith("datahub_metadata_coordinator")
    assert StockPoolResolver.stock_pool_resolution.__module__.endswith("datahub_metadata_stock_pool")


def test_datahub_metadata_modules_have_an_acyclic_dependency_graph() -> None:
    module_names = {path.stem for path in METADATA_MODULES}
    dependencies: dict[str, set[str]] = {name: set() for name in module_names}

    for path in METADATA_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            assert not node.module.startswith("app.repositories")
            imported = node.module.rsplit(".", 1)[-1]
            if node.module.startswith("app.services.") and imported in module_names:
                dependencies[path.stem].add(imported)

    visited: set[str] = set()
    active: set[str] = set()

    def visit(module: str) -> None:
        assert module not in active, f"datahub metadata dependency cycle at {module}"
        if module in visited:
            return
        active.add(module)
        for dependency in dependencies[module]:
            visit(dependency)
        active.remove(module)
        visited.add(module)

    for module in dependencies:
        visit(module)


def test_datahub_metadata_modules_remain_reviewable() -> None:
    oversized = {
        path.name: len(path.read_text(encoding="utf-8").splitlines()) for path in METADATA_MODULES if len(path.read_text(encoding="utf-8").splitlines()) > 500
    }
    assert oversized == {}


def test_stock_pool_coverage_and_persistence_share_normalized_candidate_rows() -> None:
    raw_rows = [
        make_stock_info(code="600519", market="SH").model_copy(
            update={
                "symbol": " sh600519 ",
                "code": " 600519 ",
                "market": " sh ",
                "name": " 贵州茅台 ",
                "source": " provider ",
            }
        ),
        make_stock_info(code="600519", market="SH").model_copy(update={"name": "重复行"}),
        make_stock_info(code="000001", market="SZ"),
    ]

    class Provider:
        async def stock_pool(self) -> list[StockInfo]:
            return raw_rows

    class Cache:
        def get_stock_pool(self, *args: Any, **kwargs: Any) -> list[StockInfo]:
            return []

    class Runtime:
        def attempts(
            self,
            priority_rows: list[tuple[int, str]],
            providers: dict[str, object],
            kind: str,
            errors: list[str],
        ) -> Iterator[ProviderAttempt]:
            del priority_rows, kind, errors
            yield ProviderAttempt(index=1, name="provider", provider=providers["provider"])

        async def timed_provider_call(
            self,
            name: str,
            kind: str,
            call: Callable[[], Awaitable[list[StockInfo]]],
            **kwargs: Any,
        ) -> TimedProviderCall[list[StockInfo]]:
            del name, kind, kwargs
            return TimedProviderCall(value=await call(), latency_ms=1.0)

        async def record_attempt_success_async(
            self,
            attempt: ProviderAttempt,
            kind: str,
            latency_ms: float,
        ) -> None:
            del attempt, kind, latency_ms

    observed: dict[str, list[StockInfo]] = {}

    class ObservedResolver(StockPoolResolver):
        def _select_rows(
            self,
            rows: list[StockInfo],
            request: StockPoolRequest,
        ) -> StockPoolResolution:
            observed["coverage"] = rows
            return super()._select_rows(rows, request)

        async def _save_provider_stock_pool(self, rows: list[StockInfo]) -> None:
            observed["persist"] = rows

    resolver = ObservedResolver(
        settings=SimpleNamespace(
            stock_pool_authoritative_min_count=1,
            stock_pool_provider_timeout_seconds=1,
            market_scan_min_sh_count=1,
            market_scan_min_sz_count=1,
            market_scan_min_bj_count=1,
        ),
        cache=Cache(),
        providers={"provider": Provider()},
        runtime=Runtime(),
        priority=lambda kind: [(1, "provider")],
    )
    request = StockPoolRequest(keyword=None, limit=None, refresh=True)

    resolution = asyncio.run(resolver._provider_result(request, []))

    assert resolution.resolved is True
    assert observed["coverage"] is observed["persist"]
    assert [row.symbol for row in observed["coverage"]] == ["600519.SH", "000001.SZ"]
    assert observed["coverage"][0].name == "贵州茅台"
    assert observed["coverage"][0].source == "provider"
