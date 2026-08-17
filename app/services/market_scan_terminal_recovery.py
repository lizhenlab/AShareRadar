"""Explicit recovery command for scan terminal-state persistence failures."""

from __future__ import annotations

import threading

from app.services.market_scan_contracts import MarketScanCacheProtocol
from app.services.market_scan_lifecycle import MarketScanLifecycle


TERMINAL_RECOVERY_MESSAGE = "本地扫描任务已退出，终态写入失败后自动中断；可从断点重试"
TERMINAL_RECOVERY_ERROR = "本地后台扫描已退出，但原终态未能持久化"
_ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})


class MarketScanTerminalRecovery:
    def __init__(
        self,
        cache: MarketScanCacheProtocol,
        lifecycle: MarketScanLifecycle,
    ) -> None:
        self._cache = cache
        self._lifecycle = lifecycle
        self._lock = threading.Lock()
        self._failed_run_ids: set[int] = set()

    def track(self, run_id: int, persisted: bool) -> None:
        with self._lock:
            if persisted:
                self._failed_run_ids.discard(run_id)
            else:
                self._failed_run_ids.add(run_id)

    def recover(self, run_id: int | None = None) -> int:
        if not self._lifecycle.owns_instance_guard():
            return 0
        candidates = self._candidates(run_id)
        active = set(self._lifecycle.active_run_ids)
        return sum(self._recover_one(candidate) for candidate in candidates if candidate not in active)

    def _candidates(self, run_id: int | None) -> tuple[int, ...]:
        with self._lock:
            return tuple(
                candidate
                for candidate in self._failed_run_ids
                if run_id is None or candidate == run_id
            )

    def _recover_one(self, run_id: int) -> int:
        try:
            current = self._cache.market_scan_run(run_id)
            if current.status in _ACTIVE_STATUSES:
                current = self._cache.finish_market_scan_run(
                    run_id,
                    "interrupted",
                    message=TERMINAL_RECOVERY_MESSAGE,
                    error=TERMINAL_RECOVERY_ERROR,
                )
        except Exception:
            return 0
        if current.status in _ACTIVE_STATUSES:
            return 0
        self.track(run_id, True)
        return 1


__all__ = ["MarketScanTerminalRecovery"]
