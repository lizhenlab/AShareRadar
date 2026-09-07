"""Offline future collection plans, daily byte receipts and post-seal result binding.

This protocol establishes local order and retained-byte integrity only. Generic
envelopes do not establish official source, batch-selection compliance, trusted
availability timestamps or numerical research validity. Promotion is always off.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, read_regular_file, sha256_hex
from app.services.market_scan_prospective_contract import (
    CALENDAR_PATH, MARKET_ZONE, MAX_INPUT_BYTES, MAX_PLAN_BYTES, PLAN_VERSION, RECEIPT_VERSION, RESULT_VERSION, SEAL_VERSION,
    cutoff_at, validate_input, validate_specification, verify_current_code,
)
from app.services.market_scan_prospective_store import (
    check_anchor, digested_record, encode_raw, namespace_files, plan_directory, publish_record, record_digest,
)
from app.services.market_scan_prospective_validation import ProspectiveState, load_state, validate_receipt, validate_result, validate_seal
from app.services.market_scan_trial_registry_contract import registry_date, registry_object, registry_timestamp
from app.services.market_scan_trial_registry_lock import trial_registry_write_lock
from app.utils.clock import utc_now


def create_prospective_plan(root: Path, plan_id: str, specification: Mapping[str, object]) -> dict[str, object]:
    """Freeze before the first local collection day; an exact creation retry returns the saved plan."""
    encoded_spec = canonical_json_bytes(dict(specification))
    if len(encoded_spec) > MAX_PLAN_BYTES:
        raise ValueError("prospective specification is oversized")
    spec = registry_object(decode_json_bytes(encoded_spec), "specification")
    directory = plan_directory(root, plan_id)
    with trial_registry_write_lock(directory):
        if "plan.json" in namespace_files(directory):
            state = load_state(directory, plan_id)
            if canonical_json_bytes(state.specification) != encoded_spec:
                raise ValueError("plan already exists with a different frozen specification")
            return state.plan
        if namespace_files(directory) != {".writer.lock"}:
            raise ValueError("uninitialized plan namespace contains committed records")
        calendar_bytes = read_regular_file(CALENDAR_PATH, max_bytes=MAX_PLAN_BYTES)
        spec = validate_specification(spec, calendar_bytes=calendar_bytes)
        verify_current_code(spec)
        recorded = utc_now()
        if recorded.astimezone(MARKET_ZONE).date() >= registry_date(spec["start_date"], "start_date"):
            raise ValueError("the complete collection period must start on a future local date")
        plan = digested_record({
            "schema_version": PLAN_VERSION, "plan_id": plan_id, "recorded_at": recorded.isoformat(), "specification": spec,
            "calendar_sha256": sha256_hex(calendar_bytes), "calendar_base64": encode_raw(calendar_bytes),
        })
        publish_record(directory, "plan.json", plan)
        return plan


def record_prospective_input(
    root: Path, plan_id: str, trade_date: str, *, input_path: Path | None = None, reason: str = "",
) -> dict[str, object]:
    """Append one immutable day; recording after cutoff always remains late, even on retries."""
    directory = plan_directory(root, plan_id)
    with trial_registry_write_lock(directory):
        state = load_state(directory, plan_id)
        _require_next_day(state, trade_date)
        encoded = read_regular_file(input_path, max_bytes=MAX_INPUT_BYTES) if input_path is not None else None
        recorded_at = _recording_time(state)
        receipt = _new_receipt(state, trade_date, encoded, reason, recorded_at)
        validate_receipt(receipt, state.specification)
        publish_record(directory, f"receipt-{len(state.receipts) + 1:08d}.json", receipt)
        return receipt


def _new_receipt(state: ProspectiveState, day: str, encoded: bytes | None, reason: str, recorded_at: str) -> dict[str, object]:
    envelope = validate_input(encoded, state.specification, day, recorded_at) if encoded is not None else None
    status = "missing"
    if envelope is not None:
        if reason:
            raise ValueError("input receipts cannot include a missing reason")
        status = "late" if registry_timestamp(recorded_at, "recorded_at") > cutoff_at(state.specification, day) else "local-on-time-unverified"
    return digested_record({
        "schema_version": RECEIPT_VERSION, "plan_digest": state.plan["digest"], "sequence": len(state.receipts) + 1,
        "previous_digest": state.head_digest, "trade_date": day, "recorded_at": recorded_at, "status": status, "reason": reason,
        "input_sha256": sha256_hex(encoded) if encoded is not None else None,
        "input_base64": encode_raw(encoded) if encoded is not None else None,
        "available_at": envelope["available_at"] if envelope else None, "actual_run_id": envelope["actual_run_id"] if envelope else None,
    })


def seal_prospective_inputs(root: Path, plan_id: str) -> dict[str, object]:
    """Seal the fixed full calendar, preserving late and missing dates; retries return the same seal."""
    directory = plan_directory(root, plan_id)
    with trial_registry_write_lock(directory):
        state = load_state(directory, plan_id)
        if state.seal is not None:
            return state.seal
        seal = digested_record({
            "schema_version": SEAL_VERSION, "plan_digest": state.plan["digest"], "head_digest": state.head_digest,
            "receipt_count": len(state.receipts), "recorded_at": _recording_time(state), "statuses": state.statuses,
        })
        validate_seal(ProspectiveState(state.plan, state.receipts, seal, None))
        publish_record(directory, "input-seal.json", seal)
        return seal


def bind_prospective_result(root: Path, plan_id: str, result_path: Path) -> dict[str, object]:
    """Bind preserved result bytes after input sealing; no result inference or promotion is certified."""
    directory = plan_directory(root, plan_id)
    with trial_registry_write_lock(directory):
        state = load_state(directory, plan_id)
        if state.seal is None:
            raise ValueError("cannot bind results before input sealing")
        encoded = read_regular_file(result_path, max_bytes=MAX_INPUT_BYTES)
        if state.result is not None:
            if state.result["result_sha256"] != sha256_hex(encoded):
                raise ValueError("a different result is already bound")
            return state.result
        result = digested_record({
            "schema_version": RESULT_VERSION, "plan_digest": state.plan["digest"], "input_seal_digest": state.seal["digest"],
            "recorded_at": _recording_time(state), "result_sha256": sha256_hex(encoded), "result_base64": encode_raw(encoded),
        })
        validate_result(ProspectiveState(state.plan, state.receipts, state.seal, result))
        publish_record(directory, "result.json", result)
        return result


def verify_prospective_plan(
    root: Path, plan_id: str, *, expected_plan_digest: str | None = None, expected_seal_digest: str | None = None,
) -> dict[str, object]:
    """Verify preserved bytes and the local chain; caller-supplied hashes remain unverified external claims."""
    directory = plan_directory(root, plan_id)
    with trial_registry_write_lock(directory):
        state = load_state(directory, plan_id)
        check_anchor(record_digest(state.plan), expected_plan_digest, "plan digest")
        seal_digest = record_digest(state.seal) if state.seal else None
        check_anchor(seal_digest, expected_seal_digest, "input seal digest")
        return {
            "schema_version": "market-scan-prospective-verification-v1", "plan": state.plan,
            "plan_digest": state.plan["digest"], "head_digest": state.head_digest, "input_seal_digest": seal_digest,
            "result_digest": record_digest(state.result) if state.result else None,
            "result_sha256": state.result["result_sha256"] if state.result else None,
            "statuses": state.statuses, "receipts": [{k: v for k, v in item.items() if k != "input_base64"} for item in state.receipts],
            "expected_day_count": len(cast(list[str], state.specification["trading_dates"])), "recorded_day_count": len(state.receipts),
            "input_calendar_complete": state.seal is not None, "local_integrity": "verified", "timestamp_assurance": "local-only-unverified",
            "retained_plan_anchor_checked": expected_plan_digest is not None, "trusted_timestamp_attestation": "unavailable",
            "input_admission_verified": False, "batch_selection_verified": False, "result_evaluation_verified": False,
            "machine_promotion_eligible": False,
            "limitations": [
                "Local clocks and self-reported availability are not independent timestamp evidence.",
                "Official provenance, payload semantics and latest eligible batch selection require independent admission.",
                "Declared source files were hashed at creation; manifest completeness and future execution identity are unverified.",
                "Uncommitted atomic temporary files are never receipts; restart observes input availability again.",
                "A local chain cannot exclude unrecorded experiments, a rewritten complete chain, or precomputed results.",
                "Late and missing dates remain on the fixed axis and cannot be replaced by backfilled inputs.",
            ],
        }


def _recording_time(state: ProspectiveState) -> str:
    recorded = utc_now()
    if recorded < registry_timestamp(state.last_recorded_at, "previous recorded_at"):
        raise ValueError("local clock moved backwards")
    return recorded.isoformat()


def _require_next_day(state: ProspectiveState, day: str) -> None:
    if state.seal is not None:
        raise ValueError("input plan is already sealed")
    days = cast(list[str], state.specification["trading_dates"])
    if len(state.receipts) >= len(days) or day != days[len(state.receipts)]:
        raise ValueError("record exactly the next predeclared day; duplicate, skipped or outside dates are forbidden")
