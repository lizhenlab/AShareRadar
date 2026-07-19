from __future__ import annotations

import ast
from pathlib import Path
import re

import tools.api_inventory as api_inventory
import tools.architecture_inventory as architecture_inventory
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
_MACHINE_SPECIFIC_PATH_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9_])/(?:Users|home)/"
        r"(?!(?:<[^/ >]+>|\$(?:USER|\{USER\}))(?:/|$))[^/\s`\"']+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_])/(?:opt|usr/local)/(?:anaconda|miniconda)\d*(?=/|[\s`\"']|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_])[A-Z]:[\\/]+Users[\\/]+"
        r"(?!(?:<[^\\/ >]+>|%(?:USERNAME|USERPROFILE)%)(?:[\\/]|$))[^\\/\s`\"']+",
        re.IGNORECASE,
    ),
)


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
    assert "POST | `/api/local-data/export` | - | `export_local_user_data`" in rendered
    assert "response: Response" not in rendered
    assert "POST | `/api/reviews/plans/{plan_id}/evaluate` | path `plan_id: int`<br>body `payload: AdviceReviewEvaluationRequest \\| None`" in rendered
    assert "`503`: provider, runtime, scheduler, or SQLite failures" in rendered
    assert "`GET /api/stream/quotes` returns `text/event-stream`" in rendered


def test_test_plan_indexes_every_test_module_once() -> None:
    test_plan = (ROOT / "docs" / "TEST_PLAN.md").read_text(encoding="utf-8")
    index = test_plan.split("The current test suite is split by domain:", 1)[1].split("## 4.", 1)[0]
    indexed = re.findall(r"tests/test_[A-Za-z0-9_]+\.py", index)
    expected = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests").glob("test_*.py"))]

    assert len(indexed) == len(set(indexed))
    assert sorted(indexed) == expected


def test_inventory_check_mode_detects_missing_current_and_stale_output(tmp_path, monkeypatch) -> None:
    cases = (
        (api_inventory, "API_REFERENCE.md"),
        (architecture_inventory, "FUNCTION_INVENTORY.md"),
    )
    for module, filename in cases:
        output = tmp_path / filename
        monkeypatch.setattr(module, "OUTPUT", output)

        assert module.main(["--check"]) == 1
        assert not output.exists()
        assert module.main([]) == 0
        assert module.main(["--check"]) == 0

        output.write_text("stale\n", encoding="utf-8")
        assert module.main(["--check"]) == 1


def test_documentation_does_not_embed_machine_specific_paths() -> None:
    documentation = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    offenders = [
        f"{path.relative_to(ROOT)}: {match.group(0)}"
        for path in documentation
        for pattern in _MACHINE_SPECIFIC_PATH_PATTERNS
        if (match := pattern.search(path.read_text(encoding="utf-8"))) is not None
    ]

    assert offenders == []


def test_data_directory_ignores_every_runtime_artifact() -> None:
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "data/*" in rules
    assert "!data/.gitkeep" in rules


def test_machine_specific_path_detection_is_cross_platform_and_user_agnostic() -> None:
    examples = (
        "/Users/example-account/project",
        "/home/example-account/project",
        "/opt/miniconda42/lib/python",
        r"C:\Users\example-account\project",
    )

    assert all(any(pattern.search(value) for pattern in _MACHINE_SPECIFIC_PATH_PATTERNS) for value in examples)


def test_python_dependency_layers_keep_runtime_and_engineering_tools_separate() -> None:
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8").casefold()
    development = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").casefold()
    runtime_lock = (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").casefold()
    development_lock = (ROOT / "requirements-dev-lock.txt").read_text(encoding="utf-8").casefold()
    engineering_tools = {"mypy", "pyflakes", "pytest", "pytest-cov", "ruff"}

    assert {"fastapi", "requests", "starlette", "uvicorn"} <= set(re.findall(r"^([a-z0-9-]+)", runtime, flags=re.MULTILINE))
    assert not engineering_tools & set(re.findall(r"^([a-z0-9-]+)", runtime, flags=re.MULTILINE))
    assert development.startswith("-r requirements.txt\n")
    assert engineering_tools <= set(re.findall(r"^([a-z0-9-]+)", development, flags=re.MULTILINE))
    assert "autogenerated by pip-compile with python 3.12" in runtime_lock
    assert "autogenerated by pip-compile with python 3.12" in development_lock
    assert "--generate-hashes requirements.txt" in runtime_lock
    assert "--allow-unsafe --generate-hashes requirements-dev.txt" in development_lock
    assert "--hash=sha256:" in runtime_lock
    assert "--hash=sha256:" in development_lock
    assert not engineering_tools & set(re.findall(r"^([a-z0-9-]+)==", runtime_lock, flags=re.MULTILINE))
    assert engineering_tools <= set(re.findall(r"^([a-z0-9-]+)==", development_lock, flags=re.MULTILINE))


def test_ci_keeps_the_incremental_quality_gates() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for gate in (
        'python-version: "3.12"',
        "python -m pip check",
        "python -m ruff check app tests tools",
        "python -m mypy",
        "npm run check:js",
        "tools/api_inventory.py --check",
        "tools/architecture_inventory.py --check",
        "--cov=app --cov=tools",
        "python -m pip install --require-hashes -r requirements-dev-lock.txt",
        "npm ci",
        "npx --no-install playwright install --with-deps chromium",
        "npm run test:e2e",
    ):
        assert gate in workflow

    action_refs = re.findall(r"uses:\s*[^@\s]+@([^\s#]+)", workflow)
    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)
    for action_name in ("checkout", "setup-python", "setup-node"):
        majors = re.findall(
            rf"actions/{action_name}@[0-9a-f]{{40}}\s+# v(\d+)",
            workflow,
        )
        assert majors
        assert all(int(major) >= 6 for major in majors)


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
    assert "$PYTHON tools/runtime_data.py backup" in operations
    assert "$PYTHON tools/runtime_data.py verify" in operations
    assert "cp -p data/ashare_radar.sqlite3*" not in operations
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
