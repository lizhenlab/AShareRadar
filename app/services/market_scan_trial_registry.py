"""Local immutable trial registration and an append-only attempt receipt chain.

This is an integrity and declared-family completeness boundary. A local clock
and SHA-256 cannot attest external experiments or an independently trusted
registration time. Result payloads are recorded claims, never validated alpha.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
from typing import cast

from app.artifacts.io import (
    canonical_json_bytes,
    decode_json_bytes,
    exclusive_atomic_publish,
    path_has_only_trusted_aliases,
    read_regular_file,
)
from app.services.market_scan_trial_registry_contract import (
    TRIAL_CONTRACT_VERSION,
    TrialRegistryError,
    contract_test_start,
    contract_trials,
    registry_hash,
    registry_identifier,
    registry_keys,
    registry_object,
    registry_timestamp,
    trial_registry_digest,
    validate_trial_contract,
)
from app.services.market_scan_trial_registry_lock import (
    TRIAL_REGISTRY_EXECUTION_LOCK_FILENAME,
    TRIAL_REGISTRY_LOCK_FILENAME,
    trial_registry_execution_lock,
    trial_registry_write_lock,
)
from app.utils.clock import ASHARE_TIMEZONE, utc_now


TRIAL_REGISTRY_VERSION = "market-scan-trial-registry-v1"
TRIAL_EVENT_VERSION = "market-scan-trial-event-v1"
TRIAL_SEAL_VERSION = "market-scan-trial-seal-v1"
TRIAL_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_EVENT_FILENAME = re.compile(r"event-([0-9]{8})\.json")
_REGISTRY_KEYS = frozenset({"schema_version", "contract_version", "registration_id", "recorded_at", "contract", "digest"})
_EVENT_KEYS = frozenset({
    "schema_version", "sequence", "previous_digest", "registry_digest", "recorded_at",
    "trial_id", "trial_digest", "event", "status", "result", "result_digest", "reason", "digest",
})
_SEAL_KEYS = frozenset({"schema_version", "registry_digest", "head_digest", "event_count", "recorded_at", "trials", "digest"})
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_LOCK_FILENAMES = frozenset({TRIAL_REGISTRY_LOCK_FILENAME, TRIAL_REGISTRY_EXECUTION_LOCK_FILENAME})


@dataclass(frozen=True)
class TrialRegistryState:
    registration: dict[str, object]
    events: tuple[dict[str, object], ...]
    seal: dict[str, object] | None
    statuses: dict[str, str]

    @property
    def digest(self) -> str:
        return cast(str, self.registration["digest"])

    @property
    def head_digest(self) -> str:
        return cast(str, self.events[-1]["digest"]) if self.events else self.digest


@contextmanager
def trial_registry_execution_lease(root: Path, registration_id: str) -> Iterator[None]:
    """Protect a whole execution without holding the short event publication lock."""
    directory = _directory(root, registration_id)
    read_regular_file(directory / "registration.json", max_bytes=TRIAL_MAX_ARTIFACT_BYTES)
    with trial_registry_execution_lock(directory):
        yield


def create_trial_registry(
    root: Path, registration_id: str, contract: Mapping[str, object],
) -> dict[str, object]:
    """Freeze a complete declared family using the local service clock."""
    checked = validate_trial_contract(contract)
    with trial_registry_write_lock(_directory(root, registration_id)):
        recorded_at = utc_now().isoformat()
        _validate_prospective_time(checked, recorded_at)
        registration = _digested({
            "schema_version": TRIAL_REGISTRY_VERSION, "contract_version": TRIAL_CONTRACT_VERSION,
            "registration_id": registry_identifier(registration_id, "registration_id"),
            "recorded_at": recorded_at, "contract": checked,
        })
        _publish(_directory(root, registration_id) / "registration.json", registration)
        return registration


def start_trial(root: Path, registration_id: str, trial_id: str) -> dict[str, object]:
    """Record intent before executing a declared trial, with a frozen spec digest."""
    with trial_registry_write_lock(_directory(root, registration_id)):
        state = load_trial_registry(root, registration_id)
        _require_open(state)
        if trial_id in state.statuses:
            raise TrialRegistryError("trial already started; retries need a separately frozen trial_id")
        return _append_event(root, registration_id, state, trial_id, "started", "running", None, "")


def finish_trial(
    root: Path, registration_id: str, trial_id: str, *, status: str,
    result: Mapping[str, object] | None = None, reason: str = "",
) -> dict[str, object]:
    """Preserve success, failure and cancellation; supplied results remain claims."""
    with trial_registry_write_lock(_directory(root, registration_id)):
        state = load_trial_registry(root, registration_id)
        _require_open(state)
        if state.statuses.get(trial_id) != "running":
            raise TrialRegistryError("trial must have one unmatched start receipt")
        if status not in _TERMINAL_STATUSES:
            raise TrialRegistryError("terminal status must be succeeded, failed or cancelled")
        _validate_result(status, result, reason)
        payload = dict(result) if result is not None else None
        return _append_event(root, registration_id, state, trial_id, "finished", status, payload, reason)


def seal_trial_registry(root: Path, registration_id: str) -> dict[str, object]:
    """Close a complete family, including all declared failed/cancelled trials."""
    with trial_registry_write_lock(_directory(root, registration_id)):
        state = load_trial_registry(root, registration_id)
        _require_open(state)
        expected = contract_trials(registry_object(state.registration["contract"], "contract"))
        if set(state.statuses) != set(expected) or any(value not in _TERMINAL_STATUSES for value in state.statuses.values()):
            raise TrialRegistryError("cannot seal an incomplete expected trial family")
        seal = _digested({
            "schema_version": TRIAL_SEAL_VERSION, "registry_digest": state.digest,
            "head_digest": state.head_digest, "event_count": len(state.events),
            "recorded_at": utc_now().isoformat(), "trials": state.statuses,
        })
        _validate_seal(TrialRegistryState(state.registration, state.events, seal, state.statuses))
        _publish(_directory(root, registration_id) / "seal.json", seal)
        return seal


def load_trial_registry(root: Path, registration_id: str) -> TrialRegistryState:
    """Validate exact files, declared identity, sequence, lifecycle and hash chain."""
    directory = _directory(root, registration_id)
    filenames = _filenames(directory)
    registration = _read_record(directory / "registration.json", _REGISTRY_KEYS, TRIAL_REGISTRY_VERSION)
    _validate_registration(registration, registration_id)
    events = _load_events(directory, filenames)
    statuses = _validate_events(registration, events)
    seal = _read_record(directory / "seal.json", _SEAL_KEYS, TRIAL_SEAL_VERSION) if "seal.json" in filenames else None
    state = TrialRegistryState(registration, events, seal, statuses)
    if seal is not None:
        _validate_seal(state)
    if filenames != _filenames(directory):
        raise TrialRegistryError("registry changed during verification; retry from a stable snapshot")
    return state


def verify_trial_registry(
    root: Path, registration_id: str, *, expected_registry_digest: str | None = None,
    expected_seal_digest: str | None = None,
) -> dict[str, object]:
    """Report bounded local verification, without inventing external attestation."""
    state = load_trial_registry(root, registration_id)
    _check_anchor(state.digest, expected_registry_digest, "registry")
    seal_digest = cast(str, state.seal["digest"]) if state.seal else None
    _check_anchor(seal_digest, expected_seal_digest, "seal")
    contract = registry_object(state.registration["contract"], "contract")
    return {
        "schema_version": "market-scan-trial-registry-verification-v1",
        "registry_digest": state.digest, "head_digest": state.head_digest, "seal_digest": seal_digest,
        "local_integrity": "verified", "registration_kind": contract["registration_kind"],
        "prospective_time_order": "locally_consistent" if contract["registration_kind"] == "prospective" else "not_prospective",
        "declared_family_complete": state.seal is not None,
        "expected_trial_count": len(contract_trials(contract)), "attempted_trial_count": len(state.statuses),
        "statuses": state.statuses, "external_digest_anchor_checked": expected_registry_digest is not None,
        "external_attempt_completeness": "unproven", "trusted_timestamp_attestation": "unavailable",
        "result_evaluation_verified": False, "machine_promotion_eligible": False,
        "limitations": [
            "Local hashes detect changes relative to retained anchors; they are not signatures or trusted timestamps.",
            "The declared family cannot establish that no experiments ran outside this registry.",
            "Recorded result payloads require independent replay; their numerical or statistical validity is not certified.",
        ],
    }


def _directory(root: Path, registration_id: str) -> Path:
    identifier = registry_identifier(registration_id, "registration_id")
    directory = Path(root).expanduser().absolute() / identifier
    if not path_has_only_trusted_aliases(directory):
        raise TrialRegistryError("registry path cannot traverse symlinks or aliases")
    return directory


def _publish(path: Path, record: Mapping[str, object]) -> None:
    exclusive_atomic_publish(path, canonical_json_bytes(dict(record)), max_bytes=TRIAL_MAX_ARTIFACT_BYTES)


def _digested(payload: Mapping[str, object]) -> dict[str, object]:
    return {**payload, "digest": trial_registry_digest(dict(payload))}


def _read_record(path: Path, keys: frozenset[str], version: str) -> dict[str, object]:
    record = registry_object(decode_json_bytes(read_regular_file(path, max_bytes=TRIAL_MAX_ARTIFACT_BYTES)), path.name)
    registry_keys(record, keys, path.name)
    if record["schema_version"] != version:
        raise TrialRegistryError(f"{path.name}: unsupported schema")
    digest = registry_hash(record["digest"], "digest")
    if trial_registry_digest({key: value for key, value in record.items() if key != "digest"}) != digest:
        raise TrialRegistryError(f"{path.name}: digest mismatch")
    registry_timestamp(record["recorded_at"], "recorded_at")
    return record


def _filenames(directory: Path) -> frozenset[str]:
    entries = tuple(directory.iterdir())
    names = frozenset(entry.name for entry in entries)
    if "registration.json" not in names:
        raise TrialRegistryError("registration.json missing")
    if any(name not in {"registration.json", "seal.json"} | _LOCK_FILENAMES and _EVENT_FILENAME.fullmatch(name) is None for name in names):
        raise TrialRegistryError("unexpected file in registry directory")
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise TrialRegistryError("registry accepts only regular immutable files")
    for name in names & _LOCK_FILENAMES:
        read_regular_file(directory / name, max_bytes=0)
    return names


def _load_events(directory: Path, filenames: frozenset[str]) -> tuple[dict[str, object], ...]:
    event_names = sorted(name for name in filenames if _EVENT_FILENAME.fullmatch(name))
    expected_names = [f"event-{index:08d}.json" for index in range(1, len(event_names) + 1)]
    if event_names != expected_names:
        raise TrialRegistryError("event chain contains a deleted or out-of-order receipt")
    return tuple(_read_record(directory / name, _EVENT_KEYS, TRIAL_EVENT_VERSION) for name in event_names)


def _validate_registration(registration: Mapping[str, object], registration_id: str) -> None:
    if registration["registration_id"] != registration_id or registration["contract_version"] != TRIAL_CONTRACT_VERSION:
        raise TrialRegistryError("registration identity or contract version mismatch")
    contract = validate_trial_contract(registry_object(registration["contract"], "contract"))
    _validate_prospective_time(contract, registration["recorded_at"])


def _validate_prospective_time(contract: Mapping[str, object], timestamp: object) -> None:
    recorded = registry_timestamp(timestamp, "recorded_at").astimezone(ASHARE_TIMEZONE)
    if contract["registration_kind"] == "prospective" and recorded.date() >= contract_test_start(contract):
        raise TrialRegistryError("prospective registration and trial starts must precede the first OOS signal day")


def _require_open(state: TrialRegistryState) -> None:
    if state.seal is not None:
        raise TrialRegistryError("sealed registry cannot accept additional attempts")


def _append_event(
    root: Path, registration_id: str, state: TrialRegistryState, trial_id: str,
    event: str, status: str, result: dict[str, object] | None, reason: str,
) -> dict[str, object]:
    contract = registry_object(state.registration["contract"], "contract")
    trials = contract_trials(contract)
    if trial_id not in trials:
        raise TrialRegistryError("trial absent from frozen complete candidate family")
    timestamp = utc_now().isoformat()
    if event == "started":
        _validate_prospective_time(contract, timestamp)
    sequence = len(state.events) + 1
    record = _digested({
        "schema_version": TRIAL_EVENT_VERSION, "sequence": sequence, "previous_digest": state.head_digest,
        "registry_digest": state.digest, "recorded_at": timestamp, "trial_id": trial_id,
        "trial_digest": trial_registry_digest(trials[trial_id]), "event": event, "status": status,
        "result": result, "result_digest": trial_registry_digest(result) if result is not None else None, "reason": reason,
    })
    _validate_events(state.registration, (*state.events, record))
    _publish(_directory(root, registration_id) / f"event-{sequence:08d}.json", record)
    return record


def _validate_events(
    registration: Mapping[str, object], events: tuple[dict[str, object], ...],
) -> dict[str, str]:
    contract = registry_object(registration["contract"], "contract")
    trials = contract_trials(contract)
    previous_digest = registration["digest"]
    previous_time = registry_timestamp(registration["recorded_at"], "recorded_at")
    statuses: dict[str, str] = {}
    for index, record in enumerate(events, 1):
        trial_id = registry_identifier(record["trial_id"], "trial_id")
        if record["sequence"] != index or type(record["sequence"]) is not int:
            raise TrialRegistryError("event sequence mismatch")
        if record["previous_digest"] != previous_digest or record["registry_digest"] != registration["digest"]:
            raise TrialRegistryError("event chain binding mismatch")
        if trial_id not in trials or record["trial_digest"] != trial_registry_digest(trials[trial_id]):
            raise TrialRegistryError("event candidate or parameter binding mismatch")
        timestamp = registry_timestamp(record["recorded_at"], "recorded_at")
        if timestamp < previous_time:
            raise TrialRegistryError("event clock moved backwards")
        _validate_lifecycle(contract, record, statuses)
        statuses[trial_id] = cast(str, record["status"])
        previous_digest, previous_time = record["digest"], timestamp
    return statuses


def _validate_lifecycle(
    contract: Mapping[str, object], record: Mapping[str, object], statuses: Mapping[str, str],
) -> None:
    trial_id, status = cast(str, record["trial_id"]), record["status"]
    if record["event"] == "started":
        if trial_id in statuses or status != "running" or record["result"] is not None or record["result_digest"] is not None or record["reason"] != "":
            raise TrialRegistryError("invalid or repeated start receipt")
        _validate_prospective_time(contract, record["recorded_at"])
        return
    if record["event"] != "finished" or statuses.get(trial_id) != "running" or status not in _TERMINAL_STATUSES:
        raise TrialRegistryError("invalid terminal receipt or duplicate completion")
    result = registry_object(record["result"], "result") if record["result"] is not None else None
    _validate_result(cast(str, status), result, record["reason"])
    if record["result_digest"] != (trial_registry_digest(result) if result is not None else None):
        raise TrialRegistryError("result digest mismatch")


def _validate_result(status: str, result: Mapping[str, object] | None, reason: object) -> None:
    if not isinstance(reason, str):
        raise TrialRegistryError("reason must be text")
    if status == "succeeded" and not result:
        raise TrialRegistryError("successful attempt requires a nonempty recorded result")
    if status in {"failed", "cancelled"} and not reason.strip():
        raise TrialRegistryError("failure or cancellation requires a retained reason")


def _validate_seal(state: TrialRegistryState) -> None:
    seal = cast(dict[str, object], state.seal)
    contract = registry_object(state.registration["contract"], "contract")
    if seal["registry_digest"] != state.digest or seal["head_digest"] != state.head_digest:
        raise TrialRegistryError("seal chain binding mismatch")
    if type(seal["event_count"]) is not int or seal["event_count"] != len(state.events):
        raise TrialRegistryError("seal detects deleted receipts")
    if seal["trials"] != state.statuses or set(state.statuses) != set(contract_trials(contract)):
        raise TrialRegistryError("seal does not contain complete expected trial family")
    if any(status not in _TERMINAL_STATUSES for status in state.statuses.values()):
        raise TrialRegistryError("seal contains unfinished trial")
    latest_time = state.events[-1]["recorded_at"] if state.events else state.registration["recorded_at"]
    if registry_timestamp(seal["recorded_at"], "seal time") < registry_timestamp(latest_time, "event time"):
        raise TrialRegistryError("seal clock moved backwards")


def _check_anchor(actual: str | None, expected: str | None, name: str) -> None:
    if expected is not None and actual != registry_hash(expected, f"expected {name} digest"):
        raise TrialRegistryError(f"{name} digest differs from independently retained anchor")
