from __future__ import annotations

import ast
from pathlib import Path


def test_market_scan_support_modules_keep_domain_boundary_and_bounded_size() -> None:
    root = Path(__file__).resolve().parents[1]
    modules = [
        root / "app/services/market_scan_execution.py",
        root / "app/services/market_scan_completion.py",
        root / "app/services/market_scan_lifecycle.py",
    ]

    for path in modules:
        source = path.read_text(encoding="utf-8")
        assert len(source.splitlines()) < 500, path.name
        tree = ast.parse(source)
        repository_imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("app.repositories")
        ]
        assert repository_imports == [], f"{path.name} must not depend on repository DTOs"
