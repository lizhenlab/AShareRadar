from __future__ import annotations

import ast
from pathlib import Path

from tools.api_inventory import collect_endpoints, render
from tools.architecture_inventory import SOURCE_DIRS, build_inventory


ROOT = Path(__file__).resolve().parent.parent


def test_architecture_inventory_includes_tooling_modules() -> None:
    inventory = build_inventory()

    assert SOURCE_DIRS == ("app", "tests", "tools")
    assert "| `tools/` |" in inventory
    assert "#### `tools/api_inventory.py`" in inventory
    assert "`collect_endpoints`" in inventory


def test_api_inventory_documents_business_api_scope() -> None:
    endpoints = collect_endpoints()
    rendered = render(endpoints)

    assert "/" not in {endpoint.path for endpoint in endpoints}
    assert "UI root route `/`" in rendered


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
