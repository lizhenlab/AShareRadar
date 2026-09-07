"""Check local Python module targets and Markdown file links without executing code."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import tokenize
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGES = frozenset({"app", "tools", "tests"})
CHECKS = ("imports", "links")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(?<!\\)(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)", re.DOTALL)
LINK_START = re.compile(r"(?<!\\)!?\[[^\]\n]*\]\(")
REFERENCE = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class RepositoryIssue:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def check_repository(root: Path = ROOT, checks: Sequence[str] = CHECKS) -> list[RepositoryIssue]:
    root = root.resolve()
    selected = set(checks)
    if not selected.issubset(CHECKS):
        raise ValueError("checks must contain only imports or links")
    if not root.is_dir():
        return [RepositoryIssue(str(root), 1, "repository root does not exist")]
    issues = check_python_imports(root) if "imports" in selected else []
    if "links" in selected:
        issues.extend(check_markdown_links(root))
    return sorted(issues, key=lambda issue: (issue.path, issue.line, issue.message))


def check_python_imports(root: Path) -> list[RepositoryIssue]:
    issues: list[RepositoryIssue] = []
    files = sorted(path for name in SOURCE_PACKAGES for path in (root / name).rglob("*.py"))
    for path in files:
        if "__pycache__" in path.parts or not path.is_file():
            continue
        try:
            with tokenize.open(path) as source:
                tree = ast.parse(source.read(), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            issues.append(_issue(root, path, getattr(exc, "lineno", None) or 1, f"cannot parse Python: {exc}"))
            continue
        issues.extend(_file_import_issues(root, path, tree))
    return issues


def _file_import_issues(root: Path, path: Path, tree: ast.Module) -> list[RepositoryIssue]:
    package = _source_package(root, path)
    aliases = _importlib_aliases(tree)
    issues: list[RepositoryIssue] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Call)):
            continue
        for target in _import_targets(node, package, aliases):
            if target is None:
                issues.append(_issue(root, path, node.lineno, "relative import escapes its package or lacks a package"))
            elif target.split(".")[0] in SOURCE_PACKAGES and not _module_exists(root, target):
                issues.append(_issue(root, path, node.lineno, f"missing internal module: {target}"))
    return issues


def _source_package(root: Path, path: Path) -> str:
    # Both a regular module and __init__.py resolve relative imports from their directory.
    return ".".join(path.relative_to(root).parent.parts)


def _module_exists(root: Path, name: str) -> bool:
    parts = name.split(".")
    if not all(part.isidentifier() for part in parts):
        return False
    target = root.joinpath(*parts)
    source = target.with_suffix(".py")
    return (
        source.resolve().is_relative_to(root) and source.is_file()
        or target.resolve().is_relative_to(root) and target.is_dir()
    )


def _resolve_relative(name: str, package: str) -> str | None:
    if not name.startswith("."):
        return name
    level = len(name) - len(name.lstrip("."))
    parts = package.split(".") if package else []
    if level > len(parts):
        return None
    base = parts[:len(parts) - level + 1]
    suffix = name[level:]
    return ".".join([*base, suffix] if suffix else base)


def _importlib_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    modules = {"importlib"}
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "importlib")
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib" and node.level == 0:
            functions.update(alias.asname or alias.name for alias in node.names if alias.name == "import_module")
    return modules, functions


def _import_targets(node: ast.AST, package: str, aliases: tuple[set[str], set[str]]) -> list[str | None]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        # Imported names can be attributes or re-exports; only the base is certainly a module.
        return [_resolve_relative("." * node.level + (node.module or ""), package)]
    if isinstance(node, ast.Call) and _is_literal_import_call(node, aliases):
        return _dynamic_import_targets(node, package)
    return []


def _is_literal_import_call(node: ast.Call, aliases: tuple[set[str], set[str]]) -> bool:
    modules, functions = aliases
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "__import__" or func.id in functions
    return (
        isinstance(func, ast.Attribute) and func.attr == "import_module"
        and isinstance(func.value, ast.Name) and func.value.id in modules
    )


def _dynamic_import_targets(node: ast.Call, package: str) -> list[str | None]:
    name = _literal_argument(node, 0, "name")
    if not isinstance(name, str):
        return []
    if isinstance(node.func, ast.Name) and node.func.id == "__import__":
        level = _literal_argument(node, 4, "level")
        return [_resolve_relative("." * level + name, package)] if isinstance(level, int) and level > 0 else [name]
    declared_package = _literal_argument(node, 1, "package")
    return [_resolve_relative(name, declared_package if isinstance(declared_package, str) else "")]


def _literal_argument(node: ast.Call, index: int, keyword: str) -> object:
    value: ast.expr | None
    if len(node.args) > index:
        value = node.args[index]
    else:
        value = next((item.value for item in node.keywords if item.arg == keyword), None)
    return value.value if isinstance(value, ast.Constant) else None


def check_markdown_links(root: Path) -> list[RepositoryIssue]:
    paths = [root / "README.md", *sorted((root / "docs").glob("*.md"))]
    issues: list[RepositoryIssue] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = _without_code(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            issues.append(_issue(root, path, 1, f"cannot read Markdown: {exc}"))
            continue
        for offset, destination in _markdown_destinations(text):
            problem = _link_problem(root, path, destination)
            if problem:
                issues.append(_issue(root, path, text.count("\n", 0, offset) + 1, problem))
    return issues


def _without_code(text: str) -> str:
    lines: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        match = FENCE.match(line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not line[match.end():].strip():
                fence = ""
            lines.append(_blank(line))
        elif match:
            fence = match[1]
            lines.append(_blank(line))
        else:
            lines.append(line)
    return INLINE_CODE.sub(lambda match: _blank(match[0]), "".join(lines))


def _blank(text: str) -> str:
    return "".join("\n" if character == "\n" else " " for character in text)


def _markdown_destinations(text: str) -> Iterator[tuple[int, str]]:
    for match in LINK_START.finditer(text):
        destination = _balanced_destination(text, match.end())
        if destination is not None:
            yield match.start(), _destination_path(destination)
    for match in REFERENCE.finditer(text):
        yield match.start(), _destination_path(match[1])


def _balanced_destination(text: str, start: int) -> str | None:
    depth = 1
    for index, character in _destination_parentheses(text, start):
        depth += 1 if character == "(" else -1
        if depth == 0:
            return text[start:index]
    return None


def _destination_parentheses(text: str, start: int) -> Iterator[tuple[int, str]]:
    escaped = False
    closing = ""
    for index in range(start, len(text)):
        character = text[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif closing:
            if character == closing:
                closing = ""
        elif character == "<":
            closing = ">"
        elif character in "\"'" and (index == start or text[index - 1].isspace()):
            closing = character
        elif character in "()":
            yield index, character


def _destination_path(destination: str) -> str:
    destination = destination.strip()
    if destination.startswith("<") and ">" in destination:
        destination = destination[1:destination.index(">")]
    else:
        destination = re.sub(r"\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\))\s*$", "", destination)
    return re.sub(r"\\([\\`*{}\[\]()#+.!<>_ -])", r"\1", destination)


def _link_problem(root: Path, document: Path, destination: str) -> str | None:
    if not destination or destination.startswith(("#", "//")):
        return None
    try:
        url = urlsplit(destination)
    except ValueError:
        return f"invalid local link: {destination}"
    if url.scheme and url.scheme != "file":
        return None
    if url.scheme == "file" and url.netloc not in ("", "localhost"):
        return f"local link escapes repository: {destination}"
    target = (document.parent / unquote(url.path)).resolve()
    if not target.is_relative_to(root):
        return f"local link escapes repository: {destination}"
    return None if target.exists() else f"missing local link: {destination}"


def _issue(root: Path, path: Path, line: int, message: str) -> RepositoryIssue:
    return RepositoryIssue(path.relative_to(root).as_posix(), line, message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this project)")
    parser.add_argument("--check", action="append", nargs="?", const="all", choices=("all", *CHECKS), help="select imports or links; omitted/bare checks both")
    args = parser.parse_args(argv)
    checks = CHECKS if not args.check or "all" in args.check else args.check
    try:
        issues = check_repository(args.root, checks)
    except (OSError, ValueError) as exc:
        print(f"repository consistency: {exc}", file=sys.stderr)
        return 1
    for issue in issues:
        print(issue, file=sys.stderr)
    if issues:
        print(f"repository consistency: {len(issues)} issue(s)", file=sys.stderr)
        return 1
    print(f"repository consistency ok: {', '.join(checks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
