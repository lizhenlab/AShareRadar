"""Optional, scoped backup telemetry containing no paths or exception messages."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import math
from typing import Literal

from app.utils.clock import monotonic_now


BackupStage = Literal[
    "initializing", "source_admission", "source_leases", "source_scan", "source_copy", "source_recheck",
    "source_leases_released", "archive_manifest", "archive_members", "archive_domains", "archive_recheck",
    "archive_publish", "sqlite_snapshot", "sqlite_hash", "sqlite_integrity", "sqlite_row_counts",
    "sqlite_copy", "sqlite_rotate", "completed",
]
BackupUnit = Literal["bytes", "pages", "tables"]
ProgressSink = Callable[[dict[str, object]], None]


@dataclass
class BackupProgress:
    """One operation's observer; callback failures abort rather than hide lost output."""

    sink: ProgressSink
    interval_seconds: float = 2.0
    operation: str = field(default="unknown", init=False)
    stage: BackupStage = field(default="initializing", init=False)
    source_leases_released: bool = field(default=False, init=False)
    _started: float = field(default_factory=monotonic_now, init=False)
    _phase_started: float = field(default=0, init=False)
    _last_emit: float = field(default=float("-inf"), init=False)
    _counts: dict[str, object] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if isinstance(self.interval_seconds, bool) or not math.isfinite(self.interval_seconds) or not 0.1 <= self.interval_seconds <= 60:
            raise ValueError("backup progress interval must be between 0.1 and 60 seconds")

    def checkpoint(self, stage: BackupStage | None = None, *, current: int | None = None,
                   total: int | None = None, unit: BackupUnit | None = None) -> None:
        now = monotonic_now()
        changed = stage is not None and stage != self.stage
        if changed:
            assert stage is not None
            self.stage, self._phase_started, self._counts = stage, now, {}
        if stage == "source_leases_released":
            self.source_leases_released = True
        if unit is not None:
            self._counts = {"current": current, "total": total, "unit": unit}
        if not changed and now - self._last_emit < self.interval_seconds:
            return
        self._last_emit = now
        event = "phase" if changed else "progress" if unit is not None else "heartbeat"
        self.sink({"schema_version": "workspace-backup-progress-v1", "operation": self.operation,
                   "event": event, "stage": self.stage, "elapsed_seconds": round(now - self._started, 3),
                   "stage_elapsed_seconds": round(now - self._phase_started, 3),
                   "source_leases_released": self.source_leases_released, **self._counts})

    def diagnostic(self) -> dict[str, object]:
        return {"stage": self.stage, "error_code": f"backup_{self.stage}_failed",
                "source_leases_released": self.source_leases_released}


_ACTIVE: ContextVar[BackupProgress | None] = ContextVar("workspace_backup_progress", default=None)


@contextmanager
def backup_observation(progress: BackupProgress | None, operation: str) -> Iterator[None]:
    token = _ACTIVE.set(progress)
    try:
        if progress is not None:
            progress.operation = operation
            progress._started = progress._phase_started = monotonic_now()
            progress.stage, progress.source_leases_released = "initializing", False
            progress._last_emit, progress._counts = float("-inf"), {}
            progress.checkpoint("source_admission" if operation == "create" else "archive_manifest")
        yield
    finally:
        _ACTIVE.reset(token)


def backup_checkpoint(stage: BackupStage | None = None, *, current: int | None = None,
                      total: int | None = None, unit: BackupUnit | None = None) -> None:
    progress = _ACTIVE.get()
    if progress is not None:
        progress.checkpoint(stage, current=current, total=total, unit=unit)


__all__ = ["BackupProgress", "backup_checkpoint", "backup_observation"]
