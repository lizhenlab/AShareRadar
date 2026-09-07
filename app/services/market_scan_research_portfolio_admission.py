"""Pure admission of official sealed rows and visibly synthetic fixtures."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
import math
import re

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.market_scan_official_execution import (
    OfficialExecutionSessionArtifact,
    OfficialExecutionSessionRow,
    VerifiedOfficialExecutionSession,
)
from app.services.market_scan_research_portfolio_models import (
    ResearchMarketData,
    ResearchPortfolioConfig,
    ResearchSignalBatch,
)
from app.services.trading_calendar import trading_dates_between


def validate_research_inputs(batches: Sequence[ResearchSignalBatch], sessions: Sequence[str]) -> None:
    if len(sessions) < 2 or list(sessions) != sorted(set(sessions)):
        raise ValueError("sessions must be a complete ordered trading calendar")
    expected = tuple(day.isoformat() for day in trading_dates_between(date.fromisoformat(sessions[0]), date.fromisoformat(sessions[-1])))
    if tuple(sessions) != expected:
        raise ValueError("sessions must match the complete trusted trading calendar")
    if not batches or len({batch.batch_id for batch in batches}) != len(batches):
        raise ValueError("batches must have unique nonempty identities")
    if len({batch.signal_date for batch in batches}) != len(batches):
        raise ValueError("one frozen signal batch is allowed per session")
    for batch in batches:
        _validate_batch(batch, sessions)


def _validate_batch(batch: ResearchSignalBatch, sessions: Sequence[str]) -> None:
    if not batch.batch_id.strip() or batch.signal_date not in sessions or not _sha256(batch.source_digest):
        raise ValueError("batch identity, signal session or source digest is invalid")
    ranks = [candidate.frozen_rank for candidate in batch.candidates]
    symbols = [candidate.symbol for candidate in batch.candidates]
    if len(set(ranks)) != len(ranks) or len(set(symbols)) != len(symbols):
        raise ValueError("frozen candidate ranks and symbols must be unique")
    for candidate in batch.candidates:
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", candidate.symbol) or not _sha256(candidate.source_identity):
            raise ValueError("candidate symbol or source identity is invalid")
        if isinstance(candidate.frozen_rank, bool) or not isinstance(candidate.frozen_rank, int) or candidate.frozen_rank <= 0:
            raise ValueError("candidate frozen rank must be a positive integer")


def _sha256(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


def admit_research_market_data(
    official_sessions: Sequence[VerifiedOfficialExecutionSession],
    synthetic_rows: Sequence[OfficialExecutionSessionRow],
) -> ResearchMarketData:
    if official_sessions and synthetic_rows:
        raise ValueError("official and synthetic evidence cannot be mixed")
    rows: dict[tuple[str, str], OfficialExecutionSessionRow] = {}
    artifact_digests: list[str] = []
    for session in official_sessions:
        if not isinstance(session, VerifiedOfficialExecutionSession):
            raise ValueError("official data requires strict-loader verified session tokens")
        artifact = OfficialExecutionSessionArtifact.model_validate(session.artifact)
        if artifact.artifact_digest != session.artifact_digest or artifact.session_date != session.session_date:
            raise ValueError("verified session identity changed")
        artifact_digests.append(artifact.artifact_digest)
        for item in artifact.rows:
            _admit_row(rows, item)
    for item in synthetic_rows:
        _admit_row(rows, item)
    provenance = "official_raw_file_verified" if official_sessions else "synthetic" if synthetic_rows else "unavailable"
    return ResearchMarketData(rows, provenance, tuple(sorted(artifact_digests)))


def _admit_row(rows: dict[tuple[str, str], OfficialExecutionSessionRow], item: OfficialExecutionSessionRow) -> None:
    row = OfficialExecutionSessionRow.model_validate(item.model_dump(mode="json"))
    key = (row.symbol, row.session_date)
    if key in rows:
        raise ValueError("duplicate symbol/session execution evidence")
    rows[key] = row


def research_input_digest(
    batches: Sequence[ResearchSignalBatch], sessions: Sequence[str],
    market: ResearchMarketData, config: ResearchPortfolioConfig,
) -> str:
    payload = {
        "batches": [{**asdict(batch), "candidates": [asdict(item) for item in sorted(batch.candidates, key=lambda item: item.frozen_rank)]}
                    for batch in sorted(batches, key=lambda item: (item.signal_date, item.batch_id))],
        "sessions": list(sessions), "config": asdict(config), "provenance": market.provenance_status,
        "artifacts": list(market.source_artifact_digests),
        "rows": [market.rows[key].row_digest for key in sorted(market.rows)],
    }
    return sha256_hex(canonical_json_bytes(payload))


def research_row_status(
    current: OfficialExecutionSessionRow | None, previous: OfficialExecutionSessionRow | None,
) -> str | None:
    if current is None:
        return "session_evidence_missing"
    if current.corporate_action.status != "none":
        return "corporate_action_ledger_required"
    if previous is None or previous.bar.close is None:
        return "previous_session_missing"
    reference = current.corporate_action
    if not all(math.isclose(previous.bar.close, value, rel_tol=0, abs_tol=1e-8)
               for value in (reference.previous_close, reference.reference_price)):
        return "previous_close_reference_conflict"
    if current.exchange_session_state != "trading":
        return "session_" + current.exchange_session_state
    if current.bar.close is None or current.bar.open is None or not current.bar.volume:
        return "session_valuation_missing"
    if current.instrument_rules.listing_status != "listed":
        return "listing_rule_ineligible"
    return None
