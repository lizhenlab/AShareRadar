"""Deterministic deltas between immutable published full-market scan cohorts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Literal, Protocol, TypeVar, cast

from pydantic import BaseModel

from app.artifacts.io import canonical_json_text, sha256_hex
from app.models.market_scan import MarketScanRun
from app.models.market_scan_delta import (
    MARKET_SCAN_DELTA_TOP_THRESHOLDS,
    MarketScanDeltaCohort,
    MarketScanDeltaEvidenceChange,
    MarketScanDeltaEvidenceReason,
    MarketScanDeltaEvidenceState,
    MarketScanDeltaExposureChange,
    MarketScanDeltaMembershipItem,
    MarketScanDeltaMembershipReason,
    MarketScanDeltaMovementReason,
    MarketScanDeltaRankScoreChange,
    MarketScanDeltaReasonCount,
    MarketScanDeltaResponse,
    MarketScanDeltaRunRef,
    MarketScanDeltaSummary,
    MarketScanDeltaTopBucket,
    MarketScanDeltaUnavailableReason,
)
from app.repositories.market_scan_delta import MarketScanDeltaRow, MarketScanDeltaSnapshot
from app.services.market_scan_universe import FULL_MARKET_SCOPE


_PUBLISHED = frozenset({"success", "degraded"})
_UNRANKABLE_STATUS_REASONS: Mapping[str, MarketScanDeltaMembershipReason] = {
    "pending": "current_status_pending",
    "missing": "current_status_missing",
    "skipped": "current_status_skipped",
}


class MarketScanDeltaRepositoryProtocol(Protocol):
    def comparison_snapshot(self, run_id: int) -> MarketScanDeltaSnapshot: ...


class MarketScanDeltaService:
    """Compare only stored result rows; never fetch providers or re-score."""

    def __init__(self, repository: MarketScanDeltaRepositoryProtocol) -> None:
        self._repository = repository

    def compare(self, run_id: int) -> MarketScanDeltaResponse:
        if isinstance(run_id, bool) or run_id <= 0:
            raise ValueError("run_id 必须是正整数")
        snapshot = self._repository.comparison_snapshot(run_id)
        unavailable = _unavailable_reason(snapshot)
        if unavailable is not None:
            return _unavailable_response(snapshot.current, unavailable)
        if snapshot.previous is None:  # narrowed by _unavailable_reason at runtime
            raise RuntimeError("市场扫描差异缺少上一批次")
        return _ready_response(snapshot)


def _unavailable_reason(snapshot: MarketScanDeltaSnapshot) -> MarketScanDeltaUnavailableReason | None:
    if snapshot.current.status not in _PUBLISHED:
        return "current_not_published"
    if snapshot.current.scope != FULL_MARKET_SCOPE:
        return "current_not_full_market"
    if snapshot.previous is None:
        return "previous_same_cohort_not_found"
    return None


def _unavailable_response(
    current: MarketScanRun,
    reason: MarketScanDeltaUnavailableReason,
) -> MarketScanDeltaResponse:
    return _sealed_response(
        MarketScanDeltaResponse,
        status="unavailable",
        unavailable_reason=reason,
        current=_run_ref(current),
        cohort=_cohort(current),
        summary=MarketScanDeltaSummary(
            previous_present_count=0,
            current_present_count=current.total_count,
            compared_symbol_count=0,
        ),
    )


def _ready_response(snapshot: MarketScanDeltaSnapshot) -> MarketScanDeltaResponse:
    previous_run = cast(MarketScanRun, snapshot.previous)
    previous = {row.symbol: row for row in snapshot.previous_rows}
    current = {row.symbol: row for row in snapshot.current_rows}
    top_buckets = tuple(
        _top_bucket(top_n, previous, current)
        for top_n in MARKET_SCAN_DELTA_TOP_THRESHOLDS
    )
    evidence_changes = _evidence_changes(previous, current)
    reason_counts = Counter(
        code for item in evidence_changes for code in item.reason_codes
    )
    return _sealed_response(
        MarketScanDeltaResponse,
        status="ready",
        current=_run_ref(snapshot.current),
        previous=_run_ref(previous_run),
        cohort=_cohort(snapshot.current),
        summary=MarketScanDeltaSummary(
            previous_present_count=len(previous),
            current_present_count=len(current),
            compared_symbol_count=len(previous.keys() & current.keys()),
            evidence_change_reason_counts=tuple(
                MarketScanDeltaReasonCount(code=code, count=reason_counts[code])
                for code in sorted(reason_counts)
            ),
        ),
        top_buckets=top_buckets,
        rank_score_changes=_rank_score_changes(previous, current),
        exposure_changes=_exposure_changes(previous, current),
        evidence_changes=evidence_changes,
    )


def _top_bucket(
    top_n: int,
    previous: Mapping[str, MarketScanDeltaRow],
    current: Mapping[str, MarketScanDeltaRow],
) -> MarketScanDeltaTopBucket:
    previous_top = {symbol for symbol, row in previous.items() if _in_top(row, top_n)}
    current_top = {symbol for symbol, row in current.items() if _in_top(row, top_n)}
    entrants = tuple(
        _membership_item(current[symbol], previous.get(symbol), _entrant_reasons(previous.get(symbol)))
        for symbol in _ordered_symbols(current_top - previous_top, current)
    )
    exit_symbols = previous_top - current_top
    unrankable_symbols = {
        symbol
        for symbol in exit_symbols
        if symbol in current and not _rankable(current[symbol])
    }
    ranked_exit_symbols = exit_symbols - unrankable_symbols
    exits = tuple(
        _membership_item(previous[symbol], current.get(symbol), _exit_reasons(current.get(symbol)))
        for symbol in _ordered_symbols(ranked_exit_symbols, previous)
    )
    unrankable = tuple(
        _membership_item(current[symbol], previous[symbol], _unrankable_reasons(current[symbol]))
        for symbol in _ordered_symbols(unrankable_symbols, previous)
    )
    return MarketScanDeltaTopBucket(
        top_n=cast(Literal[20, 50, 100], top_n),
        previous_count=len(previous_top),
        current_count=len(current_top),
        retained_count=len(previous_top & current_top),
        entrants=entrants,
        exits=exits,
        present_but_unrankable=unrankable,
    )


def _entrant_reasons(previous: MarketScanDeltaRow | None) -> tuple[MarketScanDeltaMembershipReason, ...]:
    if previous is None:
        return ("instrument_new_in_current_universe", "crossed_into_top_n")
    if not _rankable(previous):
        return ("became_rankable", "crossed_into_top_n")
    return ("crossed_into_top_n",)


def _exit_reasons(current: MarketScanDeltaRow | None) -> tuple[MarketScanDeltaMembershipReason, ...]:
    if current is None:
        return ("instrument_absent_from_current_universe", "crossed_out_of_top_n")
    return ("crossed_out_of_top_n",)


def _unrankable_reasons(row: MarketScanDeltaRow) -> tuple[MarketScanDeltaMembershipReason, ...]:
    status_reason = _UNRANKABLE_STATUS_REASONS.get(row.status)
    return (
        "present_but_unrankable",
        status_reason if status_reason is not None else "current_rank_missing",
    )


def _membership_item(
    primary: MarketScanDeltaRow,
    comparison: MarketScanDeltaRow | None,
    reasons: tuple[MarketScanDeltaMembershipReason, ...],
) -> MarketScanDeltaMembershipItem:
    current_row: MarketScanDeltaRow | None
    previous_row: MarketScanDeltaRow | None
    # Explicit keyword construction avoids ambiguous tuple orientation in callers.
    if reasons[0] in {"instrument_new_in_current_universe", "became_rankable", "crossed_into_top_n", "present_but_unrankable"}:
        current_row, previous_row = primary, comparison
    else:
        previous_row, current_row = primary, comparison
    return MarketScanDeltaMembershipItem(
        symbol=primary.symbol,
        name=primary.name,
        market=primary.market,
        industry=primary.industry,
        previous_rank=previous_row.rank if previous_row is not None else None,
        current_rank=current_row.rank if current_row is not None else None,
        previous_raw_score=previous_row.raw_score if previous_row is not None else None,
        current_raw_score=current_row.raw_score if current_row is not None else None,
        reason_codes=reasons,
    )


def _rank_score_changes(
    previous: Mapping[str, MarketScanDeltaRow],
    current: Mapping[str, MarketScanDeltaRow],
) -> tuple[MarketScanDeltaRankScoreChange, ...]:
    changes: list[MarketScanDeltaRankScoreChange] = []
    for symbol in previous.keys() & current.keys():
        before, after = previous[symbol], current[symbol]
        if not _rankable(before) or not _rankable(after):
            continue
        rank_change = cast(int, before.rank) - cast(int, after.rank)
        score_change = _score_change(before.raw_score, after.raw_score)
        if rank_change == 0 and (score_change is None or score_change == 0):
            continue
        rank_reason: MarketScanDeltaMovementReason = (
            "rank_improved" if rank_change > 0 else "rank_declined" if rank_change < 0 else "rank_unchanged"
        )
        score_reason: MarketScanDeltaMovementReason = (
            "score_increased"
            if score_change is not None and score_change > 0
            else "score_decreased"
            if score_change is not None and score_change < 0
            else "score_unchanged"
        )
        changes.append(
            MarketScanDeltaRankScoreChange(
                symbol=symbol,
                name=after.name,
                market=after.market,
                industry=after.industry,
                previous_rank=cast(int, before.rank),
                current_rank=cast(int, after.rank),
                rank_change=rank_change,
                previous_raw_score=before.raw_score,
                current_raw_score=after.raw_score,
                raw_score_change=score_change,
                reason_codes=(rank_reason, score_reason),
            )
        )
    return tuple(sorted(changes, key=lambda item: (-abs(item.rank_change), item.current_rank, item.symbol)))


def _exposure_changes(
    previous: Mapping[str, MarketScanDeltaRow],
    current: Mapping[str, MarketScanDeltaRow],
) -> tuple[MarketScanDeltaExposureChange, ...]:
    changes: list[MarketScanDeltaExposureChange] = []
    for top_n in MARKET_SCAN_DELTA_TOP_THRESHOLDS:
        previous_rows = tuple(row for row in previous.values() if _in_top(row, top_n))
        current_rows = tuple(row for row in current.values() if _in_top(row, top_n))
        for dimension in ("market", "industry"):
            before_counts = Counter(_exposure_key(row, dimension) for row in previous_rows)
            after_counts = Counter(_exposure_key(row, dimension) for row in current_rows)
            for category in sorted(before_counts.keys() | after_counts.keys()):
                before_count = before_counts[category]
                after_count = after_counts[category]
                before_share = before_count / len(previous_rows) if previous_rows else 0.0
                after_share = after_count / len(current_rows) if current_rows else 0.0
                if before_count == after_count and before_share == after_share:
                    continue
                changes.append(
                    MarketScanDeltaExposureChange(
                        top_n=top_n,
                        dimension=cast(Literal["market", "industry"], dimension),
                        category=category,
                        previous_count=before_count,
                        current_count=after_count,
                        count_change=after_count - before_count,
                        previous_share=before_share,
                        current_share=after_share,
                        share_change=after_share - before_share,
                    )
                )
    return tuple(changes)


def _evidence_changes(
    previous: Mapping[str, MarketScanDeltaRow],
    current: Mapping[str, MarketScanDeltaRow],
) -> tuple[MarketScanDeltaEvidenceChange, ...]:
    candidates = {
        symbol
        for symbol, row in previous.items()
        if _in_top(row, 100)
    } | {
        symbol
        for symbol, row in current.items()
        if _in_top(row, 100)
    }
    changes: list[MarketScanDeltaEvidenceChange] = []
    for symbol in sorted(candidates):
        before, after = previous.get(symbol), current.get(symbol)
        if before is None or after is None:
            continue
        reasons = _evidence_reason_codes(before, after)
        if reasons:
            changes.append(
                MarketScanDeltaEvidenceChange(
                    symbol=symbol,
                    reason_codes=reasons,
                    previous=_evidence_state(before),
                    current=_evidence_state(after),
                )
            )
    return tuple(changes)


def _evidence_reason_codes(
    before: MarketScanDeltaRow,
    after: MarketScanDeltaRow,
) -> tuple[MarketScanDeltaEvidenceReason, ...]:
    reasons: list[MarketScanDeltaEvidenceReason] = []
    checks: tuple[tuple[bool, MarketScanDeltaEvidenceReason], ...] = (
        (before.status != after.status, "status_changed"),
        (before.quote_source != after.quote_source, "quote_source_changed"),
        (before.kline_source != after.kline_source, "kline_source_changed"),
        (before.metadata_source != after.metadata_source, "metadata_source_changed"),
        (before.quote_fallback_used != after.quote_fallback_used, "quote_fallback_changed"),
        (before.kline_fallback_used != after.kline_fallback_used, "kline_fallback_changed"),
        (before.metadata_degraded != after.metadata_degraded, "metadata_degradation_changed"),
        (before.degradation_reasons != after.degradation_reasons, "degradation_reasons_changed"),
    )
    for changed, code in checks:
        if changed:
            reasons.append(code)
    return tuple(reasons)


def _evidence_state(row: MarketScanDeltaRow) -> MarketScanDeltaEvidenceState:
    return MarketScanDeltaEvidenceState(
        status=row.status,
        quote_source=row.quote_source,
        kline_source=row.kline_source,
        metadata_source=row.metadata_source,
        quote_fallback_used=row.quote_fallback_used,
        kline_fallback_used=row.kline_fallback_used,
        metadata_degraded=row.metadata_degraded,
        degradation_reasons=row.degradation_reasons,
    )


def _rankable(row: MarketScanDeltaRow) -> bool:
    return row.status == "success" and row.rank is not None


def _in_top(row: MarketScanDeltaRow, top_n: int) -> bool:
    return _rankable(row) and cast(int, row.rank) <= top_n


def _ordered_symbols(symbols: set[str], rows: Mapping[str, MarketScanDeltaRow]) -> tuple[str, ...]:
    return tuple(sorted(symbols, key=lambda symbol: (rows[symbol].rank or 10**9, symbol)))


def _score_change(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    return after - before


def _exposure_key(row: MarketScanDeltaRow, dimension: str) -> str:
    if dimension == "market":
        return row.market
    return row.industry.strip() if row.industry and row.industry.strip() else "未知行业"


def _run_ref(run: MarketScanRun) -> MarketScanDeltaRunRef:
    return MarketScanDeltaRunRef(
        run_id=run.id,
        status=run.status,
        mode=run.mode,
        scope=run.scope,
        rule_version=run.rule_version,
        data_date=run.data_date,
        finished_at=run.finished_at,
        snapshot_digest=run.snapshot_digest,
        snapshot_seal_origin=run.snapshot_seal_origin,
        snapshot_sealed_at=run.snapshot_sealed_at,
    )


def _cohort(run: MarketScanRun) -> MarketScanDeltaCohort:
    return MarketScanDeltaCohort(mode=run.mode, scope=run.scope, rule_version=run.rule_version)


_DeltaResponse = TypeVar("_DeltaResponse", bound=BaseModel)


def _sealed_response(
    response_type: type[_DeltaResponse],
    **values: object,
) -> _DeltaResponse:
    draft = response_type.model_construct(canonical_digest="0" * 64, **values)
    payload = draft.model_dump(mode="json", exclude={"canonical_digest"})
    payload["canonical_digest"] = sha256_hex(canonical_json_text(payload))
    return response_type.model_validate(payload)


__all__ = ["MarketScanDeltaRepositoryProtocol", "MarketScanDeltaService"]
