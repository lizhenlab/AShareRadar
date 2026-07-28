from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.models.reviews import (
    AdviceEvidenceRef,
    AdviceReviewBatchItem,
    AdviceReviewBatchSummary,
    AdviceReviewDetail,
    AdviceReviewEvaluation,
    AdviceReviewPlan,
    AdviceReviewPlanInput,
    AdviceReviewPlanUpdate,
    AdviceReviewSummary,
    structured_advice_evidence_refs,
)
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io
from app.services.research_replay import completed_daily_bar_cutoff, evaluate_advice_forward_window
from app.services.trading_calendar import DAILY_KLINE_PUBLISH_TIME, is_trading_day
from app.utils.audit_time import audit_datetime_to_text
from app.utils.clock import market_now_naive
from app.utils.errors import NotFoundError
from app.utils.market_time import market_local_naive
from app.utils.provider_errors import sanitize_provider_error


MIN_REVIEW_KLINE_LIMIT = 120
MAX_REVIEW_KLINE_LIMIT = 5_000
REVIEW_KLINE_BUFFER_DAYS = 40
MAX_DUE_REVIEW_CANDIDATE_SCAN = 500


def create_advice_review_plan(cache: object, payload: AdviceReviewPlanInput) -> AdviceReviewPlan:
    return cache.create_advice_review_plan(payload)


def update_advice_review_plan(
    cache: object,
    plan_id: int,
    payload: AdviceReviewPlanUpdate,
) -> AdviceReviewPlan:
    plan = cache.update_advice_review_plan(plan_id, payload)
    if plan is None:
        raise NotFoundError("研究计划不存在")
    return plan


def delete_advice_review_plan(cache: object, plan_id: int) -> None:
    if not cache.delete_advice_review_plan(plan_id):
        raise NotFoundError("研究计划不存在")


def get_advice_review_detail(cache: object, plan_id: int) -> AdviceReviewDetail:
    detail = cache.advice_review_detail(plan_id)
    if detail is None:
        raise NotFoundError("研究计划不存在")
    return detail


def list_advice_review_plans(
    cache: object,
    *,
    symbol: str | None = None,
    limit: int = 100,
) -> list[AdviceReviewPlan]:
    return cache.advice_review_plans(symbol=symbol, limit=limit)


def list_advice_review_details(
    cache: object,
    *,
    symbol: str | None = None,
    limit: int = 100,
) -> list[AdviceReviewDetail]:
    return cache.advice_review_details(symbol=symbol, limit=limit)


def build_advice_evidence_refs(snapshot: object) -> list[AdviceEvidenceRef]:
    return structured_advice_evidence_refs(snapshot)


def get_advice_review_summary(cache: object) -> AdviceReviewSummary:
    return cache.advice_review_summary()


async def evaluate_advice_review_plan(
    datahub: DataHub,
    plan_id: int,
    *,
    as_of: datetime | None = None,
    now: datetime | None = None,
) -> AdviceReviewEvaluation:
    plan = await run_cache_io(datahub.cache.advice_review_plan, plan_id)
    if plan is None:
        raise NotFoundError("研究计划不存在")
    evaluated_at_value = normalize_review_as_of(now, allow_future=True)
    as_of_value = normalize_review_as_of(as_of, now=evaluated_at_value)
    snapshot_time = _snapshot_datetime(plan.snapshot_market_time)
    if as_of_value < snapshot_time:
        raise ValueError("as_of 不能早于 advice snapshot 的 market_time")
    rows = await datahub.kline(
        plan.symbol,
        limit=_review_kline_limit(plan, snapshot_time, as_of_value),
        use_cache=True,
    )
    evaluation = evaluate_advice_forward_window(
        plan,
        rows,
        as_of=as_of_value,
        evaluated_at=audit_datetime_to_text(evaluated_at_value),
    )
    return await run_cache_io(datahub.cache.save_advice_review_evaluation, evaluation)


async def evaluate_due_advice_reviews(
    datahub: DataHub,
    *,
    as_of: datetime | None = None,
    now: datetime | None = None,
    limit: int = 20,
) -> AdviceReviewBatchSummary:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("到期复盘批量上限必须是正整数")
    limit = min(limit, 100)
    current = normalize_review_as_of(as_of, now=now, allow_future=True)
    stable_as_of = _stable_review_as_of(current)
    details = await run_cache_io(
        datahub.cache.advice_review_evaluation_candidates,
        as_of_date=stable_as_of.date().isoformat(),
        limit=min(MAX_DUE_REVIEW_CANDIDATE_SCAN, max(limit * 10, limit)),
    )
    due_details = [detail for detail in details if _review_detail_is_due(detail, stable_as_of)]
    selected = due_details[:limit]
    items: list[AdviceReviewBatchItem] = []
    sensitive_values = _background_sensitive_values(datahub)
    for detail in selected:
        items.append(
            await _evaluate_due_review(
                datahub,
                detail,
                as_of=stable_as_of,
                evaluated_at=current,
                sensitive_values=sensitive_values,
            )
        )
        await asyncio.sleep(0)
    return _due_review_summary(stable_as_of, due_details, items)


async def _evaluate_due_review(
    datahub: DataHub,
    detail: AdviceReviewDetail,
    *,
    as_of: datetime,
    evaluated_at: datetime,
    sensitive_values: tuple[object, ...],
) -> AdviceReviewBatchItem:
    plan = detail.plan
    try:
        evaluation = await evaluate_advice_review_plan(
            datahub,
            plan.id,
            as_of=as_of,
            now=evaluated_at,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return AdviceReviewBatchItem(
            plan_id=plan.id,
            symbol=plan.symbol,
            status="failed",
            message=_short_review_error(exc, sensitive_values=sensitive_values),
        )
    return AdviceReviewBatchItem(
        plan_id=plan.id,
        symbol=plan.symbol,
        status="evaluated",
        evaluation_id=evaluation.id,
        conclusion=evaluation.conclusion,
    )


def _due_review_summary(
    as_of: datetime,
    candidates: list[AdviceReviewDetail],
    items: list[AdviceReviewBatchItem],
) -> AdviceReviewBatchSummary:
    return AdviceReviewBatchSummary(
        as_of=as_of.strftime("%Y-%m-%d %H:%M:%S"),
        candidate_count=len(candidates),
        attempted_count=len(items),
        evaluated_count=sum(item.status == "evaluated" for item in items),
        failed_count=sum(item.status == "failed" for item in items),
        items=items,
    )


def normalize_review_as_of(
    value: datetime | None,
    *,
    now: datetime | None = None,
    allow_future: bool = False,
) -> datetime:
    current = market_local_naive(now) if now is not None else market_now_naive()
    parsed = market_local_naive(value) if value is not None else current
    if not allow_future and parsed > current:
        raise ValueError("as_of 不能晚于当前市场时间")
    return parsed.replace(microsecond=0)


def _snapshot_datetime(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError) as exc:
        raise ValueError("研究计划缺少有效 snapshot_market_time") from exc


def _stable_review_as_of(value: datetime) -> datetime:
    cutoff = completed_daily_bar_cutoff(value)
    return datetime.combine(cutoff, DAILY_KLINE_PUBLISH_TIME)


def _review_detail_is_due(detail: AdviceReviewDetail, as_of: datetime) -> bool:
    latest = detail.latest_evaluation
    if latest is not None and latest.status == "evaluated":
        return False
    plan = detail.plan
    snapshot_date = _snapshot_datetime(plan.snapshot_market_time).date()
    cutoff = completed_daily_bar_cutoff(as_of)
    if cutoff <= snapshot_date:
        return False
    observed = 0
    current = snapshot_date
    while current < cutoff and observed < plan.horizon_days:
        current += timedelta(days=1)
        if is_trading_day(current):
            observed += 1
    return observed >= plan.horizon_days


def _short_review_error(
    exc: Exception,
    *,
    sensitive_values: tuple[object, ...] = (),
) -> str:
    message = " ".join(sanitize_provider_error(exc, sensitive_values=sensitive_values).split()).strip()
    return (message or exc.__class__.__name__)[:160]


def _background_sensitive_values(datahub: object) -> tuple[object, ...]:
    cache = getattr(datahub, "cache", None)
    settings = getattr(datahub, "settings", None) or getattr(cache, "settings", None)
    if settings is None:
        return ()
    values = (
        getattr(settings, "tushare_token", None),
        getattr(settings, "llm_api_key", None),
        getattr(settings, "llm_base_url", None),
    )
    return tuple(value for value in values if value not in (None, ""))


def _review_kline_limit(
    plan: AdviceReviewPlan,
    snapshot_time: datetime,
    as_of: datetime,
) -> int:
    calendar_span = max(0, (as_of.date() - snapshot_time.date()).days)
    requested = calendar_span + plan.horizon_days + REVIEW_KLINE_BUFFER_DAYS
    return min(MAX_REVIEW_KLINE_LIMIT, max(MIN_REVIEW_KLINE_LIMIT, requested))


__all__ = [
    "MAX_REVIEW_KLINE_LIMIT",
    "MIN_REVIEW_KLINE_LIMIT",
    "create_advice_review_plan",
    "build_advice_evidence_refs",
    "delete_advice_review_plan",
    "evaluate_due_advice_reviews",
    "evaluate_advice_review_plan",
    "get_advice_review_summary",
    "get_advice_review_detail",
    "list_advice_review_plans",
    "list_advice_review_details",
    "normalize_review_as_of",
    "update_advice_review_plan",
]
