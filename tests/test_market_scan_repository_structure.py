from __future__ import annotations

import ast
from pathlib import Path

from app.models.market_scan import (
    MarketScanResultWrite as ModelResultWrite,
    MarketScanRetryPlan as ModelRetryPlan,
    MarketScanSeed as ModelSeed,
)
from app.repositories import market_scan
from app.repositories.market_scan import (
    MarketScanRepository,
    MarketScanResultWrite,
    MarketScanRetryPlan,
    MarketScanSeed,
)


REPOSITORY_DIR = Path(__file__).parents[1] / "app" / "repositories"
SCAN_MODULES = tuple(sorted(REPOSITORY_DIR.glob("market_scan*.py")))


def test_market_scan_facade_preserves_public_contract_and_delegates_by_mro() -> None:
    assert market_scan.__all__ == [
        "ACTIVE_SCAN_STATUSES",
        "MarketScanRepository",
        "MarketScanResultWrite",
        "MarketScanRetryPlan",
        "MarketScanSeed",
        "RETRYABLE_SCAN_STATUSES",
        "TERMINAL_SCAN_STATUSES",
    ]
    assert MarketScanResultWrite is ModelResultWrite
    assert MarketScanRetryPlan is ModelRetryPlan
    assert MarketScanSeed is ModelSeed
    assert MarketScanRepository.finish_run.__module__.endswith("market_scan_lifecycle")
    assert MarketScanRepository.save_result_batch.__module__.endswith("market_scan_results")
    assert MarketScanRepository.results_page.__module__.endswith("market_scan_queries")


def test_market_scan_repository_modules_have_an_acyclic_internal_dependency_graph() -> None:
    module_names = {path.stem for path in SCAN_MODULES}
    dependencies: dict[str, set[str]] = {name: set() for name in module_names}

    for path in SCAN_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            assert not node.module.startswith(("app.services", "app.api"))
            imported = node.module.rsplit(".", 1)[-1]
            if node.module.startswith("app.repositories.") and imported in module_names:
                dependencies[path.stem].add(imported)

    visited: set[str] = set()
    active: set[str] = set()

    def visit(module: str) -> None:
        assert module not in active, f"market scan repository dependency cycle at {module}"
        if module in visited:
            return
        active.add(module)
        for dependency in dependencies[module]:
            visit(dependency)
        active.remove(module)
        visited.add(module)

    for module in dependencies:
        visit(module)


def test_market_scan_repository_modules_remain_reviewable() -> None:
    oversized = {
        path.name: len(path.read_text(encoding="utf-8").splitlines()) for path in SCAN_MODULES if len(path.read_text(encoding="utf-8").splitlines()) > 500
    }
    assert oversized == {}
