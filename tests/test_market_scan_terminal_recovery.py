from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from app.models.market_scan import MarketScanRun
from app.services.market_scan_terminal_recovery import MarketScanTerminalRecovery


@dataclass
class _Lifecycle:
    guard_owned: bool = True
    active_run_ids: tuple[int, ...] = ()

    def owns_instance_guard(self) -> bool:
        return self.guard_owned


class _Cache:
    def __init__(self) -> None:
        self.current = _run("running")
        self.read_error: Exception | None = None
        self.finish_result = _run("interrupted")
        self.finish_calls = 0

    def market_scan_run(self, run_id: int) -> MarketScanRun:
        assert run_id == 29
        if self.read_error is not None:
            raise self.read_error
        return self.current

    def finish_market_scan_run(self, run_id: int, status: str, **details: object) -> MarketScanRun:
        assert run_id == 29
        assert status == "interrupted"
        assert "自动中断" in str(details["message"])
        assert "未能持久化" in str(details["error"])
        self.finish_calls += 1
        return self.finish_result


def test_terminal_recovery_keeps_candidate_after_read_failure_then_retries_successfully() -> None:
    cache = _Cache()
    recovery = MarketScanTerminalRecovery(cast(Any, cache), cast(Any, _Lifecycle()))
    recovery.track(29, False)
    cache.read_error = RuntimeError("database temporarily unavailable")

    assert recovery.recover(29) == 0
    assert cache.finish_calls == 0

    cache.read_error = None
    assert recovery.recover(29) == 1
    assert cache.finish_calls == 1
    assert recovery.recover(29) == 0


def test_terminal_recovery_keeps_candidate_when_finish_does_not_reach_terminal_state() -> None:
    cache = _Cache()
    cache.finish_result = _run("running")
    recovery = MarketScanTerminalRecovery(cast(Any, cache), cast(Any, _Lifecycle()))
    recovery.track(29, False)

    assert recovery.recover() == 0
    assert cache.finish_calls == 1

    cache.finish_result = _run("interrupted")
    assert recovery.recover() == 1
    assert cache.finish_calls == 2


def _run(status: str) -> MarketScanRun:
    return MarketScanRun(
        id=29,
        status=status,
        trigger="manual",
        mode="official",
        rule_version="scan-v1",
        as_of="2026-08-11 16:00:00",
        data_date="2026-08-11",
        quote_date="2026-08-11",
        scope="全市场A股",
        total_count=1,
        excluded_count=0,
        processed_count=0,
        success_count=0,
        missing_count=0,
        skipped_count=0,
        retry_count=0,
        progress_pct=0,
        coverage_pct=0,
        created_at="2026-08-11 16:00:00",
        updated_at="2026-08-11 16:00:00",
    )
