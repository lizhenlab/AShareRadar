"""Mutable runner liveness, separate from immutable study evidence and observations."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import os
from pathlib import Path
import secrets
import stat
from typing import cast

from app.artifacts.io import (
    ArtifactChangedError, canonical_json_bytes, decode_json_bytes, path_has_only_trusted_aliases,
    read_regular_file, sha256_hex,
)
from app.services.market_scan_trial_registry_contract import registry_hash, registry_identifier, registry_timestamp
from app.services.market_scan_trial_registry_lock import trial_registry_execution_lock
from app.utils.clock import utc_now


HEALTH_MAX_BYTES = 16 * 1024
HEALTH_GRACE_SECONDS = 120
HEALTH_CHECK_SECONDS = 300
HEALTH_MAX_WAIT_SECONDS = 3600
_SCHEMA = "strategy-prospective-runner-health-v1"
_FAULTS = frozenset({
    "study_blocked", "capture_failed", "service_unavailable", "submission_uncertain", "submission_conflict",
    "scan_fault", "runtime_validation_failed", "cycle_failed", "health_write_failed",
})
_KEYS = frozenset({
    "schema_version", "identity", "session_id", "pid", "status", "updated_at", "last_completed_at",
    "next_check_at", "fault_code", "digest",
})


def prospective_runner_identity(
    database: Path, root: Path, plan_id: str, control_root: Path, plan_digest: str,
    runtime_directory: Path, runtime_digest: str,
) -> dict[str, str]:
    """Bind health to explicit safe paths and the two independently supplied digests."""
    paths = {}
    for key, value in (("database", database), ("root", root), ("control_root", control_root),
                       ("runtime_directory", runtime_directory)):
        if not value.is_absolute() or ".." in value.parts or not path_has_only_trusted_aliases(value):
            raise ValueError("runner health requires safe absolute paths")
        paths[key] = str(value.resolve())
    if Path(paths["control_root"]) != Path(paths["root"]).parent / "strategy_prospective_control":
        raise ValueError("runner health requires the canonical control directory")
    return {**paths, "plan_id": registry_identifier(plan_id, "plan_id"),
            "plan_digest": registry_hash(plan_digest, "plan digest"),
            "runtime_digest": registry_hash(runtime_digest, "runtime digest")}


def runner_health_directory(identity: Mapping[str, str]) -> Path:
    return Path(identity["control_root"]) / identity["plan_id"]


def read_runner_health(identity: Mapping[str, str]) -> dict[str, object]:
    """Read one bounded, digest-verified snapshot without creating control files."""
    value = decode_json_bytes(read_regular_file(runner_health_directory(identity) / "health.json", max_bytes=HEALTH_MAX_BYTES))
    if not isinstance(value, dict) or value.keys() != _KEYS:
        raise ValueError("invalid runner health record")
    payload = {key: item for key, item in value.items() if key != "digest"}
    if value["schema_version"] != _SCHEMA or value["digest"] != sha256_hex(canonical_json_bytes(payload)):
        raise ValueError("runner health digest mismatch")
    if value["identity"] != identity or type(value["pid"]) is not int or value["pid"] <= 0:
        raise ValueError("runner health identity mismatch")
    registry_identifier(value["session_id"], "runner session")
    _validate_health_times(value)
    return value


def _validate_health_state(value: Mapping[str, object]) -> None:
    status, fault = value["status"], value["fault_code"]
    if status not in {"checking", "waiting", "stopped", "complete", "fault"} or fault not in {None, *_FAULTS}:
        raise ValueError("invalid runner health state")
    if status == "fault" and fault is None or status not in {"fault", "checking"} and fault is not None:
        raise ValueError("runner health fault state mismatch")


def _validate_health_times(value: Mapping[str, object]) -> None:
    _validate_health_state(value)
    status = value["status"]
    updated = registry_timestamp(value["updated_at"], "health updated_at")
    completed = value["last_completed_at"]
    if completed is not None and registry_timestamp(completed, "last completed") > updated:
        raise ValueError("runner health completion time is in the future")
    next_check = value["next_check_at"]
    if next_check is not None:
        seconds = (registry_timestamp(next_check, "next check") - updated).total_seconds()
        maximum = HEALTH_CHECK_SECONDS if status == "checking" else HEALTH_MAX_WAIT_SECONDS
        if not 0 < seconds <= maximum or status in {"stopped", "complete"}:
            raise ValueError("runner health next check is outside its bounded schedule")
    elif status in {"checking", "waiting"}:
        raise ValueError("active runner health lacks a next check")
    if status in {"waiting", "complete", "stopped"} and completed is None:
        raise ValueError("runner health lacks a completed inspection")


def _replace_health(directory: Path, payload: Mapping[str, object]) -> None:
    encoded = canonical_json_bytes({**payload, "digest": sha256_hex(canonical_json_bytes(payload))})
    if len(encoded) > HEALTH_MAX_BYTES or not path_has_only_trusted_aliases(directory):
        raise ValueError("runner health is oversized or its directory changed")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".health-" + secrets.token_hex(8)
    created = False
    try:
        facts, current = os.fstat(descriptor), directory.stat(follow_symlinks=False)
        if (facts.st_dev, facts.st_ino) != (current.st_dev, current.st_ino) or not path_has_only_trusted_aliases(directory):
            raise ArtifactChangedError(directory, stage="runner health publication")
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=descriptor)
        created = True
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, "health.json", src_dir_fd=descriptor, dst_dir_fd=descriptor)
        created = False
        os.fsync(descriptor)
    finally:
        try:
            if created:
                os.unlink(temporary, dir_fd=descriptor)
        finally:
            os.close(descriptor)


class ProspectiveRunnerHealth:
    """One session owns all updates while holding the independent lifetime lease."""

    def __init__(self, identity: Mapping[str, str]) -> None:
        self.identity = dict(identity)
        self.directory = runner_health_directory(identity)
        self.session_id = secrets.token_hex(16)
        path = self.directory / "health.json"
        self.previous = read_runner_health(identity) if path.exists() or path.is_symlink() else None

    def _write(self, status: str, delay: int | None, fault: str | None, *, completed: bool) -> None:
        now = utc_now()
        previous = self.previous or {}
        if previous and now < registry_timestamp(previous["updated_at"], "previous health time"):
            raise ValueError("runner health clock moved backwards")
        payload = {"schema_version": _SCHEMA, "identity": self.identity, "session_id": self.session_id,
                   "pid": os.getpid(), "status": status, "updated_at": now.isoformat(),
                   "last_completed_at": now.isoformat() if completed else previous.get("last_completed_at"),
                   "next_check_at": (now + timedelta(seconds=delay)).isoformat() if delay else None,
                   "fault_code": fault}
        _validate_health_times(payload)
        _replace_health(self.directory, payload)
        self.previous = payload

    def checking(self) -> None:
        unresolved = cast(str | None, (self.previous or {}).get("fault_code"))
        self._write("checking", HEALTH_CHECK_SECONDS, unresolved, completed=False)

    def completed(self, result: Mapping[str, object], delay: int | None) -> None:
        action = result["action"]
        codes = {"blocked": "study_blocked", "capture_failed": "capture_failed", "service_failed": "service_unavailable",
                 "submission_uncertain": "submission_uncertain", "submission_conflict": "submission_conflict"}
        fault = codes.get(cast(str, action), "scan_fault" if result.get("fault") else None)
        state = cast(Mapping[str, object], result["state"])
        status = "waiting" if delay else ("complete" if state["status"] == "collection_complete" else "stopped")
        self._write("fault" if fault else status, delay, fault, completed=True)

    def failed(self, fault: str) -> None:
        self._write("fault", None, fault, completed=False)


@contextmanager
def prospective_runner_health_session(identity: Mapping[str, str]) -> Iterator[ProspectiveRunnerHealth]:
    """Fail closed before any collection when health ownership or initial publication fails."""
    directory = runner_health_directory(identity)
    with trial_registry_execution_lock(directory / "runner-health"):
        health = ProspectiveRunnerHealth(identity)
        yield health


def runner_health_lease_active(identity: Mapping[str, str]) -> bool:
    """Probe the existing flock read-only; a stopped process cannot leave a healthy lease."""
    path = runner_health_directory(identity) / "runner-health/.executor.lock"
    if not path_has_only_trusted_aliases(path):
        raise ValueError("runner health lease path is unsafe")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        facts = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(facts.st_mode) or facts.st_size or (facts.st_dev, facts.st_ino) != (current.st_dev, current.st_ino):
            raise ArtifactChangedError(path, stage="runner health lease")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def inspect_runner_health(identity: Mapping[str, str], *, now: datetime | None = None) -> dict[str, object]:
    """Report liveness from a stable snapshot; neither this file nor the lease is evidence."""
    before = read_runner_health(identity)
    active = runner_health_lease_active(identity)
    after = read_runner_health(identity)
    if before != after:
        return {"status": "unknown", "fault_codes": ["health_changed_during_inspection"]}
    observed = now or utc_now()
    updated = registry_timestamp(after["updated_at"], "health updated_at")
    faults = []
    if observed < updated:
        faults.append("clock_regressed")
    next_check = after["next_check_at"]
    if next_check is not None and observed > registry_timestamp(next_check, "next check") + timedelta(seconds=HEALTH_GRACE_SECONDS):
        faults.append("heartbeat_expired")
    if after["fault_code"] is not None:
        faults.append(str(after["fault_code"]))
    if after["status"] != "complete" and not active:
        faults.append("runner_not_active")
    if after["status"] == "stopped":
        faults.append("runner_not_watching")
    return {"status": "fault" if faults else "healthy", "fault_codes": faults, "lease_active": active, "health": after,
            "grace_seconds": HEALTH_GRACE_SECONDS, "timestamp_assurance": "local-only-unverified"}
