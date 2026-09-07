from __future__ import annotations

import ast
from pathlib import Path

from app.services.scheduler_service import LocalDataScheduler


SERVICE_DIR = Path(__file__).parents[1] / "app" / "services"
SCHEDULER_MODULES = tuple(sorted(SERVICE_DIR.glob("scheduler*.py")))


def test_scheduler_delegates_runtime_responsibilities_by_mro() -> None:
    assert LocalDataScheduler.start.__module__.endswith("scheduler_lifecycle")
    assert LocalDataScheduler.run_once.__module__.endswith("scheduler_execution")
    assert LocalDataScheduler._refresh_watch_quotes.__module__.endswith("scheduler_tasks")


def test_scheduler_modules_have_an_acyclic_internal_dependency_graph() -> None:
    module_names = {path.stem for path in SCHEDULER_MODULES}
    dependencies: dict[str, set[str]] = {name: set() for name in module_names}

    for path in SCHEDULER_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            imported = node.module.rsplit(".", 1)[-1]
            if node.module.startswith("app.services.scheduler") and imported in module_names:
                dependencies[path.stem].add(imported)

    visited: set[str] = set()
    active: set[str] = set()

    def visit(module: str) -> None:
        assert module not in active, f"scheduler dependency cycle at {module}"
        if module in visited:
            return
        active.add(module)
        for dependency in dependencies[module]:
            visit(dependency)
        active.remove(module)
        visited.add(module)

    for module in dependencies:
        visit(module)


def test_scheduler_modules_remain_reviewable() -> None:
    oversized = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in SCHEDULER_MODULES
        if len(path.read_text(encoding="utf-8").splitlines()) > 500
    }
    assert oversized == {}
