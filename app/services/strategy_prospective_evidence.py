"""Freeze execution-data requirements and archive explicitly nonofficial inputs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from math import isfinite
from pathlib import Path
import re
from typing import cast

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.fuyao_dumps_validation import FuyaoDumpError
from app.services.strategy_prospective_archive import extract_research_archive, read_verified_research_archive
from app.services.strategy_template_tracking_metrics import STRATEGY_TEMPLATE_TRACKING_IDS, StrategyTrackingSession
from app.services.trading_calendar import ASHARE_TIMEZONE, TradingCalendarCoverageError, latest_expected_daily_kline_date, next_trade_dates, trading_dates_between
from app.utils.clock import utc_now


REQUIREMENTS_SCHEMA_VERSION = "strategy-execution-requirements-v1"
RESEARCH_EVIDENCE_SCHEMA_VERSION = "strategy-execution-research-evidence-v1"
_MAX_PAIRS = 200_000
_DAILY_FIELDS = ["open_price", "high_price", "low_price", "close_price", "volume", "turnover"]
_OFFICIAL_GAPS = ["licensed_source_registry", "original_delivery_receipt", "historical_instrument_rules",
                  "exchange_session_state", "entry_execution_state", "exit_execution_state",
                  "complete_corporate_action_coverage", "exchange_previous_close_reference"]


def build_strategy_execution_requirements(sessions: Sequence[StrategyTrackingSession], *, horizon: int) -> dict[str, object]:
    """Freeze all selected symbols before any later price availability is read."""
    signals = [_freeze_signal(session) for session in sessions]
    return _requirements_from_signals(signals, horizon)


def _freeze_signal(session: StrategyTrackingSession) -> dict[str, object]:
    return {
        "run_id": session.run_id, "signal_date": session.signal_date, "rule_version": session.rule_version,
        "score_spec_hash": session.score_spec_hash, "snapshot_digest": session.snapshot_digest, "published_at": session.published_at,
        "selections": [{"template_id": item.template_id, "status": item.status, "execution_fingerprint": item.execution_fingerprint,
                        "result_digest": item.result_digest,
                        "positions": [{"symbol": position.symbol, "target_weight": position.target_weight, "signal_price": position.signal_price}
                                      for position in item.positions]} for item in session.selections],
    }


def _requirements_from_signals(signals: list[dict[str, object]], horizon: int) -> dict[str, object]:
    if type(horizon) is not int or horizon not in (1, 5, 10, 20) or len(signals) > 200:
        raise ValueError("invalid or excessive execution requirements")
    frozen = _ordered_signals(signals)
    identities: set[tuple[object, object, object]] = set()
    runs: set[object] = set()
    symbols: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    blockers: set[str] = set()
    for signal in frozen:
        members = _validate_signal(signal)
        identity = (signal["rule_version"], signal["score_spec_hash"], signal["signal_date"])
        if identity in identities or signal["run_id"] in runs:
            raise ValueError("duplicate frozen execution source")
        identities.add(identity)
        runs.add(signal["run_id"])
        symbols.update(members)
        days = _signal_days(signal, horizon)
        if not days:
            blockers.add("calendar_unavailable")
        if any(row["status"] == "blocked" for row in cast(list[dict[str, object]], signal["selections"])):
            blockers.add("selection_blocked")
        pairs.update((symbol, day) for symbol in members for day in days)
        if len(pairs) > _MAX_PAIRS:
            raise ValueError("execution requirements exceed 200000 symbol/session pairs")
    return _requirements_payload(frozen, horizon, symbols, pairs, blockers)


def _ordered_signals(signals: list[dict[str, object]]) -> list[dict[str, object]]:
    for signal in signals:
        if not isinstance(signal, Mapping):
            raise ValueError("invalid frozen signal object")
        if type(signal.get("run_id")) is not int:
            raise ValueError("invalid frozen run identity")
        if not all(isinstance(signal.get(key), str) for key in ("signal_date", "published_at")):
            raise ValueError("invalid frozen time fields")
    return sorted(signals, key=lambda row: (cast(str, row["signal_date"]), cast(int, row["run_id"])))


def _requirements_payload(
    signals: list[dict[str, object]], horizon: int, symbols: set[str], pairs: set[tuple[str, str]], blockers: set[str],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": REQUIREMENTS_SCHEMA_VERSION, "status": "blocked" if blockers else "ready" if pairs else "needs_source",
        "horizon_sessions": horizon, "signal_count": len(signals), "symbol_count": len(symbols), "pair_count": len(pairs),
        "symbols": sorted(symbols), "signals": signals,
        "symbol_sessions": [{"symbol": symbol, "session_date": day} for symbol, day in sorted(pairs)],
        "required_fields": {"unadjusted_daily": list(_DAILY_FIELDS), "official_evidence": list(_OFFICIAL_GAPS)},
        "blocked_reasons": sorted(blockers or ({"no_frozen_candidates"} if not pairs else set())),
        "date_policy": "frozen-signal-D-through-D+H+1-inclusive;trusted-calendar",
        "selection_policy": "frozen-three-template-symbol-union;no-future-price-filtering",
    }
    return {**payload, "requirements_digest": sha256_hex(canonical_json_bytes(payload))}


def _validate_signal(signal: Mapping[str, object]) -> set[str]:
    if type(signal.get("run_id")) is not int or cast(int, signal["run_id"]) <= 0:
        raise ValueError("invalid frozen run identity")
    if not isinstance(signal.get("rule_version"), str) or not signal["rule_version"]:
        raise ValueError("invalid frozen rule identity")
    for key in ("score_spec_hash", "snapshot_digest"):
        _require_digest(signal.get(key))
    selections = signal.get("selections")
    if not isinstance(selections, list) or not all(isinstance(item, Mapping) for item in selections):
        raise ValueError("invalid frozen selection objects")
    if Counter(item.get("template_id") for item in selections) != Counter(STRATEGY_TEMPLATE_TRACKING_IDS):
        raise ValueError("execution requirements need all three frozen templates")
    members = set()
    for selection in selections:
        members.update(_selection_members(selection))
    return members


def _selection_members(selection: Mapping[str, object]) -> set[str]:
    if selection.get("status") not in {"ready", "no_trade", "blocked"}:
        raise ValueError("invalid frozen selection status")
    for key in ("execution_fingerprint", "result_digest"):
        _require_digest(selection.get(key))
    positions = selection.get("positions")
    if not isinstance(positions, list) or len(positions) > 100:
        raise ValueError("invalid bounded frozen positions")
    if not all(isinstance(item, Mapping) for item in positions):
        raise ValueError("invalid frozen position objects")
    members = [_position_symbol(position) for position in positions]
    if len(members) != len(set(members)) or selection["status"] == "no_trade" and members:
        raise ValueError("inconsistent frozen positions")
    if sum(cast(float, item["target_weight"]) for item in positions) > 1.0000001:
        raise ValueError("frozen target weights exceed capital")
    return set(members)


def _position_symbol(position: Mapping[str, object]) -> str:
    symbol = position.get("symbol")
    if not isinstance(symbol, str) or re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) is None:
        raise ValueError("invalid frozen stock identity")
    for field in ("target_weight", "signal_price"):
        value = position.get(field)
        if not _finite_positive(value):
            raise ValueError("invalid frozen numeric input")
    if cast(float, position["target_weight"]) > 1:
        raise ValueError("invalid frozen stock weight")
    return symbol


def _finite_positive(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return isfinite(value) and value > 0
    except OverflowError:
        return False


def _require_digest(value: object) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid frozen evidence digest")


def _signal_days(signal: Mapping[str, object], horizon: int) -> tuple[str, ...]:
    day = date.fromisoformat(cast(str, signal["signal_date"]))
    published = datetime.fromisoformat(cast(str, signal["published_at"]))
    if day.isoformat() != signal["signal_date"] or published.tzinfo is None or published.utcoffset() is None:
        raise ValueError("invalid frozen date or publication time")
    try:
        if trading_dates_between(day, day, allow_auto_refresh=False) != (day,):
            raise ValueError("frozen signal is not an exchange session")
        future = next_trade_dates(day, horizon + 1, allow_auto_refresh=False)
    except TradingCalendarCoverageError:
        return ()
    if not datetime.combine(day, time(15), ASHARE_TIMEZONE) <= published < datetime.combine(future[0], time(9, 30), ASHARE_TIMEZONE):
        raise ValueError("frozen decision was not published before entry")
    return (day.isoformat(), *(value.isoformat() for value in future))


def collect_strategy_execution_research(
    requirements: Mapping[str, object], history_root: Path | None, *, as_of: datetime,
) -> dict[str, object]:
    """Return an archive payload; no network, credentials, runtime writes or promotion."""
    _validate_requirements(requirements)
    completed = _completed_date(as_of)
    pairs = [(cast(str, row["symbol"]), cast(str, row["session_date"])) for row in cast(list[dict[str, object]], requirements["symbol_sessions"])]
    available = [pair for pair in pairs if pair[1] <= completed.isoformat()]
    pending = [pair for pair in pairs if pair[1] > completed.isoformat()]
    source, daily, actions, reason = _collect_archive(history_root, available, as_of, completed)
    found = {(str(row["symbol"]), str(row["session_date"])) for row in daily}
    missing = [pair for pair in available if pair not in found]
    payload = _research_payload(requirements, as_of, completed, source, reason)
    payload.update(data_rows=daily, corporate_actions=actions,
                   missing_pairs=_pair_payload(missing), pending_pairs=_pair_payload(pending),
                   completed_pair_count=len(available), collected_pair_count=len(daily), missing_pair_count=len(missing),
                   pending_pair_count=len(pending), corporate_action_record_count=len(actions))
    payload["status"] = _research_status(requirements, reason, daily, missing, pending)
    payload["artifact_digest"] = sha256_hex(canonical_json_bytes(payload))
    return payload


def _validate_requirements(requirements: Mapping[str, object]) -> None:
    signals, horizon = requirements.get("signals"), requirements.get("horizon_sessions")
    if not isinstance(signals, list) or type(horizon) is not int:
        raise ValueError("invalid execution requirement envelope")
    expected = _requirements_from_signals(signals, horizon)
    if canonical_json_bytes(dict(requirements)) != canonical_json_bytes(expected):
        raise ValueError("execution requirement digest or frozen membership mismatch")


def _completed_date(as_of: datetime) -> date:
    if as_of.tzinfo is None or as_of.utcoffset() is None or as_of > utc_now():
        raise ValueError("research capture requires a nonfuture timezone-aware cutoff")
    return latest_expected_daily_kline_date(as_of, allow_auto_refresh=False)


def _collect_archive(
    root: Path | None, pairs: list[tuple[str, str]], as_of: datetime, completed: date,
) -> tuple[dict[str, object] | None, list[dict[str, object]], list[dict[str, object]], str | None]:
    if root is None:
        return None, [], [], "history_source_not_configured"
    if not pairs:
        return None, [], [], "no_completed_symbol_sessions"
    try:
        archive = read_verified_research_archive(root)
        if archive is None:
            return None, [], [], "history_archive_unavailable"
        source = archive.manifest.model_dump(mode="json")
        observed = datetime.fromisoformat(archive.manifest.observed_at)
        if observed.tzinfo is None or observed.utcoffset() is None or observed > as_of:
            return source, [], [], "archive_not_available_at_as_of"
        daily, actions = extract_research_archive(archive, pairs, completed=completed)
        return source, daily, actions, None
    except (FuyaoDumpError, OSError, ValueError, ImportError):
        return None, [], [], "history_archive_verification_failed"


def _research_payload(
    requirements: Mapping[str, object], as_of: datetime, completed: date, source: dict[str, object] | None, reason: str | None,
) -> dict[str, object]:
    return {
        "schema_version": RESEARCH_EVIDENCE_SCHEMA_VERSION, "source": "public_vendor_research",
        "official_execution_admitted": False, "point_in_time_verified": False, "provider_calls": 0, "runtime_writes": 0,
        "requirements_digest": requirements["requirements_digest"], "symbol_count": requirements["symbol_count"],
        "required_pair_count": requirements["pair_count"], "as_of": as_of.isoformat(), "completed_date": completed.isoformat(),
        "collected_at": utc_now().isoformat(), "archive_observed_at": source.get("observed_at") if source else None,
        "source_manifest": source, "source_unavailable_reason": reason,
        "official_field_gaps": list(_OFFICIAL_GAPS), "corporate_action_coverage": "unknown",
        "limitations": ["raw_vendor_data_is_not_official_execution_evidence", "absent_bars_are_not_proof_of_suspension",
                        "no_event_row_is_not_proof_of_no_corporate_action", "same_day_actions_are_not_merged_or_reinterpreted",
                        "archive_observation_is_not_historical_provider_availability", "no_price_adjustment_or_trade_state_inference"],
    }


def _pair_payload(pairs: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"symbol": symbol, "session_date": day} for symbol, day in pairs]


def _research_status(
    requirements: Mapping[str, object], reason: str | None, daily: list[dict[str, object]],
    missing: list[tuple[str, str]], pending: list[tuple[str, str]],
) -> str:
    if requirements["status"] == "blocked":
        return "blocked"
    if reason or not daily:
        return "needs_source"
    return "partial_research_only" if missing or pending else "research_only"
