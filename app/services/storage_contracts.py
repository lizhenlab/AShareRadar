"""Narrow storage ports used by domain orchestration services."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.models.paper_trading import (
    PaperStrategy,
    PaperStrategyCreate,
    PaperTradingAccount,
    PaperTradingAccountUpdate,
    PaperTradingDashboard,
)
from app.models.reviews import (
    AdviceReviewDetail,
    AdviceReviewPlan,
    AdviceReviewPlanInput,
    AdviceReviewPlanUpdate,
    AdviceReviewSummary,
)


class AdviceReviewStorage(Protocol):
    def create_advice_review_plan(self, payload: AdviceReviewPlanInput) -> AdviceReviewPlan: ...

    def update_advice_review_plan(
        self,
        plan_id: int,
        payload: AdviceReviewPlanUpdate,
    ) -> AdviceReviewPlan | None: ...

    def delete_advice_review_plan(self, plan_id: int) -> bool: ...

    def advice_review_detail(self, plan_id: int) -> AdviceReviewDetail | None: ...

    def advice_review_plans(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AdviceReviewPlan]: ...

    def advice_review_details(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AdviceReviewDetail]: ...

    def advice_review_summary(self) -> AdviceReviewSummary: ...


class PaperTradingStorage(Protocol):
    def paper_trading_dashboard(self, *, run_id: int | None = None) -> PaperTradingDashboard: ...

    def update_paper_trading_account(
        self,
        payload: PaperTradingAccountUpdate,
    ) -> PaperTradingAccount: ...

    def advice_review_plan(self, plan_id: int) -> AdviceReviewPlan | None: ...

    def create_paper_strategy(
        self,
        plan: AdviceReviewPlan,
        payload: PaperStrategyCreate,
        *,
        activation_market_time: str,
    ) -> PaperStrategy: ...

    def delete_pending_paper_strategy(self, strategy_id: int) -> bool: ...


class MarketScanLifecycleStorage(Protocol):
    path: Path

    def reconcile_incomplete_market_scans(self) -> int: ...


__all__ = [
    "AdviceReviewStorage",
    "MarketScanLifecycleStorage",
    "PaperTradingStorage",
]
