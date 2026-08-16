"""Orchestration for point-in-time strategy runs and portfolio drafts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

from app.models.strategy_execution import (
    PortfolioCandidatePage,
    PortfolioCandidateStatus,
    PortfolioCandidateSort,
    PortfolioDraft,
    StrategyExecutionCandidateChange,
    StrategyExecutionComparison,
    StrategyExecutionPage,
    StrategyExecutionRequest,
)
from app.models.market_scan_snapshot import validate_frozen_full_market_snapshot
from app.repositories.strategy_execution import StrategyExecutionRepository
from app.services.strategy_lab import StrategyLabService
from app.services.strategy_portfolio import (
    STRATEGY_EXECUTION_FRESHNESS_POLICY_VERSION,
    build_portfolio_draft,
)
from app.services.trading_calendar import (
    TradingCalendarCoverageError,
    calendar_source,
    expected_quote_date,
    latest_expected_daily_kline_date,
    trading_day_gap,
)
from app.utils.audit_time import audit_now_text
from app.utils.clock import market_now_naive


class StrategyExecutionFreshnessError(ValueError):
    """Raised before writes when the latest full-market cohort is stale."""


class StrategyExecutionService:
    def __init__(
        self,
        repository: StrategyExecutionRepository,
        strategies: StrategyLabService,
        *,
        market_clock: Callable[[], datetime] = market_now_naive,
    ) -> None:
        self.repository = repository
        self.strategies = strategies
        self._market_clock = market_clock

    def execute(self, request: StrategyExecutionRequest) -> PortfolioDraft:
        strategy = self.strategies.get(request.strategy_id, revision=request.revision)
        if strategy.archived and request.kind == "latest_scan":
            raise ValueError("已归档策略不能执行新的最新扫描；仍可读取或重放历史证据")
        frozen = self.repository.frozen_scan(
            run_id=request.run_id,
            data_date=request.data_date,
            mode=request.mode,
        )
        validate_frozen_full_market_snapshot(frozen.run, frozen.items)
        if request.kind == "historical_replay" and frozen.run.data_date > (request.data_date or frozen.run.data_date):
            raise ValueError("历史重放不得读取目标日期之后的扫描数据")
        freshness_contract = self._latest_scan_freshness_contract(
            strategy.spec.evidence_policy.maximum_market_data_age_days,
            frozen.run.data_date,
            request,
        )
        computation = build_portfolio_draft(
            strategy,
            frozen.run,
            frozen.items,
            request,
            freshness_contract=freshness_contract,
        )
        validate_frozen_full_market_snapshot(frozen.run, frozen.items)
        execution_id = self.repository.save(
            strategy_id=strategy.strategy_id,
            strategy_revision=strategy.strategy_version,
            strategy_fingerprint=strategy.fingerprint,
            execution_fingerprint=computation.execution_fingerprint,
            kind=request.kind,
            run=frozen.run,
            cost_rule_fingerprint=computation.cost_rule_fingerprint,
            status=computation.summary.status,
            summary=computation.summary,
            candidates=computation.candidates,
            result_digest=computation.result_digest,
            timestamp=audit_now_text(),
        )
        return self.repository.draft(execution_id)

    def _latest_scan_freshness_contract(
        self,
        maximum_age_sessions: int,
        data_date_text: str,
        request: StrategyExecutionRequest,
    ) -> dict[str, object] | None:
        if request.kind != "latest_scan":
            return None
        data_date = date.fromisoformat(data_date_text)
        try:
            current = self._market_clock()
            reference_date = (
                expected_quote_date(current)
                if request.mode == "intraday"
                else latest_expected_daily_kline_date(current)
            )
            age_sessions = trading_day_gap(data_date, reference_date)
            source = calendar_source(reference_date)
        except TradingCalendarCoverageError as exc:
            raise StrategyExecutionFreshnessError(
                "可信交易日历不足，已拒绝执行最新扫描"
            ) from exc
        if data_date > reference_date:
            raise StrategyExecutionFreshnessError("最新扫描数据日晚于最近完成交易日，已拒绝执行")
        if age_sessions > maximum_age_sessions:
            raise StrategyExecutionFreshnessError(
                "最新全市场扫描已过期："
                f"{age_sessions} 个交易日，策略上限 {maximum_age_sessions} 个交易日"
            )
        return {
            "version": STRATEGY_EXECUTION_FRESHNESS_POLICY_VERSION,
            "reference_kind": (
                "current_exchange_quote_session"
                if request.mode == "intraday"
                else "latest_completed_exchange_session"
            ),
            "reference_date": reference_date.isoformat(),
            "calendar_source": source,
            "age_exchange_sessions": age_sessions,
            "maximum_age_exchange_sessions": maximum_age_sessions,
        }

    def draft(self, execution_id: int) -> PortfolioDraft:
        return self.repository.draft(execution_id)

    def require_action_source(self, execution_id: int) -> None:
        self.repository.require_action_source(execution_id)

    def candidates(
        self,
        execution_id: int,
        *,
        page: int,
        page_size: int,
        status: PortfolioCandidateStatus | None,
        sort_by: PortfolioCandidateSort = "utility_score",
        descending: bool = True,
    ) -> PortfolioCandidatePage:
        return self.repository.candidates(
            execution_id,
            page=page,
            page_size=page_size,
            status=status,
            sort_by=sort_by,
            descending=descending,
        )

    def executions(
        self,
        *,
        strategy_id: int,
        page: int,
        page_size: int,
    ) -> StrategyExecutionPage:
        self.strategies.get(strategy_id)
        return self.repository.executions(
            strategy_id=strategy_id,
            page=page,
            page_size=page_size,
        )

    def compare(self, left_execution_id: int, right_execution_id: int) -> StrategyExecutionComparison:
        left = self.draft(left_execution_id)
        right = self.draft(right_execution_id)
        left_items = {item.symbol: item for item in left.selected}
        right_items = {item.symbol: item for item in right.selected}

        def change(symbol: str) -> StrategyExecutionCandidateChange:
            old = left_items.get(symbol)
            new = right_items.get(symbol)
            item = new or old
            if item is None:
                raise RuntimeError("组合比较候选缺失")
            return StrategyExecutionCandidateChange(
                symbol=symbol,
                name=item.name,
                left_rank=old.utility_rank if old else None,
                right_rank=new.utility_rank if new else None,
                left_weight=old.target_weight if old else 0,
                right_weight=new.target_weight if new else 0,
            )

        common = left_items.keys() & right_items.keys()
        changed = [
            symbol for symbol in common
            if left_items[symbol].utility_rank != right_items[symbol].utility_rank
            or left_items[symbol].target_weight != right_items[symbol].target_weight
        ]
        return StrategyExecutionComparison(
            left=left.context,
            right=right.context,
            same_strategy_fingerprint=(
                left.context.strategy_fingerprint == right.context.strategy_fingerprint
            ),
            same_rule_version=left.context.rule_version == right.context.rule_version,
            added=[change(symbol) for symbol in sorted(right_items.keys() - left_items.keys())],
            removed=[change(symbol) for symbol in sorted(left_items.keys() - right_items.keys())],
            retained_changed=[change(symbol) for symbol in sorted(changed)],
        )


__all__ = ["StrategyExecutionFreshnessError", "StrategyExecutionService"]
