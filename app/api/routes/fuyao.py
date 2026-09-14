"""Cache-first optional source endpoints; collection is an explicit bounded job."""

from __future__ import annotations

from typing import Any
from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import get_datahub
from app.models.fuyao_research import FuyaoJob, FuyaoJobRequest
from app.services.datahub import DataHub
from app.services.fuyao_dumps import read_dump_status
from app.services.fuyao_financials import financial_health_from_bundle
from app.services.fuyao_scoring import build_fuyao_valuation_score
from app.services.value_research import build_value_research
from app.utils.audit_time import audit_now_text
from app.services.fuyao_observations import canonical_stock, valuation_history_summary
from app.services.fuyao_service import fuyao_io


router = APIRouter(prefix="/api/fuyao", tags=["扶摇研究数据"])


@router.get("/status")
async def status(response: Response, datahub: DataHub = Depends(get_datahub)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await datahub.fuyao.status()


@router.post("/jobs", response_model=FuyaoJob, status_code=202)
async def start_job(payload: FuyaoJobRequest, response: Response, datahub: DataHub = Depends(get_datahub)) -> FuyaoJob:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await datahub.fuyao.start_job(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/jobs/{job_id}", response_model=FuyaoJob)
async def job_detail(job_id: str, response: Response, datahub: DataHub = Depends(get_datahub)) -> FuyaoJob:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await datahub.fuyao.get_job(job_id)
    except LookupError:
        raise HTTPException(404, "扶摇同步任务不存在") from None


@router.post("/jobs/{job_id}/cancel", response_model=FuyaoJob, status_code=202)
async def cancel_job(job_id: str, response: Response, datahub: DataHub = Depends(get_datahub)) -> FuyaoJob:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await datahub.fuyao.cancel_job(job_id)
    except LookupError:
        raise HTTPException(404, "扶摇同步任务不存在") from None
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/jobs/{job_id}/retry", response_model=FuyaoJob, status_code=202)
async def retry_job(job_id: str, response: Response, datahub: DataHub = Depends(get_datahub)) -> FuyaoJob:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await datahub.fuyao.retry_job(job_id)
    except LookupError:
        raise HTTPException(404, "扶摇同步任务不存在") from None
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/stock")
async def stock_observations(response: Response, symbol: str = Query(...), datahub: DataHub = Depends(get_datahub)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        normalized = canonical_stock(symbol)
    except ValueError:
        raise HTTPException(422, "股票代码无效") from None
    service = datahub.fuyao
    report = await service.financials(normalized)
    valuation = await fuyao_io(service.repository.latest, "valuations", normalized)
    history = await fuyao_io(service.repository.valuation_history, normalized, 100)
    basis = valuation_history_summary([item.model_dump(mode="json") for item in history], valuation.payload) if valuation else None
    valuation_score = build_fuyao_valuation_score(normalized, valuation, audit_now_text())
    return {"symbol": normalized, "financials": report.model_dump(mode="json") if report else None,
            "financial_health": financial_health_from_bundle(report).model_dump(mode="json") if report else None,
            "valuation": valuation.model_dump(mode="json") if valuation else None, "valuation_history": basis,
            "valuation_score": valuation_score.model_dump(mode="json"),
            "value_research": build_value_research(valuation_score, report).model_dump(mode="json"),
            "available": report is not None or valuation is not None, "read_mode": "local_cache_only"}


@router.get("/market")
async def market_observations(response: Response, datahub: DataHub = Depends(get_datahub)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    service = datahub.fuyao
    sectors = await fuyao_io(service.repository.latest, "sectors", "market")
    sentiment = await fuyao_io(service.repository.latest, "sentiment", "market")
    history = await fuyao_io(partial(read_dump_status, verify_files=False), service.root / "history")
    return {"sectors": sectors.model_dump(mode="json") if sectors else None,
            "sentiment": sentiment.model_dump(mode="json") if sentiment else None,
            "history": history.model_dump(mode="json") if history else None, "read_mode": "local_cache_only"}
