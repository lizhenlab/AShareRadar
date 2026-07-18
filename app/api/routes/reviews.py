from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import get_datahub
from app.api.errors import run_api, run_sync_api_async
from app.models.schemas import (
    AdviceReviewDetail,
    AdviceReviewEvaluation,
    AdviceReviewEvaluationRequest,
    AdviceReviewPlan,
    AdviceReviewPlanInput,
    AdviceReviewPlanUpdate,
    MutationResult,
)
from app.services.advice_review import (
    create_advice_review_plan,
    delete_advice_review_plan,
    evaluate_advice_review_plan,
    get_advice_review_detail,
    list_advice_review_details,
    list_advice_review_plans,
    update_advice_review_plan,
)
from app.services.datahub import DataHub


router = APIRouter()


@router.get("/api/reviews", response_model=list[AdviceReviewDetail])
async def review_details(
    symbol: str | None = Query(default=None, description="可选，A股代码"),
    limit: int = Query(20, ge=1, le=100),
    datahub: DataHub = Depends(get_datahub),
) -> list[AdviceReviewDetail]:
    return await run_sync_api_async(
        lambda: list_advice_review_details(datahub.cache, symbol=symbol, limit=limit)
    )


@router.post(
    "/api/reviews/plans",
    response_model=AdviceReviewPlan,
    status_code=status.HTTP_201_CREATED,
)
async def create_review_plan(
    payload: AdviceReviewPlanInput,
    datahub: DataHub = Depends(get_datahub),
) -> AdviceReviewPlan:
    return await run_sync_api_async(lambda: create_advice_review_plan(datahub.cache, payload))


@router.get("/api/reviews/plans", response_model=list[AdviceReviewPlan])
async def review_plans(
    symbol: str | None = Query(default=None, description="可选，A股代码"),
    limit: int = Query(100, ge=1, le=200),
    datahub: DataHub = Depends(get_datahub),
) -> list[AdviceReviewPlan]:
    return await run_sync_api_async(
        lambda: list_advice_review_plans(datahub.cache, symbol=symbol, limit=limit)
    )


@router.get("/api/reviews/plans/{plan_id}", response_model=AdviceReviewDetail)
async def review_plan_detail(
    plan_id: int,
    datahub: DataHub = Depends(get_datahub),
) -> AdviceReviewDetail:
    return await run_sync_api_async(lambda: get_advice_review_detail(datahub.cache, plan_id))


@router.patch("/api/reviews/plans/{plan_id}", response_model=AdviceReviewPlan)
async def update_review_plan(
    plan_id: int,
    payload: AdviceReviewPlanUpdate,
    datahub: DataHub = Depends(get_datahub),
) -> AdviceReviewPlan:
    return await run_sync_api_async(
        lambda: update_advice_review_plan(datahub.cache, plan_id, payload)
    )


@router.delete("/api/reviews/plans/{plan_id}", response_model=MutationResult)
async def delete_review_plan(
    plan_id: int,
    datahub: DataHub = Depends(get_datahub),
) -> MutationResult:
    def remove() -> MutationResult:
        delete_advice_review_plan(datahub.cache, plan_id)
        return MutationResult(ok=True, removed=True)

    return await run_sync_api_async(remove)


@router.post(
    "/api/reviews/plans/{plan_id}/evaluate",
    response_model=AdviceReviewEvaluation,
)
async def evaluate_review_plan(
    plan_id: int,
    payload: AdviceReviewEvaluationRequest | None = None,
    datahub: DataHub = Depends(get_datahub),
) -> AdviceReviewEvaluation:
    request = payload or AdviceReviewEvaluationRequest()

    async def evaluate() -> AdviceReviewEvaluation:
        return await evaluate_advice_review_plan(datahub, plan_id, as_of=request.as_of)

    return await run_api(evaluate)


@router.get(
    "/api/reviews/plans/{plan_id}/evaluations",
    response_model=list[AdviceReviewEvaluation],
)
async def review_plan_evaluations(
    plan_id: int,
    limit: int = Query(100, ge=1, le=200),
    datahub: DataHub = Depends(get_datahub),
) -> list[AdviceReviewEvaluation]:
    def load() -> list[AdviceReviewEvaluation]:
        get_advice_review_detail(datahub.cache, plan_id)
        return datahub.cache.advice_review_evaluation_history(plan_id, limit=limit)

    return await run_sync_api_async(load)
