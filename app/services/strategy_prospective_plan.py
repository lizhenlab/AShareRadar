"""Predeclared template schedules and immutable local forward-data receipts.

Local timestamps and hashes preserve an audit trail, not independent provenance
or authority to adopt a strategy. No provider, database or broker is called.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import cast

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, read_regular_file, sha256_hex
from app.config_settings import get_settings
from app.models.market_strategy_templates import MarketStrategyTemplate, MarketStrategyTemplateSourceContracts
from app.services.market_scan_scoring import market_scan_score_spec
from app.services.market_scan_score_contract import stable_score_spec_hash
from app.services.market_scan_trial_registry_contract import (
    registry_date, registry_hash, registry_keys, registry_object, registry_timestamp,
)
from app.services.market_scan_trial_registry_lock import trial_registry_write_lock
from app.services.strategy_compiler import strategy_spec_fingerprint
from app.services.strategy_prospective_store import (
    MAX_PLAN_BYTES, MAX_RECEIPT_BYTES, PLAN_KEYS, RECEIPT_KEYS, RECEIPT_PATTERN,
    decode_calendar, encode_calendar, prospective_directory, prospective_namespace,
    prospective_record, publish_prospective_record, read_prospective_record,
)
from app.services.strategy_template_selection import strategy_template_selection_contract
from app.services.strategy_template_tracking import strategy_template_tracking_contracts
from app.services.strategy_template_tracking_metrics import STRATEGY_TEMPLATE_TRACKING_IDS
from app.services.trading_calendar import TradingCalendarCoverageError, next_trade_dates, trading_dates_between
from app.utils.clock import ASHARE_TIMEZONE, utc_now


PLAN_SCHEMA = "strategy-template-prospective-plan-v1"
RECEIPT_SCHEMA = "strategy-template-prospective-receipt-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CALENDAR_PATH = PROJECT_ROOT / "app/resources/trading_calendar.json"
_SPEC_KEYS = frozenset({
    "start_date", "end_date", "horizon", "notional_cash_cny", "cutoff_local", "timezone",
    "trading_dates", "signal_dates", "exit_dates", "templates", "source_contracts", "inference_contract",
    "source_manifest", "production_score_spec", "production_score_spec_hash", "selection_rule", "missing_policy",
})
_ARGUMENT_KEYS = ("start_date", "end_date", "horizon", "notional_cash_cny", "cutoff_local")
_SELECTION_RULE = "earliest-published-official-full-market-per-score-contract-session-no-failure-fallback"
_MISSING_POLICY = "retain-null-no-backfill"
_STATUSES = frozenset({"captured", "source_incomplete", "missing", "late"})


class StrategyProspectiveCalendarError(ValueError):
    """Live calendar semantics changed from the immutable prospective schedule."""


def create_strategy_prospective_plan(
    root: Path, plan_id: str, *, start_date: str, end_date: str, horizon: int = 10,
    notional_cash_cny: float = 1e6, cutoff_local: str = "20:00:00",
) -> dict[str, object]:
    """Freeze future dates and source identity; exact retries retain the original time."""
    arguments = _arguments(start_date, end_date, horizon, notional_cash_cny, cutoff_local)
    directory = prospective_directory(root, plan_id)
    with trial_registry_write_lock(directory):
        names = prospective_namespace(directory)
        if "plan.json" in names:
            plan, _receipts = _load_state(directory, plan_id, verify_code=True)
            spec = registry_object(plan["specification"], "specification")
            if {key: spec[key] for key in _ARGUMENT_KEYS} != arguments:
                raise ValueError("strategy prospective plan already has different parameters")
            return plan
        if names != {".writer.lock"}:
            raise ValueError("uninitialized strategy prospective plan contains receipts")
        recorded = utc_now()
        if recorded.astimezone(ASHARE_TIMEZONE).date() >= registry_date(start_date, "start_date"):
            raise ValueError("strategy prospective start must be a future local date")
        encoded = read_regular_file(CALENDAR_PATH, max_bytes=MAX_PLAN_BYTES)
        spec = _specification(arguments, encoded)
        plan = prospective_record({
            "schema_version": PLAN_SCHEMA, "plan_id": plan_id, "recorded_at": recorded.isoformat(),
            "specification": spec, "calendar_sha256": sha256_hex(encoded), "calendar_base64": encode_calendar(encoded),
        })
        validate_strategy_prospective_calendar(plan)
        publish_prospective_record(directory, "plan.json", plan)
        return plan


def read_strategy_prospective_plan(root: Path, plan_id: str, *, verify_code: bool = True) -> dict[str, object]:
    """Read the original plan only after validating its complete committed receipt chain."""
    plan, _receipts = read_strategy_prospective_state(root, plan_id, verify_code=verify_code)
    return plan


def read_strategy_prospective_receipts(
    root: Path, plan_id: str, *, verify_code: bool = True,
) -> tuple[dict[str, object], ...]:
    """Return full committed payloads after verifying the complete hash chain."""
    _plan, receipts = read_strategy_prospective_state(root, plan_id, verify_code=verify_code)
    return receipts


def read_strategy_prospective_state(
    root: Path, plan_id: str, *, verify_code: bool = True,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """Return one immutable plan and the single verified receipt prefix observed with it."""
    if type(verify_code) is not bool:
        raise ValueError("verify_code must be boolean")
    plan, receipts = _load_state(prospective_directory(root, plan_id), plan_id, verify_code=verify_code)
    return plan, tuple(receipts)


def append_strategy_prospective_receipt(
    root: Path, plan_id: str, trade_date: str, payload: Mapping[str, object],
) -> dict[str, object]:
    """Append exactly the next scheduled day; timing is recorded by this process."""
    day = registry_date(trade_date, "trade_date")
    content = _payload_copy(payload)
    directory = prospective_directory(root, plan_id)
    with trial_registry_write_lock(directory):
        plan, receipts = _load_state(directory, plan_id, verify_code=True)
        for receipt in receipts:
            if receipt["trade_date"] == trade_date:
                if canonical_json_bytes(receipt["payload"]) != canonical_json_bytes(content):
                    raise ValueError("a different receipt payload already exists for this day")
                return receipt
        spec = registry_object(plan["specification"], "specification")
        signals = cast(list[str], spec["signal_dates"])
        if len(receipts) >= len(signals) or trade_date != signals[len(receipts)]:
            raise ValueError("receipts must follow the fixed signal schedule without gaps")
        recorded = utc_now()
        previous = receipts[-1] if receipts else plan
        if recorded < registry_timestamp(previous["recorded_at"], "previous recorded_at"):
            raise ValueError("strategy prospective clock moved backwards")
        status = _receipt_status(spec, day, content, recorded)
        receipt = prospective_record({
            "schema_version": RECEIPT_SCHEMA, "plan_digest": plan["digest"], "sequence": len(receipts) + 1,
            "previous_digest": previous["digest"], "trade_date": trade_date, "recorded_at": recorded.isoformat(),
            "status": status, "payload": content,
        })
        publish_prospective_record(directory, f"receipt-{trade_date}.json", receipt)
        return receipt


def strategy_prospective_status(root: Path, plan_id: str) -> dict[str, object]:
    """Show the retained schedule, overdue holes and source drift without collecting data."""
    plan, receipts = _load_state(prospective_directory(root, plan_id), plan_id, verify_code=False)
    spec = registry_object(plan["specification"], "specification")
    signals = cast(list[str], spec["signal_dates"])
    exits = cast(dict[str, str | None], spec["exit_dates"])
    remaining = signals[len(receipts):]
    now = utc_now()
    due = [day for day in remaining if _window(spec, registry_date(day, "signal date"))[0] <= now]
    overdue = [day for day in due if _window(spec, registry_date(day, "signal date"))[1] < now]
    counts = dict(Counter(str(item["status"]) for item in receipts))
    code_ok = _source_manifest() == spec["source_manifest"]
    calendar_ok = _calendar_matches(plan)
    return {
        "schema_version": "strategy-template-prospective-status-v1", "plan_id": plan_id, "plan_digest": plan["digest"],
        "status": _waiting_status(remaining, due, overdue, code_ok, calendar_ok), "source_code_matches": code_ok,
        "calendar_matches_frozen": calendar_ok,
        "next_signal_date": remaining[0] if remaining else None, "due_count": len(due), "overdue_count": len(overdue),
        "missing_count": counts.get("missing", 0), "receipt_counts": counts, "receipt_count": len(receipts),
        "planned_anchor_count": len(signals), "matureable_anchor_count": sum(value is not None for value in exits.values()),
        "calendar_end": _calendar_dates(decode_calendar(plan["calendar_base64"], plan["calendar_sha256"]))[-1],
        "head_digest": receipts[-1]["digest"] if receipts else plan["digest"],
        "receipts": receipts, "adoptable_template_id": None, "promotion_eligible": False,
        "timestamp_assurance": "local-only-unverified", "official_execution_admitted": False,
    }


def validate_strategy_prospective_calendar(plan: Mapping[str, object]) -> None:
    """Require the live calendar to preserve every frozen entry/holding/exit path."""
    spec = registry_object(plan["specification"], "specification")
    calendar = _calendar_dates(decode_calendar(plan["calendar_base64"], plan["calendar_sha256"]))
    exits = cast(dict[str, str | None], spec["exit_dates"])
    last = calendar[-1] if None in exits.values() else max([cast(str, spec["end_date"]), *(str(day) for day in exits.values())])
    first = cast(str, spec["start_date"])
    expected = tuple(date.fromisoformat(day) for day in calendar if first <= day <= last)
    try:
        actual = trading_dates_between(date.fromisoformat(first), date.fromisoformat(last), allow_auto_refresh=False)
    except TradingCalendarCoverageError as exc:
        raise StrategyProspectiveCalendarError("live calendar cannot cover the frozen prospective range") from exc
    if actual != expected:
        raise StrategyProspectiveCalendarError("live calendar differs from the frozen prospective session axis")
    indices = {day: index for index, day in enumerate(calendar)}
    spacing = cast(int, spec["horizon"]) + 1
    for day in cast(list[str], spec["signal_dates"]):
        start = indices[day] + 1
        _require_signal_calendar_path(day, tuple(calendar[start:start + spacing]), spacing)


def _require_signal_calendar_path(day: str, expected: tuple[str, ...], spacing: int) -> None:
    try:
        actual = tuple(value.isoformat() for value in next_trade_dates(date.fromisoformat(day), spacing, allow_auto_refresh=False))
    except TradingCalendarCoverageError as exc:
        if len(expected) == spacing:
            raise StrategyProspectiveCalendarError("live calendar cannot cover the frozen prospective holding path") from exc
        return
    if actual != expected or len(expected) < spacing:
        raise StrategyProspectiveCalendarError("live calendar changed a frozen prospective holding path or coverage boundary")


def _calendar_matches(plan: Mapping[str, object]) -> bool:
    try:
        validate_strategy_prospective_calendar(plan)
    except StrategyProspectiveCalendarError:
        return False
    return True


def _arguments(start: str, end: str, horizon: int, notional: float, cutoff: str) -> dict[str, object]:
    first, last = registry_date(start, "start_date"), registry_date(end, "end_date")
    if first > last or type(horizon) is not int or horizon not in (1, 5, 10, 20):
        raise ValueError("invalid strategy prospective period or horizon")
    if isinstance(notional, bool) or not isinstance(notional, (int, float)) or not 1e4 <= notional <= 1e9:
        raise ValueError("invalid strategy prospective cash budget")
    if Decimal(str(notional)) != Decimal(str(notional)).quantize(Decimal("0.01")):
        raise ValueError("strategy prospective cash budget requires exact cents")
    if not isinstance(cutoff, str):
        raise ValueError("cutoff_local must be a canonical clock string")
    clock = time.fromisoformat(cutoff)
    if clock.tzinfo is not None or clock.isoformat() != cutoff or len(cutoff) != 8 or clock < time(15, 15):
        raise ValueError("cutoff_local must be HH:MM:SS at or after 15:15:00")
    return {"start_date": start, "end_date": end, "horizon": horizon, "notional_cash_cny": float(notional), "cutoff_local": cutoff}


def _specification(arguments: Mapping[str, object], calendar: bytes) -> dict[str, object]:
    _strategies, templates, contracts = strategy_template_tracking_contracts()
    score = market_scan_score_spec(min_data_quality_score=get_settings().market_scan_min_data_quality_score)
    return {
        **arguments, **_schedule(arguments, calendar), "timezone": "Asia/Shanghai", "templates": templates,
        "source_contracts": contracts, "inference_contract": strategy_template_selection_contract(cast(int, arguments["horizon"])),
        "source_manifest": _source_manifest(), "production_score_spec": score,
        "production_score_spec_hash": stable_score_spec_hash(score), "selection_rule": _SELECTION_RULE,
        "missing_policy": _MISSING_POLICY,
    }


def _calendar_dates(encoded: bytes) -> list[str]:
    snapshot = registry_object(decode_json_bytes(encoded), "calendar")
    days = snapshot.get("trade_dates")
    if not isinstance(days, list) or not days:
        raise ValueError("strategy prospective calendar is unavailable")
    parsed = [registry_date(day, "calendar date").isoformat() for day in days]
    if parsed != sorted(set(parsed)) or snapshot.get("trade_date_count") != len(parsed):
        raise ValueError("strategy prospective calendar has inconsistent sessions")
    if snapshot.get("min_date") != parsed[0] or snapshot.get("max_date") != parsed[-1]:
        raise ValueError("strategy prospective calendar has inconsistent bounds")
    return parsed


def _schedule(arguments: Mapping[str, object], encoded: bytes) -> dict[str, object]:
    calendar = _calendar_dates(encoded)
    start, end = cast(str, arguments["start_date"]), cast(str, arguments["end_date"])
    if start < calendar[0] or end > calendar[-1]:
        raise ValueError("strategy prospective dates exceed trusted calendar coverage")
    days = [day for day in calendar if start <= day <= end]
    if not days:
        raise ValueError("strategy prospective period contains no trading sessions")
    spacing = cast(int, arguments["horizon"]) + 1
    signals = days[::spacing]
    indices = {day: index for index, day in enumerate(calendar)}
    exits = {day: calendar[indices[day] + spacing] if indices[day] + spacing < len(calendar) else None for day in signals}
    return {"trading_dates": days, "signal_dates": signals, "exit_dates": exits}


def _source_manifest() -> dict[str, str]:
    paths = []
    for name in ("app", "tools"):
        directory = PROJECT_ROOT / name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("strategy prospective source directory is unsafe or missing")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError("strategy prospective source manifest cannot omit symbolic links")
            if path.suffix == ".py":
                paths.append(path)
    if not paths:
        raise ValueError("strategy prospective source manifest is empty")
    return {path.relative_to(PROJECT_ROOT).as_posix(): sha256_hex(read_regular_file(path, max_bytes=MAX_PLAN_BYTES)) for path in sorted(paths)}


def _validate_manifest(value: object) -> dict[str, object]:
    manifest = registry_object(value, "source_manifest")
    if not manifest:
        raise ValueError("strategy prospective source manifest is empty")
    for filename, digest in manifest.items():
        path = Path(filename)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != filename or path.parts[0] not in {"app", "tools"} or path.suffix != ".py":
            raise ValueError("strategy prospective source manifest has an unsafe path")
        registry_hash(digest, "source digest")
    return manifest


def _load_state(directory: Path, plan_id: str, *, verify_code: bool) -> tuple[dict[str, object], list[dict[str, object]]]:
    names = prospective_namespace(directory)
    plan = read_prospective_record(directory, "plan.json", PLAN_KEYS, PLAN_SCHEMA)
    if plan["plan_id"] != plan_id:
        raise ValueError("strategy prospective plan identity mismatch")
    spec = registry_object(plan["specification"], "specification")
    _validate_plan(plan, spec, verify_code=verify_code)
    receipts = _load_receipts(directory, names, plan, spec)
    return plan, receipts


def _validate_plan(plan: Mapping[str, object], spec: Mapping[str, object], *, verify_code: bool) -> None:
    registry_keys(spec, _SPEC_KEYS, "specification")
    arguments = _arguments(cast(str, spec["start_date"]), cast(str, spec["end_date"]), cast(int, spec["horizon"]),
                           cast(float, spec["notional_cash_cny"]), cast(str, spec["cutoff_local"]))
    encoded = decode_calendar(plan["calendar_base64"], plan["calendar_sha256"])
    schedule = _schedule(arguments, encoded)
    if any(spec[key] != value for key, value in schedule.items()):
        raise ValueError("strategy prospective frozen calendar schedule mismatch")
    if (spec["timezone"], spec["selection_rule"], spec["missing_policy"]) != ("Asia/Shanghai", _SELECTION_RULE, _MISSING_POLICY):
        raise ValueError("strategy prospective selection policies differ")
    recorded = registry_timestamp(plan["recorded_at"], "plan recorded_at")
    if recorded > utc_now() or recorded.astimezone(ASHARE_TIMEZONE).date() >= registry_date(spec["start_date"], "start_date"):
        raise ValueError("strategy prospective plan was not frozen before the collection period")
    _validate_frozen_contracts(spec)
    manifest = _validate_manifest(spec["source_manifest"])
    if verify_code and manifest != _source_manifest():
        raise ValueError("strategy prospective source code drift; preserve this plan and create a new future plan")
    if verify_code:
        validate_strategy_prospective_calendar(plan)


def _validate_frozen_contracts(spec: Mapping[str, object]) -> None:
    templates = spec["templates"]
    if not isinstance(templates, list) or [registry_object(item, "template").get("template_id") for item in templates] != list(STRATEGY_TEMPLATE_TRACKING_IDS):
        raise ValueError("strategy prospective requires the fixed three templates")
    for item in templates:
        template = MarketStrategyTemplate.model_validate({key: value for key, value in item.items() if key != "strategy_fingerprint"})
        if template.strategy_spec is None or item.get("strategy_fingerprint") != strategy_spec_fingerprint(template.strategy_spec):
            raise ValueError("strategy prospective frozen template fingerprint mismatch")
    MarketStrategyTemplateSourceContracts.model_validate(spec["source_contracts"])
    score = registry_object(spec["production_score_spec"], "production_score_spec")
    if stable_score_spec_hash(score) != registry_hash(spec["production_score_spec_hash"], "score hash"):
        raise ValueError("strategy prospective production score contract mismatch")
    if spec["inference_contract"] != strategy_template_selection_contract(cast(int, spec["horizon"])):
        raise ValueError("strategy prospective inference identity mismatch")


def _payload_copy(payload: Mapping[str, object]) -> dict[str, object]:
    encoded = canonical_json_bytes(dict(payload))
    if len(encoded) > MAX_RECEIPT_BYTES // 2:
        raise ValueError("strategy prospective receipt payload is oversized")
    content = registry_object(decode_json_bytes(encoded), "receipt payload")
    if "recorded_at" in content or content.get("status") not in _STATUSES:
        raise ValueError("receipt payload has an invalid status or external recorded_at")
    return content


def _window(spec: Mapping[str, object], day: date) -> tuple[datetime, datetime]:
    return (datetime.combine(day, time(15, 15), ASHARE_TIMEZONE),
            datetime.combine(day, time.fromisoformat(cast(str, spec["cutoff_local"])), ASHARE_TIMEZONE))


def _receipt_status(spec: Mapping[str, object], day: date, payload: Mapping[str, object], recorded: datetime) -> str:
    if day > recorded.astimezone(ASHARE_TIMEZONE).date():
        raise ValueError("cannot record a future strategy signal day")
    opened, cutoff = _window(spec, day)
    if payload["status"] == "missing":
        _require_missing_evidence(payload, recorded, cutoff)
        return "missing"
    available = registry_timestamp(payload.get("available_at"), "source available_at")
    if available > recorded:
        raise ValueError("source availability cannot be later than receipt recording")
    session = payload.get("session")
    if session is not None:
        source = registry_object(session, "session")
        if source.get("signal_date", day.isoformat()) != day.isoformat():
            raise ValueError("receipt session has a different signal date")
        if "published_at" in source and registry_timestamp(source["published_at"], "source published_at") > available:
            raise ValueError("source publication cannot follow its availability")
    if payload["status"] == "captured" and session is None:
        raise ValueError("captured receipt requires a frozen session")
    if payload["status"] == "late" or not opened <= recorded <= cutoff or not opened <= available <= cutoff:
        return "late"
    return cast(str, payload["status"])


def _require_missing_evidence(payload: Mapping[str, object], recorded: datetime, cutoff: datetime) -> None:
    if (recorded <= cutoff or payload.get("session") is not None or payload.get("available_at") is not None
            or not isinstance(payload.get("reason"), str) or not str(payload["reason"]).strip()):
        raise ValueError("missing receipts require an elapsed deadline, a reason and no session")


def _load_receipts(
    directory: Path, names: set[str], plan: Mapping[str, object], spec: Mapping[str, object],
) -> list[dict[str, object]]:
    filenames = sorted(name for name in names if RECEIPT_PATTERN.fullmatch(name))
    signals = cast(list[str], spec["signal_dates"])
    if filenames != [f"receipt-{day}.json" for day in signals[:len(filenames)]]:
        raise ValueError("strategy prospective receipt schedule is incomplete or unexpected")
    receipts: list[dict[str, object]] = []
    previous = plan
    for index, filename in enumerate(filenames, 1):
        receipt = read_prospective_record(directory, filename, RECEIPT_KEYS, RECEIPT_SCHEMA)
        _validate_receipt(receipt, previous, plan, spec, signals[index - 1], index)
        receipts.append(receipt)
        previous = receipt
    return receipts


def _validate_receipt(
    receipt: Mapping[str, object], previous: Mapping[str, object], plan: Mapping[str, object],
    spec: Mapping[str, object], day: str, sequence: int,
) -> None:
    if receipt["plan_digest"] != plan["digest"] or receipt["previous_digest"] != previous["digest"]:
        raise ValueError("strategy prospective receipt hash chain mismatch")
    if type(receipt["sequence"]) is not int or receipt["sequence"] != sequence or receipt["trade_date"] != day:
        raise ValueError("strategy prospective receipt sequence identity mismatch")
    recorded = registry_timestamp(receipt["recorded_at"], "receipt recorded_at")
    if not registry_timestamp(previous["recorded_at"], "previous recorded_at") <= recorded <= utc_now():
        raise ValueError("strategy prospective receipt times are inconsistent")
    payload = _payload_copy(registry_object(receipt["payload"], "receipt payload"))
    expected = _receipt_status(spec, registry_date(day, "signal date"), payload, recorded)
    if receipt["status"] != expected:
        raise ValueError("strategy prospective receipt status disagrees with timing")


def _waiting_status(remaining: list[str], due: list[str], overdue: list[str], code_ok: bool, calendar_ok: bool) -> str:
    if not code_ok:
        return "source_drift"
    if not calendar_ok:
        return "calendar_drift"
    if not remaining:
        return "collection_complete"
    if overdue:
        return "missing_receipts_due"
    return "collection_due" if due else "waiting_for_signal"
