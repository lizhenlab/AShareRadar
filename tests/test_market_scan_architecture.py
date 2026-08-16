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


def test_production_scoring_does_not_eagerly_import_replay_engine() -> None:
    path = Path(__file__).resolve().parents[1] / "app/services/market_scan_scoring.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    eager_imports = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "app.services.market_scan_replay" not in eager_imports


def test_market_scan_evaluation_calibration_split_stays_bounded() -> None:
    root = Path(__file__).resolve().parents[1]
    facade = root / "app/services/market_scan_evaluation.py"
    metrics = root / "app/services/market_scan_evaluation_metrics.py"
    facade_source = facade.read_text(encoding="utf-8")
    metrics_source = metrics.read_text(encoding="utf-8")

    assert len(facade_source.splitlines()) < 3_600
    assert len(metrics_source.splitlines()) < 160
    metrics_tree = ast.parse(metrics_source)
    forbidden_imports = {
        node.module
        for node in ast.walk(metrics_tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("app.services.market_scan_evaluation")
    }
    assert forbidden_imports == set()
    for compatibility_name in (
        "calibration_bucket as _calibration_bucket",
        "calibration_metrics as _calibration_metrics",
        "calibration_record as _calibration_record",
    ):
        assert compatibility_name in facade_source
