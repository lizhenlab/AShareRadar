"""Compare signal labels with actual shared-account paths without changing them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
import math
from typing import cast

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_scan import MarketScanResultItem, MarketScanRun
from app.services.market_scan_official_execution import OfficialExecutionSessionRow, VerifiedOfficialExecutionSession
from app.services.market_scan_research_challengers import ResearchChallenger
from app.services.market_scan_research_experiment import FrozenResearchDataset, research_signal_batches, validate_research_decision_time
from app.services.market_scan_research_portfolio import replay_research_portfolio, research_portfolio_payload
from app.services.market_scan_research_portfolio_admission import admit_research_market_data
from app.services.market_scan_research_portfolio_models import (
    ResearchMarketData, ResearchPortfolioConfig, ResearchPortfolioResult, ResearchPortfolioTrade, ResearchSignalBatch,
)
from app.services.market_scan_research_runner import compare_research_accounts
from app.services.trading_calendar import next_trade_dates
from app.utils.clock import ASHARE_TIMEZONE


_CAPTURE_SEAL = object()


@dataclass(frozen=True)
class ExecutionAuditDecision:
    run_id: int
    signal_date: str
    available_at: str
    source_digest: str
    naive_timestamp_count: int
    _proof: tuple[object, str] | None = field(default=None, repr=False, compare=False)


def capture_execution_audit_decision(snapshot: Mapping[str, object]) -> ExecutionAuditDecision:
    run = MarketScanRun.model_validate(snapshot["run"])
    items = [MarketScanResultItem.model_validate(item) for item in cast(list[object], snapshot["items"])]
    validate_research_decision_time(run, items)
    values = [run.as_of, run.created_at, run.updated_at, run.finished_at, run.snapshot_sealed_at, run.quote_capture_finished_at]
    values.extend(value for item in items for value in (item.updated_at, item.quote_observed_at))
    stamps = [datetime.fromisoformat(cast(str, value)) for value in values]
    digest = sha256_hex(canonical_json_bytes({"run": run.model_dump(mode="json"), "items": [
        item.model_dump(mode="json") for item in sorted(items, key=lambda item: item.symbol)
    ]}))
    aware = [stamp.replace(tzinfo=ASHARE_TIMEZONE) if stamp.tzinfo is None else stamp.astimezone(ASHARE_TIMEZONE) for stamp in stamps]
    decision = ExecutionAuditDecision(run.id, run.data_date, max(aware).isoformat(), digest, sum(stamp.tzinfo is None for stamp in stamps))
    object.__setattr__(decision, "_proof", (_CAPTURE_SEAL, sha256_hex(canonical_json_bytes(_decision_payload(decision)))))
    return decision


def _decision_payload(decision: ExecutionAuditDecision) -> dict[str, object]:
    return {"run_id": decision.run_id, "signal_date": decision.signal_date, "available_at": decision.available_at,
            "source_digest": decision.source_digest, "naive_timestamp_count": decision.naive_timestamp_count}


def audit_research_execution(
    dataset: FrozenResearchDataset, decisions: Sequence[ExecutionAuditDecision], sessions: Sequence[str], *,
    variant: ResearchChallenger = "production_v5", config: ResearchPortfolioConfig | None = None,
    official_sessions: Sequence[VerifiedOfficialExecutionSession] = (), synthetic_rows: Sequence[OfficialExecutionSessionRow] = (),
) -> dict[str, object]:
    settings = config or ResearchPortfolioConfig()
    if settings.allocation != "top-n":
        raise ValueError("execution audit compares a fixed top-n account against production")
    by_run = _admit_decisions(dataset, decisions)
    batches = research_signal_batches(dataset, variant)
    account = replay_research_portfolio(batches, sessions, config=settings, official_sessions=official_sessions, synthetic_rows=synthetic_rows)
    reference = account if variant == "production_v5" else replay_research_portfolio(
        research_signal_batches(dataset, "production_v5"), sessions, config=settings,
        official_sessions=official_sessions, synthetic_rows=synthetic_rows,
    )
    market = admit_research_market_data(official_sessions, synthetic_rows)
    records = [record for batch in batches for record in _batch_labels(batch, by_run[int(batch.batch_id)], account, market, sessions)]
    report: dict[str, object] = {
        "schema_version": "market-scan-execution-label-audit-v1", "variant": variant,
        "decisions": [_decision_payload(by_run[batch.run_id]) for batch in dataset.batches],
        "candidate_account": research_portfolio_payload(account), "production_account_digest": reference.result_digest,
        "daily_net_production_comparison": compare_research_accounts(account, reference, seed="execution-label-audit"),
        "records": records, "promotion_eligible": False, "source_provenance": market.provenance_status,
        "decision_provenance": "snapshot_metadata_bound_not_external_time_attestation",
        "summary": {"frozen_candidate_slots": len(records), "entered_slots": sum(record["entered"] is True for record in records),
                    "closed_slots": sum(record["closed"] is True for record in records)},
        "limitations": ["signal-close labels start before the official score is available",
                        "entry attenuation compares the same actual exit date; original D+H labels have a different endpoint",
                        "gross price labels do not establish fills; per-trade returns are not shared-account returns",
                        "daily execution models do not prove order-book fills; unavailable labels remain null",
                        "naive historical timestamps follow the existing explicit Shanghai-time admission convention"],
    }
    report["digest"] = sha256_hex(canonical_json_bytes(report))
    return report


def _admit_decisions(dataset: FrozenResearchDataset, decisions: Sequence[ExecutionAuditDecision]) -> dict[int, ExecutionAuditDecision]:
    by_run = {decision.run_id: decision for decision in decisions}
    if len(by_run) != len(decisions) or set(by_run) != {batch.run_id for batch in dataset.batches}:
        raise ValueError("decisions must cover the exact unique frozen batches")
    for batch in dataset.batches:
        decision = by_run[batch.run_id]
        proof = decision._proof
        if proof is None or proof[0] is not _CAPTURE_SEAL or proof[1] != sha256_hex(canonical_json_bytes(_decision_payload(decision))):
            raise ValueError("decision must be captured from its original snapshot and remain unchanged")
        stamp = datetime.fromisoformat(decision.available_at)
        if decision.source_digest != batch.source_digest or decision.signal_date != batch.signal_date or stamp.tzinfo is None:
            raise ValueError("decision identity does not match the frozen snapshot")
        close = datetime.combine(date.fromisoformat(batch.signal_date), time(15), ASHARE_TIMEZONE)
        opening = datetime.combine(next_trade_dates(date.fromisoformat(batch.signal_date), 1)[0], time(9, 30), ASHARE_TIMEZONE)
        if not close <= stamp < opening:
            raise ValueError("decision time must follow signal close and precede next opening")
    return by_run


def _batch_labels(
    batch: ResearchSignalBatch, decision: ExecutionAuditDecision, account: ResearchPortfolioResult,
    market: ResearchMarketData, sessions: Sequence[str],
) -> list[dict[str, object]]:
    trades: dict[tuple[str, str], ResearchPortfolioTrade] = {
        (trade.symbol, trade.side): trade for trade in account.trades if trade.batch_id == batch.batch_id
    }
    reasons: dict[str | None, set[str]] = {}
    for event in account.events:
        if event.batch_id == batch.batch_id:
            reasons.setdefault(event.symbol, set()).add(event.reason)
    index = sessions.index(batch.signal_date)
    original_end = sessions[index + account.config.horizon] if index + account.config.horizon < len(sessions) else None
    return [_candidate_label(batch, decision, candidate.symbol, candidate.frozen_rank, original_end, trades,
                             sorted(reasons.get(candidate.symbol, set()) | reasons.get(None, set())), market, sessions)
            for candidate in batch.candidates if candidate.frozen_rank <= account.config.top_n]


def _candidate_label(
    batch: ResearchSignalBatch, decision: ExecutionAuditDecision, symbol: str, rank: int, original_end: str | None,
    trades: Mapping[tuple[str, str], ResearchPortfolioTrade], reasons: list[str],
    market: ResearchMarketData, sessions: Sequence[str],
) -> dict[str, object]:
    buy, sell = trades.get((symbol, "buy")), trades.get((symbol, "sell"))
    end = sell.session_date if sell else None
    original = _price_label(market, sessions, symbol, batch.signal_date, original_end, "close")
    aligned = _price_label(market, sessions, symbol, batch.signal_date, end, "close")
    execution = _price_label(market, sessions, symbol, buy.session_date if buy else None, end, "open")
    gap = cast(float, aligned["return"]) - cast(float, execution["return"]) if aligned["return"] is not None and execution["return"] is not None else None
    close = datetime.combine(datetime.fromisoformat(batch.signal_date).date(), time(15), ASHARE_TIMEZONE)
    return {"batch_id": batch.batch_id, "symbol": symbol, "frozen_rank": rank, "available_at": decision.available_at,
            "signal_label_starts_before_available": close < datetime.fromisoformat(decision.available_at),
            "entered": buy is not None, "closed": sell is not None, "entry_date": buy.session_date if buy else None,
            "exit_date": end, "exit_delay_sessions": sell.exit_delay_sessions if sell else None,
            "original_signal_close_label": original, "same_exit_signal_close_label": aligned,
            "same_exit_entry_open_label": execution, "same_exit_entry_attenuation": gap,
            **_closed_trade_costs(buy, sell),
            "account_event_reasons": reasons, "unclosed_reason": None if sell else "not_entered" if buy is None else "position_not_closed"}


def _closed_trade_costs(buy: ResearchPortfolioTrade | None, sell: ResearchPortfolioTrade | None) -> dict[str, float | None]:
    if buy is None or sell is None:
        return {"actual_trade_net_return": None, "actual_trade_fees": None}
    cost = buy.gross_amount + buy.fees
    return {"actual_trade_net_return": (sell.gross_amount - sell.fees - cost) / cost,
            "actual_trade_fees": buy.fees + sell.fees}


def _price_label(
    market: ResearchMarketData, sessions: Sequence[str], symbol: str, start: str | None, end: str | None, price: str,
) -> dict[str, object]:
    value: float | None = None
    reason = "endpoint_unavailable"
    if start is not None and end is not None:
        days = sessions[sessions.index(start):sessions.index(end) + 1]
        rows = [market.rows.get((symbol, day)) for day in days]
        reason = _label_path_reason(rows)
        if not reason:
            first, last = cast(OfficialExecutionSessionRow, rows[0]), cast(OfficialExecutionSessionRow, rows[-1])
            begin = first.bar.close if price == "close" else first.bar.open
            value = cast(float, last.bar.close) / cast(float, begin) - 1
            if not math.isfinite(value):
                value, reason = None, "nonfinite_price_return"
    return {"start_date": start, "end_date": end, "start_price_field": price, "return": value,
            "reason": reason or None, "role": "unadjusted_gross_price_diagnostic_not_fill_evidence"}


def _label_path_reason(rows: Sequence[OfficialExecutionSessionRow | None]) -> str:
    if not rows or any(row is None or row.bar.close is None or row.bar.open is None for row in rows):
        return "price_path_incomplete"
    if any(row is not None and row.corporate_action.status != "none" for row in rows):
        return "corporate_action_ledger_required"
    for previous, current in zip(rows, rows[1:], strict=False):
        if previous is not None and current is not None and not all(
            math.isclose(cast(float, previous.bar.close), value, rel_tol=0, abs_tol=1e-8)
            for value in (current.corporate_action.previous_close, current.corporate_action.reference_price)
        ):
            return "previous_close_reference_conflict"
    return ""
