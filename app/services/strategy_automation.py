"""Version-pinned strategy schedules, deterministic alerts and paper plan handoff."""

from __future__ import annotations

from datetime import date
import hashlib
import json

from app.db.market_scan_integrity import MarketScanSnapshotSealError
from app.models.strategy_automation import (
    StrategyAlertEventPage,
    StrategyAutomationRunSummary,
    StrategySchedule,
    StrategyScheduleCreate,
    StrategySchedulePage,
    StrategySimulationOrder,
    StrategySimulationPlan,
)
from app.models.strategy_execution import PortfolioCandidate, PortfolioDraft, StrategyExecutionRequest
from app.repositories.strategy_automation import StrategyAutomationRepository
from app.repositories.strategy_automation import StrategyAutomationIntegrityError
from app.services.strategy_execution import StrategyExecutionService
from app.services.strategy_lab import StrategyLabService
from app.utils.audit_time import audit_now_text
from app.utils.clock import market_now_naive


class StrategyAutomationService:
    def __init__(
        self,
        repository: StrategyAutomationRepository,
        strategies: StrategyLabService,
        executions: StrategyExecutionService,
    ) -> None:
        self.repository = repository
        self.strategies = strategies
        self.executions = executions

    def create_schedule(self, payload: StrategyScheduleCreate) -> StrategySchedule:
        strategy = self.strategies.get(payload.strategy_id, revision=payload.revision)
        if strategy.archived:
            raise ValueError("已归档策略不能创建定时任务")
        return self.repository.create_schedule(
            payload,
            revision=strategy.strategy_version,
            fingerprint=strategy.fingerprint,
            timestamp=audit_now_text(),
        )

    def schedules(
        self,
        *,
        strategy_id: int | None,
        include_disabled: bool,
        page: int,
        page_size: int,
    ) -> StrategySchedulePage:
        if strategy_id is not None:
            self.strategies.get(strategy_id)
        return self.repository.schedules(
            strategy_id=strategy_id,
            include_disabled=include_disabled,
            page=page,
            page_size=page_size,
        )

    def set_enabled(self, schedule_id: int, *, enabled: bool) -> StrategySchedule:
        if enabled:
            schedule = self.repository.schedule(schedule_id)
            strategy = self.strategies.get(
                schedule.strategy_id,
                revision=schedule.strategy_version,
            )
            if strategy.archived:
                raise ValueError("已归档策略的定时任务不能重新启用")
        return self.repository.set_enabled(
            schedule_id,
            enabled=enabled,
            timestamp=audit_now_text(),
        )

    def events(
        self,
        *,
        strategy_id: int | None,
        schedule_id: int | None,
        page: int,
        page_size: int,
    ) -> StrategyAlertEventPage:
        return self.repository.events(
            strategy_id=strategy_id,
            schedule_id=schedule_id,
            page=page,
            page_size=page_size,
        )

    def run_due(self) -> StrategyAutomationRunSummary:
        checked = executed = skipped = failed = event_count = 0
        errors: list[str] = []
        for schedule in self.repository.enabled_schedules():
            checked += 1
            outcome, emitted, error = self._run_schedule(schedule)
            event_count += emitted
            if outcome == "executed":
                executed += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1
                errors.append(f"定时任务#{schedule.schedule_id}: {error or '未知错误'}")
        return StrategyAutomationRunSummary(
            checked_count=checked,
            executed_count=executed,
            skipped_count=skipped,
            failed_count=failed,
            event_count=event_count,
            errors=errors[:20],
        )

    def _run_schedule(self, schedule: StrategySchedule) -> tuple[str, int, str | None]:
        strategy = self.strategies.get(
            schedule.strategy_id,
            revision=schedule.strategy_version,
        )
        if strategy.archived:
            self.repository.set_enabled(
                schedule.schedule_id,
                enabled=False,
                timestamp=audit_now_text(),
            )
            return "skipped", 0, None
        run_id = self.repository.latest_published_run_id(schedule.mode)
        if run_id is None or run_id == schedule.last_market_scan_run_id:
            return "skipped", 0, None
        if not self.repository.claim_run(
            schedule.schedule_id,
            run_id,
            timestamp=audit_now_text(),
        ):
            return "skipped", 0, None
        try:
            event_count, execution_id = self._execute_claimed_schedule(schedule, run_id)
        except Exception as exc:
            message = " ".join(str(exc).split())[:300] or type(exc).__name__
            self.repository.finish_run(
                schedule.schedule_id,
                run_id,
                execution_id=None,
                error=message,
                timestamp=audit_now_text(),
            )
            return "failed", 0, message
        self.repository.finish_run(
            schedule.schedule_id,
            run_id,
            execution_id=execution_id,
            error=None,
            timestamp=audit_now_text(),
        )
        return "executed", event_count, None

    def _execute_claimed_schedule(
        self,
        schedule: StrategySchedule,
        run_id: int,
    ) -> tuple[int, int]:
        previous = (
            self.executions.draft(schedule.last_execution_id)
            if schedule.last_execution_id is not None
            else None
        )
        if previous is not None:
            self._require_action_source(previous)
        current = self.executions.execute(
            StrategyExecutionRequest(
                strategy_id=schedule.strategy_id,
                revision=schedule.strategy_version,
                kind="latest_scan",
                mode=schedule.mode,
                notional_cash_cny=schedule.notional_cash_cny,
            )
        )
        if current.context.market_scan_run_id != run_id:
            raise RuntimeError("定时执行期间最新扫描批次发生变化，请重试")
        return self._emit_events(schedule, previous, current), current.context.execution_id

    def _require_action_source(self, draft: PortfolioDraft) -> None:
        _require_publication_action_source(draft)
        try:
            self.executions.require_action_source(draft.context.execution_id)
        except MarketScanSnapshotSealError as exc:
            raise StrategyAutomationIntegrityError(
                "策略自动化动作来源未通过全市场发布门禁"
            ) from exc

    def create_simulation_plan(self, execution_id: int) -> StrategySimulationPlan:
        draft = self.executions.draft(execution_id)
        self._require_action_source(draft)
        strategy = self.strategies.get(
            draft.context.strategy_id,
            revision=draft.context.strategy_version,
        )
        orders = [
            StrategySimulationOrder(
                symbol=item.symbol,
                name=item.name,
                board_label=item.board_label,
                target_weight=item.target_weight,
                target_quantity=item.target_quantity,
                estimated_gross_amount_cny=item.estimated_gross_amount_cny,
                estimated_round_trip_cost_cny=item.estimated_round_trip_cost_cny,
                earliest_exit_policy=(
                    f"A股 T+1；计划持有 {strategy.spec.rebalance_policy.hold_sessions} 个交易日，"
                    "实际模拟退出仍受停牌和涨跌停约束"
                ),
                constraint_notes=item.reasons,
            )
            for item in draft.selected
        ]
        payload: dict[str, object] = {
            "execution_id": execution_id,
            "strategy_id": draft.context.strategy_id,
            "strategy_version": draft.context.strategy_version,
            "strategy_fingerprint": draft.context.strategy_fingerprint,
            "execution_fingerprint": draft.context.execution_fingerprint,
            "rule_version": draft.context.rule_version,
            "data_as_of": draft.context.data_as_of,
            "cost_rule_fingerprint": draft.context.cost_rule_fingerprint,
            "status": "draft" if orders else "no_trade",
            "orders": [item.model_dump(mode="json") for item in orders],
            "disclaimers": [
                "这是由冻结组合草案生成的模拟交易研究计划，不连接券商、不提交真实委托。",
                "日K数据无法证明盘口排队或真实成交，涨跌停、停牌和零成交可能导致无法成交。",
                "计划固定绑定策略、执行、评分、数据时点和成本规则指纹。",
            ],
        }
        digest = _stable_digest(payload)
        payload["plan_digest"] = digest
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self.repository.save_simulation_plan(
            values=payload,
            rendered_plan=rendered,
            timestamp=audit_now_text(),
        )

    def simulation_plan(self, execution_id: int) -> StrategySimulationPlan | None:
        # Require the execution to exist even when no plan has been generated,
        # so a bad identifier is a typed 404 rather than an ambiguous null.
        self._require_action_source(self.executions.draft(execution_id))
        return self.repository.simulation_plan(execution_id)

    def _emit_events(
        self,
        schedule: StrategySchedule,
        previous: PortfolioDraft | None,
        current: PortfolioDraft,
    ) -> int:
        conditions = {item.event_type: item for item in schedule.alert_conditions}
        previous_by_symbol = _selected_by_symbol(previous)
        current_by_symbol = _selected_by_symbol(current)
        emitted = 0
        if "new_entry" in conditions:
            for symbol in sorted(current_by_symbol.keys() - previous_by_symbol.keys()):
                emitted += self._event(
                    schedule, current, "new_entry", symbol,
                    f"{symbol} 新进入策略组合草案",
                    {"previous_execution_id": previous.context.execution_id if previous else None},
                )
        if "removed" in conditions:
            for symbol in sorted(previous_by_symbol.keys() - current_by_symbol.keys()):
                emitted += self._event(
                    schedule, current, "removed", symbol,
                    f"{symbol} 已从策略组合草案移除",
                    {"previous_execution_id": previous.context.execution_id if previous else None},
                )
        emitted += self._emit_utility_events(
            schedule,
            previous,
            current,
            previous_by_symbol,
            current_by_symbol,
        )
        emitted += self._emit_policy_events(schedule, current)
        return emitted

    def _emit_utility_events(
        self,
        schedule: StrategySchedule,
        previous: PortfolioDraft | None,
        current: PortfolioDraft,
        previous_by_symbol: dict[str, PortfolioCandidate],
        current_by_symbol: dict[str, PortfolioCandidate],
    ) -> int:
        condition = next(
            (item for item in schedule.alert_conditions if item.event_type == "utility_cross"),
            None,
        )
        if condition is None or previous is None:
            return 0
        threshold = float(condition.utility_threshold or 0)
        emitted = 0
        for symbol in sorted(previous_by_symbol.keys() & current_by_symbol.keys()):
            old = previous_by_symbol[symbol].utility_score
            new = current_by_symbol[symbol].utility_score
            if old is not None and new is not None and old < threshold <= new:
                emitted += self._event(
                    schedule, current, "utility_cross", symbol,
                    f"{symbol} 策略效用分由 {old:.2f} 上穿 {threshold:.2f}",
                    {"previous": old, "current": new, "threshold": threshold},
                )
        return emitted

    def _emit_policy_events(self, schedule: StrategySchedule, current: PortfolioDraft) -> int:
        event_types = {item.event_type for item in schedule.alert_conditions}
        strategy = self.strategies.get(schedule.strategy_id, revision=schedule.strategy_version)
        emitted = 0
        if "data_stale" in event_types:
            age_days = (market_now_naive().date() - date.fromisoformat(current.context.data_date)).days
            maximum = strategy.spec.evidence_policy.maximum_market_data_age_days
            if age_days > maximum:
                emitted += self._event(
                    schedule, current, "data_stale", None,
                    f"策略数据已过期：{age_days} 天，策略上限 {maximum} 天",
                    {"age_days": age_days, "maximum_age_days": maximum},
                )
        evidence_invalid = (
            current.summary.selected_count > current.summary.evidence_verified_count
            or current.summary.no_trade
        )
        if "evidence_invalid" in event_types and evidence_invalid:
            emitted += self._event(
                schedule, current, "evidence_invalid", None,
                "策略证据校验失效或当前输出为 no_trade",
                {
                    "selected_count": current.summary.selected_count,
                    "verified_count": current.summary.evidence_verified_count,
                    "no_trade": current.summary.no_trade,
                },
            )
        return emitted

    def _event(
        self,
        schedule: StrategySchedule,
        current: PortfolioDraft,
        event_type: str,
        symbol: str | None,
        message: str,
        trigger: dict[str, object],
    ) -> int:
        self.repository.add_event(
            schedule,
            execution_id=current.context.execution_id,
            execution_fingerprint=current.context.execution_fingerprint,
            data_as_of=current.context.data_as_of,
            event_type=event_type,
            symbol=symbol,
            message=message,
            trigger=trigger,
            timestamp=audit_now_text(),
        )
        return 1


def _selected_by_symbol(draft: PortfolioDraft | None) -> dict[str, PortfolioCandidate]:
    if draft is None:
        return {}
    return {item.symbol: item for item in draft.selected}


def _require_publication_action_source(draft: PortfolioDraft) -> None:
    if draft.context.source_snapshot_seal_origin != "publication":
        raise StrategyAutomationIntegrityError(
            "策略自动化动作要求原发布时快照封印"
        )


def _stable_digest(value: object) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


__all__ = ["StrategyAutomationService"]
