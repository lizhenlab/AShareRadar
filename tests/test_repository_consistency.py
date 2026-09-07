from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from tools.check_repository import check_repository, main


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_repository.py"


def write(root: Path, name: str, content: str = "") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize("statement", ["import app.missing", "from tools.missing import value", "import tests.missing as fixture"])
def test_missing_internal_module_is_reported_without_executing_code(tmp_path: Path, statement: str) -> None:
    write(tmp_path, "app/check.py", f"raise RuntimeError('must not execute')\n{statement}\n")
    issues = check_repository(tmp_path, ["imports"])
    assert len(issues) == 1
    assert issues[0].path == "app/check.py" and issues[0].line == 2
    assert "missing internal module" in issues[0].message


def test_namespace_packages_attributes_and_third_party_modules_are_not_false_positives(tmp_path: Path) -> None:
    write(tmp_path, "app/namespace/values.py", "answer = 42\n")
    write(tmp_path, "app/check.py", """
import app.namespace.values
from app.namespace.values import answer
from app.namespace import values
from app.namespace.values import generated_attribute
from pandas.missing import third_party_attribute
import numpy.missing
""")
    assert check_repository(tmp_path, ["imports"]) == []


def test_module_targets_require_python_files_or_package_directories(tmp_path: Path) -> None:
    write(tmp_path, "app/extensionless")
    (tmp_path / "app" / "directory.py").mkdir()
    write(tmp_path, "app/check.py", "import app.extensionless\nimport app.directory\n")
    assert [issue.message for issue in check_repository(tmp_path, ["imports"])] == [
        "missing internal module: app.extensionless", "missing internal module: app.directory",
    ]


def test_relative_module_imports_resolve_from_modules_and_package_initializers(tmp_path: Path) -> None:
    write(tmp_path, "app/shared.py", "value = 1\n")
    write(tmp_path, "app/pack/child.py", "from ..shared import value\nfrom . import package_attribute\n")
    write(tmp_path, "app/pack/__init__.py", "from .child import value\npackage_attribute = 2\n")
    write(tmp_path, "tools/jobs/task.py", "from .worker import run\n")
    write(tmp_path, "tools/jobs/worker.py", "def run(): pass\n")
    assert check_repository(tmp_path, ["imports"]) == []
    write(tmp_path, "app/pack/child.py", "from .absent import value\nfrom ...outside import value\n")
    messages = [issue.message for issue in check_repository(tmp_path, ["imports"])]
    assert messages == [
        "missing internal module: app.pack.absent",
        "relative import escapes its package or lacks a package",
    ]


@pytest.mark.parametrize("statement", [
    'import importlib; importlib.import_module("app.missing")',
    'import importlib as loader; loader.import_module(name="app.missing")',
    'from importlib import import_module as load; load("app.missing")',
    '__import__("app.missing")',
    '__import__(name="app.missing")',
])
def test_literal_dynamic_internal_imports_are_checked(tmp_path: Path, statement: str) -> None:
    write(tmp_path, "tools/job.py", statement)
    issues = check_repository(tmp_path, ["imports"])
    assert len(issues) == 1
    assert issues[0].message == "missing internal module: app.missing"


def test_dynamic_relative_imports_require_explicit_package_and_ignore_variables(tmp_path: Path) -> None:
    write(tmp_path, "app/pack/child.py")
    write(tmp_path, "app/pack/main.py", """
import importlib
importlib.import_module(".child", package="app.pack")
__import__("child", level=1)
importlib.import_module(variable_name)
object_loader.import_module("app.not_a_real_importlib_call")
importlib.import_module("pandas.third_party")
""")
    assert check_repository(tmp_path, ["imports"]) == []
    write(tmp_path, "app/pack/main.py", 'import importlib\nimportlib.import_module(".child")\n')
    assert "lacks a package" in check_repository(tmp_path, ["imports"])[0].message


def test_syntax_failures_are_short_diagnostics_and_cache_files_are_excluded(tmp_path: Path) -> None:
    write(tmp_path, "app/bad.py", "def broken(:\n")
    write(tmp_path, "app/__pycache__/ignored.py", "not valid python ???")
    issues = check_repository(tmp_path, ["imports"])
    assert len(issues) == 1
    assert issues[0].path == "app/bad.py" and issues[0].line == 1
    assert "cannot parse Python" in str(issues[0])


def test_valid_markdown_paths_support_spaces_parentheses_titles_fragments_and_references(tmp_path: Path) -> None:
    write(tmp_path, "docs/My Guide (v2).md", "[home](../README.md#intro)\n")
    write(tmp_path, "docs/image.png")
    write(tmp_path, "README.md", """
[`guide`](<docs/My Guide (v2).md#intro> "title")
[guide](docs/My%20Guide%20(v2).md#other)
[guide](docs/My Guide (v2).md)
![image](docs/image.png)
[reference][guide]
[guide]: <docs/My Guide (v2).md> 'title'
""")
    assert check_repository(tmp_path, ["links"]) == []


def test_missing_markdown_links_and_images_report_document_and_line(tmp_path: Path) -> None:
    write(tmp_path, "README.md", "# Intro\n[missing](docs/absent.md#anchor)\n![image](missing.png)\n")
    issues = check_repository(tmp_path, ["links"])
    assert [(item.path, item.line) for item in issues] == [("README.md", 2), ("README.md", 3)]
    assert issues[0].message == "missing local link: docs/absent.md#anchor"


def test_markdown_titles_do_not_change_paths_or_parenthesis_balancing(tmp_path: Path) -> None:
    write(tmp_path, "docs/guide.md")
    write(tmp_path, "docs/escaped(name).md")
    write(tmp_path, "README.md", r'''
[guide](docs/guide.md "a title with ) punctuation")
[guide](docs/guide.md 'a title with ( punctuation')
[guide](docs/guide.md (a parenthesized title))
[escaped](docs/escaped\(name\).md)
[incomplete](not-a-rendered-link.md
''')
    assert check_repository(tmp_path, ["links"]) == []


def test_external_links_anchors_and_code_examples_are_not_local_file_links(tmp_path: Path) -> None:
    write(tmp_path, "README.md", """
[remote](https://example.com/missing)
[mail](mailto:reader@example.com)
[anchor](#not-a-file)
[protocol relative](//example.com/missing)
`[inline example](not-a-file.md)`
``[backtick ` in code](also-not-a-file.md)``
```markdown
[fenced](not-a-file.md)
[ref]: not-a-file.md
```
~~~text
[tilde fence](not-a-file.md)
~~~
""")
    assert check_repository(tmp_path, ["links"]) == []


@pytest.mark.parametrize("destination", ["../outside.md", "%2e%2e/outside.md", "file:///outside.md"])
def test_local_links_cannot_escape_repository(tmp_path: Path, destination: str) -> None:
    write(tmp_path, "README.md", f"[outside]({destination})\n")
    assert "escapes repository" in check_repository(tmp_path, ["links"])[0].message


def test_absolute_local_paths_and_symlinks_are_checked_against_root(tmp_path: Path) -> None:
    inside = write(tmp_path, "inside.md")
    write(tmp_path, "README.md", f"[inside](<{inside}>)\n")
    assert check_repository(tmp_path, ["links"]) == []
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    write(tmp_path, "README.md", "[outside](escape/sibling.md)\n")
    assert "escapes repository" in check_repository(tmp_path, ["links"])[0].message


def test_only_readme_and_top_level_docs_are_checked(tmp_path: Path) -> None:
    write(tmp_path, "docs/current.md", "[missing](missing.md)\n")
    write(tmp_path, "docs/research/archived.md", "[old absolute link](/obsolete/report.json)\n")
    write(tmp_path, "notes.md", "[unowned](missing.md)\n")
    issues = check_repository(tmp_path, ["links"])
    assert len(issues) == 1 and issues[0].path == "docs/current.md"


def test_cli_supports_explicit_checks_and_reports_nonzero_without_traceback(tmp_path: Path) -> None:
    write(tmp_path, "app/main.py", "import app.missing\n")
    write(tmp_path, "README.md", "# Valid\n")
    command = [sys.executable, str(SCRIPT), "--root", str(tmp_path)]
    links = subprocess.run([*command, "--check", "links"], capture_output=True, text=True, check=False)
    all_checks = subprocess.run([*command, "--check"], capture_output=True, text=True, check=False)
    assert links.returncode == 0 and "consistency ok: links" in links.stdout
    assert all_checks.returncode == 1 and "app/main.py:1: missing internal module" in all_checks.stderr
    assert "Traceback" not in all_checks.stderr


def test_invalid_root_and_check_selection_are_explicit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(tmp_path / "missing")]) == 1
    assert "repository root does not exist" in capsys.readouterr().err
    with pytest.raises(ValueError, match="checks must contain"):
        check_repository(tmp_path, ["unknown"])
