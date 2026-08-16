from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import get_datahub
from app.api.errors import run_api, run_sync_api_async
from app.models.reviews import (
    AdviceReviewDetail,
    AdviceReviewBatchSummary,
    AdviceReviewDueItem,
    AdviceReviewEvaluation,
    AdviceReviewBatchEvaluationRequest,
    AdviceReviewEvaluationRequest,
    AdviceReviewPlan,
    AdviceReviewPlanInput,
    AdviceReviewPlanUpdate,
    AdviceReviewSummary,
)
from app.models.system import (
    MutationResult,
)
from app.services.advice_review import (
    create_advice_review_plan,
    delete_advice_review_plan,
    evaluate_advice_review_plan,
    evaluate_due_advice_reviews,
    get_advice_review_detail,
    get_advice_review_summary,
    list_advice_review_details,
    list_due_advice_reviews,
    list_advice_review_plans,
    update_advice_review_plan,
)
from app.services.datahub import DataHub


router = APIRouter()


@router.get("/api/reviews", response_model=list[AdviceReviewDetail])
async def review_details(
    response: Response,
    symbol: str | None = Query(default=None, description="可选，A股代码"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100_000),
    datahub: DataHub = Depends(get_datahub),
) -> list[AdviceReviewDetail]:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: list_advice_review_details(datahub.cache, symbol=symbol, limit=limit, offset=offset)
    )


@router.get("/api/reviews/summary", response_model=AdviceReviewSummary)
async def review_summary(
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> AdviceReviewSummary:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: get_advice_review_summary(datahub.cache))


@router.get("/api/reviews/due", response_model=list[AdviceReviewDueItem])
async def due_reviews(
    response: Response,
    as_of: datetime | None = Query(default=None),
    limit: int = Query(100, ge=1, le=200),
    datahub: DataHub = Depends(get_datahub),
) -> list[AdviceReviewDueItem]:
    response.headers["Cache-Control"] = "no-store"
    return await run_api(lambda: list_due_advice_reviews(datahub, as_of=as_of, limit=limit))


@router.post("/api/reviews/evaluate-due", response_model=AdviceReviewBatchSummary)
async def evaluate_due_reviews(
    payload: AdviceReviewBatchEvaluationRequest,
    response: Response,
    limit: int = Query(20, ge=1, le=100),
    datahub: DataHub = Depends(get_datahub),
) -> AdviceReviewBatchSummary:
    response.headers["Cache-Control"] = "no-store"
    return await run_api(lambda: evaluate_due_advice_reviews(datahub, as_of=payload.as_of, limit=limit))


@router.post(
    "/api/reviews/plans",
    response_model=AdviceReviewPlan,
    status_code=status.HTTP_201_CREATED,
)
async def create_review_plan(
    payload: AdviceReviewPlanInput,
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> AdviceReviewPlan:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: create_advice_review_plan(datahub.cache, payload))


@router.get("/api/reviews/plans", response_model=list[AdviceReviewPlan])
async def review_plans(
    response: Response,
    symbol: str | None = Query(default=None, description="可选，A股代码"),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100_000),
    datahub: DataHub = Depends(get_datahub),
) -> list[AdviceReviewPlan]:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: list_advice_review_plans(datahub.cache, symbol=symbol, limit=limit, offset=offset)
    )


@router.get("/api/reviews/plans/{plan_id}", response_model=AdviceReviewDetail)
async def review_plan_detail(
    plan_id: int,
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> AdviceReviewDetail:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: get_advice_review_detail(datahub.cache, plan_id))


@router.patch("/api/reviews/plans/{plan_id}", response_model=AdviceReviewPlan)
async def update_review_plan(
    plan_id: int,
    payload: AdviceReviewPlanUpdate,
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> AdviceReviewPlan:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: update_advice_review_plan(datahub.cache, plan_id, payload)
    )


@router.delete("/api/reviews/plans/{plan_id}", response_model=MutationResult)
async def delete_review_plan(
    plan_id: int,
    response: Response,
    expected_revision: int = Query(ge=1),
    datahub: DataHub = Depends(get_datahub),
) -> MutationResult:
    response.headers["Cache-Control"] = "no-store"
    def remove() -> MutationResult:
        delete_advice_review_plan(
            datahub.cache,
            plan_id,
            expected_revision=expected_revision,
        )
        return MutationResult(ok=True, removed=True)

    return await run_sync_api_async(remove)


@router.post(
    "/api/reviews/plans/{plan_id}/evaluate",
    response_model=AdviceReviewEvaluation,
)
async def evaluate_review_plan(
    plan_id: int,
    response: Response,
    payload: AdviceReviewEvaluationRequest,
    datahub: DataHub = Depends(get_datahub),
) -> AdviceReviewEvaluation:
    response.headers["Cache-Control"] = "no-store"
    async def evaluate() -> AdviceReviewEvaluation:
        return await evaluate_advice_review_plan(
            datahub,
            plan_id,
            expected_revision=payload.expected_revision,
            as_of=payload.as_of,
        )

    return await run_api(evaluate)


@router.get(
    "/api/reviews/plans/{plan_id}/evaluations",
    response_model=list[AdviceReviewEvaluation],
)
async def review_plan_evaluations(
    plan_id: int,
    response: Response,
    limit: int = Query(100, ge=1, le=200),
    datahub: DataHub = Depends(get_datahub),
) -> list[AdviceReviewEvaluation]:
    response.headers["Cache-Control"] = "no-store"
    def load() -> list[AdviceReviewEvaluation]:
        get_advice_review_detail(datahub.cache, plan_id)
        return datahub.cache.advice_review_evaluation_history(plan_id, limit=limit)

    return await run_sync_api_async(load)
