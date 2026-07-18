from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import TypeVar

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from app.services.provider_errors import sanitize_provider_error
from app.utils.errors import NotFoundError


T = TypeVar("T")
LOGGER = logging.getLogger(__name__)
INTERNAL_VALIDATION_DETAIL = "内部数据格式异常，当前数据暂不可用"


@dataclass(frozen=True)
class ValidationMessageRule:
    name: str
    kinds: frozenset[str]
    message: Callable[[dict], str]

    def matches(self, kind: str) -> bool:
        return kind in self.kinds


async def run_api(call: Callable[[], Awaitable[T]]) -> T:
    try:
        return await call()
    except (NotFoundError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        raise _api_exception(exc) from exc


def run_sync_api(call: Callable[[], T]) -> T:
    try:
        return call()
    except (NotFoundError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        raise _api_exception(exc) from exc


async def run_sync_api_async(call: Callable[[], T]) -> T:
    try:
        return await run_in_threadpool(call)
    except (NotFoundError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        raise _api_exception(exc) from exc


def _api_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=sanitize_provider_error(exc))
    if isinstance(exc, ValidationError):
        _log_internal_validation_error(exc)
        return HTTPException(status_code=503, detail=INTERNAL_VALIDATION_DETAIL)
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=sanitize_provider_error(exc))
    if isinstance(exc, sqlite3.DatabaseError):
        return HTTPException(status_code=503, detail=f"本地数据库暂不可用：{sanitize_provider_error(exc)}")
    return HTTPException(status_code=503, detail=sanitize_provider_error(exc))


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = []
    for error in exc.errors():
        loc = " / ".join(str(item) for item in error.get("loc", []) if item != "query")
        msg = _validation_message(error)
        details.append(f"{loc}: {msg}" if loc else str(msg))
    return JSONResponse(status_code=422, content={"detail": "；".join(details) or "输入参数不合法"})


async def internal_validation_exception_handler(request: Request, exc: ValidationError) -> JSONResponse:
    _log_internal_validation_error(exc)
    return JSONResponse(status_code=503, content={"detail": INTERNAL_VALIDATION_DETAIL})


def _log_internal_validation_error(exc: ValidationError) -> None:
    LOGGER.error(
        "Internal model validation failed",
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def _validation_message(error: dict) -> str:
    kind = str(error.get("type") or "")
    for rule in VALIDATION_MESSAGE_RULES:
        if rule.matches(kind):
            return rule.message(error)
    return _fallback_validation_message(error)


def _validation_ctx(error: dict) -> dict:
    ctx = error.get("ctx") or {}
    return ctx if isinstance(ctx, dict) else {}


def _ctx_value_message(key: str, template: str) -> Callable[[dict], str]:
    return lambda error: template.format(value=_validation_ctx(error).get(key))


def _constant_validation_message(message: str) -> Callable[[dict], str]:
    return lambda _error: message


def _fallback_validation_message(error: dict) -> str:
    return str(error.get("msg") or "输入参数不合法")


VALIDATION_MESSAGE_RULES = (
    ValidationMessageRule("less_than_equal", frozenset({"less_than_equal"}), _ctx_value_message("le", "应小于等于 {value}")),
    ValidationMessageRule("greater_than_equal", frozenset({"greater_than_equal"}), _ctx_value_message("ge", "应大于等于 {value}")),
    ValidationMessageRule("string_too_short", frozenset({"string_too_short"}), _ctx_value_message("min_length", "长度不能少于 {value} 个字符")),
    ValidationMessageRule("string_too_long", frozenset({"string_too_long"}), _ctx_value_message("max_length", "长度不能超过 {value} 个字符")),
    ValidationMessageRule("number_parsing", frozenset({"float_parsing", "int_parsing"}), _constant_validation_message("应为有效数字")),
    ValidationMessageRule("number_type", frozenset({"float_type", "int_type", "finite_number"}), _constant_validation_message("应为有效数字")),
    ValidationMessageRule("string_type", frozenset({"string_type"}), _constant_validation_message("应为文本")),
    ValidationMessageRule("bool_parsing", frozenset({"bool_parsing"}), _constant_validation_message("应为布尔值")),
    ValidationMessageRule("bool_type", frozenset({"bool_type"}), _constant_validation_message("应为布尔值")),
    ValidationMessageRule("list_type", frozenset({"list_type"}), _constant_validation_message("应为列表")),
    ValidationMessageRule("dict_type", frozenset({"dict_type", "model_type"}), _constant_validation_message("应为对象")),
    ValidationMessageRule("missing", frozenset({"missing"}), _constant_validation_message("缺少必填字段")),
    ValidationMessageRule("extra_forbidden", frozenset({"extra_forbidden"}), _constant_validation_message("不支持的字段")),
)
