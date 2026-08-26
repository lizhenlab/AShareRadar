from __future__ import annotations

from concurrent.futures import Executor, Future
from dataclasses import dataclass
from functools import partial
from queue import Empty, Queue
import threading
from typing import Any, Callable, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class _WorkItem:
    future: Future[Any]
    call: Callable[[], Any]


class DaemonThreadPoolExecutor(Executor):
    """Bounded executor whose uncooperative workers cannot block process exit."""

    def __init__(self, max_workers: int, *, thread_name_prefix: str) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers <= 0:
            raise ValueError("max_workers must be a positive integer")
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix.strip() or "daemon-worker"
        self._queue: Queue[_WorkItem | None] = Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._shutdown = False

    def submit(
        self,
        fn: Callable[..., T],
        /,
        *args: object,
        **kwargs: object,
    ) -> Future[T]:
        future: Future[T] = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if len(self._threads) < self._max_workers:
                try:
                    self._start_worker_locked()
                except BaseException as exc:
                    future.set_exception(exc)
                    return future
            self._queue.put(_WorkItem(future=future, call=partial(fn, *args, **kwargs)))
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            if not self._shutdown:
                self._shutdown = True
                if cancel_futures:
                    self._cancel_pending_locked()
                for _thread in self._threads:
                    self._queue.put(None)
            threads = tuple(self._threads)
        if wait:
            for thread in threads:
                thread.join()

    def _start_worker_locked(self) -> None:
        index = len(self._threads)
        thread = threading.Thread(
            name=f"{self._thread_name_prefix}_{index}",
            target=_worker,
            args=(self._queue,),
            daemon=True,
        )
        thread.start()
        self._threads.append(thread)

    def _cancel_pending_locked(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                return
            if item is not None:
                item.future.cancel()


def _worker(queue: Queue[_WorkItem | None]) -> None:
    while True:
        item = queue.get()
        if item is None:
            return
        if not item.future.set_running_or_notify_cancel():
            continue
        try:
            result = item.call()
        except BaseException as exc:
            item.future.set_exception(exc)
        else:
            item.future.set_result(result)


@dataclass
class DaemonLoopExecutorLease:
    """Own a daemon default executor installed on one running event loop."""

    loop: Any
    executor: DaemonThreadPoolExecutor | None

    def close(self) -> None:
        executor = self.executor
        if executor is None:
            return
        if getattr(self.loop, "_default_executor", None) is executor:
            self.loop._default_executor = None
        executor.shutdown(wait=False, cancel_futures=True)
        self.executor = None


def install_daemon_loop_executor(
    loop: Any,
    *,
    max_workers: int = 8,
) -> DaemonLoopExecutorLease:
    """Keep uncancellable DNS/default-I/O workers from blocking process restart."""
    if getattr(loop, "_default_executor", None) is not None:
        return DaemonLoopExecutorLease(loop=loop, executor=None)
    executor = DaemonThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="ashare-default-io",
    )
    loop._default_executor = executor
    return DaemonLoopExecutorLease(loop=loop, executor=executor)


__all__ = [
    "DaemonLoopExecutorLease",
    "DaemonThreadPoolExecutor",
    "install_daemon_loop_executor",
]
