from __future__ import annotations

import ast
import re

from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
import pytest

from app.api.routes import market_scan
from tools import api_inventory


@pytest.mark.parametrize("suffix", ["results", "export.xlsx"])
def test_inventory_matches_real_market_scan_dependency_parameters(suffix: str) -> None:
    path = f"/api/market-scans/{{run_id}}/{suffix}"
    route = next(route for route in market_scan.router.routes if isinstance(route, APIRoute) and route.path == path)
    operation = get_openapi(title="Inventory contract", version="1", routes=[route])["paths"][path]["get"]
    expected = {(item["in"], item["name"]): item for item in operation["parameters"]}
    endpoint = next(item for item in api_inventory.collect_endpoints() if item.path == path and item.method == "GET")
    documented = {}
    for value in endpoint.inputs:
        match = re.match(r"^(query|path) `([a-z_]+):", value)
        assert match is not None, value
        key = match.group(1), match.group(2)
        assert key not in documented, f"Repeated public parameter: {key}"
        documented[key] = value

    assert documented.keys() == expected.keys()
    constraints = {"minimum": "ge", "maximum": "le", "minLength": "min_length", "maxLength": "max_length", "pattern": "pattern"}
    for key, parameter in expected.items():
        schema = parameter["schema"]
        branches = schema.get("anyOf", [schema])
        for branch in branches:
            for field, label in constraints.items():
                if field in branch:
                    assert f"{label}={branch[field]}" in documented[key]
        if "default" in schema:
            assert f"= {schema['default']!r}`" in documented[key]
    assert "list[MarketCode]" in documented["query", "market"]
    assert "list[MarketScanSort]" in documented["query", "sort"]
    assert {"filters", "scanner", "admission", "response"}.isdisjoint(name for _, name in documented)


def test_inventory_expands_local_dependency_diamonds_and_cycles_once() -> None:
    tree = ast.parse('''
from unavailable_module import external_service
router = APIRouter(prefix="/api")

def common(keyword: str = Query("", max_length=80), *, offset: int = Query(0, ge=0)):
    raise AssertionError("Dependency bodies must never run")

def left(shared=Depends(common), peer=Depends(right), repository=Depends(external_service)):
    return None

async def right(shared=Depends(dependency=common), peer=Depends(left), *, threshold: float = Query(0.5, ge=0, le=1)):
    return None

@router.get("/items/{item_id}")
def items(item_id: int, keyword: str = Query("", max_length=80), first=Depends(left), second=Depends(right, use_cache=False)):
    return None
''')

    endpoint, = api_inventory.route_decorator_endpoints(tree, "fixture.py")

    assert len(endpoint.inputs) == 4
    assert set(endpoint.inputs) == {
        "path `item_id: int`",
        "query `keyword: str = ''` (max_length=80)",
        "query `offset: int = 0` (ge=0)",
        "query `threshold: float = 0.5` (ge=0; le=1)",
    }


def test_dependency_walk_is_bounded_by_local_declarations_without_imports(tmp_path, monkeypatch) -> None:
    source = tmp_path / "routes"
    source.mkdir()
    declarations = [
        'raise AssertionError("Importing this module would create runtime resources")',
        'from nonexistent_provider import get_service',
        'router = APIRouter()',
        'def dependency_0(value: str = Query("", max_length=32), peer=Depends(dependency_1099)): pass',
    ]
    declarations.extend(f"def dependency_{index}(previous=Depends(dependency_{index - 1})): pass" for index in range(1, 1100))
    declarations.extend([
        '@router.get("/api/static")',
        'def static_route(parameters=Depends(dependency_1099), service=Depends(get_service)): pass',
    ])
    (source / "static.py").write_text("\n".join(declarations), encoding="utf-8")
    monkeypatch.setattr(api_inventory, "ROOT", tmp_path)
    monkeypatch.setattr(api_inventory, "ROUTES_DIR", source)

    endpoint, = api_inventory.collect_endpoints()

    assert endpoint.inputs == ("query `value: str = ''` (max_length=32)",)


def test_external_factories_and_local_service_objects_are_not_client_inputs() -> None:
    tree = ast.parse('''
router = APIRouter()
def local_service(request: Request, response: Response, store: Repository, settings: Settings, token=Depends(external_token)):
    return Repository()
def query_fields(required: int, /, optional: str = "all", *, bounded: list[str] = Query(None, max_length=3)):
    return None
@router.get("/api/local")
def endpoint(local=Depends(local_service), public=Depends(query_fields), external=Depends(provider.service), dynamic=Depends(factory()), anonymous=Depends(lambda: None), inferred: Service = Depends()):
    return None
''')

    endpoint, = api_inventory.route_decorator_endpoints(tree, "fixture.py")

    assert endpoint.inputs == (
        "query `required: int = -`",
        "query `optional: str = 'all'`",
        "query `bounded: list[str] = None` (max_length=3)",
    )


def test_dependency_path_constraints_preserve_separately_registered_handlers() -> None:
    tree = ast.parse('''
router = APIRouter()
def lookup(item_id: int = Path(..., ge=1)):
    return None
@router.get("/api/first/{item_id}")
def endpoint(value=Depends(lookup)):
    return None
@router.get("/api/second/{item_id}")
def endpoint(value=Depends(lookup)):
    return None
''')

    endpoints = api_inventory.route_decorator_endpoints(tree, "fixture.py")

    assert {endpoint.path for endpoint in endpoints} == {"/api/first/{item_id}", "/api/second/{item_id}"}
    assert all(endpoint.inputs == ("path `item_id: int = ...` (ge=1)",) for endpoint in endpoints)


def test_error_contract_includes_actual_revision_and_domain_admission_failures() -> None:
    from fastapi import HTTPException

    from app.api.errors import _api_exception
    from app.repositories.advice_reviews import AdviceReviewRevisionConflictError

    rendered = api_inventory.render([])
    revision_error = _api_exception(AdviceReviewRevisionConflictError("stale revision"))
    with pytest.raises(HTTPException) as admission_error:
        market_scan._validated_probability_horizon(2)

    assert f"`{revision_error.status_code}`: revision conflicts" in rendered
    assert f"`{admission_error.value.status_code}`: invalid request shapes, query constraints, or explicit domain admission checks" in rendered
    assert "`403`: untrusted API hosts or browser origins" in rendered
    assert "before route logic runs" not in rendered
    assert "same-module Depends declarations" in rendered
    assert "actual API request validation remains authoritative" in rendered
