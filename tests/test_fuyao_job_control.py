from __future__ import annotations

import asyncio

from app.models.fuyao_research import FuyaoJob, FuyaoJobRequest
from test_fuyao_service import service as service_fixture

service = service_fixture


def test_published_item_has_atomic_durable_checkpoint(service, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def collect(job, _request):
        await service._save_item(job, 'valuations', '600519.SH', {'values': {'pe_ttm': 2}})
        entered.set()
        await release.wait()
    monkeypatch.setattr(service, '_collect', collect)
    async def run():
        await service.start_job(FuyaoJobRequest(kind='valuations', symbols=['600519.SH', '000001.SZ']))
        await entered.wait()
        stored = service.repository.jobs()[0]
        try:
            assert stored.completed == 1
            assert stored.completed_symbols == ['600519.SH']
            assert stored.request.symbols == ['600519.SH', '000001.SZ']
            assert stored.updated_at
        finally:
            release.set()
            await asyncio.gather(*tuple(service._tasks.values()))
            await service.aclose()
    asyncio.run(run())


def test_retry_is_idempotent_and_excludes_completed_symbols(service, monkeypatch):
    async def collect(job, request):
        for symbol in request.symbols:
            await service._save_item(job, 'valuations', symbol, {'values': {'pe_ttm': 2}})
    monkeypatch.setattr(service, '_collect', collect)
    parent = FuyaoJob(id='partial', kind='valuations', status='degraded', created_at='2026-09-10T00:00:00Z',
                     total=2, completed=1, completed_symbols=['600519.SH'],
                     request=FuyaoJobRequest(kind='valuations', symbols=['600519.SH', '000001.SZ']))
    service.repository.save_job(parent)
    async def run():
        child = await service.retry_job('partial')
        duplicate = await service.retry_job('partial')
        assert child.id == duplicate.id and child.parent_job_id == 'partial'
        assert child.request.symbols == ['000001.SZ']
        await asyncio.gather(*tuple(service._tasks.values()))
        assert (await service.get_job('partial')).status == 'degraded'
        assert service.repository.latest('valuations', '600519.SH') is None
        await service.aclose()
    asyncio.run(run())


def test_explicit_cancel_is_idempotent_and_releases_slot_after_cleanup(service, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def collect(job, _request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()
    monkeypatch.setattr(service, '_collect', collect)
    async def run():
        job = await service.start_job(FuyaoJobRequest(kind='history_full'))
        await entered.wait()
        cancelling = await service.cancel_job(job.id)
        assert cancelling.status == 'cancelling' and job.id in service._tasks
        again = await service.cancel_job(job.id)
        assert again.status == 'cancelling'
        release.set()
        await asyncio.gather(*tuple(service._tasks.values()), return_exceptions=True)
        final = await service.get_job(job.id)
        assert final.status == 'cancelled'
        assert (await service.cancel_job(job.id)).status == 'cancelled'
        await service.aclose()
    asyncio.run(run())


def test_cancel_before_first_execution_never_calls_provider(service, monkeypatch):
    from app.services.fuyao_job_runtime import FuyaoJobExecution
    async def run():
        request = FuyaoJobRequest(kind='valuations', symbols=['600519.SH'])
        job = FuyaoJob(id='not-started', kind='valuations', status='running', created_at='2026-09-10T00:00:00Z', request=request)
        service._recovered = True
        service.repository.save_job(job)
        execution = FuyaoJobExecution(job)
        service._job_records[job.id], service._executions[job.id] = job, execution
        task = asyncio.create_task(service._run_job(job, request))
        service._tasks[job.id] = task
        job.status = 'cancelling'
        execution.stop(task)
        assert not execution.started and not task.cancelled()
        await task
        assert service.repository.job(job.id).status == 'cancelled'
        service._finished(job.id, task)
        await service.aclose()
    asyncio.run(run())


def test_recovery_includes_old_cancelling_jobs_beyond_recent_hundred(service):
    service.repository.save_job(FuyaoJob(id='old', kind='history_full', status='cancelling', created_at='2026-09-10T00:00:00Z'))
    for index in range(105):
        service.repository.save_job(FuyaoJob(id=str(index), kind='history_full', status='completed', created_at='2026-09-11T00:00:00Z'))
    async def run():
        recovered = await service.get_job('old')
        assert recovered.status == 'interrupted' and recovered.updated_at == recovered.finished_at
        assert 'old' not in {job['id'] for job in (await service.status())['jobs']}
        await service.aclose()
    asyncio.run(run())


def test_legacy_terminal_jobs_remain_readable_and_cannot_retry_without_request(service):
    import pytest
    service.repository.save_job(FuyaoJob(id='old', kind='financials', status='failed', created_at='2026-09-10T00:00:00Z'))
    async def run():
        job = await service.get_job('old')
        assert job.request is None and job.completed_symbols == [] and job.progress is None
        with pytest.raises(RuntimeError, match='未保存同步输入'):
            await service.retry_job('old')
        assert (await service.cancel_job('old')).model_dump() == job.model_dump()
        for operation in (service.get_job, service.cancel_job, service.retry_job):
            with pytest.raises(LookupError):
                await operation('missing')
        await service.aclose()
    asyncio.run(run())


def test_retry_keeps_non_stock_inputs_and_preserves_previous_job(service, monkeypatch):
    async def collect(_job, _request):
        return None
    monkeypatch.setattr(service, '_collect', collect)
    request = FuyaoJobRequest(kind='sectors', index_symbols=['881155.TI'], symbols=['600519.SH'], period='quarterly', limit=2, report='2025-4')
    parent = FuyaoJob(id='parent', kind='sectors', status='interrupted', created_at='2026-09-10T00:00:00Z', request=request)
    service.repository.save_job(parent)
    async def run():
        child = await service.retry_job('parent')
        assert child.request == request and child.parent_job_id == parent.id
        await asyncio.gather(*tuple(service._tasks.values()))
        assert (await service.retry_job('parent')).id == child.id
        assert (await service.get_job('parent')) == parent
        await service.aclose()
    asyncio.run(run())


def test_cancelled_post_caller_does_not_lose_persisted_stop_intent(service, monkeypatch):
    import pytest
    import threading
    entered, release = threading.Event(), threading.Event()
    collect_started, cleanup_release = asyncio.Event(), asyncio.Event()
    save = service.repository.save_job
    def delayed(job):
        if job.status == 'cancelling':
            entered.set()
            assert release.wait(3)
        return save(job)
    async def collect(_job, _request):
        collect_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await cleanup_release.wait()
    monkeypatch.setattr(service, '_collect', collect)
    monkeypatch.setattr(service.repository, 'save_job', delayed)
    async def run():
        job = await service.start_job(FuyaoJobRequest(kind='history_full'))
        await collect_started.wait()
        cancellation = asyncio.create_task(service.cancel_job(job.id))
        assert await asyncio.to_thread(entered.wait, 2)
        cancellation.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancellation
        assert service.repository.job(job.id).status == 'cancelling'
        cleanup_release.set()
        await asyncio.gather(*tuple(service._tasks.values()), return_exceptions=True)
        assert service.repository.job(job.id).status == 'cancelled'
        await service.aclose()
    asyncio.run(run())


def test_atomic_checkpoint_rolls_back_observation_if_job_missing(service):
    import pytest
    job = FuyaoJob(id='missing', kind='valuations', status='running', created_at='2026-09-10T00:00:00Z')
    with pytest.raises(ValueError, match='同步任务不存在'):
        service.repository.publish_item(job, 'valuations', '600519.SH', '2026-09-10T00:00:00Z', {'values': {'pe_ttm': 2}})
    assert service.repository.latest('valuations', '600519.SH') is None
    assert job.completed == 0 and job.completed_symbols == []


def test_cancellation_resistant_transport_cannot_retry_with_real_provider_runtime(service):
    import httpx
    from pydantic import SecretStr
    from app.services.datahub_runtime import ProviderRuntime
    from app.services.fuyao_client import FuyaoClient
    attempts = []
    entered, interrupted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def handler(_request):
        attempts.append('attempt')
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            await release.wait()
        return httpx.Response(503, json={'code': 5003})
    async def run():
        settings = service.settings.model_copy(update={'fuyao_request_interval_seconds': 0, 'fuyao_api_key': SecretStr('synthetic-cancel-key')})
        service.runtime = ProviderRuntime(None, settings)
        service.client = FuyaoClient(settings, transport=httpx.MockTransport(handler))
        job = await service.start_job(FuyaoJobRequest(kind='valuations', symbols=['600519.SH']))
        await entered.wait()
        response = await service.cancel_job(job.id)
        assert response.status == 'cancelling'
        await interrupted.wait()
        assert job.id in service._tasks
        assert (await service.get_job(job.id)).status == 'cancelling'
        release.set()
        await asyncio.gather(*tuple(service._tasks.values()), return_exceptions=True)
        assert service.repository.job(job.id).status == 'cancelled'
        assert attempts == ['attempt']
        assert not service.runtime.provider_call_in_flight('fuyao', 'research')
        await service.aclose()
        assert await service.runtime.aclose()
    asyncio.run(run())


def test_atomic_publication_real_commit_failure_rolls_back_both_records(service):
    import sqlite3
    import pytest
    job = FuyaoJob(id='commit', kind='valuations', status='running', created_at='2026-09-10T00:00:00Z', total=1)
    service.repository.save_job(job)
    with service.repository._connections.connect() as connection:
        connection.executescript('''
            CREATE TABLE referenced_entity (id INTEGER PRIMARY KEY);
            CREATE TABLE deferred_check (id INTEGER REFERENCES referenced_entity(id) DEFERRABLE INITIALLY DEFERRED);
            CREATE TRIGGER refuse_commit AFTER INSERT ON observations BEGIN INSERT INTO deferred_check VALUES (123); END;
        ''')
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        service.repository.publish_item(job, 'valuations', '600519.SH', '2026-09-10T00:00:00Z', {'values': {'pe_ttm': 2}})
    assert service.repository.latest('valuations', '600519.SH') is None
    assert service.repository.job(job.id) == job


def test_atomic_publication_validation_and_encoding_failures_roll_back(service, monkeypatch):
    import pytest
    job = FuyaoJob(id='validation', kind='valuations', status='running', created_at='2026-09-10T00:00:00Z', total=1)
    service.repository.save_job(job)
    with pytest.raises(ValueError):
        service.repository.publish_item(job, 'valuations', '600519.SH', '2026-09-10T00:00:00Z', {'value': float('nan')})
    validate = FuyaoJob.model_validate_json
    def invalid_model(*_args, **_kwargs):
        raise ValueError('synthetic invalid model')
    monkeypatch.setattr(FuyaoJob, 'model_validate_json', invalid_model)
    with pytest.raises(ValueError, match='synthetic invalid model'):
        service.repository.publish_item(job, 'valuations', '600519.SH', '2026-09-10T00:00:00Z', {'values': {'pe_ttm': 2}})
    monkeypatch.setattr(FuyaoJob, 'model_validate_json', validate)
    assert service.repository.latest('valuations', '600519.SH') is None
    assert service.repository.job(job.id) == job


def test_serialization_failure_after_observation_insert_rolls_back(service, monkeypatch):
    import pytest
    job = FuyaoJob(id='serialization', kind='valuations', status='running', created_at='2026-09-10T00:00:00Z', total=1)
    service.repository.save_job(job)
    def refuse_serialization(*_args, **_kwargs):
        raise ValueError('synthetic serialization failure')
    monkeypatch.setattr(FuyaoJob, 'model_dump_json', refuse_serialization)
    with pytest.raises(ValueError, match='synthetic serialization failure'):
        service.repository.publish_item(job, 'valuations', '600519.SH', '2026-09-10T00:00:00Z', {'values': {'pe_ttm': 2}})
    assert service.repository.latest('valuations', '600519.SH') is None
    assert service.repository.job(job.id) == job


def test_history_progress_is_persistent_and_terminal_cannot_be_overwritten(service, monkeypatch):
    import app.services.fuyao_dumps as dumps
    entered, release = asyncio.Event(), asyncio.Event()
    async def sync(*_args, control, **_kwargs):
        control.checkpoint('validate_daily', 8192, 16000, 'rows')
        entered.set()
        await release.wait()
        control.checkpoint('published', 1, 1, 'version')
    monkeypatch.setattr(dumps, 'sync_market_dumps', sync)
    async def run():
        job = await service.start_job(FuyaoJobRequest(kind='history_full'))
        await entered.wait()
        async with asyncio.timeout(3):
            while service.repository.job(job.id).progress is None:
                await asyncio.sleep(0.01)
        progress = service.repository.job(job.id).progress
        assert progress.stage == 'validate_daily' and progress.current == 8192 and progress.total == 16000
        release.set()
        await asyncio.gather(*tuple(service._tasks.values()))
        final = service.repository.job(job.id)
        assert final.status == 'completed' and final.progress.stage == 'published'
        await asyncio.sleep(0.6)
        assert service.repository.job(job.id) == final
        await service.aclose()
    asyncio.run(run())


def test_history_cooperative_cancellation_is_cancelled_not_failed(service, monkeypatch):
    from app.services.fuyao_sync_control import FuyaoSyncCancelled
    import app.services.fuyao_dumps as dumps
    async def stopped(*_args, **_kwargs):
        raise FuyaoSyncCancelled('synthetic cooperative stop')
    monkeypatch.setattr(dumps, 'sync_market_dumps', stopped)
    async def run():
        job = await service.start_job(FuyaoJobRequest(kind='history_full'))
        await asyncio.gather(*tuple(service._tasks.values()))
        assert (await service.get_job(job.id)).status == 'cancelled'
        await service.aclose()
    asyncio.run(run())


def test_cancel_after_history_publication_preserves_completed_outcome(service, monkeypatch):
    import app.services.fuyao_dumps as dumps
    monitor_entered, monitor_release = asyncio.Event(), asyncio.Event()
    async def sync(*_args, **_kwargs):
        return None
    async def monitor(_execution, _done):
        monitor_entered.set()
        await monitor_release.wait()
    monkeypatch.setattr(dumps, 'sync_market_dumps', sync)
    monkeypatch.setattr(service, '_monitor_history', monitor)
    async def run():
        job = await service.start_job(FuyaoJobRequest(kind='history_full'))
        await monitor_entered.wait()
        assert service._job_records[job.id].completed == 1
        cancellation = asyncio.create_task(service.cancel_job(job.id))
        await asyncio.sleep(0.01)
        assert not cancellation.done()
        assert not service._executions[job.id].control.cancelled
        monitor_release.set()
        assert (await cancellation).status == 'completed'
        assert service.repository.job(job.id).status == 'completed'
        await service.aclose()
    asyncio.run(run())


def test_shutdown_stops_owned_tasks_even_if_cancelling_intent_cannot_persist(service, monkeypatch):
    entered = asyncio.Event()
    closed = []
    save = service.repository.save_job
    def fail_intent(job):
        if job.status == 'cancelling':
            raise OSError('synthetic disk failure')
        return save(job)
    async def collect(_job, _request):
        entered.set()
        await asyncio.Event().wait()
    async def close_client():
        closed.append(True)
    monkeypatch.setattr(service.repository, 'save_job', fail_intent)
    monkeypatch.setattr(service, '_collect', collect)
    monkeypatch.setattr(service.client, 'aclose', close_client)
    async def run():
        job = await service.start_job(FuyaoJobRequest(kind='history_full'))
        await entered.wait()
        async with asyncio.timeout(3):
            await service.aclose()
        assert not service._tasks and closed == [True]
        assert service.repository.job(job.id).status == 'cancelled'
    asyncio.run(run())


def test_progress_storage_failure_stops_unpublished_sync_and_drains(service, monkeypatch):
    import app.services.fuyao_dumps as dumps
    stopped = []
    save = service.repository.save_job
    def fail_progress(job):
        if job.status == 'running' and job.progress is not None:
            raise OSError('synthetic progress failure')
        return save(job)
    async def sync(*_args, control, **_kwargs):
        control.checkpoint('downloading_daily', 4, 100, 'bytes')
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(control.cancelled)
    monkeypatch.setattr(dumps, 'sync_market_dumps', sync)
    monkeypatch.setattr(service.repository, 'save_job', fail_progress)
    async def run():
        job = await service.start_job(FuyaoJobRequest(kind='history_full'))
        async with asyncio.timeout(3):
            await asyncio.gather(*tuple(service._tasks.values()), return_exceptions=True)
        final = await service.get_job(job.id)
        assert stopped == [True] and final.status == 'failed' and final.completed == 0
        assert '进度记录保存失败' in final.message
        await service.aclose()
    asyncio.run(run())


def test_progress_storage_failure_after_publication_preserves_completed_result(service, monkeypatch):
    import app.services.fuyao_dumps as dumps
    save = service.repository.save_job
    def fail_progress(job):
        if job.status == 'running' and job.progress is not None:
            raise OSError('synthetic progress failure')
        return save(job)
    async def sync(*_args, control, **_kwargs):
        control.checkpoint('publishing')
        return None
    monkeypatch.setattr(dumps, 'sync_market_dumps', sync)
    monkeypatch.setattr(service.repository, 'save_job', fail_progress)
    async def run():
        job = await service.start_job(FuyaoJobRequest(kind='history_full'))
        await asyncio.gather(*tuple(service._tasks.values()))
        final = await service.get_job(job.id)
        assert final.status == 'completed' and final.completed == 1 and '数据已发布' in final.message
        assert final.errors == ['同步进度未能持久保存，请检查本地存储']
        await service.aclose()
    asyncio.run(run())
