from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
import re
import sqlite3
import sys
from urllib.parse import urlsplit, urlunsplit

from app.models.market_scan import (
    MarketScanPublicationDiagnostics,
    MarketScanPublicationSummary,
    MarketScanRun,
    MarketScanRunStatus,
    MarketScanScoreDistribution,
)
from app.services.datahub_runtime import run_cache_io
from app.services.market_scan_publication_decision import (
    MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS,
    MARKET_SCAN_PUBLISH_MIN_COVERAGE,
    MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO,
    MARKET_SCAN_SCORE_DISTRIBUTION_POLICY,
    assess_market_scan_score_distribution,
    completion_diagnostics,
    completion_status,
    publication_blockers,
)
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
        validate_before_commit: Callable[[], None] | None = None,
    ) -> bool:
        score_distribution = await self._score_distribution(run)
        status, message = completion_status(
            run,
            degraded_count,
            publication_summary=publication_summary,
            score_distribution=score_distribution,
        )
        diagnostics = completion_diagnostics(
            run,
            message,
            warnings=warnings,
            publication_summary=publication_summary,
            score_distribution=score_distribution,
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
                score_distribution=score_distribution,
            ),
            publication_diagnostics=diagnostics,
            validate_before_commit=validate_before_commit,
        )

    async def _score_distribution(self, run: MarketScanRun) -> MarketScanScoreDistribution | None:
        policy = MARKET_SCAN_SCORE_DISTRIBUTION_POLICY
        if run.success_count < policy.minimum_sample_count:
            return None
        score_reader = getattr(self._cache, "market_scan_success_raw_scores", None)
        if callable(score_reader):
            raw_scores = await run_cache_io(score_reader, run.id)
            return MarketScanScoreDistribution.from_raw_scores(
                raw_scores,
                expected_count=run.success_count,
                policy=policy,
            )
        results = getattr(self._cache, "market_scan_results", None)
        if not callable(results):
            return MarketScanScoreDistribution.from_raw_scores(
                (),
                expected_count=run.success_count,
                policy=policy,
            )
        page = await run_cache_io(
            results,
            run.id,
            page=1,
            page_size=max(1, run.success_count),
            status="success",
            market=None,
            industry=None,
            is_st=None,
            is_new=None,
            min_data_quality_score=None,
            keyword=None,
            sort="raw_score",
            order="desc",
        )
        items = getattr(page, "items", ())
        return MarketScanScoreDistribution.from_raw_scores(
            (getattr(item, "raw_score", None) for item in items),
            expected_count=run.success_count,
            policy=policy,
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
        publication_diagnostics: MarketScanPublicationDiagnostics | None = None,
        validate_before_commit: Callable[[], None] | None = None,
    ) -> bool:
        for attempt in range(1, TERMINAL_WRITE_MAX_ATTEMPTS + 1):
            try:
                finish_run = getattr(self._cache, "finish_market_scan_run")
                kwargs: dict[str, object] = {
                    "message": message,
                    "error": error,
                    "publication_diagnostics": publication_diagnostics,
                }
                if validate_before_commit is not None:
                    kwargs["validate_before_commit"] = validate_before_commit
                await run_cache_io(finish_run, run_id, status, **kwargs)
            except MarketScanPublicationValidationError:
                raise
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


class MarketScanPublicationValidationError(RuntimeError):
    """A fresh temporal publication check rejected a success/degraded commit."""


def terminal_diagnostic(
    run: MarketScanRun,
    status: MarketScanRunStatus,
    degraded_count: int,
    warnings: tuple[str, ...],
    *,
    publication_summary: MarketScanPublicationSummary | None = None,
    score_distribution: MarketScanScoreDistribution | None = None,
) -> str | None:
    details = list(warnings[:3])
    if publication_summary is not None:
        details.extend(publication_blockers(publication_summary))
    if score_distribution is not None:
        assessment = assess_market_scan_score_distribution(score_distribution)
        if assessment.status in {"failed", "degraded"}:
            details.extend(assessment.reasons)
            details.append(score_distribution.audit_text())
    if run.stock_pool_source == "stale-fallback":
        details.append("股票池使用本地缓存（stale-fallback）")
    if degraded_count:
        details.append(f"{degraded_count} 只股票使用备用数据或元数据不完整")
    if run.missing_count or run.skipped_count:
        details.append(f"逐股结果含缺失 {run.missing_count}、跳过 {run.skipped_count}")
    if status == "failed" and not details:
        details.append("没有生成有效排名")
    return "；".join(dict.fromkeys(details))[:800] or None


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
    "MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO",
    "MARKET_SCAN_SCORE_DISTRIBUTION_POLICY",
    "MarketScanFinalizer",
    "MarketScanPublicationValidationError",
    "assess_market_scan_score_distribution",
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
