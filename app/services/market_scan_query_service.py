"""Read-only market-scan queries and research projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from functools import cmp_to_key
import math
from pathlib import Path
from typing import Literal, cast

from app.models.market_scan import (
    MarketScanFilterValues,
    MarketScanMode,
    MarketScanProductionScoreContract,
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanRunStatus,
    MarketScanSortOrderValues,
    MarketScanSortValues,
)
from app.models.market_scan_screening import (
    MarketBreadthV1,
    MarketScanScreenEvaluateRequest,
    MarketScanScreenEvaluationV1,
)
from app.models.market_scan_polling import MarketScanPollingIdentity
from app.models.market_scan_snapshot import (
    MarketScanSnapshotIntegrityError,
    validate_market_scan_cohort_binding,
    validate_market_scan_run_binding,
)
from app.services.market_scan_contracts import (
    MarketScanCacheProtocol,
    MarketScanVerifiedReadProtocol,
)
from app.services.market_scan_export import (
    PUBLISHED_MARKET_SCAN_STATUSES,
    MarketScanExportFilters,
    validate_market_scan_export_run,
)
from app.services.market_scan_future_range_store import (
    FutureRangeResearchUnavailable,
    not_generated_future_range_research,
)
from app.services.market_scan_future_range_artifact import FutureRangeArtifactError
from app.services.market_scan_probability_research import PROBABILITY_PRIMARY_TARGET
from app.services.market_scan_probability import probability_filter_qualified
from app.services.market_scan_official_execution_store import OfficialExecutionStoreStatus
from app.services.market_scan_probability_artifact import ProbabilityArtifactError
from app.services.market_scan_probability_ranking import (
    PROBABILITY_RANKING_SCORE_RULE_VERSION,
    probability_ranking_score_spec_hash,
)
from app.services.market_scan_scoring import FULL_MARKET_SCORE_RULE_VERSION
from app.services.market_scan_probability_historical_context import (
    HistoricalProbabilityContextError,
    not_generated_historical_probability_context,
    unavailable_historical_probability_context,
)
from app.services.market_scan_probability_store import (
    ProbabilityFilterUnavailable,
    ProbabilityResearchUnavailable,
    not_generated_probability_research,
)
from app.services.market_scan_research_stores import MarketScanResearchStores
from app.services.market_scan_screening import MarketScanScreeningService
from app.services.market_scan_universe import FULL_MARKET_SCOPE


_RESULT_QUERY_FIELDS = (
    "page",
    "page_size",
    "status",
    "market",
    "industry",
    "is_st",
    "is_new",
    "min_score",
    "max_score",
    "min_trend_score",
    "max_trend_score",
    "min_change_pct",
    "max_change_pct",
    "min_turnover_rate",
    "max_turnover_rate",
    "min_amount",
    "max_amount",
    "min_data_quality_score",
    "max_data_quality_score",
    "min_confidence",
    "max_risk",
    "min_tradability",
    "keyword",
    "symbols",
    "sort",
    "order",
)
_JOINT_MAINTENANCE_UNAVAILABLE = frozenset({"maintenance_pending", "maintenance_failed"})


class MarketScanQueryService:
    """Side-effect-free read model for persisted scan runs and artifacts."""

    def __init__(self, cache: MarketScanCacheProtocol, stores: MarketScanResearchStores) -> None:
        self._cache = cache
        self._stores = stores
        self._screening = MarketScanScreeningService(cache)

    def run(self, run_id: int) -> MarketScanRun:
        return self._cache.market_scan_run(run_id)

    def latest_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        return self._cache.latest_market_scan_run(mode=mode)

    def polling_identity(self, *, mode: MarketScanMode) -> MarketScanPollingIdentity:
        """Return non-authorizing change tokens for browser idle polling."""
        return self._cache.market_scan_polling_identity(mode=mode)

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

    def run_identities(
        self,
        *,
        page: int,
        page_size: int,
        mode: MarketScanMode | None = None,
        status: MarketScanRunStatus | Literal["published"] | None = None,
        data_date: str | None = None,
    ) -> MarketScanRunPage:
        """Return non-authorizing identities for history navigation only."""
        return self._cache.market_scan_run_identities(
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
        _validate_probability_minimum(min_upside_probability)
        values = locals()
        query = {name: values[name] for name in _RESULT_QUERY_FIELDS if name != "symbols"}
        with self._cache.verified_market_scan_read(run_id) as verified:
            return self._results_from_verified(
                verified,
                query=query,
                probability_horizon=probability_horizon,
                minimum=min_upside_probability,
            )

    def export_projection(
        self,
        run_id: int,
        *,
        filters: MarketScanExportFilters,
    ) -> tuple[MarketScanResultPage, dict[str, object]]:
        """Read one complete export from exactly one verified DB snapshot."""
        normalized = filters.normalized()
        _validate_probability_minimum(normalized.min_upside_probability)
        with self._cache.verified_market_scan_read(run_id) as verified:
            run = verified.run
            validate_market_scan_export_run(run)
            page = self._results_from_verified(
                verified,
                query=_export_result_query(normalized, total_count=run.total_count),
                probability_horizon=normalized.probability_horizon,
                minimum=normalized.min_upside_probability,
            )
        return page, self._future_range_export_for_verified_run(run)

    def _results_from_verified(
        self,
        verified: MarketScanVerifiedReadProtocol,
        *,
        query: dict[str, object],
        probability_horizon: Literal[1, 5, 20],
        minimum: float | None,
    ) -> MarketScanResultPage:
        run = verified.run
        eligible = _probability_run_eligible(run)
        research, all_probabilities, symbols = self._result_probability_filter(
            verified, eligible=eligible, horizon=probability_horizon, minimum=minimum,
        )
        ranking_context, ranking_records = self._production_ranking_projection(run.id)
        if minimum is not None and ranking_context.get("reason") in _JOINT_MAINTENANCE_UNAVAILABLE:
            raise ProbabilityFilterUnavailable("正式概率证据维护尚未完成，暂不能使用概率筛选")
        base_page = None
        if ranking_context.get("status") == "active":
            base_page, page_result = _production_ranking_pages(
                verified,
                query=query,
                symbols=symbols,
                context=ranking_context,
                records=ranking_records,
            )
        else:
            page_result = verified.results_page(**query, symbols=symbols)
            _validate_result_page_binding(run, page_result)
        page_symbols = tuple(item.symbol for item in page_result.items)
        if eligible and minimum is None:
            research, probabilities = self._probability_projection_for_verified(
                verified,
                symbols=page_symbols,
            )
        else:
            probabilities = {symbol: all_probabilities[symbol] for symbol in page_symbols if symbol in all_probabilities}
        if _maintenance_projection_unavailable(research) and base_page is not None:
            ranking_context = _inactive_ranking_context(run.id, str(research["availability"]))
            # The retained base rows already have the SQL filters and base ordering.
            page_result = _ranked_result_page(
                base_page, _score_filtered_items(base_page.items, query), ranking_context,
                page=page_result.page, page_size=page_result.page_size,
            )
        return _attach_probability_projection(
            page_result,
            research,
            probabilities,
            production_ranking=ranking_context,
        )

    def _result_probability_filter(
        self, verified: MarketScanVerifiedReadProtocol, *, eligible: bool,
        horizon: Literal[1, 5, 20], minimum: float | None,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]], tuple[str, ...] | None]:
        if minimum is not None and not eligible:
            raise ProbabilityFilterUnavailable("上涨概率筛选仅支持已发布的盘后正式全市场批次")
        research = _unavailable_probability_research(verified.run)
        if not eligible:
            research = self._attach_historical_probability_context(research)
        probabilities: dict[str, dict[str, object]] = {}
        if eligible and minimum is not None:
            research, probabilities = self._probability_projection_for_verified(verified)
        symbols = _probability_filter_symbols(
            research, probabilities, horizon=horizon, minimum=minimum,
            joint_filter_qualified=self._joint_filter_qualified(verified.run.id, research),
        )
        return research, probabilities, symbols

    def breadth(self, run_id: int) -> MarketBreadthV1:
        return self._screening.breadth(run_id)

    def evaluate_screen(
        self,
        run_id: int,
        request: MarketScanScreenEvaluateRequest,
    ) -> MarketScanScreenEvaluationV1:
        return self._screening.evaluate(run_id, request)

    def probability_research(self, run_id: int) -> dict[str, object]:
        with self._cache.verified_market_scan_read(run_id) as verified:
            _require_probability_eligible_run(verified.run)
            return self._probability_research_for_verified(verified)

    def experimental_probability_results(
        self, run_id: int, *, minimum: float | None = None, market: str | None = None,
        keyword: str = "", sort: Literal["probability", "base_rank"] = "probability", page: int = 1, page_size: int = 50,
        prediction_kind: Literal["net_h5", "close_d1", "close_d2", "close_d5"] = "net_h5",
    ) -> dict[str, object]:
        from app.services.experimental_probability_process import isolated_experimental_results

        return isolated_experimental_results(
            Path(self._cache.path), run_id, prediction_kind=prediction_kind, minimum=minimum, market=market,
            keyword=keyword, sort=sort, page=page, page_size=page_size,
        )

    def _probability_research_for_verified(
        self,
        verified: MarketScanVerifiedReadProtocol,
    ) -> dict[str, object]:
        run = verified.run
        capture, gated = _probability_capture_gate(verified)
        if gated is not None:
            return self._attach_historical_probability_context(gated)
        joint_projection = self._joint_probability_projection(run.id, symbols=())
        if joint_projection is not None:
            research, _records = joint_projection
            return self._finalize_probability_projection(verified, research, {})[0]
        store = self._stores.probability
        research = store.research_projection(run.id) if store is not None else not_generated_probability_research(run.id)
        research = self._resolve_probability_source_research(
            run,
            research,
            capture=capture,
        )
        return self._finalize_probability_projection(verified, research, {})[0]

    def probability_projection(
        self,
        run_id: int,
        *,
        symbols: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        with self._cache.verified_market_scan_read(run_id) as verified:
            _require_probability_eligible_run(verified.run)
            return self._probability_projection_for_verified(
                verified,
                symbols=symbols,
            )

    def _probability_projection_for_verified(
        self,
        verified: MarketScanVerifiedReadProtocol,
        *,
        symbols: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        run = verified.run
        capture, gated = _probability_capture_gate(verified)
        if gated is not None:
            return self._attach_historical_probability_context(gated), {}
        joint_projection = self._joint_probability_projection(run.id, symbols=symbols)
        if joint_projection is not None:
            return self._finalize_probability_projection(verified, *joint_projection)
        store = self._stores.probability
        if store is None:
            research = not_generated_probability_research(run.id)
            probabilities: dict[str, dict[str, object]] = {}
        else:
            research, probabilities = store.run_projection(run.id, symbols=symbols)
        research = self._resolve_probability_source_research(
            run,
            research,
            capture=capture,
        )
        if research.get("availability") is not None:
            probabilities = {}
        return self._finalize_probability_projection(verified, research, probabilities)

    def _joint_probability_projection(
        self, run_id: int, *, symbols: tuple[str, ...] | None
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]] | None:
        joint = self._stores.joint_probability
        projection = getattr(joint, "run_projection", None)
        if not callable(projection):
            return None
        research, probabilities = projection(run_id, symbols=symbols)
        # A false has_current_projection() cannot distinguish rebuilding evidence
        # from an unavailable model; rebuilding must never authorize a legacy fallback.
        if research.get("status") != "not_generated" or _maintenance_projection_unavailable(research):
            return research, probabilities
        return None

    def _finalize_probability_projection(
        self, verified: MarketScanVerifiedReadProtocol, research: dict[str, object],
        probabilities: dict[str, dict[str, object]],
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        validated = _validate_probability_run_binding(
            verified.run,
            research,
            score_contract=self._score_contract(verified, research),
        )
        output = self._attach_historical_probability_context(validated)
        return output, {} if _maintenance_projection_unavailable(output) else probabilities

    def _attach_historical_probability_context(
        self,
        research: dict[str, object],
    ) -> dict[str, object]:
        joint_context = research.get("joint_execution_evidence")
        if not isinstance(joint_context, Mapping) or joint_context.get("status") not in _JOINT_MAINTENANCE_UNAVAILABLE:
            joint = self._stores.joint_probability
            joint_context = joint.status_projection() if joint is not None else {
                "contract_version": "market-scan-joint-execution-maintenance-v1",
                "status": "store_unavailable",
                "filter_ready": False,
                "blockers": ["joint_execution_store_unavailable"],
            }
        if joint_context.get("status") in _JOINT_MAINTENANCE_UNAVAILABLE:
            return _maintenance_probability_context(research, joint_context)
        output = deepcopy(research)
        store = self._stores.historical_probability
        if store is None:
            context = not_generated_historical_probability_context()
        else:
            try:
                context = store.research_projection()
            except HistoricalProbabilityContextError:
                context = unavailable_historical_probability_context()
        output["historical_context"] = context
        official_store = self._stores.official_execution
        output["official_execution_evidence"] = (
            official_store.status().payload()
            if official_store is not None
            else {
                "contract_version": "official-execution-store-status-v1",
                "configured": False,
                "status": "store_unavailable",
                "registry_digest": None,
                "verified_session_count": 0,
                "first_session_date": None,
                "latest_session_date": None,
                "failures": ["official_execution_store_unavailable"],
                "formal_evidence_available": False,
                "public_vendor_auto_upgrade_forbidden": True,
            }
        )
        output["joint_execution_evidence"] = deepcopy(dict(joint_context))
        return output

    def _joint_filter_qualified(
        self,
        run_id: int,
        research: Mapping[str, object],
    ) -> bool:
        if research.get("authority_backend") != "joint_execution_opaque_v1":
            return False
        joint = self._stores.joint_probability
        return joint is not None and joint.filter_qualified(run_id)

    def _production_ranking_projection(
        self,
        run_id: int,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        joint = self._stores.joint_probability
        if joint is None:
            return _inactive_ranking_context(run_id), {}
        projection = getattr(joint, "production_ranking_projection", None)
        if not callable(projection):
            return _inactive_ranking_context(run_id, "ranking_projection_unavailable"), {}
        return projection(run_id)

    def _resolve_probability_source_research(
        self,
        run: MarketScanRun,
        research: dict[str, object],
        *,
        capture: Mapping[str, object] | None,
    ) -> dict[str, object]:
        expected_digest = _capture_archive_digest(capture, run_id=run.id)
        source = self._stores.probability_source
        if source is None or not callable(getattr(source, "preload", None)):
            raise ProbabilityArtifactError("上涨概率归档已完成，但 source 只读索引不可用")
        source_research = source.research_projection(run.id)
        if not _source_projection_matches_capture(source_research, expected_digest):
            refresh_pending = getattr(source, "refresh_pending", None)
            if callable(refresh_pending) and refresh_pending():
                return _probability_capture_state(
                    run.id,
                    availability="source_index_verification_pending",
                    limitation="source_index_verification_pending",
                    pipeline_stage="source_index_verification_pending",
                )
            source.preload()
            source_research = source.research_projection(run.id)
        if not _source_projection_matches_capture(source_research, expected_digest):
            raise ProbabilityArtifactError("上涨概率归档已完成，但 source artifact 缺失或未进入只读索引")
        if research.get("status") != "not_generated" or research.get("availability") is not None:
            if _probability_artifact_matches_capture(research, expected_digest):
                return research
            return _source_research_with_unbound_probability_artifact(source_research)
        return source_research

    def _score_contract(
        self,
        verified: MarketScanVerifiedReadProtocol,
        research: Mapping[str, object],
    ) -> MarketScanProductionScoreContract | None:
        binding = research.get("run_binding")
        if not isinstance(binding, Mapping) or binding.get("binding_status") != "verified":
            return None
        return verified.success_score_contract

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
        run = self.run(run_id)
        _require_future_range_eligible_run(run)
        store = self._stores.future_range
        if store is None:
            projection = not_generated_future_range_research(run_id)
        else:
            projection = store.research_projection(
                run_id,
                page=page,
                page_size=page_size,
                session_offset=session_offset,
                symbol=symbol,
                include_research=True,
            )
        _validate_future_range_run_binding(run, projection)
        _validate_future_range_snapshot_stable(run, self.run(run.id))
        if not include_research:
            projection = dict(projection)
            projection["research"] = None
        return projection

    def future_range_export_projection(
        self,
        run_id: int,
        *,
        expected_run: MarketScanRun,
    ) -> dict[str, object]:
        run = self.run(run_id)
        _validate_future_range_snapshot_stable(expected_run, run)
        _require_future_range_eligible_run(run)
        store = self._stores.future_range
        projection = store.export_projection(run_id) if store is not None else not_generated_future_range_research(run_id)
        _validate_future_range_run_binding(run, projection)
        _validate_future_range_snapshot_stable(run, self.run(run.id))
        return projection

    def _future_range_export_for_verified_run(
        self,
        run: MarketScanRun,
    ) -> dict[str, object]:
        store = self._stores.future_range
        if run.mode != "official" or store is None:
            return not_generated_future_range_research(run.id)
        _require_future_range_eligible_run(run)
        projection = store.export_projection(run.id)
        _validate_future_range_run_binding(run, projection)
        return projection


def _maintenance_projection_unavailable(research: Mapping[str, object]) -> bool:
    return research.get("availability") in _JOINT_MAINTENANCE_UNAVAILABLE


def _maintenance_probability_context(
    research: Mapping[str, object], joint_context: Mapping[str, object]
) -> dict[str, object]:
    run_id = research.get("run_id")
    binding = research.get("run_binding")
    if run_id is None and isinstance(binding, Mapping):
        run_id = binding.get("run_id")
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        raise ProbabilityArtifactError("维护中的概率投影缺少当前批次标识")
    status = str(joint_context["status"])
    output = _probability_capture_state(
        run_id, availability=status, limitation=f"joint_execution_{status}", pipeline_stage=status,
    )
    official = joint_context.get("official_execution")
    pinned_digest = official.get("registry_digest") if isinstance(official, Mapping) else None
    official_context = OfficialExecutionStoreStatus(
        configured=isinstance(pinned_digest, str),
        status="maintenance_pending" if status == "maintenance_pending" else "store_unavailable",
        registry_digest=pinned_digest if isinstance(pinned_digest, str) else None,
        verified_session_count=0, first_session_date=None, latest_session_date=None,
        failures=(f"joint_execution_{status}",),
    ).payload()
    historical = not_generated_historical_probability_context()
    historical.update({"status": "unavailable", "availability": status})
    output.update({
        "authority_backend": "joint_execution_opaque_v1", "filter_qualified": False,
        "run_binding": None, "joint_execution_evidence": deepcopy(dict(joint_context)),
        "official_execution_evidence": official_context, "historical_context": historical,
    })
    return output


def _inactive_ranking_context(run_id: int, reason: str | None = None) -> dict[str, object]:
    context: dict[str, object] = {
        "contract_version": "market-scan-probability-ranking-projection-v1",
        "status": "inactive", "run_id": run_id,
        "base_v5_mutated": False, "historical_ranks_mutated": False,
    }
    if reason is not None:
        context["reason"] = reason
    return context


def _export_result_query(
    filters: MarketScanExportFilters,
    *,
    total_count: int,
) -> dict[str, object]:
    return {
        "page": 1,
        "page_size": max(1, total_count),
        "status": filters.status,
        "market": filters.market,
        "industry": filters.industry,
        "is_st": filters.is_st,
        "is_new": filters.is_new,
        "min_score": filters.min_score,
        "max_score": filters.max_score,
        "min_trend_score": filters.min_trend_score,
        "max_trend_score": filters.max_trend_score,
        "min_change_pct": filters.min_change_pct,
        "max_change_pct": filters.max_change_pct,
        "min_turnover_rate": filters.min_turnover_rate,
        "max_turnover_rate": filters.max_turnover_rate,
        "min_amount": filters.min_amount,
        "max_amount": filters.max_amount,
        "min_data_quality_score": filters.min_data_quality_score,
        "max_data_quality_score": filters.max_data_quality_score,
        "min_confidence": filters.min_confidence,
        "max_risk": filters.max_risk,
        "min_tradability": filters.min_tradability,
        "keyword": filters.keyword,
        "sort": filters.sort,
        "order": filters.order,
    }


def _probability_filter_symbols(
    research: dict[str, object],
    probabilities: dict[str, dict[str, object]],
    *,
    horizon: Literal[1, 5, 20],
    minimum: float | None,
    joint_filter_qualified: bool = False,
) -> tuple[str, ...] | None:
    if minimum is None:
        return None
    _validate_probability_minimum(minimum)
    summary = _probability_summary(research, horizon)
    if summary.get("status") != "calibrated_shadow":
        raise ProbabilityFilterUnavailable("当前批次与周期尚无已校准 Shadow 概率，不能使用概率筛选")
    binding = research.get("run_binding")
    if not isinstance(binding, Mapping) or binding.get("binding_status") != "verified" or binding.get("legacy") is not False:
        raise ProbabilityFilterUnavailable("当前概率证据属于旧版或未完整绑定 artifact，禁止用于选股筛选")
    authorization = summary.get("filter_qualification")
    joint_authority = research.get("authority_backend") == "joint_execution_opaque_v1"
    qualified = (
        joint_filter_qualified and summary.get("filter_qualified") is True
        if joint_authority
        else probability_filter_qualified(
            summary,
            authorization if isinstance(authorization, Mapping) else None,
        )
    )
    if not qualified:
        raise ProbabilityFilterUnavailable("当前批次虽已拟合，但尚未通过完整统计、校准、漂移与执行门禁，不能使用概率筛选")
    return tuple(symbol for symbol, horizons in probabilities.items() if _meets_probability_minimum(horizons, horizon, minimum))


def _validate_result_page_binding(
    run: MarketScanRun,
    page: MarketScanResultPage,
) -> None:
    _validate_result_snapshot_stable(run, page.run)
    if page.total > run.total_count:
        raise MarketScanSnapshotIntegrityError("榜单分页总数超过冻结批次完整结果数")
    symbols = [item.symbol for item in page.items]
    if len(symbols) != len(set(symbols)):
        raise MarketScanSnapshotIntegrityError("榜单分页包含重复股票代码")
    if any(item.run_id != run.id for item in page.items):
        raise MarketScanSnapshotIntegrityError("榜单分页包含其他批次结果")
    if any(item.status == "success" and item.data_date != run.data_date for item in page.items):
        raise MarketScanSnapshotIntegrityError("榜单分页成功结果日期与冻结批次不一致")


def _validate_result_snapshot_stable(
    expected: MarketScanRun,
    observed: MarketScanRun,
) -> None:
    validate_market_scan_cohort_binding(expected, observed)
    if expected.status in PUBLISHED_MARKET_SCAN_STATUSES:
        validate_market_scan_run_binding(expected, observed)


def _validate_probability_minimum(minimum: float | None) -> None:
    if minimum is not None and (not math.isfinite(minimum) or not 0 <= minimum <= 1):
        raise ValueError("最低上涨概率必须在 0 到 1 之间")


def _require_future_range_eligible_run(run: MarketScanRun) -> None:
    if run.mode != "official":
        raise FutureRangeResearchUnavailable("未来区间研究仅支持盘后正式批次")
    if run.scope != FULL_MARKET_SCOPE:
        raise FutureRangeResearchUnavailable("未来区间研究仅支持盘后正式全市场批次")
    if run.status not in PUBLISHED_MARKET_SCAN_STATUSES:
        raise FutureRangeResearchUnavailable("未来区间研究仅支持已发布批次")
    if run.snapshot_seal_origin != "publication":
        raise FutureRangeResearchUnavailable("未来区间研究要求原发布时快照封印")
    if run.quote_date != run.data_date:
        raise FutureRangeResearchUnavailable("未来区间研究要求行情日期与完整日K截止日一致")


def _probability_run_eligible(run: MarketScanRun) -> bool:
    return bool(
        run.mode == "official"
        and run.scope == FULL_MARKET_SCOPE
        and run.status in PUBLISHED_MARKET_SCAN_STATUSES
        and run.snapshot_seal_origin == "publication"
        and run.quote_date == run.data_date
    )


def _require_probability_eligible_run(run: MarketScanRun) -> None:
    if run.mode != "official":
        raise ProbabilityResearchUnavailable("上涨概率研究仅支持盘后正式批次")
    if run.scope != FULL_MARKET_SCOPE:
        raise ProbabilityResearchUnavailable("上涨概率研究仅支持盘后正式全市场批次")
    if run.status not in PUBLISHED_MARKET_SCAN_STATUSES:
        raise ProbabilityResearchUnavailable("上涨概率研究仅支持已发布批次")
    if run.snapshot_seal_origin != "publication":
        raise ProbabilityResearchUnavailable("上涨概率研究要求原发布时快照封印")
    if run.quote_date != run.data_date:
        raise ProbabilityResearchUnavailable("上涨概率研究要求行情日期与完整日K截止日一致")


def _validate_future_range_snapshot_stable(
    expected: MarketScanRun,
    observed: MarketScanRun,
) -> None:
    try:
        validate_market_scan_run_binding(expected, observed)
    except MarketScanSnapshotIntegrityError as exc:
        raise FutureRangeArtifactError("未来区间读取期间当前榜单冻结绑定发生变化") from exc


def _validate_future_range_run_binding(
    run: MarketScanRun,
    projection: Mapping[str, object],
) -> None:
    status = projection.get("generation_status")
    if status == "not_generated":
        return
    if status not in {"ready", "insufficient_data"}:
        raise FutureRangeArtifactError("未来区间 API generation_status 无效")
    research = projection.get("research")
    if not isinstance(research, Mapping):
        raise FutureRangeArtifactError("未来区间 artifact 缺少当前榜单绑定契约")
    binding = research.get("run")
    if not isinstance(binding, Mapping):
        raise FutureRangeArtifactError("未来区间 artifact 缺少当前榜单 run 绑定")
    expected = {
        "run_id": run.id,
        "mode": run.mode,
        "scope": run.scope,
        "rule_version": run.rule_version,
        "as_of": run.as_of,
        "quote_date": run.quote_date,
        "data_date": run.data_date,
    }
    mismatches = [name for name, value in expected.items() if binding.get(name) != value]
    if mismatches:
        raise FutureRangeArtifactError(f"未来区间 artifact 与当前榜单绑定不一致：{','.join(mismatches)}")


def _validate_probability_run_binding(
    run: MarketScanRun,
    research: dict[str, object],
    *,
    score_contract: MarketScanProductionScoreContract | None,
) -> dict[str, object]:
    if research.get("status") == "not_generated":
        return research
    raw = research.get("run_binding")
    if not isinstance(raw, Mapping):
        raise ProbabilityArtifactError("上涨概率 artifact 缺少当前榜单绑定契约")
    expected_cohort = {
        "mode": run.mode,
        "scope": run.scope,
        "rule_version": run.rule_version,
    }
    expected_hash = run.rule_version.rsplit(":", 1)[-1]
    expected = {
        "run_id": run.id,
        "mode": run.mode,
        "scope": run.scope,
        "rule_version": run.rule_version,
        "quote_date": run.quote_date,
        "data_date": run.data_date,
        "scan_rule_hash": expected_hash,
        "cohort_contract": expected_cohort,
    }
    mismatches = [name for name, value in expected.items() if raw.get(name) != value]
    if mismatches:
        raise ProbabilityArtifactError(f"上涨概率 artifact 与当前榜单绑定不一致：{','.join(mismatches)}")
    if raw.get("binding_status") not in {"verified", "legacy_unbound"}:
        raise ProbabilityArtifactError("上涨概率 artifact run binding 状态无效")
    if raw.get("binding_status") == "verified":
        if score_contract is None or score_contract.success_count != run.success_count:
            raise ProbabilityArtifactError("当前榜单缺少全覆盖且唯一的生产评分合同")
        score_expected = {
            "production_score_rule_version": score_contract.production_score_rule_version,
            "production_score_spec_hash": score_contract.production_score_spec_hash,
        }
        score_mismatches = [name for name, value in score_expected.items() if raw.get(name) != value]
        if score_mismatches:
            raise ProbabilityArtifactError(f"上涨概率 artifact 与当前生产评分合同不一致：{','.join(score_mismatches)}")
    return research


def _unavailable_probability_research(run: MarketScanRun) -> dict[str, object]:
    research = not_generated_probability_research(run.id)
    research["availability"] = "ineligible_run_contract"
    research["limitations"] = ["probability_requires_published_official_full_market_run"]
    return research


def _probability_capture_state(
    run_id: int,
    *,
    availability: str,
    limitation: str,
    pipeline_stage: str | None = None,
) -> dict[str, object]:
    research = not_generated_probability_research(run_id)
    research["availability"] = availability
    research["limitations"] = [limitation]
    if pipeline_stage is not None:
        research["pipeline_stage"] = pipeline_stage
    horizons = research.get("horizons")
    if isinstance(horizons, dict):
        for targets in horizons.values():
            if not isinstance(targets, dict):
                continue
            for summary in targets.values():
                if not isinstance(summary, dict):
                    continue
                summary["availability"] = availability
                summary["limitations"] = [limitation]
                if pipeline_stage is not None:
                    summary["pipeline_stage"] = pipeline_stage
    return research


def _probability_capture_gate(
    verified: MarketScanVerifiedReadProtocol,
) -> tuple[Mapping[str, object] | None, dict[str, object] | None]:
    run = verified.run
    snapshot_digest = verified.snapshot_digest
    action_digest = verified.action_source_digest
    if action_digest is None or snapshot_digest is None or action_digest != snapshot_digest or snapshot_digest != run.snapshot_digest:
        return None, _probability_capture_state(
            run.id,
            availability="source_scan_action_ineligible",
            limitation="source_scan_action_ineligible",
        )
    capture = verified.probability_source_capture_state
    capture_status = _capture_status(capture, run_id=run.id)
    if capture_status in {"pending", "processing"}:
        return capture, _probability_capture_state(
            run.id,
            availability="source_capture_pending",
            limitation="source_capture_pending",
            pipeline_stage="source_capture_pending",
        )
    if capture_status == "skipped":
        return capture, _probability_capture_state(
            run.id,
            availability="source_capture_skipped",
            limitation="source_capture_skipped",
        )
    if capture_status is None:
        return None, _probability_capture_state(
            run.id,
            availability="source_capture_outbox_missing",
            limitation="source_capture_outbox_missing",
        )
    if capture_status != "succeeded":
        raise ProbabilityArtifactError("上涨概率 source capture 状态不受支持")
    return capture, None


def _capture_status(
    capture: Mapping[str, object] | None,
    *,
    run_id: int,
) -> str | None:
    if capture is None:
        return None
    if not isinstance(capture, Mapping):
        raise ProbabilityArtifactError(f"run {run_id} 上涨概率 source capture state 无效")
    status = capture.get("status")
    if status not in {"pending", "processing", "succeeded", "skipped"}:
        raise ProbabilityArtifactError(f"run {run_id} 上涨概率 source capture 状态无效")
    return cast(str, status)


def _capture_archive_digest(
    capture: Mapping[str, object] | None,
    *,
    run_id: int,
) -> str:
    digest = capture.get("archive_digest") if isinstance(capture, Mapping) else None
    if not isinstance(digest, str) or len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
        raise ProbabilityArtifactError(f"run {run_id} succeeded source capture 缺少有效 archive_digest")
    return digest


def _source_projection_matches_capture(
    research: Mapping[str, object],
    expected_digest: str,
) -> bool:
    if research.get("status") == "not_generated":
        return False
    binding = research.get("run_binding")
    return isinstance(binding, Mapping) and binding.get("source_integrity_digest") == expected_digest


def _probability_artifact_matches_capture(
    research: Mapping[str, object],
    expected_digest: str,
) -> bool:
    binding = research.get("run_binding")
    return bool(
        isinstance(binding, Mapping)
        and binding.get("binding_status") == "verified"
        and binding.get("legacy") is False
        and binding.get("source_integrity_digest") == expected_digest
    )


def _source_research_with_unbound_probability_artifact(
    source_research: Mapping[str, object],
) -> dict[str, object]:
    research = deepcopy(dict(source_research))
    research["availability"] = "probability_artifact_source_unbound"
    limitations = research.get("limitations")
    values = list(limitations) if isinstance(limitations, list) else []
    if "probability_artifact_source_unbound" not in values:
        values.append("probability_artifact_source_unbound")
    research["limitations"] = values
    return research


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


def _production_ranking_pages(
    verified: MarketScanVerifiedReadProtocol,
    *,
    query: Mapping[str, object],
    symbols: Sequence[str] | None,
    context: Mapping[str, object],
    records: Mapping[str, Mapping[str, object]],
) -> tuple[MarketScanResultPage, MarketScanResultPage]:
    run = verified.run
    _validate_production_ranking_context(run, context, records)
    _require_v5_score_contract(verified)
    requested_page = _positive_query_integer(query.get("page"), "page")
    requested_size = _positive_query_integer(query.get("page_size"), "page_size")
    base_query = _production_base_query(query, run.total_count)
    base_page = verified.results_page(**base_query, symbols=symbols)
    _validate_result_page_binding(run, base_page)
    artifact_digest = _digest_value(
        context.get("artifact_digest"),
        "production_ranking.artifact_digest",
    )
    ordered = _production_ranked_items(base_page, records, artifact_digest, query)
    return base_page, _ranked_result_page(
        base_page,
        ordered,
        context,
        page=requested_page,
        page_size=requested_size,
    )


def _require_v5_score_contract(verified: MarketScanVerifiedReadProtocol) -> None:
    contract = verified.success_score_contract
    if (
        contract is None
        or contract.production_score_rule_version != FULL_MARKET_SCORE_RULE_VERSION
        or contract.success_count != verified.run.success_count
    ):
        raise ProbabilityArtifactError("v6 生产排名缺少精确、不可变的 v5 分数合同")


def _production_base_query(
    query: Mapping[str, object],
    total_count: int,
) -> dict[str, object]:
    base_query = dict(query)
    base_query.update(
        {"page": 1, "page_size": max(1, total_count), "min_score": None, "max_score": None}
    )
    return base_query


def _production_ranked_items(
    base_page: MarketScanResultPage,
    records: Mapping[str, Mapping[str, object]],
    artifact_digest: str,
    query: Mapping[str, object],
) -> list[MarketScanResultItem]:
    overlaid: list[MarketScanResultItem] = [
        _production_ranking_item(item, records, artifact_digest=artifact_digest)
        for item in base_page.items
    ]
    filtered = _score_filtered_items(overlaid, query)
    def compare(left: MarketScanResultItem, right: MarketScanResultItem) -> int:
        return _compare_production_ranking_items(
            left,
            right,
            sort=query.get("sort"),
            order=query.get("order"),
        )

    return sorted(filtered, key=cmp_to_key(compare))


def _score_filtered_items(
    items: Sequence[MarketScanResultItem], query: Mapping[str, object],
) -> list[MarketScanResultItem]:
    minimum = _optional_query_score(query.get("min_score"), "min_score")
    maximum = _optional_query_score(query.get("max_score"), "max_score")
    return [
        item for item in items
        if (minimum is None or item.score is not None and item.score >= minimum)
        and (maximum is None or item.score is not None and item.score <= maximum)
    ]


def _ranked_result_page(
    base_page: MarketScanResultPage,
    ordered: Sequence[MarketScanResultItem],
    context: Mapping[str, object],
    *,
    page: int,
    page_size: int,
) -> MarketScanResultPage:
    total = len(ordered)
    start = (page - 1) * page_size
    items = ordered[start : start + page_size]
    # The complete cohort remains available for ranking and maintenance fallback;
    # only the selected page needs serialization in the public response.
    payload = base_page.model_dump(mode="python", exclude={"items"})
    payload.update(
        {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "page_count": (total + page_size - 1) // page_size,
            "production_ranking": dict(context),
        }
    )
    return MarketScanResultPage.model_validate(payload)


def _validate_production_ranking_context(
    run: MarketScanRun,
    context: Mapping[str, object],
    records: Mapping[str, Mapping[str, object]],
) -> None:
    if (
        context.get("contract_version")
        != "market-scan-probability-ranking-projection-v1"
        or context.get("status") != "active"
        or context.get("run_id") != run.id
        or context.get("score_rule_version")
        != PROBABILITY_RANKING_SCORE_RULE_VERSION
        or context.get("score_spec_hash") != probability_ranking_score_spec_hash()
        or context.get("base_snapshot_digest") != run.snapshot_digest
        or context.get("base_v5_mutated") is not False
        or context.get("historical_ranks_mutated") is not False
    ):
        raise ProbabilityArtifactError("v6 生产排名上下文与已发布 v5 批次不一致")
    artifact_digest = _digest_value(
        context.get("artifact_digest"),
        "production_ranking.artifact_digest",
    )
    del artifact_digest
    _digest_value(context.get("promotion_digest"), "production_ranking.promotion_digest")
    record_count = _positive_query_integer(
        context.get("record_count"),
        "production_ranking.record_count",
    )
    if record_count != run.success_count or len(records) != record_count:
        raise ProbabilityArtifactError("v6 生产排名覆盖数与原始 success 集合不一致")


def _production_ranking_item(
    item: MarketScanResultItem,
    records: Mapping[str, Mapping[str, object]],
    *,
    artifact_digest: str,
) -> MarketScanResultItem:
    record = records.get(item.symbol)
    if item.status != "success":
        if record is not None:
            raise ProbabilityArtifactError(
                "v6 生产排名包含非 success 来源股票"
            )
        return item
    if record is None:
        raise ProbabilityArtifactError("v6 生产排名未覆盖全部 success 股票")
    base_rank = _positive_query_integer(record.get("base_rank"), "base_rank")
    base_score = _optional_query_score(record.get("base_score"), "base_score")
    base_raw = _finite_ranking_number(record.get("base_raw_score"), "base_raw_score")
    if (
        base_score is None
        or item.rank != base_rank
        or item.score != base_score
        or item.raw_score is None
        or not math.isclose(item.raw_score, base_raw, rel_tol=0, abs_tol=1e-9)
    ):
        raise ProbabilityArtifactError("v6 生产排名与不可变 v5 来源行不一致")
    rank = _positive_query_integer(record.get("rank"), "rank")
    score = _optional_query_score(record.get("score"), "score")
    raw_score = _finite_ranking_number(record.get("raw_score"), "raw_score")
    adjustment = _finite_ranking_number(
        record.get("probability_adjustment"),
        "probability_adjustment",
    )
    if score is None or not -6 <= adjustment <= 6:
        raise ProbabilityArtifactError("v6 生产排名分数或概率调整无效")
    payload = item.model_dump(mode="python")
    payload.update(
        {
            "base_production_rank": item.rank,
            "base_production_score": item.score,
            "base_production_raw_score": item.raw_score,
            "rank": rank,
            "score": score,
            "raw_score": raw_score,
            "production_score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
            "probability_ranking_adjustment": adjustment,
            "probability_ranking_artifact_digest": artifact_digest,
            "probability_ranking_details": deepcopy(dict(record)),
        }
    )
    return MarketScanResultItem.model_validate(payload)


def _compare_production_ranking_items(
    left: MarketScanResultItem,
    right: MarketScanResultItem,
    *,
    sort: object,
    order: object,
) -> int:
    sorts = _query_text_sequence(sort) or ("rank",)
    orders = _query_text_sequence(order) or tuple(
        "asc" if name in {"rank", "symbol"} else "desc" for name in sorts
    )
    if len(sorts) != len(orders):
        raise ProbabilityArtifactError("v6 生产排名排序字段与方向不一致")
    for name, direction in zip(sorts, orders, strict=True):
        if direction not in {"asc", "desc"}:
            raise ProbabilityArtifactError("v6 生产排名排序方向无效")
        comparison = _compare_ordered_optional_values(
            _production_sort_value(left, name),
            _production_sort_value(right, name),
            direction=direction,
        )
        if comparison:
            return comparison
    if sorts == ("rank",):
        return (left.symbol > right.symbol) - (left.symbol < right.symbol)
    raw_comparison = _compare_ordered_optional_values(left.raw_score, right.raw_score, direction="desc")
    if raw_comparison:
        return raw_comparison
    return (left.symbol > right.symbol) - (left.symbol < right.symbol)


def _compare_ordered_optional_values(left: object, right: object, *, direction: str) -> int:
    """Match frozen SQL: missing values stay last regardless of sort direction."""
    comparison = _compare_optional_values(left, right)
    if left is None or right is None:
        return comparison
    return comparison if direction == "asc" else -comparison


def _production_sort_value(item: MarketScanResultItem, name: str) -> object:
    if name in {
        "rank",
        "score",
        "raw_score",
        "trend_score",
        "change_pct",
        "amount",
        "turnover_rate",
        "data_quality_score",
        "symbol",
    }:
        return getattr(item, name)
    if name in {"alpha_5d", "confidence", "risk", "tradability"}:
        components = item.score_details.get("components")
        dimensions = (
            components.get("score_dimensions")
            if isinstance(components, Mapping)
            else None
        )
        scores = dimensions.get("scores") if isinstance(dimensions, Mapping) else None
        return scores.get(name) if isinstance(scores, Mapping) else None
    raise ProbabilityArtifactError(f"v6 生产排名不支持排序字段：{name}")


def _compare_optional_values(left: object, right: object) -> int:
    if left is None and right is None:
        return 0
    if left is None:
        return 1
    if right is None:
        return -1
    if isinstance(left, str) and isinstance(right, str):
        return (left > right) - (left < right)
    left_number = _finite_ranking_number(left, "sort.left")
    right_number = _finite_ranking_number(right, "sort.right")
    return (left_number > right_number) - (left_number < right_number)


def _query_text_sequence(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return tuple(str(item) for item in value)
    raise ProbabilityArtifactError("v6 生产排名排序参数无效")


def _positive_query_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProbabilityArtifactError(f"v6 生产排名 {label} 必须是正整数")
    return value


def _optional_query_score(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ProbabilityArtifactError(f"v6 生产排名 {label} 必须是 0..100 整数")
    return value


def _finite_ranking_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProbabilityArtifactError(f"v6 生产排名 {label} 必须是数值")
    output = float(value)
    if not math.isfinite(output):
        raise ProbabilityArtifactError(f"v6 生产排名 {label} 必须有限")
    return output


def _digest_value(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProbabilityArtifactError(f"{label} 必须是 SHA-256")
    return value


def _attach_probability_projection(
    page: MarketScanResultPage,
    research: dict[str, object],
    probabilities: dict[str, dict[str, object]],
    *,
    production_ranking: dict[str, object],
) -> MarketScanResultPage:
    items = [item.model_copy(update={"upside_probabilities": probabilities.get(item.symbol, {})}) for item in page.items]
    return page.model_copy(
        update={
            "items": items,
            "probability_research": research,
            "production_ranking": production_ranking,
        }
    )


__all__ = ["MarketScanQueryService"]
