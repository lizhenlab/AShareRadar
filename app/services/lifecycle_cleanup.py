from __future__ import annotations

import asyncio
from typing import TypeVar


T = TypeVar("T")


async def await_cleanup(cleanup: asyncio.Task[T]) -> T:
    """Drain owned cleanup before propagating even repeated caller cancellation."""

    cancelled = False
    while True:
        try:
            result = await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError:
            if cleanup.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


__all__ = ["await_cleanup"]
