from __future__ import annotations

from app.models.strategy_automation import StrategyScheduleCreate
from app.models.strategy_execution import StrategyExecutionRequest
from app.models.strategy_lab import StrategySpecArchiveRequest, StrategySpecUpdate
from tests.test_strategy_execution import _environment


def test_schedule_is_version_pinned_runs_once_and_emits_fingerprinted_events(tmp_path) -> None:
    cache, _execution_service, strategy_id, run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(
        StrategyScheduleCreate(strategy_id=strategy_id)
    )
    original = cache.strategy_lab_service.get(strategy_id)
    cache.strategy_lab_service.update(
        strategy_id,
        StrategySpecUpdate(
            spec=original.spec.model_copy(update={"description": "新修订"}),
            expected_revision=1,
            confirmed=True,
        ),
    )

    first = cache.strategy_automation_service.run_due()
    second = cache.strategy_automation_service.run_due()
    stored = cache.strategy_automation_service.schedules(
        strategy_id=strategy_id,
        include_disabled=True,
        page=1,
        page_size=20,
    ).items[0]
    events = cache.strategy_automation_service.events(
        strategy_id=strategy_id,
        schedule_id=schedule.schedule_id,
        page=1,
        page_size=100,
    )

    assert first.executed_count == 1
    assert first.failed_count == 0
    assert second.executed_count == 0
    assert second.skipped_count == 1
    assert stored.strategy_version == 1
    assert stored.last_market_scan_run_id == run_id
    assert stored.last_execution_id is not None
    assert events.total >= 2
    assert all(item.strategy_version == 1 for item in events.items)
    assert all(item.strategy_fingerprint == schedule.strategy_fingerprint for item in events.items)
    assert all(len(item.execution_fingerprint) == 64 for item in events.items)


def test_simulation_plan_preserves_lifecycle_fingerprints_and_never_submits_orders(tmp_path) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))

    plan = cache.strategy_automation_service.create_simulation_plan(draft.context.execution_id)
    repeated = cache.strategy_automation_service.create_simulation_plan(draft.context.execution_id)
    loaded = cache.strategy_automation_service.simulation_plan(draft.context.execution_id)

    assert plan == repeated
    assert loaded == plan
    assert plan.strategy_fingerprint == draft.context.strategy_fingerprint
    assert plan.execution_fingerprint == draft.context.execution_fingerprint
    assert plan.cost_rule_fingerprint == draft.context.cost_rule_fingerprint
    assert plan.rule_version == draft.context.rule_version
    assert len(plan.orders) == draft.summary.selected_count
    assert all(item.research_side == "paper_buy" for item in plan.orders)
    assert any("不连接券商" in item for item in plan.disclaimers)


def test_archived_strategy_cannot_execute_or_reenable_automation(tmp_path) -> None:
    import pytest

    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(
        StrategyScheduleCreate(strategy_id=strategy_id)
    )
    cache.strategy_lab_service.archive(
        strategy_id,
        StrategySpecArchiveRequest(expected_revision=1, archived=True),
    )

    summary = cache.strategy_automation_service.run_due()
    stored = cache.strategy_automation_service.schedules(
        strategy_id=strategy_id,
        include_disabled=True,
        page=1,
        page_size=20,
    ).items[0]

    assert summary.skipped_count == 1
    assert stored.enabled is False
    with pytest.raises(ValueError, match="已归档策略"):
        execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    with pytest.raises(ValueError, match="不能重新启用"):
        cache.strategy_automation_service.set_enabled(schedule.schedule_id, enabled=True)
