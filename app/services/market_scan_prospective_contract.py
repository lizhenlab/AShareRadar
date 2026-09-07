"""Frozen future collection declarations; local integrity never proves provenance."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, read_regular_file, sha256_hex
from app.services.market_scan_trial_registry_contract import (
    registry_date, registry_hash, registry_identifier, registry_keys, registry_object, registry_timestamp,
)


PLAN_VERSION = "market-scan-prospective-plan-v1"
INPUT_VERSION = "market-scan-prospective-input-v1"
RECEIPT_VERSION = "market-scan-prospective-receipt-v1"
SEAL_VERSION = "market-scan-prospective-input-seal-v1"
RESULT_VERSION = "market-scan-prospective-result-v1"
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 48 * 1024 * 1024
MAX_PLAN_BYTES = 2 * 1024 * 1024
MARKET_ZONE = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CALENDAR_PATH = PROJECT_ROOT / "app/resources/trading_calendar.json"
SPEC_KEYS = frozenset({
    "start_date", "end_date", "trading_dates", "daily_cutoff", "timezone", "batch_slot",
    "selection_rule", "missing_policy", "candidates", "statistics", "code_manifest", "execution_policy",
})
INPUT_KEYS = frozenset({"schema_version", "trade_date", "batch_slot", "actual_run_id", "published_at", "available_at", "payload"})


def validate_specification(value: Mapping[str, object], *, calendar_bytes: bytes | None = None) -> dict[str, object]:
    """Accept one fully declared family and an exact covered exchange calendar."""
    spec = registry_object(decode_json_bytes(canonical_json_bytes(dict(value))), "specification")
    registry_keys(spec, SPEC_KEYS, "specification")
    validate_calendar(spec, calendar_bytes=calendar_bytes)
    cutoff = spec["daily_cutoff"]
    if not isinstance(cutoff, str) or time.fromisoformat(cutoff).isoformat() != cutoff or len(cutoff) != 8:
        raise ValueError("daily_cutoff must be HH:MM:SS")
    if spec["timezone"] != "Asia/Shanghai" or spec["missing_policy"] != "retain-null-no-backfill":
        raise ValueError("explicit Asia/Shanghai and retain-null-no-backfill policies required")
    registry_identifier(spec["batch_slot"], "batch_slot")
    if spec["selection_rule"] != "latest-published-official-full-market-before-cutoff":
        raise ValueError("unsupported daily batch selection rule")
    _validate_candidates(spec)
    _validate_code_manifest(spec["code_manifest"])
    if not registry_object(spec["execution_policy"], "execution_policy"):
        raise ValueError("execution_policy must freeze the intended account and execution rules")
    return spec


def validate_calendar(spec: Mapping[str, object], *, calendar_bytes: bytes | None = None) -> list[str]:
    """Use the trusted bundled snapshot at creation, preserved original bytes for historical audit."""
    start = registry_date(spec["start_date"], "start_date")
    end = registry_date(spec["end_date"], "end_date")
    days = spec["trading_dates"]
    encoded = calendar_bytes if calendar_bytes is not None else read_regular_file(CALENDAR_PATH, max_bytes=MAX_PLAN_BYTES)
    trusted = _calendar_sessions(encoded)
    if start > end or start < min(trusted) or end > max(trusted):
        raise ValueError("future period is outside trusted bundled calendar coverage")
    expected = [day.isoformat() for day in trusted if start <= day <= end]
    if not expected or days != expected:
        raise ValueError("trading_dates must include every trusted session in the frozen period")
    return expected


def _calendar_sessions(encoded: bytes) -> list[date]:
    if len(encoded) > MAX_PLAN_BYTES:
        raise ValueError("trusted calendar snapshot is oversized")
    snapshot = registry_object(decode_json_bytes(encoded), "bundled calendar")
    values = snapshot.get("trade_dates")
    if not isinstance(values, list) or not values:
        raise ValueError("trusted bundled calendar is unavailable")
    trusted = [registry_date(value, "calendar date") for value in values]
    if trusted != sorted(set(trusted)) or snapshot.get("trade_date_count") != len(trusted):
        raise ValueError("trusted bundled calendar has inconsistent sessions")
    if snapshot.get("min_date") != trusted[0].isoformat() or snapshot.get("max_date") != trusted[-1].isoformat():
        raise ValueError("trusted bundled calendar has inconsistent coverage")
    return trusted


def _validate_candidates(spec: Mapping[str, object]) -> None:
    candidates = spec["candidates"]
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 100:
        raise ValueError("declare a reference and 1 to 99 candidates")
    identifiers: list[str] = []
    for raw in candidates:
        item = registry_object(raw, "candidate")
        registry_keys(item, frozenset({"candidate_id", "specification"}), "candidate")
        identifiers.append(registry_identifier(item["candidate_id"], "candidate_id"))
        if not registry_object(item["specification"], "candidate specification"):
            raise ValueError("candidate specification cannot be empty")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate candidate_id")
    _validate_statistics(spec["statistics"], identifiers)


def _validate_statistics(value: object, identifiers: list[str]) -> None:
    stats = registry_object(value, "statistics")
    registry_keys(stats, frozenset({
        "reference", "family", "target", "alternative", "method", "block_length", "minimum_dates", "bootstrap_samples", "alpha",
    }), "statistics")
    reference = registry_identifier(stats["reference"], "reference")
    if reference not in identifiers or stats["family"] != [item for item in identifiers if item != reference]:
        raise ValueError("statistics family must contain every declared non-reference candidate in order")
    expected = {"target": "mean_daily_net_return_improvement", "alternative": "greater", "method": "benjamini-yekutieli-fdr"}
    if any(stats[key] != value for key, value in expected.items()):
        raise ValueError("unsupported optimization inference identity")
    for key, minimum in (("block_length", 6), ("minimum_dates", 40), ("bootstrap_samples", 1000)):
        if type(stats[key]) is not int or not minimum <= cast(int, stats[key]) <= 1000000:
            raise ValueError(f"{key} must be an integer in [{minimum}, 1000000]")
    if type(stats["alpha"]) is not float or not 0 < stats["alpha"] <= .05:
        raise ValueError("alpha must be a finite float in (0, .05]")


def _validate_code_manifest(value: object) -> dict[str, object]:
    manifest = registry_object(value, "code_manifest")
    if not manifest:
        raise ValueError("code_manifest cannot be empty")
    for filename, digest in manifest.items():
        path = Path(filename)
        if path.is_absolute() or ".." in path.parts or str(path) != filename:
            raise ValueError("code_manifest requires canonical repository-relative paths")
        registry_hash(digest, "code SHA-256")
    return manifest


def verify_current_code(spec: Mapping[str, object]) -> None:
    """Bind creation to actual local source bytes; later verification preserves old identities."""
    for filename, expected in _validate_code_manifest(spec["code_manifest"]).items():
        if sha256_hex(read_regular_file(PROJECT_ROOT / filename, max_bytes=MAX_PLAN_BYTES)) != expected:
            raise ValueError(f"code_manifest does not match current source bytes: {filename}")


def cutoff_at(spec: Mapping[str, object], day: str) -> datetime:
    return datetime.combine(registry_date(day, "trade_date"), time.fromisoformat(cast(str, spec["daily_cutoff"])), MARKET_ZONE)


def validate_input(encoded: bytes, spec: Mapping[str, object], day: str, recorded_at: str) -> dict[str, object]:
    """Validate envelope linkage only; source, run selection and payload semantics remain unverified."""
    item = registry_object(decode_json_bytes(encoded), "input")
    registry_keys(item, INPUT_KEYS, "input")
    if item["schema_version"] != INPUT_VERSION or item["trade_date"] != day or item["batch_slot"] != spec["batch_slot"]:
        raise ValueError("input schema, trade_date or logical batch_slot mismatch")
    registry_identifier(item["actual_run_id"], "actual_run_id")
    published = registry_timestamp(item["published_at"], "published_at")
    available = registry_timestamp(item["available_at"], "available_at")
    recorded = registry_timestamp(recorded_at, "recorded_at")
    if not registry_date(day, "trade_date") <= published.astimezone(MARKET_ZONE).date() or not published <= available <= recorded:
        raise ValueError("input publication/availability must precede recording and follow its signal date")
    if not registry_object(item["payload"], "payload"):
        raise ValueError("input payload cannot be empty")
    return item
