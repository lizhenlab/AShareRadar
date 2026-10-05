"""Compare selected persisted rows inside one verified SQLite read snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict
import math
from typing import Protocol

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE, MarketScanResultItem, MarketScanRun
from app.models.market_scan_comparison import (
    MarketScanComparisonItem,
    MarketScanComparisonRequest,
    MarketScanComparisonResponse,
)
from app.models.market_scan_screening import MarketScanScreenEvidence
from app.models.market_scan_snapshot import MarketScanSnapshotIntegrityError, validate_market_scan_run_binding
from app.services.market_scan_contracts import MarketScanVerifiedReadProtocol
from app.services.market_scan_export import MarketScanExportFilters, market_scan_board_label


class MarketScanComparisonUnavailable(ValueError):
    """The requested frozen selection cannot be compared."""


class MarketScanComparisonConflict(ValueError):
    """The user's displayed snapshot differs from the verified publication."""


class MarketScanComparisonRepositoryProtocol(Protocol):
    def verified_market_scan_read(self, run_id: int) -> AbstractContextManager[MarketScanVerifiedReadProtocol]: ...


class MarketScanComparisonService:
    def __init__(self, repository: MarketScanComparisonRepositoryProtocol) -> None:
        self._repository = repository

    def compare(self, run_id: int, request: MarketScanComparisonRequest) -> MarketScanComparisonResponse:
        with self._repository.verified_market_scan_read(run_id) as verified:
            run = verified.run
            _require_comparable_run(run)
            if verified.snapshot_digest != request.expected_snapshot_digest:
                raise MarketScanComparisonConflict("榜单快照已变化，请刷新当前结果后重新选择对比股票")
            query = _comparison_query(request.symbols)
            page = verified.results_page(**query)
            validate_market_scan_run_binding(run, page.run)
            items = _selected_items(page.items, request.symbols, run_id=run.id, total=page.total)
            return _comparison_response(run, request, items, action_eligible=verified.action_source_digest is not None)


def _require_comparable_run(run: MarketScanRun) -> None:
    if run.status not in {"success", "degraded"} or run.scope != MARKET_SCAN_FULL_MARKET_SCOPE:
        raise MarketScanComparisonUnavailable("候选对比只支持已发布的完整全市场批次")
    if run.snapshot_digest is None or run.snapshot_seal_origin is None or run.snapshot_sealed_at is None:
        raise MarketScanComparisonUnavailable("候选对比缺少完整冻结快照证据")


def _comparison_query(symbols: list[str]) -> dict[str, object]:
    query: dict[str, object] = asdict(MarketScanExportFilters(status=None, sort="symbol"))
    query.pop("probability_horizon")
    query.pop("min_upside_probability")
    query.update(symbols=tuple(symbols), page=1, page_size=len(symbols))
    return query


def _selected_items(
    items: list[MarketScanResultItem], symbols: list[str], *, run_id: int, total: int,
) -> list[MarketScanComparisonItem]:
    by_symbol = {item.symbol: item for item in items}
    if len(by_symbol) != len(items) or any(item.run_id != run_id for item in items) or set(by_symbol) - set(symbols):
        raise MarketScanSnapshotIntegrityError("对比结果与请求的冻结股票集合不一致")
    if total != len(items):
        raise MarketScanSnapshotIntegrityError("对比结果数量与同快照分页不一致")
    if set(by_symbol) != set(symbols):
        raise MarketScanComparisonUnavailable("所选股票不完全属于当前冻结批次，请重新选择")
    return [_comparison_item(by_symbol[symbol]) for symbol in symbols]


def _comparison_item(item: MarketScanResultItem) -> MarketScanComparisonItem:
    derived = {"board", "confidence", "risk", "tradability"}
    values = {field: getattr(item, field) for field in MarketScanComparisonItem.model_fields if field not in derived}
    values["board"] = market_scan_board_label(item.code, item.market)
    values.update({field: _research_dimension(item.score_details, field) for field in ("confidence", "risk", "tradability")})
    return MarketScanComparisonItem.model_validate(values)


def _research_dimension(details: Mapping[str, object], field: str) -> float | None:
    value: object = details
    for key in ("components", "score_dimensions", "scores", field):
        value = value.get(key) if isinstance(value, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def _comparison_response(
    run: MarketScanRun, request: MarketScanComparisonRequest, items: list[MarketScanComparisonItem], *, action_eligible: bool,
) -> MarketScanComparisonResponse:
    values = {field: getattr(run, field) for field in MarketScanScreenEvidence.model_fields if field != "run_id"}
    evidence = MarketScanScreenEvidence.model_validate({"run_id": run.id, **values})
    draft = MarketScanComparisonResponse.model_construct(
        evidence=evidence, requested_symbols=list(request.symbols), items=items,
        action_source_eligible=action_eligible, canonical_digest="0" * 64,
    )
    payload = draft.model_dump(mode="json", exclude={"canonical_digest"})
    payload["canonical_digest"] = sha256_hex(canonical_json_bytes(payload))
    return MarketScanComparisonResponse.model_validate(payload)


__all__ = ["MarketScanComparisonConflict", "MarketScanComparisonService", "MarketScanComparisonUnavailable"]
