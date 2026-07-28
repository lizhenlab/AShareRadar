from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
import re
import sqlite3
import sys
from urllib.parse import urlsplit, urlunsplit

from app.models.market_scan import (
    MarketScanPublicationSummary,
    MarketScanRun,
    MarketScanRunStatus,
)
from app.services.datahub_runtime import run_cache_io
from app.utils.provider_errors import sanitize_provider_error


_URL_RE = re.compile(r"https?://[^\s<>{}\"']+", re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = ".,;:!?)，。；！）"
_SENSITIVE_SETTING_MARKERS = (
    "api_key",
    "access_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "signature",
    "token",
)
TERMINAL_WRITE_MAX_ATTEMPTS = 3
TERMINAL_WRITE_RETRY_BASE_SECONDS = 0.05
TERMINAL_WRITE_RETRY_MAX_SECONDS = 0.2
MARKET_SCAN_BULK_QUOTE_MIN_SYMBOLS = 10
MARKET_SCAN_BULK_QUOTE_MIN_COVERAGE_RATIO = 0.8
MARKET_SCAN_PUBLISH_MIN_COVERAGE = {
    "ALL": 0.95,
    "SH": 0.95,
    "SZ": 0.95,
    "BJ": 0.95,
}
MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS = 15 * 60
_RETRYABLE_SQLITE_PRIMARY_CODES = {
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
}
_RETRYABLE_SQLITE_MESSAGES = (
    "database is busy",
    "database is locked",
    "database schema is locked",
    "database table is locked",
)


class MarketScanFinalizer:
    """Persist terminal scan state and make persistence failures observable."""

    def __init__(self, cache: object, *, sensitive_values: Iterable[object] = ()) -> None:
        self._cache = cache
        self._sensitive_values = tuple(value for value in sensitive_values if value is not None and value != "")

    async def finish_completed(
        self,
        run: MarketScanRun,
        *,
        degraded_count: int,
        warnings: tuple[str, ...],
        publication_summary: MarketScanPublicationSummary | None = None,
    ) -> bool:
        status, message = completion_status(
            run,
            degraded_count,
            publication_summary=publication_summary,
        )
        return await self.finish(
            run.id,
            status,
            message=message,
            error=terminal_diagnostic(
                run,
                status,
                degraded_count,
                warnings,
                publication_summary=publication_summary,
            ),
        )

    async def finish_cancelled(self, run_id: int) -> bool:
        return await self.finish(
            run_id,
            "cancelled",
            message="全市场扫描已取消，可从断点重试",
        )

    async def finish_interrupted(self, run_id: int) -> bool:
        return await self.finish(
            run_id,
            "interrupted",
            message="应用关闭中断扫描，可从断点重试",
            error="应用关闭时终止后台扫描任务",
        )

    async def finish_failed(self, run_id: int, exc: Exception) -> bool:
        error = short_scan_error(exc, sensitive_values=self._sensitive_values)
        return await self.finish(
            run_id,
            "failed",
            message=f"全市场扫描失败：{error}",
            error=error,
        )

    async def finish(
        self,
        run_id: int,
        status: MarketScanRunStatus,
        *,
        message: str,
        error: str | None = None,
    ) -> bool:
        for attempt in range(1, TERMINAL_WRITE_MAX_ATTEMPTS + 1):
            try:
                await run_cache_io(
                    getattr(self._cache, "finish_market_scan_run"),
                    run_id,
                    status,
                    message=message,
                    error=error,
                )
            except Exception as exc:
                if attempt < TERMINAL_WRITE_MAX_ATTEMPTS and is_retryable_sqlite_error(exc):
                    await asyncio.sleep(terminal_write_retry_delay(attempt))
                    continue
                report_terminal_persistence_failure(
                    run_id,
                    status,
                    exc,
                    sensitive_values=self._sensitive_values,
                )
                return False
            return True
        return False


def completion_status(
    run: MarketScanRun,
    degraded_count: int = 0,
    *,
    publication_summary: MarketScanPublicationSummary | None = None,
) -> tuple[MarketScanRunStatus, str]:
    blockers = publication_blockers(publication_summary) if publication_summary is not None else ()
    if blockers:
        return "failed", "全市场扫描未达到发布可信度：" + "；".join(blockers)
    if run.success_count == 0:
        return "failed", f"全市场扫描没有生成有效排名；缺失 {run.missing_count}，跳过 {run.skipped_count}"
    stale_stock_pool = run.stock_pool_source == "stale-fallback"
    if run.missing_count or run.skipped_count or run.processed_count < run.total_count or degraded_count or stale_stock_pool:
        degraded_details: list[str] = []
        if degraded_count:
            degraded_details.append(f"降级结果 {degraded_count}")
        if stale_stock_pool:
            degraded_details.append("股票池使用本地缓存")
        degraded_suffix = f"，{'，'.join(degraded_details)}" if degraded_details else ""
        return "degraded", (
            f"全市场扫描降级完成：成功 {run.success_count}/{run.total_count}，" f"缺失 {run.missing_count}，跳过 {run.skipped_count}{degraded_suffix}"
        )
    return "success", f"全市场扫描完成：成功 {run.success_count}/{run.total_count}"


def terminal_diagnostic(
    run: MarketScanRun,
    status: MarketScanRunStatus,
    degraded_count: int,
    warnings: tuple[str, ...],
    *,
    publication_summary: MarketScanPublicationSummary | None = None,
) -> str | None:
    details = list(warnings[:3])
    if publication_summary is not None:
        details.extend(publication_blockers(publication_summary))
    if run.stock_pool_source == "stale-fallback":
        details.append("股票池使用本地缓存（stale-fallback）")
    if degraded_count:
        details.append(f"{degraded_count} 只结果使用备用数据或缺少上市日期")
    if run.missing_count or run.skipped_count:
        details.append(f"逐股结果含缺失 {run.missing_count}、跳过 {run.skipped_count}")
    if status == "failed" and not details:
        details.append("没有生成有效排名")
    return "；".join(dict.fromkeys(details))[:800] or None


def publication_blockers(summary: MarketScanPublicationSummary) -> tuple[str, ...]:
    blockers: list[str] = []
    cluster = summary.systemic_stale_cluster
    if cluster is not None:
        markets = "/".join(cluster.markets) or "unknown"
        blockers.append(
            "系统性同日滞后："
            f"{cluster.data_date} 有 {cluster.count}/{cluster.total_count} 只，涉及 {markets}"
        )
    if summary.invalid_snapshot_timestamps:
        examples = "、".join(summary.invalid_snapshot_timestamps[:3])
        blockers.append(
            f"报价时间不可解析：{len(summary.invalid_snapshot_timestamps)} 个（{examples}）"
        )
    span = summary.snapshot_span_seconds
    if span is not None and span > MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS:
        blockers.append(
            f"报价快照跨度 {span:g} 秒超过 {MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS:g} 秒门槛"
        )
    for scope in ("ALL", "SH", "SZ", "BJ"):
        coverage = summary.coverage_for(scope)  # type: ignore[arg-type]
        threshold = MARKET_SCAN_PUBLISH_MIN_COVERAGE[scope]
        if coverage is not None and coverage.coverage_ratio >= threshold:
            continue
        total = coverage.total_count if coverage is not None else 0
        success = coverage.success_count if coverage is not None else 0
        ratio = coverage.coverage_ratio if coverage is not None else 0.0
        blockers.append(
            f"{scope} 发布覆盖不足：{success}/{total}（{ratio:.2%}，门槛 {threshold:.2%}）"
        )
    return tuple(blockers)


def short_scan_error(exc: Exception, *, sensitive_values: Iterable[object] = ()) -> str:
    sanitized = sanitize_terminal_error(exc, sensitive_values=sensitive_values)
    return " ".join(sanitized.split()).strip()[:300] or "未知错误"


def quote_batch_error(
    missing_count: int,
    provider_errors: tuple[str, ...],
    *,
    sensitive_values: Iterable[object] = (),
) -> str | None:
    details = tuple(dict.fromkeys(short_scan_error(RuntimeError(error), sensitive_values=sensitive_values) for error in provider_errors if str(error).strip()))
    if missing_count <= 0:
        return f"批量行情已由备用源补齐：{'；'.join(details[:2])}"[:300] if details else None
    suffix = f"：{'；'.join(details[:2])}" if details else ""
    return f"批量行情缺失 {missing_count} 只{suffix}"[:300]


def bulk_quote_coverage_error(returned_count: int, requested_count: int) -> str | None:
    if requested_count < MARKET_SCAN_BULK_QUOTE_MIN_SYMBOLS:
        return None
    if returned_count / requested_count >= MARKET_SCAN_BULK_QUOTE_MIN_COVERAGE_RATIO:
        return None
    return f"批量行情覆盖率异常：{returned_count}/{requested_count}"


def sensitive_setting_values(settings: object) -> tuple[object, ...]:
    model_dump = getattr(settings, "model_dump", None)
    if callable(model_dump):
        values = model_dump()
    else:
        values = vars(settings)
    if not isinstance(values, Mapping):
        return ()
    return tuple(
        value
        for name, value in values.items()
        if value is not None and value != "" and any(marker in str(name).lower() for marker in _SENSITIVE_SETTING_MARKERS)
    )


def is_retryable_sqlite_error(exc: Exception) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    error_code = getattr(exc, "sqlite_errorcode", None)
    if error_code is not None:
        try:
            if int(error_code) & 0xFF in _RETRYABLE_SQLITE_PRIMARY_CODES:
                return True
        except (TypeError, ValueError):
            pass
    message = str(exc).casefold()
    return any(marker in message for marker in _RETRYABLE_SQLITE_MESSAGES)


def terminal_write_retry_delay(attempt: int) -> float:
    return min(
        TERMINAL_WRITE_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)),
        TERMINAL_WRITE_RETRY_MAX_SECONDS,
    )


def sanitize_terminal_error(value: object, *, sensitive_values: Iterable[object] = ()) -> str:
    without_url_parameters = _URL_RE.sub(_strip_url_parameters, str(value))
    return sanitize_provider_error(without_url_parameters, sensitive_values=sensitive_values)


def report_terminal_persistence_failure(
    run_id: int,
    status: MarketScanRunStatus,
    exc: Exception,
    *,
    sensitive_values: Iterable[object] = (),
) -> None:
    error = short_scan_error(exc, sensitive_values=sensitive_values)
    line = "[AShareRadar][market-scan] terminal persistence failed " f"run_id={run_id} target_status={status} error_type={type(exc).__name__} error={error}"
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass


def _strip_url_parameters(match: re.Match[str]) -> str:
    raw = match.group(0)
    end = len(raw)
    while end > 0 and raw[end - 1] in _URL_TRAILING_PUNCTUATION:
        end -= 1
    url, suffix = raw[:end], raw[end:]
    try:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) + suffix
    except (TypeError, ValueError):
        return url.split("?", 1)[0].split("#", 1)[0] + suffix


__all__ = [
    "MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS",
    "MARKET_SCAN_PUBLISH_MIN_COVERAGE",
    "MarketScanFinalizer",
    "bulk_quote_coverage_error",
    "completion_status",
    "is_retryable_sqlite_error",
    "quote_batch_error",
    "publication_blockers",
    "report_terminal_persistence_failure",
    "sanitize_terminal_error",
    "sensitive_setting_values",
    "short_scan_error",
    "terminal_write_retry_delay",
    "terminal_diagnostic",
]
