from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from app.api.errors import (
    INTERNAL_VALIDATION_DETAIL,
    MARKET_SCAN_INTEGRITY_DETAIL,
    NO_STORE_INTERNAL_DETAIL,
    WORKBENCH_INTEGRITY_DETAIL,
    VALIDATION_MESSAGE_RULES,
    _validation_message,
    internal_validation_exception_handler,
    no_store_internal_exception,
    sensitive_individual_exception_handler,
    response_validation_exception_handler,
    run_api,
    run_sync_api,
    validation_exception_handler,
    sensitive_individual_http_exception_handler,
)
from app.db.market_scan_integrity import MarketScanSnapshotSealError
from fastapi.exceptions import ResponseValidationError
from app.services.workbench_context import WorkbenchContextIntegrityError
from app.repositories.advice_reviews import AdviceReviewRevisionConflictError


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ({"type": "less_than_equal", "ctx": {"le": 100}}, "应小于等于 100"),
        ({"type": "greater_than_equal", "ctx": {"ge": 1}}, "应大于等于 1"),
        ({"type": "string_too_short", "ctx": {"min_length": 6}}, "长度不能少于 6 个字符"),
        ({"type": "string_too_long", "ctx": {"max_length": 10}}, "长度不能超过 10 个字符"),
        ({"type": "float_parsing"}, "应为有效数字"),
        ({"type": "int_parsing"}, "应为有效数字"),
        ({"type": "float_type"}, "应为有效数字"),
        ({"type": "int_type"}, "应为有效数字"),
        ({"type": "finite_number"}, "应为有效数字"),
        ({"type": "string_type"}, "应为文本"),
        ({"type": "bool_parsing"}, "应为布尔值"),
        ({"type": "bool_type"}, "应为布尔值"),
        ({"type": "list_type"}, "应为列表"),
        ({"type": "dict_type"}, "应为对象"),
        ({"type": "model_type"}, "应为对象"),
        ({"type": "missing"}, "缺少必填字段"),
        ({"type": "extra_forbidden"}, "不支持的字段"),
        ({"type": "unknown", "msg": "raw message"}, "raw message"),
        ({"type": "unknown"}, "输入参数不合法"),
    ],
)
def test_validation_message_rules_render_chinese_text(error: dict, message: str) -> None:
    assert _validation_message(error) == message


def test_validation_message_rule_order_is_explicit() -> None:
    assert [rule.name for rule in VALIDATION_MESSAGE_RULES] == [
        "less_than_equal",
        "greater_than_equal",
        "string_too_short",
        "string_too_long",
        "number_parsing",
        "number_type",
        "string_type",
        "bool_parsing",
        "bool_type",
        "list_type",
        "dict_type",
        "missing",
        "extra_forbidden",
    ]


def test_validation_exception_handler_joins_locations_and_messages() -> None:
    exc = SimpleNamespace(
        errors=lambda: [
            {"loc": ("query", "limit"), "type": "less_than_equal", "ctx": {"le": 100}},
            {"loc": ("body", "symbol"), "type": "string_too_short", "ctx": {"min_length": 6}},
        ]
    )

    response = asyncio.run(validation_exception_handler(SimpleNamespace(), exc))

    assert response.status_code == 422
    assert json.loads(response.body) == {"detail": "limit: 应小于等于 100；body / symbol: 长度不能少于 6 个字符"}


def test_run_sync_api_maps_sqlite_errors_to_service_unavailable() -> None:
    def load() -> object:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(HTTPException) as exc_info:
        run_sync_api(load)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "本地数据库暂不可用：database is locked"


def test_run_api_maps_sqlite_errors_to_service_unavailable() -> None:
    async def load() -> object:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(run_api(load))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "本地数据库暂不可用：database is locked"


def test_api_errors_redact_provider_credentials_before_returning_details() -> None:
    def load() -> object:
        raise RuntimeError("source down https://example.test/quote?api_key=secret-key&symbol=600519")

    with pytest.raises(HTTPException) as exc_info:
        run_sync_api(load)

    assert exc_info.value.status_code == 503
    assert "secret-key" not in exc_info.value.detail
    assert "api_key=<redacted>" in exc_info.value.detail


def test_snapshot_seal_error_is_generic_but_plain_value_error_keeps_bad_request_semantics() -> None:
    with pytest.raises(HTTPException) as seal_info:
        run_sync_api(
            lambda: (_ for _ in ()).throw(
                MarketScanSnapshotSealError("摘要 abc123 与 /private/market.db 不一致")
            )
        )
    with pytest.raises(HTTPException) as value_info:
        run_sync_api(lambda: (_ for _ in ()).throw(ValueError("普通业务输入无效")))

    assert seal_info.value.status_code == 409
    assert seal_info.value.detail == MARKET_SCAN_INTEGRITY_DETAIL
    assert seal_info.value.headers == {"Cache-Control": "no-store"}
    assert "abc123" not in seal_info.value.detail
    assert value_info.value.status_code == 400
    assert value_info.value.detail == "普通业务输入无效"


def test_workbench_context_integrity_error_is_generic_conflict() -> None:
    async def load() -> object:
        raise WorkbenchContextIntegrityError("内部错绑：600519.SH -> 000001.SZ")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(run_api(load))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == WORKBENCH_INTEGRITY_DETAIL
    assert "600519" not in exc_info.value.detail


def test_advice_review_revision_conflict_is_generic_conflict() -> None:
    with pytest.raises(HTTPException) as exc_info:
        run_sync_api(
            lambda: (_ for _ in ()).throw(
                AdviceReviewRevisionConflictError("期望修订 1，当前 99 /private/ledger.db")
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "复盘计划已更新，请刷新后重试"
    assert "99" not in exc_info.value.detail


@pytest.mark.parametrize(
    "path",
    [
        "/api/reviews",
        "/api/advice/timeline",
        "/api/paper-trading/run",
        "/api/market-scans/7/results",
        "/api/discovery/presets/7/apply",
        "/api/strategy-lab/executions",
    ],
)
def test_sensitive_review_validation_errors_are_no_store(path: str) -> None:
    exc = SimpleNamespace(errors=lambda: [{"loc": ("body", "value"), "type": "missing"}])
    request = SimpleNamespace(url=SimpleNamespace(path=path))

    response = asyncio.run(validation_exception_handler(request, exc))

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"


def test_no_store_internal_exception_is_generic_and_non_cacheable() -> None:
    exc = no_store_internal_exception(TimeoutError("primary timeout /private/secret.json"))

    assert exc.status_code == 503
    assert exc.detail == NO_STORE_INTERNAL_DETAIL
    assert exc.headers == {"Cache-Control": "no-store"}
    assert "secret" not in str(exc.detail)


class _InternalRow(BaseModel):
    amount: int


def _internal_validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as exc_info:
        _InternalRow(amount="dirty-private-value")
    return exc_info.value


def test_run_sync_api_maps_internal_model_validation_to_sanitized_service_unavailable() -> None:
    validation_error = _internal_validation_error()

    def load() -> object:
        raise validation_error

    with pytest.raises(HTTPException) as exc_info:
        run_sync_api(load)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == INTERNAL_VALIDATION_DETAIL
    assert "dirty-private-value" not in str(exc_info.value.detail)


def test_run_api_maps_internal_model_validation_to_sanitized_service_unavailable() -> None:
    validation_error = _internal_validation_error()

    async def load() -> object:
        raise validation_error

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(run_api(load))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == INTERNAL_VALIDATION_DETAIL


def test_internal_validation_handler_never_returns_model_details() -> None:
    validation_error = _internal_validation_error()

    response = asyncio.run(internal_validation_exception_handler(SimpleNamespace(), validation_error))

    assert response.status_code == 503
    assert json.loads(response.body) == {"detail": INTERNAL_VALIDATION_DETAIL}
    assert b"dirty-private-value" not in response.body


@pytest.mark.parametrize(
    "path",
    ["/api/analyze", "/api/market-scans/latest", "/api/discovery/presets", "/api/strategy-lab/executions/1"],
)
def test_sensitive_response_validation_is_generic_and_no_store(path: str) -> None:
    request = SimpleNamespace(url=SimpleNamespace(path=path))
    exc = ResponseValidationError(
        [{"type": "missing", "loc": ("response", "quote"), "msg": "secret response"}],
        body={"private": "/private/response-secret.json"},
    )

    response = asyncio.run(response_validation_exception_handler(request, exc))

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body) == {"detail": NO_STORE_INTERNAL_DETAIL}
    assert b"private" not in response.body


def test_generic_exception_handler_preserves_non_sensitive_error_behavior() -> None:
    error = RuntimeError("ordinary route failure")
    request = SimpleNamespace(url=SimpleNamespace(path="/api/health/live"))

    response = asyncio.run(sensitive_individual_exception_handler(request, error))

    assert response.status_code == 500
    assert response.body == b"Internal Server Error"
    assert response.headers["content-type"].startswith("text/plain")


def test_non_sensitive_http_exception_preserves_fastapi_default_semantics() -> None:
    request = SimpleNamespace(url=SimpleNamespace(path="/api/plates"))
    error = HTTPException(
        status_code=418,
        detail={"message": "safe teapot"},
        headers={"X-Test-Reason": "teapot"},
    )

    response = asyncio.run(sensitive_individual_http_exception_handler(request, error))

    assert response.status_code == 418
    assert json.loads(response.body) == {"detail": {"message": "safe teapot"}}
    assert response.headers["x-test-reason"] == "teapot"
    assert "cache-control" not in response.headers


def test_sensitive_arbitrary_service_unavailable_cannot_impersonate_market_scan_busy() -> None:
    request = SimpleNamespace(url=SimpleNamespace(path="/api/market-scans/latest"))
    error = HTTPException(
        status_code=503,
        detail="unsafe caller-controlled busy response",
        headers={"Retry-After": "999"},
    )

    response = asyncio.run(sensitive_individual_http_exception_handler(request, error))

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "retry-after" not in response.headers
    assert json.loads(response.body) == {"detail": NO_STORE_INTERNAL_DETAIL}
