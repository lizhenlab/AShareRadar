"""Replay prospective input receipts without treating local claims as certification."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.services.market_scan_prospective_contract import (
    MARKET_ZONE, MAX_PLAN_BYTES, PLAN_VERSION, RECEIPT_VERSION, RESULT_VERSION, SEAL_VERSION, cutoff_at, validate_input, validate_specification,
)
from app.services.market_scan_prospective_store import (
    PLAN_KEYS, RECEIPT_KEYS, RESULT_KEYS, SEAL_KEYS, decode_raw, namespace_files, read_record, receipt_names, record_digest,
)
from app.services.market_scan_trial_registry_contract import registry_date, registry_hash, registry_object, registry_timestamp


@dataclass(frozen=True)
class ProspectiveState:
    plan: dict[str, object]
    receipts: tuple[dict[str, object], ...]
    seal: dict[str, object] | None
    result: dict[str, object] | None

    @property
    def specification(self) -> dict[str, object]:
        return registry_object(self.plan["specification"], "specification")

    @property
    def head_digest(self) -> str:
        return record_digest(self.receipts[-1] if self.receipts else self.plan)

    @property
    def statuses(self) -> dict[str, str]:
        return {cast(str, item["trade_date"]): cast(str, item["status"]) for item in self.receipts}

    @property
    def last_recorded_at(self) -> str:
        return cast(str, (self.result or self.seal or (self.receipts[-1] if self.receipts else self.plan))["recorded_at"])


def load_state(directory: Path, plan_id: str) -> ProspectiveState:
    """Caller holds the writer lock so cooperative appends cannot interleave reads."""
    names = namespace_files(directory)
    plan = read_record(directory, "plan.json", PLAN_KEYS, PLAN_VERSION, max_bytes=MAX_PLAN_BYTES * 3)
    calendar_bytes = decode_raw(plan["calendar_base64"], plan["calendar_sha256"])
    spec = validate_specification(registry_object(plan["specification"], "specification"), calendar_bytes=calendar_bytes)
    recorded = registry_timestamp(plan["recorded_at"], "recorded_at")
    if plan["plan_id"] != plan_id or recorded.astimezone(MARKET_ZONE).date() >= registry_date(spec["start_date"], "start_date"):
        raise ValueError("plan identity mismatch or collection period was not future at creation")
    registry_hash(plan["calendar_sha256"], "calendar_sha256")
    receipts = _load_receipts(directory, plan, spec, receipt_names(names))
    seal = read_record(directory, "input-seal.json", SEAL_KEYS, SEAL_VERSION) if "input-seal.json" in names else None
    result = read_record(directory, "result.json", RESULT_KEYS, RESULT_VERSION) if "result.json" in names else None
    state = ProspectiveState(plan, receipts, seal, result)
    validate_seal(state)
    validate_result(state)
    if names != namespace_files(directory):
        raise ValueError("prospective namespace changed during verification")
    return state


def _load_receipts(
    directory: Path, plan: dict[str, object], spec: dict[str, object], filenames: list[str],
) -> tuple[dict[str, object], ...]:
    """Verify one raw batch at a time; retained state contains only bounded receipt metadata."""
    days = cast(list[str], spec["trading_dates"])
    if len(filenames) > len(days):
        raise ValueError("more receipts than predeclared dates")
    receipts: list[dict[str, object]] = []
    previous = plan
    for sequence, filename in enumerate(filenames, 1):
        item = read_record(directory, filename, RECEIPT_KEYS, RECEIPT_VERSION)
        if type(item["sequence"]) is not int or item["sequence"] != sequence or item["trade_date"] != days[sequence - 1]:
            raise ValueError("receipt dates and sequence must exactly follow the frozen calendar")
        if item["plan_digest"] != plan["digest"] or item["previous_digest"] != previous["digest"]:
            raise ValueError("receipt hash chain or plan binding mismatch")
        if registry_timestamp(item["recorded_at"], "recorded_at") < registry_timestamp(previous["recorded_at"], "previous recorded_at"):
            raise ValueError("receipt clock moved backwards")
        validate_receipt(item, spec)
        item.pop("input_base64")
        receipts.append(item)
        previous = item
    return tuple(receipts)


def validate_receipt(item: dict[str, object], spec: dict[str, object]) -> None:
    day = cast(str, item["trade_date"])
    recorded_at = cast(str, item["recorded_at"])
    recorded = registry_timestamp(recorded_at, "recorded_at")
    deadline = cutoff_at(spec, day)
    if recorded.astimezone(MARKET_ZONE).date() < registry_date(day, "trade_date"):
        raise ValueError("cannot record a future input day")
    if item["status"] == "missing":
        if recorded < deadline or not isinstance(item["reason"], str) or not item["reason"].strip() or len(item["reason"]) > 1000:
            raise ValueError("missing requires a reason and elapsed collection deadline")
        if any(item[key] is not None for key in ("input_sha256", "input_base64", "available_at", "actual_run_id")):
            raise ValueError("missing receipt cannot contain an input")
        return
    encoded = decode_raw(item["input_base64"], item["input_sha256"])
    envelope = validate_input(encoded, spec, day, recorded_at)
    available = registry_timestamp(envelope["available_at"], "available_at")
    expected_status = "late" if max(recorded, available) > deadline else "local-on-time-unverified"
    if item["status"] != expected_status or item["reason"] != "":
        raise ValueError("receipt status must reflect actual local recording time; late inputs cannot be backfilled")
    if item["available_at"] != envelope["available_at"] or item["actual_run_id"] != envelope["actual_run_id"]:
        raise ValueError("receipt metadata differs from preserved original input bytes")


def validate_seal(state: ProspectiveState) -> None:
    seal = state.seal
    if seal is None:
        return
    days = cast(list[str], state.specification["trading_dates"])
    if len(state.receipts) != len(days) or type(seal["receipt_count"]) is not int or seal["receipt_count"] != len(days):
        raise ValueError("input sealing requires a receipt for every predeclared day")
    if seal["plan_digest"] != state.plan["digest"] or seal["head_digest"] != state.head_digest or seal["statuses"] != state.statuses:
        raise ValueError("input seal does not bind the complete receipt chain")
    sealed = registry_timestamp(seal["recorded_at"], "sealed at")
    if sealed < max(cutoff_at(state.specification, days[-1]), registry_timestamp(state.receipts[-1]["recorded_at"], "recorded_at")):
        raise ValueError("input seal precedes the final collection deadline or receipt")


def validate_result(state: ProspectiveState) -> None:
    result = state.result
    if result is None:
        return
    if state.seal is None or result["plan_digest"] != state.plan["digest"] or result["input_seal_digest"] != state.seal["digest"]:
        raise ValueError("results require binding to an already complete input seal")
    if registry_timestamp(result["recorded_at"], "result recorded_at") < registry_timestamp(state.seal["recorded_at"], "seal recorded_at"):
        raise ValueError("result binding precedes input sealing")
    decode_raw(result["result_base64"], result["result_sha256"])
