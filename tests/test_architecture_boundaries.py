from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
LOWER_LAYER_DIRS = ("artifacts", "db", "repositories", "models", "utils")
FORBIDDEN_LOWER_PREFIXES = ("app.api", "app.services", "app.workflows")
MAX_CROSS_MODULE_PRIVATE_IMPORTS = 298


def _python_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in APP.rglob("*.py"):
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    return modules


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _application_module_aliases(tree: ast.Module) -> dict[str, str]:
    known_modules = set(_python_modules())
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in known_modules and imported.asname is not None:
                    aliases[imported.asname] = imported.name
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for imported in node.names:
                module = f"{node.module}.{imported.name}"
                if module in known_modules:
                    aliases[imported.asname or imported.name] = module
    return aliases


def _private_module_attribute_imports(path: Path, tree: ast.Module) -> list[str]:
    aliases = _application_module_aliases(tree)
    return [
        f"{path.relative_to(ROOT)}:{node.lineno} imports {aliases[node.value.id]}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases
        and node.attr.startswith("_")
        and not node.attr.startswith("__")
    ]


def _imported_modules(path: Path, module: str, known_modules: set[str]) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom):
            package = module if path.name == "__init__.py" else module.rpartition(".")[0]
            if node.level:
                package_parts = package.split(".") if package else []
                keep = len(package_parts) - node.level + 1
                prefix = ".".join(package_parts[: max(0, keep)])
                base = ".".join(part for part in (prefix, node.module or "") if part)
            else:
                base = node.module or ""
            if base:
                imported.add(base)
            for alias in node.names:
                candidate = ".".join(part for part in (base, alias.name) if part)
                if candidate in known_modules:
                    imported.add(candidate)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def _internal_dependencies(
    module: str,
    path: Path,
    modules: dict[str, Path],
) -> set[str]:
    dependencies: set[str] = set()
    for imported in _imported_modules(path, module, set(modules)):
        target = imported
        while target not in modules and "." in target:
            target = target.rsplit(".", 1)[0]
        if target in modules and target != module:
            dependencies.add(target)
    parent = module
    while "." in parent:
        parent = parent.rsplit(".", 1)[0]
        if parent in modules and parent != module:
            dependencies.add(parent)
    return dependencies


def test_lower_layers_do_not_depend_on_services_workflows_or_api() -> None:
    offenders = [
        f"{path.relative_to(ROOT)} -> {module}"
        for directory in LOWER_LAYER_DIRS
        for path in sorted((APP / directory).glob("*.py"))
        for module in sorted(_imported_modules(path, "", set(_python_modules())))
        if module.startswith(FORBIDDEN_LOWER_PREFIXES)
    ]

    assert offenders == []


def test_production_code_uses_domain_models_instead_of_schema_facade() -> None:
    facade = APP / "models" / "schemas.py"
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted(APP.rglob("*.py"))
        if path != facade and "app.models.schemas" in _imported_modules(path, "", set(_python_modules()))
    ]

    assert offenders == []


def test_application_internal_import_graph_is_acyclic() -> None:
    modules = _python_modules()
    dependencies: dict[str, set[str]] = defaultdict(set)
    for name, path in modules.items():
        dependencies[name].update(_internal_dependencies(name, path, modules))

    active: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, trail: tuple[str, ...]) -> None:
        assert module not in active, "application import cycle: " + " -> ".join((*trail, module))
        if module in visited:
            return
        active.add(module)
        for dependency in sorted(dependencies[module]):
            visit(dependency, (*trail, module))
        active.remove(module)
        visited.add(module)

    for module in sorted(modules):
        visit(module, ())


def test_direct_wall_clock_access_is_isolated_to_clock_adapter() -> None:
    clock_path = APP / "utils" / "clock.py"
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if path == clock_path:
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if node.func.attr == "now" and isinstance(owner, ast.Name) and owner.id == "datetime":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert offenders == []


def test_cross_module_private_imports_cannot_increase() -> None:
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = _tree(path)
        offenders.extend(
            f"{path.relative_to(ROOT)}:{node.lineno} imports {node.module}.{alias.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("app.")
            for alias in node.names
            if alias.name.startswith("_") and not alias.name.startswith("__")
        )
        offenders.extend(_private_module_attribute_imports(path, tree))

    assert len(offenders) <= MAX_CROSS_MODULE_PRIVATE_IMPORTS, (
        "cross-module private imports increased; publish a contract instead:\n"
        + "\n".join(offenders[MAX_CROSS_MODULE_PRIVATE_IMPORTS:])
    )
