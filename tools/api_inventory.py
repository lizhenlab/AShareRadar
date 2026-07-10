from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = ROOT / "app" / "api" / "routes"
OUTPUT = ROOT / "docs" / "API_REFERENCE.md"
METHODS = {"get", "post", "patch", "delete", "put"}


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    handler: str
    response_model: str
    file: str
    inputs: tuple[str, ...] = ()


MISSING_DEFAULT = object()
CLIENT_SKIP_ANNOTATIONS = {"DataHub", "Settings", "Request"}
CLIENT_SKIP_NAMES = {"datahub", "settings", "request"}
FASTAPI_PARAMETER_FACTORIES = {"Query", "Path", "Body"}
FASTAPI_CONSTRAINT_KEYWORDS = (
    "description",
    "ge",
    "gt",
    "le",
    "lt",
    "min_length",
    "max_length",
    "pattern",
    "regex",
)
STOCK_GET_INPUTS = ("query `symbol: str = '600519'` (description=6位A股代码)",)


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    endpoints = collect_endpoints()
    OUTPUT.write_text(render(endpoints), encoding="utf-8")


def collect_endpoints() -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    for path in sorted(ROUTES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(ROOT))
        endpoints.extend(route_decorator_endpoints(tree, rel))
        endpoints.extend(stock_registry_endpoints(tree, rel))
    return sorted(endpoints, key=lambda item: (item.path, item.method))


def route_decorator_endpoints(tree: ast.Module, rel: str) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            parsed = parse_route_decorator(decorator)
            if parsed is None:
                continue
            method, path, response_model = parsed
            endpoints.append(Endpoint(method, path, node.name, response_model, rel, endpoint_inputs(node, path)))
    return endpoints


def parse_route_decorator(node: ast.expr) -> tuple[str, str, str] | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in METHODS:
        return None
    if not isinstance(node.func.value, ast.Name) or node.func.value.id not in {"router", "app"}:
        return None
    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
        return None
    response_model = "-"
    for keyword in node.keywords:
        if keyword.arg == "response_model":
            response_model = ast.unparse(keyword.value)
            break
    return node.func.attr.upper(), node.args[0].value, response_model


def stock_registry_endpoints(tree: ast.Module, rel: str) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        if not isinstance(node.iter, ast.List):
            continue
        for item in node.iter.elts:
            if not isinstance(item, ast.Tuple) or len(item.elts) < 3:
                continue
            path_node, model_node, handler_node = item.elts[:3]
            if not isinstance(path_node, ast.Constant) or not isinstance(path_node.value, str):
                continue
            endpoints.append(
                Endpoint(
                    "GET",
                    path_node.value,
                    ast.unparse(handler_node),
                    ast.unparse(model_node),
                    rel,
                    STOCK_GET_INPUTS,
                )
            )
    return endpoints


def endpoint_inputs(node: ast.FunctionDef | ast.AsyncFunctionDef, path: str) -> tuple[str, ...]:
    inputs: list[str] = []
    args = list(node.args.posonlyargs) + list(node.args.args)
    defaults: list[ast.expr | object] = [MISSING_DEFAULT] * (len(args) - len(node.args.defaults)) + list(node.args.defaults)
    for arg, default in zip(args, defaults):
        formatted = endpoint_input(arg, default, path)
        if formatted:
            inputs.append(formatted)
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        formatted = endpoint_input(arg, default if default is not None else MISSING_DEFAULT, path)
        if formatted:
            inputs.append(formatted)
    return tuple(inputs)


def endpoint_input(arg: ast.arg, default: ast.expr | object, path: str) -> str | None:
    name = arg.arg
    annotation = annotation_text(arg)
    if should_skip_client_input(name, annotation, default):
        return None
    location = input_location(name, annotation, default, path)
    default_text = parameter_default_text(default)
    details = parameter_details(default)
    if location == "body":
        default_text = "-"
    if location in {"body", "path"} and default_text == "-":
        value = f"{name}: {annotation}"
    else:
        value = f"{name}: {annotation} = {default_text}"
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"{location} `{value}`{suffix}"


def annotation_text(arg: ast.arg) -> str:
    return ast.unparse(arg.annotation) if arg.annotation is not None else "Any"


def should_skip_client_input(name: str, annotation: str, default: ast.expr | object) -> bool:
    if name in CLIENT_SKIP_NAMES or annotation in CLIENT_SKIP_ANNOTATIONS:
        return True
    return is_fastapi_call(default, "Depends")


def input_location(name: str, annotation: str, default: ast.expr | object, path: str) -> str:
    if f"{{{name}}}" in path:
        return "path"
    if is_fastapi_call(default, "Query"):
        return "query"
    if is_fastapi_call(default, "Path"):
        return "path"
    if is_fastapi_call(default, "Body"):
        return "body"
    if default is MISSING_DEFAULT and not primitive_annotation(annotation):
        return "body"
    return "query"


def primitive_annotation(annotation: str) -> bool:
    normalized = annotation.replace(" ", "")
    primitive_names = {"str", "int", "float", "bool", "None", "Any"}
    return all(part in primitive_names for part in normalized.replace("|", ",").split(","))


def is_fastapi_call(node: ast.expr | object, name: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == name
    if isinstance(func, ast.Attribute):
        return func.attr == name
    return False


def parameter_default_text(default: ast.expr | object) -> str:
    if default is MISSING_DEFAULT:
        return "-"
    if isinstance(default, ast.Call) and fastapi_parameter_factory(default):
        if default.args:
            return ast.unparse(default.args[0])
        for keyword in default.keywords:
            if keyword.arg == "default":
                return ast.unparse(keyword.value)
        return "-"
    if isinstance(default, ast.expr):
        return ast.unparse(default)
    return "-"


def fastapi_parameter_factory(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id in FASTAPI_PARAMETER_FACTORIES:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in FASTAPI_PARAMETER_FACTORIES:
        return func.attr
    return None


def parameter_details(default: ast.expr | object) -> tuple[str, ...]:
    if not isinstance(default, ast.Call) or not fastapi_parameter_factory(default):
        return ()
    details: list[str] = []
    for keyword in default.keywords:
        if keyword.arg in FASTAPI_CONSTRAINT_KEYWORDS:
            details.append(f"{keyword.arg}={clean_literal(keyword.value)}")
    return tuple(details)


def clean_literal(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ast.unparse(node)


def render(endpoints: list[Endpoint]) -> str:
    lines = [
        "# API Reference",
        "",
        "This file is generated by `$PYTHON tools/api_inventory.py` from FastAPI route declarations.",
        "Dynamic stock endpoints registered in `app/api/routes/stock.py` are included from the route registry list.",
        "The UI root route `/` is served from `app/main.py` and intentionally excluded from this business API reference.",
        "",
        "## Summary",
        "",
        f"Total endpoints: {len(endpoints)}",
        "",
        "| Method | Path | Inputs | Handler | Response model | File |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for endpoint in endpoints:
        lines.append(
            f"| {endpoint.method} | `{endpoint.path}` | {input_cell(endpoint.inputs)} | `{endpoint.handler}` | `{escape(endpoint.response_model)}` | `{endpoint.file}` |"
        )
    lines.extend(
        [
            "",
            "## Error Contract",
            "",
            "- `400`: domain validation errors from routes or workflows, including malformed stock symbols or unsupported intervals.",
            "- `404`: not-found responses for local user-state records or confirmed missing stocks.",
            "- `422`: FastAPI/Pydantic request-shape validation before route logic runs.",
            "- `503`: provider, runtime, scheduler, or SQLite failures mapped through `app/api/errors.py`.",
            "- `GET /api/stream/quotes` returns `text/event-stream`; normal frames contain JSON quote arrays and `quote-error` frames contain `{ \"message\": \"...\" }`.",
            "",
            "## API Design Notes",
            "",
            "- Route handlers should stay thin: validate parameters, call workflow/service functions, and return response models.",
            "- All user-facing failures should pass through `app/api/errors.py` or explicit `HTTPException` with Chinese messages.",
            "- New endpoints should be added to the relevant route module and this file should be regenerated.",
            "- Prefer `GET /api/stock/workbench` for frontend workbench loading to avoid repeated provider calls.",
            "",
        ]
    )
    return "\n".join(lines)


def input_cell(inputs: tuple[str, ...]) -> str:
    if not inputs:
        return "-"
    return "<br>".join(escape(item) for item in inputs)


def escape(value: str) -> str:
    return value.replace("|", "\\|")


if __name__ == "__main__":
    main()
