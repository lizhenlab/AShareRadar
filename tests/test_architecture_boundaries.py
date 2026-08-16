from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
LOWER_LAYER_DIRS = ("artifacts", "db", "repositories", "models", "utils")
FORBIDDEN_LOWER_PREFIXES = ("app.api", "app.services", "app.workflows")
MAX_CROSS_MODULE_PRIVATE_IMPORTS = 298
PRIVATE_IMPORT_BASELINE = ROOT / "tests" / "fixtures" / "cross_module_private_imports_v1.json"


@dataclass(frozen=True, order=True)
class _PrivateDependency:
    source: str
    target: str
    access: str
    line: int

    def description(self) -> str:
        return f"{self.source}:{self.line} accesses {self.access} from {self.target}"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _python_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in APP.rglob("*.py"):
        modules[_module_name(path)] = path
    return modules


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _import_from_base(path: Path, module: str, node: ast.ImportFrom) -> str:
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    if not node.level:
        return node.module or ""
    package_parts = package.split(".") if package else []
    keep = len(package_parts) - node.level + 1
    prefix = ".".join(package_parts[: max(0, keep)])
    return ".".join(part for part in (prefix, node.module or "") if part)


def _application_module_aliases(
    path: Path,
    module: str,
    tree: ast.Module,
    known_modules: set[str],
) -> dict[tuple[str, ...], str]:
    aliases: dict[tuple[str, ...], str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name not in known_modules:
                    continue
                alias = (imported.asname,) if imported.asname is not None else tuple(imported.name.split("."))
                aliases[alias] = imported.name
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_base(path, module, node)
            for imported in node.names:
                candidate = ".".join(part for part in (base, imported.name) if part)
                if candidate in known_modules:
                    aliases[(imported.asname or imported.name,)] = candidate
    return aliases


def _attribute_chain(node: ast.expr) -> tuple[str, ...] | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _known_module_prefix(parts: tuple[str, ...], known_modules: set[str]) -> str | None:
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in known_modules:
            return candidate
    return None


def _private_attribute_dependencies(
    path: Path,
    module: str,
    tree: ast.Module,
    known_modules: set[str],
) -> list[_PrivateDependency]:
    aliases = _application_module_aliases(path, module, tree, known_modules)
    dependencies: set[_PrivateDependency] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_") or node.attr.startswith("__"):
            continue
        chain = _attribute_chain(node)
        if chain is None:
            continue
        matches = [(prefix, target) for prefix, target in aliases.items() if len(prefix) < len(chain) and chain[: len(prefix)] == prefix]
        if not matches:
            continue
        prefix, imported_module = max(matches, key=lambda item: len(item[0]))
        resolved = (*imported_module.split("."), *chain[len(prefix) :])
        target = _known_module_prefix(resolved[:-1], known_modules)
        if target is None or target == module:
            continue
        dependencies.add(
            _PrivateDependency(
                source=module,
                target=target,
                access=".".join(resolved),
                line=node.lineno,
            )
        )
    return sorted(dependencies)


def _private_from_dependencies(
    path: Path,
    module: str,
    tree: ast.Module,
    known_modules: set[str],
) -> list[_PrivateDependency]:
    dependencies: list[_PrivateDependency] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        target = _import_from_base(path, module, node)
        if not target.startswith("app.") or target == module:
            continue
        resolved_target = _known_module_prefix(tuple(target.split(".")), known_modules) or target
        for imported in node.names:
            if imported.name.startswith("_") and not imported.name.startswith("__"):
                dependencies.append(
                    _PrivateDependency(
                        source=module,
                        target=resolved_target,
                        access=f"{target}.{imported.name}",
                        line=node.lineno,
                    )
                )
    return dependencies


def _private_dependencies(
    path: Path,
    tree: ast.Module,
    modules: dict[str, Path],
) -> tuple[_PrivateDependency, ...]:
    module = _module_name(path)
    known_modules = set(modules)
    dependencies = [
        *_private_from_dependencies(path, module, tree, known_modules),
        *_private_attribute_dependencies(path, module, tree, known_modules),
    ]
    return tuple(sorted(set(dependencies)))


@lru_cache(maxsize=1)
def _all_private_dependencies() -> tuple[_PrivateDependency, ...]:
    modules = _python_modules()
    return tuple(
        dependency
        for module, path in sorted(modules.items())
        for dependency in _private_dependencies(path, _tree(path), modules)
        if dependency.source == module
    )


def _private_import_baseline() -> tuple[dict[str, int], dict[str, int]]:
    payload = json.loads(PRIVATE_IMPORT_BASELINE.read_text(encoding="utf-8"))
    assert set(payload) == {"by_source", "by_target"}
    by_source = {str(module): int(count) for module, count in payload["by_source"].items()}
    by_target = {str(module): int(count) for module, count in payload["by_target"].items()}
    assert all(module.startswith("app.") and count > 0 for mapping in (by_source, by_target) for module, count in mapping.items())
    return by_source, by_target


def _ratchet_overages(actual: Counter[str], baseline: dict[str, int]) -> list[str]:
    return [f"{module}: {count} > {baseline.get(module, 0)}" for module, count in sorted(actual.items()) if count > baseline.get(module, 0)]


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


def test_api_routes_do_not_resolve_domain_services_through_cache() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path in sorted((APP / "api" / "routes").glob("*.py"))
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Attribute) and node.attr.endswith("_service") and isinstance(node.value, ast.Attribute) and node.value.attr == "cache"
    ]

    assert offenders == []


def test_scheduler_does_not_resolve_domain_services_through_cache() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path in sorted((APP / "services").glob("scheduler*.py"))
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Attribute) and node.attr.endswith("_service") and isinstance(node.value, ast.Attribute) and node.value.attr == "cache"
    ]

    assert offenders == []

    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((APP / "services").glob("scheduler*.py"))
    )
    assert "bound_domain_services" not in source


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


def test_private_import_detector_covers_supported_import_forms() -> None:
    modules = _python_modules()
    path = APP / "main.py"
    cases = (
        (
            "from-import-private",
            "from app.services.scoring import _clamp\n",
            "app.services.scoring._clamp",
        ),
        (
            "module-alias-private",
            "import app.services.scoring as scoring\nscoring._clamp(1)\n",
            "app.services.scoring._clamp",
        ),
        (
            "qualified-module-private",
            "import app.services.scoring\napp.services.scoring._clamp(1)\n",
            "app.services.scoring._clamp",
        ),
        (
            "nested-alias-private",
            "import app.services.scoring as alias\nalias.helpers._private(1)\n",
            "app.services.scoring.helpers._private",
        ),
    )

    for label, source, expected_access in cases:
        dependencies = _private_dependencies(path, ast.parse(source), modules)
        assert [(item.target, item.access) for item in dependencies] == [
            ("app.services.scoring", expected_access)
        ], label


def test_cross_module_private_imports_cannot_increase() -> None:
    dependencies = _all_private_dependencies()

    assert len(dependencies) <= MAX_CROSS_MODULE_PRIVATE_IMPORTS, (
        f"cross-module private imports increased: {len(dependencies)} > "
        f"{MAX_CROSS_MODULE_PRIVATE_IMPORTS}; publish a contract instead"
    )


def test_private_import_module_ratchet_allows_only_debt_reduction() -> None:
    baseline = {"app.services.example": 2}

    assert _ratchet_overages(Counter({"app.services.example": 1}), baseline) == []
    assert _ratchet_overages(Counter({"app.services.example": 3}), baseline) == [
        "app.services.example: 3 > 2"
    ]
    assert _ratchet_overages(Counter({"app.services.new_dependency": 1}), baseline) == [
        "app.services.new_dependency: 1 > 0"
    ]


def test_cross_module_private_imports_cannot_shift_between_modules() -> None:
    dependencies = _all_private_dependencies()
    source_baseline, target_baseline = _private_import_baseline()
    assert sum(source_baseline.values()) == MAX_CROSS_MODULE_PRIVATE_IMPORTS
    assert sum(target_baseline.values()) == MAX_CROSS_MODULE_PRIVATE_IMPORTS

    source_overages = _ratchet_overages(
        Counter(item.source for item in dependencies),
        source_baseline,
    )
    target_overages = _ratchet_overages(
        Counter(item.target for item in dependencies),
        target_baseline,
    )

    assert source_overages == [], "private imports increased in source modules:\n" + "\n".join(source_overages)
    assert target_overages == [], "private imports increased against target modules:\n" + "\n".join(target_overages)
