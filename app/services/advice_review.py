from __future__ import annotations

from datetime import datetime

from app.models.reviews import (
    AdviceReviewDetail,
    AdviceReviewEvaluation,
    AdviceReviewPlan,
    AdviceReviewPlanInput,
    AdviceReviewPlanUpdate,
)
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io
from app.services.research_replay import evaluate_advice_forward_window
from app.utils.errors import NotFoundError
from app.utils.market_time import ASHARE_TIMEZONE, market_local_naive


MIN_REVIEW_KLINE_LIMIT = 120
MAX_REVIEW_KLINE_LIMIT = 5_000
REVIEW_KLINE_BUFFER_DAYS = 40


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
        evaluated_at=evaluated_at_value.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return await run_cache_io(datahub.cache.save_advice_review_evaluation, evaluation)


def normalize_review_as_of(
    value: datetime | None,
    *,
    now: datetime | None = None,
    allow_future: bool = False,
) -> datetime:
    current = market_local_naive(now or datetime.now(ASHARE_TIMEZONE))
    parsed = market_local_naive(value) if value is not None else current
    if not allow_future and parsed > current:
        raise ValueError("as_of 不能晚于当前市场时间")
    return parsed.replace(microsecond=0)


def _snapshot_datetime(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError) as exc:
        raise ValueError("研究计划缺少有效 snapshot_market_time") from exc


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
    "delete_advice_review_plan",
    "evaluate_advice_review_plan",
    "get_advice_review_detail",
    "list_advice_review_plans",
    "list_advice_review_details",
    "normalize_review_as_of",
    "update_advice_review_plan",
]
