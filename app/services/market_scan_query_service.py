"""Read-only market-scan queries and research projections."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math
from typing import Literal, cast

from app.models.market_scan import (
    MarketScanFilterValues,
    MarketScanMode,
    MarketScanProductionScoreContract,
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
from app.services.market_scan_export import PUBLISHED_MARKET_SCAN_STATUSES
from app.services.market_scan_future_range_store import (
    FutureRangeResearchUnavailable,
    not_generated_future_range_research,
)
from app.services.market_scan_future_range_artifact import FutureRangeArtifactError
from app.services.market_scan_probability_research import PROBABILITY_PRIMARY_TARGET
from app.services.market_scan_probability import probability_filter_qualified
from app.services.market_scan_probability_artifact import ProbabilityArtifactError
from app.services.market_scan_probability_store import (
    ProbabilityFilterUnavailable,
    ProbabilityResearchUnavailable,
    not_generated_probability_research,
)
from app.services.market_scan_research_stores import MarketScanResearchStores
from app.services.market_scan_screening import MarketScanScreeningService
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
        min_score: int | None = None, max_score: int | None = None,
        min_trend_score: int | None = None, max_trend_score: int | None = None,
        min_change_pct: float | None = None, max_change_pct: float | None = None,
        min_turnover_rate: float | None = None, max_turnover_rate: float | None = None,
        min_amount: float | None = None, max_amount: float | None = None,
        min_data_quality_score: int | None,
        max_data_quality_score: int | None = None, min_confidence: float | None = None,
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
        query = {
            name: values[name]
            for name in _RESULT_QUERY_FIELDS
            if name != "symbols"
        }
        with self._cache.verified_market_scan_read(run_id) as verified:
            return self._results_from_verified(
                verified,
                query=query,
                probability_horizon=probability_horizon,
                minimum=min_upside_probability,
            )

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
        if minimum is not None and not eligible:
            raise ProbabilityFilterUnavailable(
                "上涨概率筛选仅支持已发布的盘后正式全市场批次"
            )
        research = _unavailable_probability_research(run)
        all_probabilities: dict[str, dict[str, object]] = {}
        if eligible and minimum is not None:
            research, all_probabilities = self._probability_projection_for_verified(
                verified
            )
        symbols = _probability_filter_symbols(
            research,
            all_probabilities,
            horizon=probability_horizon,
            minimum=minimum,
        )
        page_result = verified.results_page(**query, symbols=symbols)
        _validate_result_page_binding(run, page_result)
        page_symbols = tuple(item.symbol for item in page_result.items)
        if eligible and minimum is None:
            research, probabilities = self._probability_projection_for_verified(
                verified,
                symbols=page_symbols,
            )
        else:
            probabilities = {
                symbol: all_probabilities[symbol]
                for symbol in page_symbols
                if symbol in all_probabilities
            }
        return _attach_probability_projection(page_result, research, probabilities)

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

    def _probability_research_for_verified(
        self,
        verified: MarketScanVerifiedReadProtocol,
    ) -> dict[str, object]:
        run = verified.run
        capture, gated = _probability_capture_gate(verified)
        if gated is not None:
            return gated
        store = self._stores.probability
        research = (
            store.research_projection(run.id)
            if store is not None
            else not_generated_probability_research(run.id)
        )
        research = self._resolve_probability_source_research(
            run,
            research,
            capture=capture,
        )
        return _validate_probability_run_binding(
            run,
            research,
            score_contract=self._score_contract(verified, research),
        )

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
            return gated, {}
        store = self._stores.probability
        if store is None:
            research = not_generated_probability_research(run.id)
            research = self._resolve_probability_source_research(
                run,
                research,
                capture=capture,
            )
            return _validate_probability_run_binding(
                run,
                research,
                score_contract=self._score_contract(verified, research),
            ), {}
        research, probabilities = store.run_projection(run.id, symbols=symbols)
        research = self._resolve_probability_source_research(
            run,
            research,
            capture=capture,
        )
        if research.get("availability") is not None:
            probabilities = {}
        validated = _validate_probability_run_binding(
            run,
            research,
            score_contract=self._score_contract(verified, research),
        )
        return validated, probabilities

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
        projection = (
            store.export_projection(run_id)
            if store is not None
            else not_generated_future_range_research(run_id)
        )
        _validate_future_range_run_binding(run, projection)
        _validate_future_range_snapshot_stable(run, self.run(run.id))
        return projection


def _probability_filter_symbols(
    research: dict[str, object],
    probabilities: dict[str, dict[str, object]],
    *,
    horizon: Literal[1, 5, 20],
    minimum: float | None,
) -> tuple[str, ...] | None:
    if minimum is None:
        return None
    _validate_probability_minimum(minimum)
    summary = _probability_summary(research, horizon)
    if summary.get("status") != "calibrated_shadow":
        raise ProbabilityFilterUnavailable("当前批次与周期尚无已校准 Shadow 概率，不能使用概率筛选")
    binding = research.get("run_binding")
    if not isinstance(binding, Mapping) or binding.get("binding_status") != "verified" or binding.get("legacy") is not False:
        raise ProbabilityFilterUnavailable(
            "当前概率证据属于旧版或未完整绑定 artifact，禁止用于选股筛选"
        )
    authorization = summary.get("filter_qualification")
    if not probability_filter_qualified(
        summary,
        authorization if isinstance(authorization, Mapping) else None,
    ):
        raise ProbabilityFilterUnavailable(
            "当前批次虽已拟合，但尚未通过完整统计、校准、漂移与执行门禁，不能使用概率筛选"
        )
    return tuple(
        symbol
        for symbol, horizons in probabilities.items()
        if _meets_probability_minimum(horizons, horizon, minimum)
    )


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
    if any(
        item.status == "success" and item.data_date != run.data_date
        for item in page.items
    ):
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
        raise FutureRangeArtifactError(
            f"未来区间 artifact 与当前榜单绑定不一致：{','.join(mismatches)}"
        )


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
        raise ProbabilityArtifactError(
            f"上涨概率 artifact 与当前榜单绑定不一致：{','.join(mismatches)}"
        )
    if raw.get("binding_status") not in {"verified", "legacy_unbound"}:
        raise ProbabilityArtifactError("上涨概率 artifact run binding 状态无效")
    if raw.get("binding_status") == "verified":
        if score_contract is None or score_contract.success_count != run.success_count:
            raise ProbabilityArtifactError("当前榜单缺少全覆盖且唯一的生产评分合同")
        score_expected = {
            "production_score_rule_version": score_contract.production_score_rule_version,
            "production_score_spec_hash": score_contract.production_score_spec_hash,
        }
        score_mismatches = [
            name for name, value in score_expected.items() if raw.get(name) != value
        ]
        if score_mismatches:
            raise ProbabilityArtifactError(
                f"上涨概率 artifact 与当前生产评分合同不一致：{','.join(score_mismatches)}"
            )
    return research


def _unavailable_probability_research(run: MarketScanRun) -> dict[str, object]:
    research = not_generated_probability_research(run.id)
    research["availability"] = "ineligible_run_contract"
    research["limitations"] = [
        "probability_requires_published_official_full_market_run"
    ]
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
    if (
        action_digest is None
        or snapshot_digest is None
        or action_digest != snapshot_digest
        or snapshot_digest != run.snapshot_digest
    ):
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
    if not isinstance(digest, str) or len(digest) != 64 or any(
        value not in "0123456789abcdef" for value in digest
    ):
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
