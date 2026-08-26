from __future__ import annotations

import asyncio
import threading

from app.services.daemon_executor import install_daemon_loop_executor


def test_daemon_loop_executor_runs_default_io_on_daemon_and_detaches() -> None:
    async def scenario() -> tuple[str, bool, bool]:
        loop = asyncio.get_running_loop()
        lease = install_daemon_loop_executor(loop, max_workers=1)
        try:
            thread_name, is_daemon = await loop.run_in_executor(
                None,
                lambda: (threading.current_thread().name, threading.current_thread().daemon),
            )
        finally:
            lease.close()
            lease.close()
        return thread_name, is_daemon, getattr(loop, "_default_executor", None) is None

    thread_name, is_daemon, detached = asyncio.run(scenario())

    assert thread_name.startswith("ashare-default-io")
    assert is_daemon is True
    assert detached is True
