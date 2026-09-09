from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
from unittest.mock import patch

import pytest

from app.config import Settings
from app.models.strategy_automation import StrategyScheduleCreate
from app.repositories.strategy_automation import (
    StrategyAutomationIntegrityError,
    StrategyAutomationRepository,
)
from app.repositories import strategy_automation as automation_repository
from app.services.cache import SQLiteCache
from tests.test_strategy_execution import _environment


@contextmanager
def _isolated_environment(tmp_path):
    def cache_factory(path):
        return SQLiteCache(path, settings=Settings(cache_path=path))
    with patch("tests.test_strategy_execution.SQLiteCache", side_effect=cache_factory):
        yield _environment(tmp_path)


@pytest.mark.parametrize("failure", ("second_event", "completion", "schedule_pointer"))
def test_failed_event_batch_is_invisible_and_retry_publishes_once(tmp_path, failure) -> None:
    with _isolated_environment(tmp_path) as (cache, _execution, strategy_id, run_id):
        service = cache.domain_services.strategy_automation
        schedule = service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id))
        with cache._connect() as conn:
            if failure == "second_event":
                conn.execute("""
                    CREATE TRIGGER injected_failure BEFORE INSERT ON strategy_alert_event
                    WHEN (
                        SELECT COUNT(*) FROM strategy_alert_event
                        WHERE schedule_id = NEW.schedule_id
                          AND execution_id = NEW.execution_id
                    ) = 1
                    BEGIN SELECT RAISE(ABORT, 'injected second event failure'); END
                """)
            elif failure == "completion":
                conn.execute("""
                    CREATE TRIGGER injected_failure BEFORE UPDATE ON strategy_schedule_run
                    WHEN NEW.status = 'completed'
                    BEGIN SELECT RAISE(ABORT, 'injected completion failure'); END
                """)
            else:
                conn.execute("""
                    CREATE TRIGGER injected_failure BEFORE UPDATE ON strategy_schedule
                    WHEN NEW.last_execution_id IS NOT NULL
                    BEGIN SELECT RAISE(ABORT, 'injected schedule pointer failure'); END
                """)
        first = service.run_due()
        assert first.failed_count == 1
        assert first.event_count == 0
        assert service.events(strategy_id=None, schedule_id=schedule.schedule_id, page=1, page_size=100).total == 0
        stored = service.repository.schedule(schedule.schedule_id)
        assert stored.last_execution_id is None
        assert stored.last_market_scan_run_id is None
        with cache._connect() as conn:
            assert conn.execute("SELECT status FROM strategy_schedule_run").fetchone()[0] == "failed"
            conn.execute("DROP TRIGGER injected_failure")
        retry = service.run_due()
        repeated = service.run_due()
        assert retry.executed_count == 1
        assert repeated.skipped_count == 1
        events = service.events(strategy_id=None, schedule_id=schedule.schedule_id, page=1, page_size=100)
        assert events.total == retry.event_count
        keys = [(event.event_type, event.symbol, event.data_as_of) for event in events.items]
        assert all(count == 1 for count in Counter(keys).values())
        stored = service.repository.schedule(schedule.schedule_id)
        assert stored.last_market_scan_run_id == run_id
        assert {event.execution_id for event in events.items} == {stored.last_execution_id}


def test_concurrent_evaluation_cannot_publish_same_claim_twice(tmp_path) -> None:
    with _isolated_environment(tmp_path) as (cache, _execution, strategy_id, _run_id):
        service = cache.domain_services.strategy_automation
        schedule = service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id))
        entered, release = threading.Event(), threading.Event()
        execute = service._execute_claimed_schedule
        def blocked_execute(*args):
            entered.set()
            assert release.wait(5)
            return execute(*args)
        with ThreadPoolExecutor(max_workers=1) as workers, patch.object(
            service, "_execute_claimed_schedule", side_effect=blocked_execute,
        ):
            first = workers.submit(service.run_due)
            try:
                assert entered.wait(1)
                second = service.run_due()
                assert second.skipped_count == 1
                assert second.event_count == 0
            finally:
                release.set()
            completed = first.result(timeout=2)
        events = service.events(strategy_id=None, schedule_id=schedule.schedule_id, page=1, page_size=100)
        assert completed.executed_count == 1
        assert events.total == completed.event_count
        assert len({event.execution_id for event in events.items}) == 1


def test_other_connection_never_observes_a_partial_event_batch(tmp_path) -> None:
    with _isolated_environment(tmp_path) as (cache, _execution, strategy_id, _run_id):
        service = cache.domain_services.strategy_automation
        schedule = service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id))
        reader = StrategyAutomationRepository(cache.path)
        entered, release = threading.Event(), threading.Event()
        insert = automation_repository._insert_event
        def blocked_insert(*args):
            insert(*args)
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
        with ThreadPoolExecutor(max_workers=1) as workers, patch.object(
            automation_repository, "_insert_event", side_effect=blocked_insert,
        ):
            first = workers.submit(service.run_due)
            try:
                assert entered.wait(1)
                assert reader.events(strategy_id=None, schedule_id=schedule.schedule_id, page=1, page_size=100).total == 0
                assert reader.schedule(schedule.schedule_id).last_execution_id is None
            finally:
                release.set()
            completed = first.result(timeout=2)
        assert reader.events(strategy_id=None, schedule_id=schedule.schedule_id, page=1, page_size=100).total == completed.event_count


def test_completed_claim_rejects_a_second_completion_without_event_writes(tmp_path) -> None:
    with _isolated_environment(tmp_path) as (cache, execution, strategy_id, run_id):
        service = cache.domain_services.strategy_automation
        schedule = service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id))
        first = service.run_due()
        stored = service.repository.schedule(schedule.schedule_id)
        draft = execution.draft(stored.last_execution_id)
        events = service._build_events(schedule, None, draft)
        with pytest.raises(StrategyAutomationIntegrityError, match="已结束"):
            service.repository.complete_run(schedule, run_id, draft.context, events, timestamp=stored.updated_at)
        assert service.events(strategy_id=None, schedule_id=schedule.schedule_id, page=1, page_size=100).total == first.event_count
        service.repository.fail_run(schedule.schedule_id, run_id, error="late failure", timestamp=stored.updated_at)
        with cache._connect() as conn:
            assert conn.execute("SELECT status FROM strategy_schedule_run").fetchone()[0] == "completed"


def test_schedule_without_alert_conditions_completes_once(tmp_path) -> None:
    with _isolated_environment(tmp_path) as (cache, _execution, strategy_id, run_id):
        service = cache.domain_services.strategy_automation
        schedule = service.create_schedule(StrategyScheduleCreate(strategy_id=strategy_id, alert_conditions=[]))
        first = service.run_due()
        assert first.executed_count == 1
        assert first.event_count == 0
        assert service.repository.schedule(schedule.schedule_id).last_market_scan_run_id == run_id
        assert service.run_due().skipped_count == 1
