from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import TypeVar

from anyio import to_thread
from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError
from starlette.requests import Request

from app.db.market_scan_integrity import MarketScanSnapshotSealError
from app.models.market_scan_snapshot import MarketScanSnapshotIntegrityError
from app.repositories.strategy_evidence import StrategyEvidenceIntegrityError
from app.repositories.advice_reviews import AdviceReviewIntegrityError, AdviceReviewRevisionConflictError
from app.repositories.strategy_automation import StrategyAutomationIntegrityError
from app.repositories.strategy_execution import StrategyExecutionIntegrityError
from app.services.market_scan_future_range_artifact import FutureRangeArtifactError
from app.services.market_scan_probability_artifact import ProbabilityArtifactError
from app.services.market_scan_probability_outcomes import ProbabilityOutcomeError
from app.services.market_scan_probability_source import ProbabilitySourceError
from app.services.workbench_context import WorkbenchContextIntegrityError
from app.utils.provider_errors import sanitize_provider_error
from app.utils.errors import NotFoundError


T = TypeVar("T")
LOGGER = logging.getLogger(__name__)
INTERNAL_VALIDATION_DETAIL = "内部数据格式异常，当前数据暂不可用"
ARTIFACT_INTEGRITY_DETAIL = "研究 artifact 完整性校验失败，已拒绝读取"
WORKBENCH_INTEGRITY_DETAIL = "个股研究上下文完整性校验失败，已拒绝读取"
ADVICE_REVIEW_INTEGRITY_DETAIL = "复盘账本完整性校验失败，已拒绝读取"
STRATEGY_EXECUTION_INTEGRITY_DETAIL = "策略执行账本完整性校验失败，已拒绝读取"
MARKET_SCAN_INTEGRITY_DETAIL = "全市场冻结快照完整性校验失败，已拒绝读取"
MARKET_SCAN_BUSY_DETAIL = "全市场冻结快照正在校验，请稍后重试"
MARKET_SCAN_BUSY_RETRY_SECONDS = 2
NO_STORE_HEADER = {"Cache-Control": "no-store"}
NO_STORE_INTERNAL_DETAIL = "个股研究暂不可用，请稍后重试"
SENSITIVE_INDIVIDUAL_PATHS = frozenset(
    {
        "/api/analyze",
        "/api/review",
        "/api/stock/workbench",
        "/api/stock/upside-probability",
    }
)
SENSITIVE_RESEARCH_PATH_PREFIXES = (
    "/api/reviews",
    "/api/advice/history",
    "/api/advice/timeline",
    "/api/paper-trading",
    "/api/market-scans",
    "/api/discovery",
    "/api/strategy-lab",
)


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
        return await to_thread.run_sync(call, abandon_on_cancel=False)
    except (NotFoundError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        raise _api_exception(exc) from exc


class MarketScanHeavyReadBusy(HTTPException):
    """Trusted fail-closed response for a saturated snapshot verifier."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail=MARKET_SCAN_BUSY_DETAIL,
            headers={
                "Cache-Control": "no-store",
                "Retry-After": str(MARKET_SCAN_BUSY_RETRY_SECONDS),
            },
        )


def artifact_integrity_guard(call: Callable[[], T]) -> T:
    """Map every persisted-research integrity failure to one non-disclosing 409."""

    try:
        return call()
    except MarketScanSnapshotSealError as exc:
        raise HTTPException(
            status_code=409,
            detail=MARKET_SCAN_INTEGRITY_DETAIL,
            headers=NO_STORE_HEADER,
        ) from exc
    except (
        FutureRangeArtifactError,
        MarketScanSnapshotIntegrityError,
        ProbabilityArtifactError,
        ProbabilityOutcomeError,
        ProbabilitySourceError,
        StrategyAutomationIntegrityError,
        StrategyEvidenceIntegrityError,
    ) as exc:
        raise HTTPException(status_code=409, detail=ARTIFACT_INTEGRITY_DETAIL) from exc


def no_store_http_exception(exc: HTTPException) -> HTTPException:
    """Preserve the no-store boundary when FastAPI replaces a route response."""

    if isinstance(exc, MarketScanHeavyReadBusy):
        return MarketScanHeavyReadBusy()
    if exc.status_code >= 500:
        return no_store_internal_exception(exc)
    headers = dict(exc.headers or {})
    headers.update(NO_STORE_HEADER)
    return HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers)


def no_store_internal_exception(exc: Exception) -> HTTPException:
    """Return a generic non-cacheable failure without exposing internal details."""

    LOGGER.error(
        "Unexpected individual-research API failure",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return HTTPException(
        status_code=503,
        detail=NO_STORE_INTERNAL_DETAIL,
        headers=NO_STORE_HEADER,
    )


def _api_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, MarketScanSnapshotSealError):
        return HTTPException(
            status_code=409,
            detail=MARKET_SCAN_INTEGRITY_DETAIL,
            headers=NO_STORE_HEADER,
        )
    if isinstance(exc, AdviceReviewRevisionConflictError):
        return HTTPException(status_code=409, detail="复盘计划已更新，请刷新后重试")
    if isinstance(exc, AdviceReviewIntegrityError):
        return HTTPException(status_code=409, detail=ADVICE_REVIEW_INTEGRITY_DETAIL)
    if isinstance(exc, StrategyExecutionIntegrityError):
        return HTTPException(status_code=409, detail=STRATEGY_EXECUTION_INTEGRITY_DETAIL)
    if isinstance(exc, WorkbenchContextIntegrityError):
        return HTTPException(status_code=409, detail=WORKBENCH_INTEGRITY_DETAIL)
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
    sensitive = _is_sensitive_research_path(_request_path(request))
    return JSONResponse(
        status_code=422,
        content={"detail": "；".join(details) or "输入参数不合法"},
        headers=NO_STORE_HEADER if sensitive else None,
    )


async def internal_validation_exception_handler(request: Request, exc: ValidationError) -> JSONResponse:
    _log_internal_validation_error(exc)
    sensitive = _is_sensitive_research_path(_request_path(request))
    return JSONResponse(
        status_code=503,
        content={"detail": NO_STORE_INTERNAL_DETAIL if sensitive else INTERNAL_VALIDATION_DETAIL},
        headers=NO_STORE_HEADER if sensitive else None,
    )


async def response_validation_exception_handler(
    request: Request,
    exc: ResponseValidationError,
) -> JSONResponse:
    """Never expose or cache malformed responses from sensitive individual-research APIs."""

    LOGGER.error(
        "API response model validation failed",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    sensitive = _is_sensitive_research_path(_request_path(request))
    return JSONResponse(
        status_code=503,
        content={"detail": NO_STORE_INTERNAL_DETAIL if sensitive else INTERNAL_VALIDATION_DETAIL},
        headers=NO_STORE_HEADER if sensitive else None,
    )


async def sensitive_individual_http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> Response:
    """Apply the sensitive no-store boundary before route code can run."""

    if not _is_sensitive_research_path(_request_path(request)):
        return await http_exception_handler(request, exc)
    return await http_exception_handler(request, no_store_http_exception(exc))


async def sensitive_individual_exception_handler(
    request: Request,
    exc: Exception,
) -> Response:
    """Protect sensitive individual endpoints even when dependencies fail before route entry."""

    if not _is_sensitive_research_path(_request_path(request)):
        return PlainTextResponse("Internal Server Error", status_code=500)
    LOGGER.error(
        "Unexpected individual-research dependency or transport failure",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=503,
        content={"detail": NO_STORE_INTERNAL_DETAIL},
        headers=NO_STORE_HEADER,
    )


def _log_internal_validation_error(exc: ValidationError) -> None:
    LOGGER.error(
        "Internal model validation failed",
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def _request_path(request: object) -> str:
    return str(getattr(getattr(request, "url", None), "path", ""))


def _is_sensitive_research_path(path: str) -> bool:
    return path in SENSITIVE_INDIVIDUAL_PATHS or any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in SENSITIVE_RESEARCH_PATH_PREFIXES
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
