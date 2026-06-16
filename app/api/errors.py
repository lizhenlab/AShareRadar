from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.requests import Request

from app.utils.errors import NotFoundError


T = TypeVar("T")


async def run_api(call: Callable[[], Awaitable[T]]) -> T:
    try:
        return await call()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def run_sync_api(call: Callable[[], T]) -> T:
    try:
        return call()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def validation_exception_handler(request: Request, exc: RequestValidationError | ValidationError) -> JSONResponse:
    details = []
    for error in exc.errors():
        loc = " / ".join(str(item) for item in error.get("loc", []) if item != "query")
        msg = _validation_message(error)
        details.append(f"{loc}: {msg}" if loc else str(msg))
    return JSONResponse(status_code=422, content={"detail": "；".join(details) or "输入参数不合法"})


def _validation_message(error: dict) -> str:
    kind = str(error.get("type") or "")
    ctx = error.get("ctx") or {}
    if kind == "less_than_equal":
        return f"应小于等于 {ctx.get('le')}"
    if kind == "greater_than_equal":
        return f"应大于等于 {ctx.get('ge')}"
    if kind == "string_too_short":
        return f"长度不能少于 {ctx.get('min_length')} 个字符"
    if kind == "string_too_long":
        return f"长度不能超过 {ctx.get('max_length')} 个字符"
    if kind in {"float_parsing", "int_parsing"}:
        return "应为有效数字"
    if kind == "bool_parsing":
        return "应为布尔值"
    if kind == "missing":
        return "缺少必填字段"
    return str(error.get("msg") or "输入参数不合法")
