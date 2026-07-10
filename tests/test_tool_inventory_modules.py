from __future__ import annotations

import ast
from pathlib import Path
import re

from tools.api_inventory import collect_endpoints, render
from tools.architecture_inventory import (
    MAX_PRODUCTION_FUNCTION_BRANCHES,
    MAX_PRODUCTION_FUNCTION_LINES,
    PRODUCTION_SOURCE_DIRS,
    SOURCE_DIRS,
    build_inventory,
    collect_source_inventories,
)


ROOT = Path(__file__).resolve().parent.parent


def test_architecture_inventory_includes_tooling_modules() -> None:
    inventory = build_inventory()

    assert SOURCE_DIRS == ("app", "tests", "tools")
    assert "| `tools/` |" in inventory
    assert "## Python Function Health" in inventory
    assert "#### `tools/api_inventory.py`" in inventory
    assert "`collect_endpoints`" in inventory


def test_architecture_inventory_source_collection_matches_config() -> None:
    sources = collect_source_inventories()

    assert [source.name for source in sources] == list(SOURCE_DIRS)
    assert all("__pycache__" not in path.parts for source in sources for path in source.files)
    assert all(source.summary["lines"] > 0 for source in sources)
    assert all(source.function_metrics for source in sources)


def test_production_python_functions_stay_reviewable() -> None:
    offenders: list[str] = []
    for source in collect_source_inventories():
        if source.name not in PRODUCTION_SOURCE_DIRS:
            continue
        for metric in source.function_metrics:
            if metric.lines > MAX_PRODUCTION_FUNCTION_LINES or metric.branches > MAX_PRODUCTION_FUNCTION_BRANCHES:
                offenders.append(
                    f"{metric.path.relative_to(ROOT)}:{metric.qualified_name} "
                    f"has {metric.lines} lines and {metric.branches} branch points"
                )

    assert offenders == []


def test_api_inventory_documents_business_api_scope() -> None:
    endpoints = collect_endpoints()
    rendered = render(endpoints)

    assert "/" not in {endpoint.path for endpoint in endpoints}
    assert "UI root route `/`" in rendered
    assert "GET | `/api/review` | query `symbol: str = '600519'`" in rendered
    assert "query `period_days: int = 60` (ge=20; le=240)" in rendered
    assert "GET | `/api/stock/workbench` | query `symbol: str = '600519'`" in rendered
    assert "POST | `/api/stock/ask` | body `payload: StockQuestionInput`" in rendered
    assert "`503`: provider, runtime, scheduler, or SQLite failures" in rendered
    assert "`GET /api/stream/quotes` returns `text/event-stream`" in rendered


def test_test_plan_mentions_every_test_module() -> None:
    test_plan = (ROOT / "docs" / "TEST_PLAN.md").read_text(encoding="utf-8")
    missing = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "tests").glob("test_*.py"))
        if str(path.relative_to(ROOT)) not in test_plan
    ]

    assert missing == []


def test_test_report_keeps_auditable_latest_verification_table() -> None:
    test_plan = (ROOT / "docs" / "TEST_PLAN.md").read_text(encoding="utf-8")

    assert "| Date | Worktree State | Environment | Command | Scope | Result | Notes |" in test_plan
    assert re.search(r"`npm run check` \| Python compile, pyflakes, JS syntax, full pytest suite \| \d+ passed", test_plan)
    assert "Recent targeted checks kept for traceability" in test_plan


def test_operations_documents_backup_before_runtime_data_deletion() -> None:
    operations = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")

    backup_index = operations.index("Before deleting or replacing local data")
    delete_index = operations.index("rm -f data/ashare_radar.sqlite3")
    assert backup_index < delete_index
    assert "cp -p data/ashare_radar.sqlite3*" in operations
    assert "Restore a backup while the service is stopped" in operations
    assert "tail -f /tmp/ashare_radar.log" in operations


def test_model_classes_do_not_repeat_field_names() -> None:
    duplicates: list[str] = []
    for path in sorted((ROOT / "app" / "models").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                seen: set[str] = set()
                for item in node.body:
                    if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                        continue
                    name = item.target.id
                    if name in seen:
                        duplicates.append(f"{path.relative_to(ROOT)}:{node.name}.{name}")
                    seen.add(name)

    assert duplicates == []
