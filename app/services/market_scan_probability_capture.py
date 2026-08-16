"""Published-run boundary for immutable probability source capture."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from app.models.market_scan import MarketScanResultPage, MarketScanRun, MarketScanRunPage
from app.services.datahub_runtime import run_cache_io
from app.services.market_scan_probability_source import (
    ProbabilitySourceError,
    capture_source_snapshot,
    is_current_writable_production_score_contract,
    is_registered_production_score_contract,
    list_probability_source_snapshots,
    project_probability_source_capture,
)
from app.models.market_scan_snapshot import validate_market_scan_run_binding
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.utils.clock import ASHARE_TIMEZONE, market_now, utc_now
from app.utils.provider_errors import sanitize_provider_error


PROBABILITY_SOURCE_MONITOR_CATEGORY = "research"
PROBABILITY_SOURCE_ARCHIVE_RELATIVE_PATH = Path("research/market_scan_probability_source")
_PUBLISHED_STATUSES = frozenset({"success", "degraded"})
ProbabilitySourceCaptureStatus = Literal["captured", "skipped", "failed"]
PROBABILITY_SOURCE_CAPTURE_LEASE_SECONDS = 30 * 60
PROBABILITY_SOURCE_CAPTURE_RETRY_BASE_SECONDS = 60
PROBABILITY_SOURCE_CAPTURE_RETRY_MAX_SECONDS = 60 * 60
PROBABILITY_SOURCE_CAPTURE_MAX_ATTEMPTS = 8
PROBABILITY_SOURCE_CAPTURE_BATCH_LIMIT = 64
LOGGER = logging.getLogger(__name__)


class ProbabilitySourceCaptureError(ProbabilitySourceError):
    """Raised when a persisted run is not a safe canonical capture source."""


class ProbabilitySourceCaptureIneligible(ProbabilitySourceCaptureError):
    """Raised for expected run types that are outside the source cohort."""


def capture_market_scan_probability_source(
    cache: object,
    run_id: int,
    *,
    directory: str | Path | None = None,
    captured_at: datetime | str | None = None,
) -> dict[str, object]:
    """Backfill or capture one canonical published run from the runtime cache."""
    run = _published_source_run(cache, run_id)
    _require_canonical_latest(cache, run)
    existing = _existing_capture(cache, run_id, directory=directory)
    if existing is not None:
        _require_existing_capture_binding(existing, run)
        _require_canonical_latest(cache, run)
        _require_run_unchanged(cache, run)
        return existing
    results = _complete_success_results(cache, run)
    try:
        projection = project_probability_source_capture(
            run,
            results,
            canonical_published=True,
        )
    except ProbabilitySourceError as exc:
        raise ProbabilitySourceCaptureError(
            f"run {run.id} 的已发布PIT输入无法确定性投影：{exc}"
        ) from exc
    _validate_publish_binding(cache, run)
    info = capture_source_snapshot(
        _archive_directory(cache, directory),
        run=cast(dict[str, object], projection["run"]),
        records=cast(list[dict[str, object]], projection["records"]),
        captured_at=_captured_at_text(captured_at),
        projection_receipt=projection,
        before_publish=lambda: None,
        database_path=Path(cast(Path, getattr(cache, "path"))),
    )
    _require_canonical_latest(cache, run)
    _require_run_unchanged(cache, run)
    return info


def audit_market_scan_probability_source_archives(cache: object) -> int:
    """Reconcile durable succeeded claims with verified on-disk archives."""

    directory = _archive_directory(cache, None)
    candidates = list_probability_source_snapshots(directory)
    newest: dict[int, tuple[float, str]] = {}
    for item in candidates:
        run_id = int(cast(int, item["run_id"]))
        captured_at = _aware_timestamp(str(item["captured_at"])).timestamp()
        digest = str(item["digest"])
        previous = newest.get(run_id)
        if previous is None or captured_at > previous[0]:
            newest[run_id] = captured_at, digest
        elif captured_at == previous[0] and digest != previous[1]:
            raise ProbabilitySourceCaptureError(f"run {run_id} 存在同 captured_at 的冲突 source archives")
    auditor = getattr(cache, "audit_probability_source_capture_archives", None)
    if not callable(auditor):
        return 0
    return int(auditor({run_id: value[1] for run_id, value in newest.items()}))


async def capture_market_scan_probability_source_best_effort(
    cache: object,
    run_id: int,
    *,
    directory: str | Path | None = None,
    captured_at: datetime | str | None = None,
    sensitive_values: Iterable[object] = (),
) -> dict[str, object]:
    """Capture after publication without ever downgrading the published run."""
    try:
        info = await run_cache_io(
            capture_market_scan_probability_source,
            cache,
            run_id,
            directory=directory,
            captured_at=captured_at,
        )
    except asyncio.CancelledError:
        raise
    except ProbabilitySourceCaptureIneligible as exc:
        return {
            "status": "skipped",
            "run_id": run_id,
            "message": " ".join(str(exc).split())[:600],
        }
    except Exception as exc:
        message = _failure_message(run_id, exc, sensitive_values=sensitive_values)
        await _save_monitor_event(cache, "warning", message)
        return {"status": "failed", "run_id": run_id, "message": message}
    message = _success_message(info)
    await _save_monitor_event(cache, "info", message)
    return {"status": "captured", "run_id": run_id, "message": message, "archive": info}


async def process_market_scan_probability_capture_outbox(
    cache: object,
    *,
    owner: str | None = None,
    limit: int = PROBABILITY_SOURCE_CAPTURE_BATCH_LIMIT,
    sensitive_values: Iterable[object] = (),
) -> dict[str, int]:
    """Drain due durable captures under SQLite leases; safe across restarts."""
    normalized_limit = max(1, min(int(limit), PROBABILITY_SOURCE_CAPTURE_BATCH_LIMIT))
    lease_owner = _capture_lease_owner(owner)
    counts = {"captured": 0, "skipped": 0, "failed": 0}
    for _item in range(normalized_limit):
        claim = await _claim_due_probability_source_capture(cache, owner=lease_owner)
        if claim is None:
            break
        outcome = await _process_probability_source_capture_claim(
            cache,
            claim,
            owner=lease_owner,
            sensitive_values=sensitive_values,
        )
        counts[outcome] += 1
    return counts


def _capture_lease_owner(owner: str | None) -> str:
    normalized = " ".join(str(owner or f"probability-source-capture-{uuid4().hex}").split()).strip()[:120]
    if not normalized:
        raise ValueError("上涨概率归档租约 owner 不能为空")
    return normalized


async def _claim_due_probability_source_capture(
    cache: object,
    *,
    owner: str,
) -> dict[str, object] | None:
    lease_expires_at = _utc_text(utc_now() + timedelta(seconds=PROBABILITY_SOURCE_CAPTURE_LEASE_SECONDS))
    claim = await run_cache_io(
        getattr(cache, "claim_probability_source_capture"),
        owner=owner,
        lease_expires_at=lease_expires_at,
    )
    return claim if isinstance(claim, dict) else None


async def _process_probability_source_capture_claim(
    cache: object,
    claim: dict[str, object],
    *,
    owner: str,
    sensitive_values: Iterable[object],
) -> Literal["captured", "skipped", "failed"]:
    run_id = int(cast(int, claim["run_id"]))
    attempt_count = int(cast(int, claim["attempt_count"]))
    try:
        info = await run_cache_io(
            capture_market_scan_probability_source,
            cache,
            run_id,
            captured_at=str(claim["captured_at"]),
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            _retry_claim(
                cache,
                run_id,
                owner=owner,
                attempt_count=attempt_count,
                error="上涨概率PIT样本归档任务被取消",
                immediate=True,
            )
        )
        raise
    except ProbabilitySourceCaptureIneligible as exc:
        await _complete_skipped_claim(cache, run_id, owner=owner, error=exc)
        return "skipped"
    except ProbabilitySourceCaptureError as exc:
        # Published run inputs are immutable. A deterministic contract or
        # projection failure cannot heal on retry, so close the outbox row.
        await _complete_skipped_claim(cache, run_id, owner=owner, error=exc)
        return "skipped"
    except Exception as exc:
        await _complete_failed_claim(
            cache,
            run_id,
            owner=owner,
            attempt_count=attempt_count,
            error=exc,
            sensitive_values=sensitive_values,
        )
        return "failed"
    await _complete_captured_claim(cache, run_id, owner=owner, info=info)
    return "captured"


async def _complete_skipped_claim(
    cache: object,
    run_id: int,
    *,
    owner: str,
    error: Exception,
) -> None:
    message = " ".join(str(error).split())[:600]
    await run_cache_io(
        getattr(cache, "finish_probability_source_capture"),
        run_id,
        owner=owner,
        status="skipped",
        message=message,
    )
    await _save_monitor_event(cache, "info", f"上涨概率PIT样本归档跳过：{message}")


async def _complete_failed_claim(
    cache: object,
    run_id: int,
    *,
    owner: str,
    attempt_count: int,
    error: Exception,
    sensitive_values: Iterable[object],
) -> None:
    message = _failure_message(run_id, error, sensitive_values=sensitive_values)
    if attempt_count >= PROBABILITY_SOURCE_CAPTURE_MAX_ATTEMPTS:
        terminal = (
            f"{message}；已达到自动重试上限 "
            f"{PROBABILITY_SOURCE_CAPTURE_MAX_ATTEMPTS} 次，归档任务终止"
        )
        await run_cache_io(
            getattr(cache, "finish_probability_source_capture"),
            run_id,
            owner=owner,
            status="skipped",
            message=terminal,
        )
        await _save_monitor_event(cache, "warning", terminal)
        return
    await _retry_claim(
        cache,
        run_id,
        owner=owner,
        attempt_count=attempt_count,
        error=message,
    )
    await _save_monitor_event(cache, "warning", message)


async def _complete_captured_claim(
    cache: object,
    run_id: int,
    *,
    owner: str,
    info: dict[str, object],
) -> None:
    message = _success_message(info)
    await run_cache_io(
        getattr(cache, "finish_probability_source_capture"),
        run_id,
        owner=owner,
        status="succeeded",
        archive_digest=str(info.get("digest") or "") or None,
        message=message,
    )
    await _save_monitor_event(cache, "info", message)


async def _retry_claim(
    cache: object,
    run_id: int,
    *,
    owner: str,
    attempt_count: int,
    error: str,
    immediate: bool = False,
) -> None:
    delay = (
        0
        if immediate
        else min(
            PROBABILITY_SOURCE_CAPTURE_RETRY_MAX_SECONDS,
            PROBABILITY_SOURCE_CAPTURE_RETRY_BASE_SECONDS * (2 ** min(16, max(0, attempt_count - 1))),
        )
    )
    await run_cache_io(
        getattr(cache, "retry_probability_source_capture"),
        run_id,
        owner=owner,
        next_attempt_at=_utc_text(utc_now() + timedelta(seconds=delay)),
        error=error,
    )


def _published_source_run(cache: object, run_id: int) -> MarketScanRun:
    run = cast(MarketScanRun, getattr(cache, "market_scan_run")(run_id))
    if run.status not in _PUBLISHED_STATUSES:
        raise ProbabilitySourceCaptureIneligible(f"run {run_id} 未发布，跳过上涨概率PIT样本归档")
    if run.snapshot_seal_origin != "publication":
        raise ProbabilitySourceCaptureIneligible(f"run {run_id} 不是发布事务原始封印，跳过上涨概率PIT样本归档")
    if run.mode != "official" or run.scope != FULL_MARKET_SCOPE:
        raise ProbabilitySourceCaptureIneligible(f"run {run_id} 不是盘后正式全市场批次，跳过PIT归档")
    action_source = getattr(cache, "market_scan_action_source_digest", None)
    action_digest = action_source(run_id) if callable(action_source) else None
    if action_digest is None or action_digest != run.snapshot_digest:
        raise ProbabilitySourceCaptureIneligible(
            f"run {run_id} 缺少统一动作源回执或跳过证据无效，跳过PIT归档"
        )
    if run.quote_date != run.data_date:
        raise ProbabilitySourceCaptureError(f"run {run_id} 的 quote_date/data_date 不一致")
    if run.success_count <= 0:
        raise ProbabilitySourceCaptureError(f"run {run_id} 没有可归档的成功结果")
    _require_writable_score_generation(cache, run)
    return run


def _require_writable_score_generation(cache: object, run: MarketScanRun) -> None:
    reader = getattr(cache, "market_scan_success_score_contract", None)
    if not callable(reader):
        raise ProbabilitySourceCaptureIneligible(
            f"run {run.id} 缺少可验证的生产评分合同，跳过PIT归档"
        )
    contract = reader(run.id)
    rule_version = getattr(contract, "production_score_rule_version", None)
    spec_hash = getattr(contract, "production_score_spec_hash", None)
    record_count = getattr(contract, "success_count", None)
    if record_count != run.success_count:
        raise ProbabilitySourceCaptureIneligible(
            f"run {run.id} 生产评分合同缺失、混合或记录数不完整，跳过PIT归档"
        )
    if is_current_writable_production_score_contract(rule_version, spec_hash):
        return
    if is_registered_production_score_contract(rule_version, spec_hash):
        raise ProbabilitySourceCaptureIneligible(
            f"run {run.id} 使用历史只读评分合同 {rule_version}，不创建新PIT归档"
        )
    raise ProbabilitySourceCaptureIneligible(
        f"run {run.id} 生产评分合同未注册或不唯一，跳过PIT归档"
    )


def _require_canonical_latest(cache: object, run: MarketScanRun) -> None:
    page = cast(
        MarketScanRunPage,
        getattr(cache, "market_scan_runs")(
            page=1,
            page_size=10_000,
            mode=run.mode,
            status="published",
            data_date=run.data_date,
        ),
    )
    candidates = [
        item
        for item in page.items
        if item.status in _PUBLISHED_STATUSES
        and item.mode == run.mode
        and item.scope == run.scope
        and item.rule_version == run.rule_version
        and item.quote_date == run.quote_date
    ]
    if not candidates:
        raise ProbabilitySourceCaptureError(f"run {run.id} 不在同日期同cohort已发布集合中")
    canonical = max(candidates, key=_canonical_order)
    if canonical.id != run.id:
        raise ProbabilitySourceCaptureIneligible(f"run {run.id} 已被同日期同cohort的 run {canonical.id} 替代，拒绝归档")


def _require_run_unchanged(cache: object, expected: MarketScanRun) -> None:
    observed = cast(MarketScanRun, getattr(cache, "market_scan_run")(expected.id))
    try:
        validate_market_scan_run_binding(expected, observed)
    except ValueError as exc:
        raise ProbabilitySourceCaptureError(f"run {expected.id} 在归档期间发生变化") from exc


def _validate_publish_binding(cache: object, expected: MarketScanRun) -> None:
    _require_canonical_latest(cache, expected)
    _require_run_unchanged(cache, expected)


def _canonical_order(run: MarketScanRun) -> tuple[float, int]:
    timestamp = run.as_of
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return (float("-inf"), run.id)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ASHARE_TIMEZONE)
    return (parsed.timestamp(), run.id)


def _complete_success_results(cache: object, run: MarketScanRun) -> list[object]:
    page = cast(
        MarketScanResultPage,
        getattr(cache, "market_scan_results")(
            run.id,
            page=1,
            page_size=max(1, run.success_count),
            status="success",
            market=None,
            industry=None,
            is_st=None,
            is_new=None,
            min_score=None,
            max_score=None,
            min_trend_score=None,
            max_trend_score=None,
            min_change_pct=None,
            max_change_pct=None,
            min_turnover_rate=None,
            max_turnover_rate=None,
            min_amount=None,
            max_amount=None,
            min_data_quality_score=None,
            max_data_quality_score=None,
            min_confidence=None,
            max_risk=None,
            min_tradability=None,
            keyword=None,
            symbols=None,
            sort="rank",
            order="asc",
        ),
    )
    if page.run.id != run.id or page.total != run.success_count or len(page.items) != run.success_count:
        raise ProbabilitySourceCaptureError(f"run {run.id} 成功结果读取不完整：{len(page.items)}/{page.total}/{run.success_count}")
    return list(page.items)


def _archive_directory(cache: object, directory: str | Path | None) -> Path:
    if directory is not None:
        return Path(directory).expanduser().absolute()
    cache_path = getattr(cache, "path", None)
    if not isinstance(cache_path, str | Path):
        raise ProbabilitySourceCaptureError("runtime cache 缺少可解析路径，无法确定上涨概率归档目录")
    return Path(cache_path).expanduser().resolve().parent / PROBABILITY_SOURCE_ARCHIVE_RELATIVE_PATH


def _existing_capture(
    cache: object,
    run_id: int,
    *,
    directory: str | Path | None,
) -> dict[str, object] | None:
    # Explicit roots are used by manual backfills/tests and must always pass
    # through the publisher's stricter output-root validation.
    if directory is not None:
        return None
    existing = list_probability_source_snapshots(
        _archive_directory(cache, directory),
        run_id=run_id,
    )
    if not existing:
        return None
    return max(existing, key=lambda item: (str(item["captured_at"]), str(item["digest"])))


def _require_existing_capture_binding(
    info: dict[str, object],
    run: MarketScanRun,
) -> None:
    if int(cast(int, info.get("run_id"))) != run.id:
        raise ProbabilitySourceCaptureError("现有上涨概率归档 run_id 冲突")
    if str(info.get("quote_date")) != run.quote_date:
        raise ProbabilitySourceCaptureError("现有上涨概率归档 quote_date 冲突")
    digest = str(info.get("digest") or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ProbabilitySourceCaptureError("现有上涨概率归档 digest 无效")


def _captured_at_text(value: datetime | str | None) -> str:
    if value is None:
        parsed = market_now()
    elif isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        except ValueError as exc:
            raise ProbabilitySourceCaptureError("captured_at 必须是 ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ASHARE_TIMEZONE)
    return parsed.isoformat(timespec="seconds")


def _aware_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ProbabilitySourceCaptureError("captured_at 必须是 ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProbabilitySourceCaptureError("captured_at 必须包含时区")
    return parsed


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


async def _save_monitor_event(cache: object, level: str, message: str) -> None:
    writer = getattr(cache, "save_monitor_event", None)
    if not callable(writer):
        if level == "warning":
            LOGGER.warning("%s", message)
        return
    try:
        await run_cache_io(writer, level, PROBABILITY_SOURCE_MONITOR_CATEGORY, message[:800])
    except Exception:
        LOGGER.warning("上涨概率PIT样本归档事件持久化失败：%s", message, exc_info=True)
        return


def _success_message(info: dict[str, object]) -> str:
    quality = info.get("quality")
    record_count = quality.get("record_count") if isinstance(quality, dict) else "--"
    return (
        f"上涨概率PIT样本归档完成：run #{info.get('run_id')}，交易日 {info.get('quote_date')}，"
        f"记录 {record_count}，digest {str(info.get('digest') or '')[:12]}"
    )


def _failure_message(
    run_id: int,
    exc: Exception,
    *,
    sensitive_values: Iterable[object],
) -> str:
    detail = sanitize_provider_error(exc, sensitive_values=sensitive_values)
    return f"上涨概率PIT样本归档失败：run #{run_id}，{type(exc).__name__}: {' '.join(detail.split())[:600]}"


__all__ = [
    "PROBABILITY_SOURCE_ARCHIVE_RELATIVE_PATH",
    "PROBABILITY_SOURCE_MONITOR_CATEGORY",
    "ProbabilitySourceCaptureError",
    "ProbabilitySourceCaptureIneligible",
    "audit_market_scan_probability_source_archives",
    "capture_market_scan_probability_source",
    "capture_market_scan_probability_source_best_effort",
    "process_market_scan_probability_capture_outbox",
]
