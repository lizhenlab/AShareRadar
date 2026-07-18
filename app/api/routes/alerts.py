from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_datahub
from app.api.errors import run_api, run_sync_api_async
from app.models.schemas import (
    AlertEvaluationSummary,
    AlertEventItem,
    AlertRuleInput,
    AlertRuleItem,
    AlertRuleUpdate,
    MutationResult,
)
from app.repositories.alerts import MAX_ALERT_EVENT_PAGE_SIZE
from app.services.alerts import evaluate_alert_rules, validate_alert_condition
from app.services.datahub import DataHub
from app.utils.symbols import normalize_symbol


router = APIRouter()


@router.get("/api/alerts", response_model=list[AlertRuleItem])
async def alert_rules(
    symbol: str | None = Query(default=None, description="可选，6位A股代码"),
    include_disabled: bool = True,
    datahub: DataHub = Depends(get_datahub),
) -> list[AlertRuleItem]:
    def load() -> list[AlertRuleItem]:
        if symbol:
            normalize_symbol(symbol)
        return datahub.cache.alert_rules(symbol=symbol, include_disabled=include_disabled)

    return await run_sync_api_async(load)


@router.post("/api/alerts", response_model=AlertRuleItem)
async def create_alert_rule(
    payload: AlertRuleInput,
    datahub: DataHub = Depends(get_datahub),
) -> AlertRuleItem:
    async def create() -> AlertRuleItem:
        normalize_symbol(payload.symbol)
        validate_alert_condition(payload.condition_type, payload.threshold)
        quote = await datahub.quote(payload.symbol)
        return await run_sync_api_async(lambda: datahub.cache.create_alert_rule(quote, payload))

    return await run_api(create)


@router.delete("/api/alerts/{rule_id}", response_model=MutationResult)
async def delete_alert_rule(rule_id: int, datahub: DataHub = Depends(get_datahub)) -> MutationResult:
    def remove() -> MutationResult:
        removed = datahub.cache.delete_alert_rule(rule_id)
        if not removed:
            raise HTTPException(status_code=404, detail="预警规则不存在")
        return MutationResult(ok=True, removed=removed)

    return await run_sync_api_async(remove)


@router.patch("/api/alerts/{rule_id}", response_model=AlertRuleItem)
async def update_alert_rule(
    rule_id: int,
    payload: AlertRuleUpdate,
    datahub: DataHub = Depends(get_datahub),
) -> AlertRuleItem:
    def update() -> AlertRuleItem:
        current = datahub.cache.alert_rule(rule_id)
        if current is None:
            raise HTTPException(status_code=404, detail="预警规则不存在")
        condition_type = payload.condition_type or current.condition_type
        threshold = current.threshold if payload.threshold is None else payload.threshold
        if payload.condition_type is not None or payload.threshold is not None:
            validate_alert_condition(condition_type, threshold)
        rule = datahub.cache.update_alert_rule(rule_id, payload)
        if rule is None:
            raise HTTPException(status_code=404, detail="预警规则不存在")
        return rule

    return await run_sync_api_async(update)


@router.post("/api/alerts/evaluate", response_model=AlertEvaluationSummary)
async def evaluate_alerts(
    symbol: str | None = Query(default=None, description="可选，6位A股代码"),
    datahub: DataHub = Depends(get_datahub),
) -> AlertEvaluationSummary:
    async def evaluate() -> AlertEvaluationSummary:
        if symbol:
            normalize_symbol(symbol)
        return await evaluate_alert_rules(datahub, symbol=symbol)

    return await run_api(evaluate)


@router.get("/api/alerts/events", response_model=list[AlertEventItem])
async def alert_events(
    symbol: str | None = Query(default=None, description="可选，6位A股代码"),
    limit: int = Query(100, ge=1, le=MAX_ALERT_EVENT_PAGE_SIZE),
    after_created_at: str | None = Query(default=None, max_length=64),
    after_id: int | None = Query(default=None, ge=0),
    datahub: DataHub = Depends(get_datahub),
) -> list[AlertEventItem]:
    def load() -> list[AlertEventItem]:
        if symbol:
            normalize_symbol(symbol)
        if after_created_at is not None and after_id is None:
            raise HTTPException(status_code=422, detail="使用 after_created_at 时必须同时提供 after_id")
        return datahub.cache.alert_events(
            symbol=symbol,
            limit=limit,
            after_created_at=after_created_at,
            after_id=after_id,
        )

    return await run_sync_api_async(load)
