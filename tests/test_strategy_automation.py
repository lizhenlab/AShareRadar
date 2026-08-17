from __future__ import annotations

from datetime import date, datetime
import json
from unittest.mock import patch

import pytest

from app.artifacts.io import canonical_json_text, sha256_hex
from app.db.market_scan_action_source import MarketScanActionSourceError
from app.db.market_scan_integrity import (
    MarketScanSnapshotSealError,
    market_scan_snapshot_digest,
    seal_market_scan_snapshot,
)
from app.models.strategy_automation import StrategyAlertCondition, StrategyScheduleCreate
from app.models.strategy_execution import PortfolioDraft, StrategyExecutionRequest
from app.models.strategy_lab import StrategySpecArchiveRequest, StrategySpecUpdate
from app.repositories.strategy_automation import StrategyAutomationIntegrityError
from app.services.cache import SQLiteCache
from tests.market_scan_test_support import distribution_degraded_publication_diagnostics
from tests.test_strategy_execution import (
    _disable_market_scan_immutability,
    _environment,
    _seed_scan,
)


def test_schedule_is_version_pinned_runs_once_and_emits_fingerprinted_events(tmp_path) -> None:
    cache, _execution_service, strategy_id, run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id))
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


def test_stale_latest_schedule_fails_before_execution_event_or_order_writes(tmp_path) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    cache.strategy_automation_service.create_schedule(
        StrategyScheduleCreate(strategy_id=strategy_id)
    )
    execution_service._market_clock = lambda: datetime(2026, 8, 13, 16, 0)  # noqa: SLF001

    summary = cache.strategy_automation_service.run_due()

    assert summary.executed_count == 0
    assert summary.failed_count == 1
    assert "已过期" in summary.errors[0]
    with cache._connect() as conn:  # noqa: SLF001 - fail-closed write assertion
        assert conn.execute("SELECT COUNT(*) FROM strategy_execution").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM strategy_alert_event").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM strategy_simulation_plan").fetchone()[0] == 0


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


@pytest.mark.parametrize("action", ("create", "read"))
def test_simulation_plan_rejects_legacy_backfill_execution_source(
    tmp_path,
    action: str,
) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    if action == "read":
        cache.strategy_automation_service.create_simulation_plan(
            draft.context.execution_id
        )
    _reseal_execution_source_as_legacy(cache, draft)

    with pytest.raises(StrategyAutomationIntegrityError, match="原发布时快照"):
        if action == "create":
            cache.strategy_automation_service.create_simulation_plan(
                draft.context.execution_id
            )
        else:
            cache.strategy_automation_service.simulation_plan(
                draft.context.execution_id
            )


def test_schedule_rejects_legacy_previous_execution_before_alert_actions(
    tmp_path,
) -> None:
    cache, execution_service, strategy_id, run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(
        StrategyScheduleCreate(strategy_id=strategy_id)
    )
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    _reseal_execution_source_as_legacy(cache, draft)
    schedule = schedule.model_copy(
        update={"last_execution_id": draft.context.execution_id}
    )

    with (
        patch.object(execution_service, "execute") as execute,
        pytest.raises(StrategyAutomationIntegrityError, match="原发布时快照"),
    ):
        cache.strategy_automation_service._execute_claimed_schedule(schedule, run_id)
    execute.assert_not_called()


@pytest.mark.parametrize("action", ("create", "read"))
def test_simulation_plan_rejects_distribution_degraded_execution_source(
    tmp_path,
    action: str,
) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    if action == "read":
        cache.strategy_automation_service.create_simulation_plan(
            draft.context.execution_id
        )
    _reseal_execution_source_as_distribution_degraded(cache, draft)

    with pytest.raises(StrategyAutomationIntegrityError, match="发布门禁"):
        if action == "create":
            cache.strategy_automation_service.create_simulation_plan(
                draft.context.execution_id
            )
        else:
            cache.strategy_automation_service.simulation_plan(
                draft.context.execution_id
            )


def test_simulation_plan_rejects_payload_tamper(tmp_path) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    cache.strategy_automation_service.create_simulation_plan(draft.context.execution_id)
    with cache._connect() as conn:  # noqa: SLF001 - integrity boundary mutation
        row = conn.execute(
            "SELECT id, plan_json FROM strategy_simulation_plan WHERE execution_id = ?",
            (draft.context.execution_id,),
        ).fetchone()
        payload = json.loads(str(row["plan_json"]))
        payload["orders"][0]["name"] = "被篡改的模拟订单"
        conn.execute(
            "UPDATE strategy_simulation_plan SET plan_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), int(row["id"])),
        )

    with pytest.raises(StrategyAutomationIntegrityError, match="摘要不一致"):
        cache.strategy_automation_service.simulation_plan(draft.context.execution_id)


def test_simulation_plan_rejects_resealed_execution_binding_drift(tmp_path) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    cache.strategy_automation_service.create_simulation_plan(draft.context.execution_id)
    with cache._connect() as conn:  # noqa: SLF001 - integrity boundary mutation
        row = conn.execute(
            "SELECT id, plan_json FROM strategy_simulation_plan WHERE execution_id = ?",
            (draft.context.execution_id,),
        ).fetchone()
        payload = json.loads(str(row["plan_json"]))
        payload["data_as_of"] = "2099-01-01 15:00:00"
        unsigned = {name: value for name, value in payload.items() if name != "plan_digest"}
        payload["plan_digest"] = sha256_hex(canonical_json_text(unsigned))
        conn.execute(
            """
            UPDATE strategy_simulation_plan
            SET data_as_of = ?, plan_json = ?, plan_digest = ?
            WHERE id = ?
            """,
            (
                payload["data_as_of"],
                json.dumps(payload, ensure_ascii=False),
                payload["plan_digest"],
                int(row["id"]),
            ),
        )

    with pytest.raises(StrategyAutomationIntegrityError, match="原始策略执行"):
        cache.strategy_automation_service.simulation_plan(draft.context.execution_id)


def test_latest_automation_run_ignores_newer_custom_scope(tmp_path) -> None:
    cache, _execution_service, _strategy_id, full_run_id = _environment(tmp_path)
    partial = cache.create_market_scan_run(
        trigger="manual",
        mode="official",
        rule_version="partial-test",
        as_of="2099-01-02 15:00:00",
        data_date="2099-01-02",
        quote_date="2099-01-02",
        scope="自定义部分股票池",
    )
    cache.start_market_scan_run(partial.id)
    with cache._connect() as conn:  # noqa: SLF001 - selector boundary fixture
        conn.execute(
            """
            UPDATE market_scan_run
            SET status = 'success', finished_at = updated_at, message = '部分池测试批次'
            WHERE id = ?
            """,
            (partial.id,),
        )

    assert cache.strategy_automation_repo.latest_published_run_id("official") == full_run_id


def test_latest_distribution_degraded_run_fails_closed_without_old_run_fallback(
    tmp_path,
) -> None:
    cache, _execution_service, strategy_id, trusted_run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(
        StrategyScheduleCreate(strategy_id=strategy_id)
    )
    degraded_run_id = _seed_scan(cache, action_eligible=False)
    assert degraded_run_id > trusted_run_id

    with pytest.raises(MarketScanActionSourceError, match="评分分布门禁"):
        cache.strategy_automation_service.run_due()

    stored = cache.strategy_automation_service.schedules(
        strategy_id=strategy_id,
        include_disabled=True,
        page=1,
        page_size=20,
    ).items[0]
    assert stored.schedule_id == schedule.schedule_id
    assert stored.last_market_scan_run_id is None
    with cache._connect() as conn:  # noqa: SLF001 - fail-closed write assertion
        assert conn.execute("SELECT COUNT(*) FROM strategy_schedule_run").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM strategy_execution").fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ("tampered", "legacy_backfill"))
def test_latest_automation_run_rejects_newer_untrusted_published_snapshot(
    tmp_path,
    mutation: str,
) -> None:
    cache, _execution_service, _strategy_id, trusted_run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - selector attack fixture
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_run SET data_date = '2099-01-02' WHERE id = ?",
            (trusted_run_id,),
        )
        conn.execute(
            "UPDATE market_scan_run SET snapshot_digest = NULL WHERE id = ?",
            (trusted_run_id,),
        )
        conn.execute(
            "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
            (market_scan_snapshot_digest(conn, trusted_run_id), trusted_run_id),
        )
        newer_id = int(
            conn.execute(
                """
                INSERT INTO market_scan_run (
                    status, trigger, mode, rule_version, as_of, data_date, quote_date,
                    scope, total_count, processed_count, created_at, updated_at,
                    finished_at, snapshot_seal_origin, snapshot_sealed_at
                ) SELECT status, trigger, mode, rule_version, '2099-01-03 16:00:00',
                         '2099-01-03', '2099-01-03', scope, 0, 0,
                         '2099-01-03', '2099-01-03', '2099-01-03',
                         ?, '2099-01-03'
                  FROM market_scan_run WHERE id = ?
                """,
                (
                    "legacy_backfill" if mutation == "legacy_backfill" else "publication",
                    trusted_run_id,
                ),
            ).lastrowid
        )
        conn.execute(
            "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
            (market_scan_snapshot_digest(conn, newer_id), newer_id),
        )
        if mutation == "tampered":
            conn.execute(
                "UPDATE market_scan_run SET message = 'tampered' WHERE id = ?",
                (newer_id,),
            )

    with pytest.raises(MarketScanSnapshotSealError):
        cache.strategy_automation_repo.latest_published_run_id("official")


def test_archived_strategy_cannot_execute_or_reenable_automation(tmp_path) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id))
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


def test_archived_strategy_cannot_create_schedule_and_listing_accepts_no_filter(tmp_path) -> None:
    cache, _execution_service, strategy_id, _run_id = _environment(tmp_path)
    cache.strategy_lab_service.archive(
        strategy_id,
        StrategySpecArchiveRequest(expected_revision=1, archived=True),
    )

    with pytest.raises(ValueError, match="已归档策略不能创建定时任务"):
        cache.strategy_automation_service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id))

    page = cache.strategy_automation_service.schedules(
        strategy_id=None,
        include_disabled=False,
        page=1,
        page_size=20,
    )
    assert page.total == 0


def test_schedule_can_be_disabled_without_loading_strategy_revision(tmp_path) -> None:
    cache, _execution_service, strategy_id, _run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id))

    with patch.object(cache.strategy_lab_service, "get", side_effect=AssertionError("disable must not resolve strategy")):
        disabled = cache.strategy_automation_service.set_enabled(schedule.schedule_id, enabled=False)

    assert disabled.enabled is False


def test_blank_execution_failure_is_recorded_with_exception_type(tmp_path) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    cache.strategy_automation_service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id))

    with patch.object(execution_service, "execute", side_effect=RuntimeError()):
        summary = cache.strategy_automation_service.run_due()

    assert summary.failed_count == 1
    assert summary.errors == ["定时任务#1: RuntimeError"]
    stored = cache.strategy_automation_service.schedules(
        strategy_id=None,
        include_disabled=True,
        page=1,
        page_size=20,
    ).items[0]
    assert stored.last_market_scan_run_id is None


def test_claimed_schedule_rejects_market_scan_batch_drift(tmp_path) -> None:
    cache, _execution_service, strategy_id, run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id))

    with pytest.raises(RuntimeError, match="最新扫描批次发生变化"):
        cache.strategy_automation_service._execute_claimed_schedule(schedule, run_id + 1)


def test_simulation_plan_marks_empty_draft_as_no_trade(tmp_path) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    empty = draft.model_copy(
        update={
            "selected": [],
            "summary": draft.summary.model_copy(
                update={
                    "status": "no_trade",
                    "no_trade": True,
                    "selected_count": 0,
                    "evidence_verified_count": 0,
                }
            ),
        }
    )

    with patch.object(execution_service, "draft", return_value=empty):
        plan = cache.strategy_automation_service.create_simulation_plan(draft.context.execution_id)

    assert plan.status == "no_trade"
    assert plan.orders == []


def test_utility_and_policy_events_ignore_null_or_non_crossing_values(tmp_path) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(
        StrategyScheduleCreate(
            strategy_id=strategy_id,
            alert_conditions=[
                StrategyAlertCondition(event_type="utility_cross", utility_threshold=60),
                StrategyAlertCondition(event_type="data_stale"),
                StrategyAlertCondition(event_type="evidence_invalid"),
            ],
        )
    )
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    candidate = draft.selected[0]
    previous = draft.model_copy(update={"selected": [candidate.model_copy(update={"utility_score": None})]})
    current = draft.model_copy(
        update={
            "context": draft.context.model_copy(update={"data_date": date.today().isoformat()}),
            "selected": [candidate.model_copy(update={"utility_score": 59.0})],
            "summary": draft.summary.model_copy(
                update={
                    "selected_count": 1,
                    "evidence_verified_count": 1,
                    "no_trade": False,
                }
            ),
        }
    )

    emitted = cache.strategy_automation_service._emit_utility_events(
        schedule,
        previous,
        current,
        {candidate.symbol: previous.selected[0]},
        {candidate.symbol: current.selected[0]},
    )
    policy_emitted = cache.strategy_automation_service._emit_policy_events(schedule, current)

    assert emitted == 0
    assert policy_emitted == 0


def test_policy_events_report_stale_data_and_invalid_evidence(tmp_path) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(
        StrategyScheduleCreate(
            strategy_id=strategy_id,
            alert_conditions=[
                StrategyAlertCondition(event_type="data_stale"),
                StrategyAlertCondition(event_type="evidence_invalid"),
            ],
        )
    )
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    current = draft.model_copy(
        update={
            "context": draft.context.model_copy(update={"data_date": "2020-01-02"}),
            "summary": draft.summary.model_copy(
                update={
                    "selected_count": 1,
                    "evidence_verified_count": 0,
                    "no_trade": False,
                }
            ),
        }
    )

    with patch(
        "app.services.strategy_automation.market_now_naive",
        return_value=datetime(2026, 5, 13, 16, 0),
    ):
        emitted = cache.strategy_automation_service._emit_policy_events(schedule, current)

    events = cache.strategy_automation_service.events(
        strategy_id=strategy_id,
        schedule_id=schedule.schedule_id,
        page=1,
        page_size=20,
    )
    assert emitted == 2
    assert {item.event_type for item in events.items} == {
        "data_stale",
        "evidence_invalid",
    }


def _reseal_execution_source_as_legacy(
    cache: SQLiteCache,
    draft: PortfolioDraft,
) -> None:
    with cache._connect() as conn:  # noqa: SLF001 - privileged migration fixture
        _disable_market_scan_immutability(conn)
        conn.execute("DROP TRIGGER IF EXISTS trg_strategy_execution_no_update")
        conn.execute(
            """
            UPDATE market_scan_run
            SET snapshot_digest = NULL,
                snapshot_seal_origin = NULL,
                snapshot_sealed_at = NULL
            WHERE id = ?
            """,
            (draft.context.market_scan_run_id,),
        )
        digest = seal_market_scan_snapshot(
            conn,
            draft.context.market_scan_run_id,
            origin="legacy_backfill",
            sealed_at="2099-01-01 00:00:00",
        )
        conn.execute(
            """
            UPDATE strategy_execution
            SET source_snapshot_digest = ?,
                source_snapshot_seal_origin = 'legacy_backfill'
            WHERE id = ?
            """,
            (digest, draft.context.execution_id),
        )


def _reseal_execution_source_as_distribution_degraded(
    cache: SQLiteCache,
    draft: PortfolioDraft,
) -> None:
    with cache._connect() as conn:  # noqa: SLF001 - privileged migration fixture
        _disable_market_scan_immutability(conn)
        conn.execute("DROP TRIGGER IF EXISTS trg_strategy_execution_no_update")
        conn.execute(
            """
            UPDATE market_scan_run
            SET snapshot_digest = NULL,
                snapshot_seal_origin = NULL,
                snapshot_sealed_at = NULL,
                publication_diagnostics_json = ?
            WHERE id = ?
            """,
            (
                distribution_degraded_publication_diagnostics().model_dump_json(),
                draft.context.market_scan_run_id,
            ),
        )
        digest = seal_market_scan_snapshot(
            conn,
            draft.context.market_scan_run_id,
            origin="publication",
            sealed_at="2099-01-01 00:00:00",
        )
        conn.execute(
            """
            UPDATE strategy_execution
            SET source_snapshot_digest = ?
            WHERE id = ?
            """,
            (digest, draft.context.execution_id),
        )


def test_utility_cross_emits_one_fingerprinted_event(tmp_path) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    schedule = cache.strategy_automation_service.create_schedule(
        StrategyScheduleCreate(
            strategy_id=strategy_id,
            alert_conditions=[StrategyAlertCondition(event_type="utility_cross", utility_threshold=60)],
        )
    )
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    candidate = draft.selected[0]
    previous = draft.model_copy(update={"selected": [candidate.model_copy(update={"utility_score": 59.0})]})
    current = draft.model_copy(update={"selected": [candidate.model_copy(update={"utility_score": 60.0})]})

    emitted = cache.strategy_automation_service._emit_utility_events(
        schedule,
        previous,
        current,
        {candidate.symbol: previous.selected[0]},
        {candidate.symbol: current.selected[0]},
    )

    assert emitted == 1
    events = cache.strategy_automation_service.events(
        strategy_id=strategy_id,
        schedule_id=schedule.schedule_id,
        page=1,
        page_size=20,
    )
    assert events.items[0].event_type == "utility_cross"
