from __future__ import annotations

import csv
from io import StringIO
import json
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import get_datahub
from app.api.errors import run_api, run_sync_api_async
from app.models.paper_trading import (
    PaperRunComparison,
    PaperRunExport,
    PaperSimulationRequest,
    PaperSimulationSummary,
    PaperStrategy,
    PaperStrategyCreate,
    PaperTradingAccount,
    PaperTradingAccountUpdate,
    PaperTradingDashboard,
    PaperTradingRun,
)
from app.models.system import MutationResult
from app.services.datahub import DataHub
from app.services.paper_trading import (
    create_paper_strategy,
    delete_pending_paper_strategy,
    get_paper_trading_dashboard,
    run_paper_simulation,
    update_paper_trading_account,
)


router = APIRouter(prefix="/api/paper-trading", tags=["paper-trading"])


@router.get("", response_model=PaperTradingDashboard)
async def paper_trading_dashboard(
    response: Response,
    run_id: int | None = Query(default=None, gt=0),
    datahub: DataHub = Depends(get_datahub),
) -> PaperTradingDashboard:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: get_paper_trading_dashboard(datahub.cache, run_id=run_id))


@router.patch("/account", response_model=PaperTradingAccount)
async def patch_paper_trading_account(
    payload: PaperTradingAccountUpdate,
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> PaperTradingAccount:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: update_paper_trading_account(datahub.cache, payload))


@router.post(
    "/strategies",
    response_model=PaperStrategy,
    status_code=status.HTTP_201_CREATED,
)
async def post_paper_strategy(
    payload: PaperStrategyCreate,
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> PaperStrategy:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: create_paper_strategy(datahub.cache, payload))


@router.delete("/strategies/{strategy_id}", response_model=MutationResult)
async def delete_paper_strategy(
    strategy_id: int,
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> MutationResult:
    response.headers["Cache-Control"] = "no-store"
    def remove() -> MutationResult:
        delete_pending_paper_strategy(datahub.cache, strategy_id)
        return MutationResult(ok=True, removed=True)

    return await run_sync_api_async(remove)


@router.post("/run", response_model=PaperSimulationSummary)
async def run_paper_trading(
    payload: PaperSimulationRequest,
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> PaperSimulationSummary:
    response.headers["Cache-Control"] = "no-store"
    return await run_api(lambda: run_paper_simulation(datahub, payload))


@router.get("/runs", response_model=list[PaperTradingRun])
async def paper_trading_runs(
    response: Response,
    limit: int = Query(default=100, ge=1, le=500),
    datahub: DataHub = Depends(get_datahub),
) -> list[PaperTradingRun]:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: datahub.cache.paper_trading_runs(limit=limit))


@router.get("/runs/compare", response_model=PaperRunComparison)
async def compare_paper_trading_runs(
    response: Response,
    left_run_id: int = Query(gt=0),
    right_run_id: int = Query(gt=0),
    datahub: DataHub = Depends(get_datahub),
) -> PaperRunComparison:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: datahub.cache.compare_paper_trading_runs(left_run_id, right_run_id)
    )


@router.get("/runs/{run_id}", response_model=PaperTradingDashboard)
async def paper_trading_run_dashboard(
    run_id: int,
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> PaperTradingDashboard:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: get_paper_trading_dashboard(datahub.cache, run_id=run_id))


@router.get("/runs/{run_id}/export.json", response_model=PaperRunExport)
async def export_paper_trading_run_json(
    run_id: int,
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> PaperRunExport:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Disposition"] = f'attachment; filename="paper-trading-run-{run_id}.json"'
    return await run_sync_api_async(lambda: datahub.cache.paper_trading_run_export(run_id))


@router.get("/runs/{run_id}/export.csv")
async def export_paper_trading_run_csv(
    run_id: int,
    dataset: Literal["trades", "events"] = Query(default="trades"),
    datahub: DataHub = Depends(get_datahub),
) -> Response:
    exported = await run_sync_api_async(lambda: datahub.cache.paper_trading_run_export(run_id))
    content = _paper_csv(exported, dataset)
    return Response(
        content="\ufeff" + content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="paper-trading-run-{run_id}-{dataset}.csv"',
        },
    )


def _paper_csv(exported: PaperRunExport, dataset: Literal["trades", "events"]) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    if dataset == "trades":
        writer.writerow(
            [
                "run_id", "strategy_id", "symbol", "side", "trade_date", "price",
                "quantity", "gross_amount", "commission", "stamp_duty",
                "transfer_fee", "slippage", "total_cost", "reason",
            ]
        )
        for item in exported.trades:
            writer.writerow(
                [
                    item.run_id, item.strategy_id, _safe_csv(item.symbol), item.side,
                    item.trade_date, item.price, item.quantity, item.gross_amount,
                    item.commission_amount, item.stamp_duty_amount,
                    item.transfer_fee_amount, item.slippage_amount,
                    item.friction_amount, _safe_csv(item.reason),
                ]
            )
    else:
        writer.writerow(
            [
                "run_id", "sequence", "strategy_id", "symbol", "event_date",
                "event_code", "category", "severity", "message", "details_json",
            ]
        )
        for item in exported.events:
            writer.writerow(
                [
                    item.run_id, item.sequence, item.strategy_id or "",
                    _safe_csv(item.symbol or ""), item.event_date,
                    _safe_csv(item.event_code), item.category, item.severity,
                    _safe_csv(item.message),
                    _safe_csv(json.dumps(item.details, ensure_ascii=False, sort_keys=True)),
                ]
            )
    return output.getvalue()


def _safe_csv(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


__all__ = ["router"]
