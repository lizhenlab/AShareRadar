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
    VALIDATION_MESSAGE_RULES,
    _validation_message,
    internal_validation_exception_handler,
    run_api,
    run_sync_api,
    validation_exception_handler,
)


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
