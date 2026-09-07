from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _javascript_project(tmp_path: Path) -> Path:
    root = tmp_path / "project with spaces"
    for directory in ("static/js", "tests/e2e", "tools"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    (root / "package.json").write_text(json.dumps({
        "type": "module", "scripts": {"check:js": package["scripts"]["check:js"]},
    }), encoding="utf-8")
    for relative in ("static/app.js", "static/js/aaa.js", "static/js/zzz.js", "playwright.config.js"):
        (root / relative).write_text("export const valid = 1;\n", encoding="utf-8")
    checker = ROOT / "tools" / "check_javascript.mjs"
    if checker.is_file():
        shutil.copyfile(checker, root / "tools" / checker.name)
    return root


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npm", "run", "check:js"], cwd=root, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("relative", [
    "static/js/zzz.js",
    "static/js/nested/broken.js",
    "tests/e2e/broken.spec.js",
    "tests/broken-helper.mjs",
    "tools/broken.cjs",
    "playwright.config.js",
])
def test_syntax_gate_rejects_errors_across_all_owned_javascript(tmp_path: Path, relative: str) -> None:
    root = _javascript_project(tmp_path)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("const = ;\n", encoding="utf-8")

    result = _check(root)

    assert result.returncode != 0, f"syntax error was ignored in {relative}: {result.stdout}"
    assert target.name in result.stderr


def test_syntax_gate_parses_without_executing_modules(tmp_path: Path) -> None:
    root = _javascript_project(tmp_path)
    (root / "static/js/zzz.js").write_text("throw new Error('must not execute');\n", encoding="utf-8")

    result = _check(root)

    assert result.returncode == 0, result.stderr


def test_syntax_gate_rejects_missing_source_directory(tmp_path: Path) -> None:
    root = _javascript_project(tmp_path)
    (root / "tests/e2e").rmdir()
    (root / "tests").rmdir()

    result = _check(root)

    assert result.returncode != 0
