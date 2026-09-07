from __future__ import annotations

import gc
from queue import Queue
import threading
import weakref

import pytest

from app.services import daemon_executor


class _Payload:
    pass


class _ObservedQueue(Queue):
    """Signal when the single worker has finished work and waits for its next job."""

    def __init__(self) -> None:
        super().__init__()
        self.idle = threading.Event()
        self._gets = 0

    def get(self, block: bool = True, timeout: float | None = None):
        self._gets += 1
        if self._gets == 2:
            self.idle.set()
        return super().get(block=block, timeout=timeout)


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_idle_worker_releases_completed_call_graph(monkeypatch, outcome: str) -> None:
    queue = _ObservedQueue()
    monkeypatch.setattr(daemon_executor, "Queue", lambda: queue)
    executor = daemon_executor.DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="retention")

    def call(argument: _Payload) -> _Payload:
        if outcome == "failure":
            raise RuntimeError("provider failed")
        return _Payload()

    try:
        argument = _Payload()
        argument_ref = weakref.ref(argument)
        future = executor.submit(call, argument)
        del argument
        assert queue.idle.wait(timeout=2), "worker did not finish its first call"
        if outcome == "success":
            result_ref = weakref.ref(future.result())
        else:
            assert isinstance(future.exception(), RuntimeError)
        future_ref = weakref.ref(future)
        del future

        gc.collect()

        assert argument_ref() is None
        assert future_ref() is None
        if outcome == "success":
            assert result_ref() is None
    finally:
        executor.shutdown(wait=True)
