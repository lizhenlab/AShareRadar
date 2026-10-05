"""Attach execution research to captured baskets without selecting new source runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from app.artifacts.io import canonical_json_bytes, exclusive_atomic_publish, sha256_hex
from app.services.market_scan_official_execution import VerifiedOfficialExecutionSession
from app.services.market_scan_trial_registry_contract import registry_timestamp
from app.services.public_execution_comparison import compare_public_execution_facts
from app.services.public_execution_store import DEFAULT_PUBLIC_EXECUTION_ROOT, read_public_execution_sources
from app.services.strategy_prospective_evidence import build_strategy_execution_requirements, collect_strategy_execution_research
from app.services.strategy_prospective_plan import read_strategy_prospective_state
from app.services.strategy_prospective_replay import replay_prospective_captured_session
from app.services.strategy_prospective_snapshot import verify_strategy_scan_archive
from app.services.strategy_template_net_returns import evaluate_strategy_template_net_returns
from app.services.strategy_template_tracking_metrics import StrategyTrackingSession
from app.utils.clock import utc_now


@dataclass(frozen=True)
class _ReceiptSnapshot:
    plan: dict[str, object]
    receipts: tuple[dict[str, object], ...]
    as_of: datetime

    @property
    def identity(self) -> dict[str, object]:
        return {"plan_digest": self.plan["digest"], "head_digest": self.receipts[-1]["digest"] if self.receipts else self.plan["digest"],
                "receipt_count": len(self.receipts), "as_of": self.as_of.isoformat(), "generated_at": self.as_of.isoformat()}


def _read_snapshot(root: Path, plan_id: str) -> _ReceiptSnapshot:
    plan, receipts = read_strategy_prospective_state(root, plan_id)
    now = utc_now()
    if any(registry_timestamp(row["recorded_at"], "receipt time") > now for row in receipts):
        raise ValueError("prospective receipt is newer than the report as_of")
    return _ReceiptSnapshot(plan, receipts, now)


def prospective_captured_sessions(root: Path, plan_id: str) -> tuple[StrategyTrackingSession, ...]:
    """Accept only on-time baskets that exactly replay from their complete original inputs."""
    snapshot = _read_snapshot(root, plan_id)
    return _captured_sessions(root / plan_id, snapshot)


def _captured_sessions(directory: Path, snapshot: _ReceiptSnapshot) -> tuple[StrategyTrackingSession, ...]:
    spec = cast(dict[str, object], snapshot.plan["specification"])
    sessions = []
    for receipt in snapshot.receipts:
        payload = cast(Mapping[str, object], receipt["payload"])
        if receipt["status"] == "captured":
            sessions.append(replay_prospective_captured_session(directory, spec, receipt))
        elif payload.get("snapshot_path") is not None:
            verify_strategy_scan_archive(directory, payload)
    return tuple(sessions)


def collect_prospective_execution_evidence(
    root: Path, plan_id: str, history_root: Path | None, *, public_root: Path = DEFAULT_PUBLIC_EXECUTION_ROOT,
) -> dict[str, object]:
    snapshot = _read_snapshot(root, plan_id)
    spec = cast(dict[str, Any], snapshot.plan["specification"])
    sessions = _captured_sessions(root / plan_id, snapshot)
    requirements = build_strategy_execution_requirements(sessions, horizon=spec["horizon"])
    research = collect_strategy_execution_research(requirements, history_root, as_of=snapshot.as_of)
    pairs = [(row["symbol"], row["session_date"]) for row in cast(list[dict[str, str]], requirements["symbol_sessions"])]
    sources = read_public_execution_sources(public_root, [day for _symbol, day in pairs], as_of=snapshot.as_of, wanted_pairs=pairs)
    public = compare_public_execution_facts(pairs, sources, as_of=snapshot.as_of)
    report = {"schema_version": "strategy-prospective-execution-collection-v1", **snapshot.identity,
              "public_execution_facts": public,
              "requirements": requirements, "research": research, "source_statuses": _source_statuses(snapshot),
              "official_execution_admitted": False, "adoptable_template_id": None}
    path = publish_prospective_report(root / plan_id / "research", "execution", report)
    return {"report_path": str(path), "status": research["status"], "pair_count": requirements["pair_count"],
            "official_execution_admitted": False}


def evaluate_prospective_execution(
    root: Path, plan_id: str, *, official_sessions: Sequence[VerifiedOfficialExecutionSession] = (),
) -> dict[str, object]:
    snapshot = _read_snapshot(root, plan_id)
    spec = cast(dict[str, Any], snapshot.plan["specification"])
    sessions = _captured_sessions(root / plan_id, snapshot)
    net = evaluate_strategy_template_net_returns(sessions, spec["templates"], as_of=snapshot.as_of, horizon=spec["horizon"],
                                                notional_cash_cny=spec["notional_cash_cny"], official_sessions=official_sessions)
    report = {"schema_version": "strategy-prospective-execution-outcomes-v1", **snapshot.identity,
              "planned_signal_dates": spec["signal_dates"], "source_statuses": _source_statuses(snapshot),
              "net_comparison": net, "timestamp_assurance": "local-only-unverified",
              "adoptable_template_id": None, "promotion_eligible": False,
              "limitations": ["Collection receipts are local time evidence, not an independent timestamp attestation.",
                              "Incomplete source dates stay on the planned axis; outcomes never recreate selections.",
                              "Continuous-account validation and prospective statistical evaluation are not implemented."]}
    path = publish_prospective_report(root / plan_id / "research", "outcomes", report)
    return {"report_path": str(path), "captured_session_count": len(sessions), "adoptable_template_id": None}


def _source_statuses(snapshot: _ReceiptSnapshot) -> list[dict[str, object]]:
    return [{"trade_date": row["trade_date"], "status": row["status"], "digest": row["digest"]} for row in snapshot.receipts]


def publish_prospective_report(directory: Path, kind: str, report: Mapping[str, object]) -> Path:
    if kind not in {"execution", "outcomes"}:
        raise ValueError("invalid prospective report kind")
    encoded = canonical_json_bytes(dict(report))
    target = directory / f"{kind}-{sha256_hex(encoded)}.json"
    exclusive_atomic_publish(target, encoded, max_bytes=32 * 1024 * 1024)
    return target
