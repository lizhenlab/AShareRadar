"""Read-only market-scan queries and research projections."""

from __future__ import annotations

import math
from typing import Literal

from app.models.market_scan import (
    MarketScanFilterValues,
    MarketScanMode,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanRunStatus,
    MarketScanSortOrderValues,
    MarketScanSortValues,
)
from app.services.market_scan_contracts import MarketScanCacheProtocol
from app.services.market_scan_export import PUBLISHED_MARKET_SCAN_STATUSES
from app.services.market_scan_future_range_store import (
    FutureRangeResearchUnavailable,
    not_generated_future_range_research,
)
from app.services.market_scan_probability_research import PROBABILITY_PRIMARY_TARGET
from app.services.market_scan_probability import probability_selection_qualified
from app.services.market_scan_probability_store import (
    ProbabilityFilterUnavailable,
    not_generated_probability_research,
)
from app.services.market_scan_research_stores import MarketScanResearchStores
from app.services.market_scan_universe import FULL_MARKET_SCOPE


_RESULT_QUERY_FIELDS = (
    "page", "page_size", "status", "market", "industry", "is_st", "is_new",
    "min_score", "max_score", "min_trend_score", "max_trend_score",
    "min_change_pct", "max_change_pct", "min_turnover_rate", "max_turnover_rate",
    "min_amount", "max_amount", "min_data_quality_score", "max_data_quality_score",
    "min_confidence", "max_risk", "min_tradability", "keyword", "symbols", "sort", "order",
)


class MarketScanQueryService:
    """Side-effect-free read model for persisted scan runs and artifacts."""

    def __init__(self, cache: MarketScanCacheProtocol, stores: MarketScanResearchStores) -> None:
        self._cache = cache
        self._stores = stores

    def run(self, run_id: int) -> MarketScanRun:
        return self._cache.market_scan_run(run_id)

    def latest_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        return self._cache.latest_market_scan_run(mode=mode)

    def latest_published_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        return self._cache.latest_published_market_scan_run(mode=mode)

    def runs(
        self,
        *,
        page: int,
        page_size: int,
        mode: MarketScanMode | None = None,
        status: MarketScanRunStatus | Literal["published"] | None = None,
        data_date: str | None = None,
    ) -> MarketScanRunPage:
        return self._cache.market_scan_runs(
            page=page,
            page_size=page_size,
            mode=mode,
            status=status,
            data_date=data_date,
        )

    def results(
        self,
        run_id: int,
        *,
        page: int,
        page_size: int,
        status: MarketScanResultStatus | None,
        market: MarketScanFilterValues,
        industry: MarketScanFilterValues,
        is_st: bool | None,
        is_new: bool | None,
        min_score: int | None = None,
        max_score: int | None = None,
        min_trend_score: int | None = None,
        max_trend_score: int | None = None,
        min_change_pct: float | None = None,
        max_change_pct: float | None = None,
        min_turnover_rate: float | None = None,
        max_turnover_rate: float | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        min_data_quality_score: int | None,
        max_data_quality_score: int | None = None,
        min_confidence: float | None = None,
        max_risk: float | None = None,
        min_tradability: float | None = None,
        keyword: str | None,
        sort: MarketScanSortValues,
        order: MarketScanSortOrderValues,
        probability_horizon: Literal[1, 5, 20] = 5,
        min_upside_probability: float | None = None,
    ) -> MarketScanResultPage:
        research = self.probability_research(run_id)
        all_probabilities = (
            self.probability_projection(run_id)[1]
            if min_upside_probability is not None
            else {}
        )
        symbols = _probability_filter_symbols(
            research,
            all_probabilities,
            horizon=probability_horizon,
            minimum=min_upside_probability,
        )
        values = locals()
        query = {name: values[name] for name in _RESULT_QUERY_FIELDS}
        page_result = self._cache.market_scan_results(run_id, **query)
        page_symbols = tuple(item.symbol for item in page_result.items)
        _summary, probabilities = self.probability_projection(run_id, symbols=page_symbols)
        return _attach_probability_projection(page_result, research, probabilities)

    def probability_research(self, run_id: int) -> dict[str, object]:
        store = self._stores.probability
        research = (
            store.research_projection(run_id)
            if store is not None
            else not_generated_probability_research(run_id)
        )
        source = self._stores.probability_source
        if research.get("status") == "not_generated" and source is not None:
            return source.research_projection(run_id)
        return research

    def probability_projection(
        self,
        run_id: int,
        *,
        symbols: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        store = self._stores.probability
        if store is None:
            return self.probability_research(run_id), {}
        research, probabilities = store.run_projection(run_id, symbols=symbols)
        source = self._stores.probability_source
        if research.get("status") == "not_generated" and source is not None:
            research = source.research_projection(run_id)
        return research, probabilities

    def future_range_research(
        self,
        run_id: int,
        *,
        page: int,
        page_size: int,
        session_offset: Literal[1, 2, 3] | None,
        symbol: str | None,
        include_research: bool,
    ) -> dict[str, object]:
        _require_future_range_eligible_run(self.run(run_id))
        store = self._stores.future_range
        if store is None:
            return not_generated_future_range_research(run_id)
        return store.research_projection(
            run_id,
            page=page,
            page_size=page_size,
            session_offset=session_offset,
            symbol=symbol,
            include_research=include_research,
        )


def _probability_filter_symbols(
    research: dict[str, object],
    probabilities: dict[str, dict[str, object]],
    *,
    horizon: Literal[1, 5, 20],
    minimum: float | None,
) -> tuple[str, ...] | None:
    if minimum is None:
        return None
    if not math.isfinite(minimum) or not 0 <= minimum <= 1:
        raise ValueError("最低上涨概率必须在 0 到 1 之间")
    summary = _probability_summary(research, horizon)
    if summary.get("status") != "calibrated_shadow":
        raise ProbabilityFilterUnavailable("当前批次与周期尚无已校准 Shadow 概率，不能使用概率筛选")
    if not probability_selection_qualified(summary):
        raise ProbabilityFilterUnavailable(
            "当前批次虽已拟合，但尚未通过样本外效力门禁，不能使用概率筛选"
        )
    return tuple(
        symbol
        for symbol, horizons in probabilities.items()
        if _meets_probability_minimum(horizons, horizon, minimum)
    )


def _require_future_range_eligible_run(run: MarketScanRun) -> None:
    if run.mode != "official":
        raise FutureRangeResearchUnavailable("未来区间研究仅支持盘后正式批次")
    if run.scope != FULL_MARKET_SCOPE:
        raise FutureRangeResearchUnavailable("未来区间研究仅支持盘后正式全市场批次")
    if run.status not in PUBLISHED_MARKET_SCAN_STATUSES:
        raise FutureRangeResearchUnavailable("未来区间研究仅支持已发布批次")


def _probability_summary(research: dict[str, object], horizon: int) -> dict[str, object]:
    horizons = research.get("horizons")
    targets = horizons.get(str(horizon)) if isinstance(horizons, dict) else None
    summary = targets.get(PROBABILITY_PRIMARY_TARGET) if isinstance(targets, dict) else None
    return summary if isinstance(summary, dict) else {}


def _meets_probability_minimum(
    horizons: dict[str, object],
    horizon: int,
    minimum: float,
) -> bool:
    targets = horizons.get(str(horizon))
    record = targets.get(PROBABILITY_PRIMARY_TARGET) if isinstance(targets, dict) else None
    probability = record.get("probability") if isinstance(record, dict) else None
    return (
        isinstance(record, dict)
        and record.get("status") == "calibrated_shadow"
        and isinstance(probability, int | float)
        and not isinstance(probability, bool)
        and math.isfinite(float(probability))
        and float(probability) >= minimum
    )


def _attach_probability_projection(
    page: MarketScanResultPage,
    research: dict[str, object],
    probabilities: dict[str, dict[str, object]],
) -> MarketScanResultPage:
    items = [
        item.model_copy(update={"upside_probabilities": probabilities.get(item.symbol, {})})
        for item in page.items
    ]
    return page.model_copy(update={"items": items, "probability_research": research})


__all__ = ["MarketScanQueryService"]
