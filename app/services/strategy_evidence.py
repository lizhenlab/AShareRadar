"""Evidence-center orchestration over a retained offline evaluation artifact.

The expensive cross-date evaluator intentionally stays out of the request path. Its
report is generated explicitly by ``tools/evaluate_market_scan_shadow.py`` and this
service only compacts that immutable baseline together with one strategy execution.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence, cast

from app.artifacts.io import ArtifactIOError, decode_json_bytes, read_regular_file
from app.models.strategy_evidence import (
    EvidenceAvailability,
    EvidenceCenterStatus,
    StrategyDimensionEvidence,
    StrategyEvidenceCenter,
    StrategyEvidenceCoverage,
    StrategyEvidenceExecution,
    StrategyEvidenceExecutionCompatibility,
    StrategyEvidenceResearchBoundary,
    StrategyPromotionEvidence,
    StrategyRankEvidence,
    StrategyShadowConstraintEvidence,
    StrategyShadowCoverageEvidence,
    StrategyShadowEvidence,
    StrategyShadowExposureEvidence,
    StrategyShadowPromotionGateEvidence,
    StrategyShadowRankDeltaEvidence,
    StrategyShadowTopNEvidence,
    StrategyTopNEvidence,
)
from app.models.strategy_execution import PortfolioCandidate, PortfolioDraftSummary
from app.models.market_scan import MarketScanProductionScoreContract
from app.repositories.strategy_evidence import (
    StrategyEvidenceRepository,
    strategy_evidence_digest,
)
from app.services.strategy_lab import StrategyLabService
from app.utils.audit_time import audit_now_text


_BOARD_LABELS = {
    "sh_main": "上海主板",
    "star": "科创板",
    "sz_main": "深圳主板",
    "chinext": "创业板",
    "beijing": "北交所",
}
_DIMENSIONS = ("alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability")
_OFFLINE_REPORT = Path(__file__).resolve().parents[2] / "docs" / "research" / "FULL_MARKET_SELECTION_SHADOW_V55_2026.json"
_OFFLINE_REPORT_DIGEST = "b3c5301e201bd3faaa3abadfb819d1e132b7051ae7ec181752b9cb5b68587183"
_OFFLINE_REPORT_MAX_BYTES = 10_000_000
_BASELINE_PRODUCTION_SCORE_RULE_VERSION = "full-market-score-v4"
_BASELINE_PRODUCTION_SCORE_SPEC_HASH = (
    "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
)
_OFFLINE_REPORT_KEYS = frozenset(
    {
        "artifact_projection",
        "candidates",
        "generated_at",
        "production",
        "promotion",
        "schema_version",
        "status",
    }
)


class StrategyEvidenceService:
    def __init__(
        self,
        path: Path,
        repository: StrategyEvidenceRepository,
        strategies: StrategyLabService,
    ) -> None:
        self.path = Path(path)
        self.repository = repository
        self.strategies = strategies

    def latest(
        self,
        strategy_id: int,
        *,
        revision: int | None,
        mode: str,
    ) -> StrategyEvidenceCenter | None:
        strategy = self.strategies.get(strategy_id, revision=revision)
        return self.repository.latest(
            strategy_id,
            revision=strategy.strategy_version,
            mode=mode,
        )

    def refresh(
        self,
        strategy_id: int,
        *,
        revision: int | None,
        mode: str,
    ) -> StrategyEvidenceCenter:
        strategy = self.strategies.get(strategy_id, revision=revision)
        report = _load_offline_evaluation_report()
        execution_row, summary, candidates, score_contract = self.repository.latest_execution(
            strategy_id,
            strategy.strategy_version,
            mode,
        )
        generated_at = audit_now_text()
        compatibility = _execution_contract_compatibility(
            execution_row,
            score_contract,
        )
        promotion = _promotion_for_execution(
            _promotion(report),
            compatibility=compatibility,
        )
        status = (
            "insufficient_data"
            if compatibility == "incompatible"
            else _center_status(report, promotion)
        )
        payload = _refresh_payload(
            strategy,
            strategy_id=strategy_id,
            report=report,
            execution_row=execution_row,
            summary=summary,
            candidates=candidates,
            score_contract=score_contract,
            mode=mode,
            status=status,
            generated_at=generated_at,
            compatibility=compatibility,
            promotion=promotion,
        )
        digest = _stable_digest(payload)
        return self.repository.save(
            strategy_id=strategy_id,
            revision=strategy.strategy_version,
            fingerprint=strategy.fingerprint,
            mode=mode,
            status=status,
            payload=payload,
            digest=digest,
            generated_at=generated_at,
        )


def _refresh_payload(
    strategy: Any,
    *,
    strategy_id: int,
    report: Mapping[str, object],
    execution_row: sqlite3.Row | None,
    summary: PortfolioDraftSummary | None,
    candidates: Sequence[PortfolioCandidate],
    score_contract: MarketScanProductionScoreContract | None,
    mode: str,
    status: EvidenceCenterStatus,
    generated_at: str,
    compatibility: StrategyEvidenceExecutionCompatibility,
    promotion: StrategyPromotionEvidence,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "strategy_id": strategy_id,
        "strategy_version": strategy.strategy_version,
        "strategy_fingerprint": strategy.fingerprint,
        "strategy_name": strategy.spec.name,
        "mode": mode,
        "status": status,
        "generated_at": generated_at,
        "baseline_generated_at": _optional_text(report.get("generated_at")),
        "baseline_report_digest": _stable_digest(report),
        "baseline_schema_version": _optional_text(report.get("schema_version")),
        "baseline_projection_schema_version": _optional_text(
            _mapping(report.get("artifact_projection")).get("schema_version")
        ),
        "research_boundary": StrategyEvidenceResearchBoundary(
            execution_contract_compatibility=compatibility,
        ).model_dump(mode="json"),
        "execution": _execution_evidence(
            execution_row, summary, candidates, score_contract,
        ).model_dump(mode="json"),
        "coverage": [item.model_dump(mode="json") for item in _coverage(candidates)],
        "dimensions": [item.model_dump(mode="json") for item in _dimension_evidence(candidates)],
        "top_n": [item.model_dump(mode="json") for item in _top_n(report, mode=mode)],
        "rank_evidence": [item.model_dump(mode="json") for item in _rank_evidence(report, mode=mode)],
        "exposure_audit": _exposure_audit(report, mode=mode),
        "shadow_candidates": [item.model_dump(mode="json") for item in _shadow(report, mode=mode)],
        "promotion": promotion.model_dump(mode="json"),
        "data_sources": _sources(report),
        "freshness_notes": _freshness_notes(execution_row, report=report),
        "limitations": _limitations(report, has_execution=execution_row is not None),
    }


def _execution_evidence(
    row: sqlite3.Row | None,
    summary: PortfolioDraftSummary | None,
    candidates: Sequence[PortfolioCandidate],
    score_contract: MarketScanProductionScoreContract | None,
) -> StrategyEvidenceExecution:
    if row is None or summary is None:
        return StrategyEvidenceExecution(evidence_digest_verified=False)
    reconstructed = _stable_digest(
        {
            "execution_fingerprint": str(row["execution_fingerprint"]),
            "summary": summary.model_dump(mode="json"),
            "candidates": [item.model_dump(mode="json") for item in candidates],
        }
    )
    return StrategyEvidenceExecution(
        execution_id=_int(row["id"]),
        execution_fingerprint=str(row["execution_fingerprint"]),
        market_scan_run_id=_int(row["market_scan_run_id"]),
        rule_version=str(row["rule_version"]),
        production_score_rule_version=(
            score_contract.production_score_rule_version
            if score_contract is not None
            else None
        ),
        production_score_spec_hash=(
            score_contract.production_score_spec_hash
            if score_contract is not None
            else None
        ),
        data_as_of=str(row["data_as_of"]),
        data_date=str(row["data_date"]),
        cost_rule_fingerprint=str(row["cost_rule_fingerprint"]),
        evidence_digest_verified=reconstructed == str(row["result_digest"]),
    )


def _execution_contract_compatibility(
    row: sqlite3.Row | None,
    score_contract: MarketScanProductionScoreContract | None,
) -> StrategyEvidenceExecutionCompatibility:
    if row is None:
        return "not_available"
    if score_contract is None:
        return "incompatible"
    if (
        score_contract.production_score_rule_version
        == _BASELINE_PRODUCTION_SCORE_RULE_VERSION
        and score_contract.production_score_spec_hash
        == _BASELINE_PRODUCTION_SCORE_SPEC_HASH
    ):
        return "compatible"
    return "incompatible"


def _promotion_for_execution(
    promotion: StrategyPromotionEvidence,
    *,
    compatibility: StrategyEvidenceExecutionCompatibility,
) -> StrategyPromotionEvidence:
    if compatibility != "incompatible":
        return promotion
    blocker = (
        "当前执行评分合同与离线 full-market-score-v4 基线不兼容；"
        "必须生成同合同评估 artifact"
    )
    return promotion.model_copy(
        update={
            "eligible_for_manual_review": False,
            "blockers": list(dict.fromkeys([*promotion.blockers, blocker]))[:30],
            "conclusion": "当前执行合同不能使用历史 v4 基线支持晋级。",
        }
    )


def _coverage(candidates: Sequence[PortfolioCandidate]) -> list[StrategyEvidenceCoverage]:
    grouped: dict[str, list[PortfolioCandidate]] = defaultdict(list)
    grouped["全市场"].extend(candidates)
    for item in candidates:
        grouped[_BOARD_LABELS.get(item.board, item.board)].append(item)
    scopes = ["全市场", *_BOARD_LABELS.values()]
    return [_coverage_record(scope, grouped.get(scope, [])) for scope in scopes]


def _coverage_record(scope: str, items: Sequence[PortfolioCandidate]) -> StrategyEvidenceCoverage:
    total = len(items)
    verified = sum(item.evidence_verified for item in items)
    return StrategyEvidenceCoverage(
        scope=scope,
        total_count=total,
        selected_count=sum(item.status == "selected" for item in items),
        rejected_count=sum(item.status == "rejected" for item in items),
        constraint_adjusted_count=sum(item.status == "constraint_adjusted" for item in items),
        unfilled_count=sum(item.status == "unfilled" for item in items),
        verified_count=verified,
        coverage_ratio=verified / total if total else 0.0,
    )


def _dimension_evidence(candidates: Sequence[PortfolioCandidate]) -> list[StrategyDimensionEvidence]:
    selected = [item for item in candidates if item.status in {"selected", "constraint_adjusted"}]
    return [
        StrategyDimensionEvidence(
            dimension=cast(Any, name),
            selected_average=_average(selected, name),
            candidate_average=_average(candidates, name),
        )
        for name in _DIMENSIONS
    ]


def _average(items: Sequence[PortfolioCandidate], field: str) -> float | None:
    values = [float(value) for item in items if (value := getattr(item, field, None)) is not None]
    return sum(values) / len(values) if values else None


def _top_n(report: Mapping[str, object], *, mode: str) -> list[StrategyTopNEvidence]:
    production = _mapping(report.get("production"))
    rows = _sequence_of_mappings(production.get("cohorts"))
    compact: list[StrategyTopNEvidence] = []
    for item in rows:
        dimensions = _mapping(item.get("dimensions"))
        if len(dimensions) != 3:
            continue
        if dimensions.get("mode") != mode:
            continue
        execution = _mapping(item.get("execution"))
        compact.append(
            StrategyTopNEvidence(
                top_n=_int(item.get("top_n"), 1),
                horizon_trading_days=_int(item.get("horizon_trading_days"), 1),
                status=str(item.get("status") or "insufficient_data"),
                sample_size=_int(item.get("sample_size")),
                independent_session_count=_int(item.get("independent_session_count")),
                gross_return=_float(item.get("session_average_return")),
                net_return=_float(execution.get("average_net_return")),
                cost_drag=_float(execution.get("average_cost_drag")),
                turnover_rate=_matching_turnover(production, item, mode=mode),
                maximum_drawdown=_float(item.get("session_maximum_drawdown")),
                maximum_adverse_excursion=_float(item.get("maximum_adverse_excursion")),
                confidence_interval_95=_float_list(item.get("session_return_confidence_interval_95")),
                insufficient_reasons=[str(value) for value in _sequence(item.get("insufficient_reasons"))][:20],
            )
        )
    return compact[:30]


def _matching_turnover(
    production: Mapping[str, object],
    cohort: Mapping[str, object],
    *,
    mode: str,
) -> float | None:
    top_n = _int(cohort.get("top_n"))
    values = [
        _float(item.get("turnover_rate"))
        for item in _sequence_of_mappings(production.get("stability"))
        if _int(item.get("top_n")) == top_n and item.get("mode") == mode
    ]
    clean = [value for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def _rank_evidence(report: Mapping[str, object], *, mode: str) -> list[StrategyRankEvidence]:
    production = _mapping(report.get("production"))
    deciles = {
        _int(item.get("horizon_trading_days"), 1): item.get("monotonic")
        for item in _sequence_of_mappings(production.get("deciles"))
        if _item_mode(item) == mode
    }
    return [
        StrategyRankEvidence(
            horizon_trading_days=_int(item.get("horizon_trading_days"), 1),
            status=str(item.get("status") or "insufficient_data"),
            independent_session_count=_int(item.get("independent_session_count")),
            rank_ic=_float(item.get("mean_rank_ic")),
            icir=_float(item.get("icir")),
            confidence_interval_95=_float_list(item.get("confidence_interval_95")),
            decile_monotonic=cast(bool | None, deciles.get(_int(item.get("horizon_trading_days"), 1))),
        )
        for item in _sequence_of_mappings(production.get("rank_ic"))
        if _item_mode(item) == mode
    ]


def _exposure_audit(report: Mapping[str, object], *, mode: str) -> dict[str, object]:
    production = _mapping(report.get("production"))
    records = [item for item in _sequence_of_mappings(production.get("exposure_audit")) if _item_mode(item) in {None, mode}]
    return {
        "status": "available" if records else "insufficient_data",
        "latest": records[-1] if records else {},
        "record_count": len(records),
    }


def _shadow(report: Mapping[str, object], *, mode: str) -> list[StrategyShadowEvidence]:
    records = []
    promotion = _mapping(report.get("promotion"))
    candidate_gates = _mapping(promotion.get("candidate_gates"))
    for candidate_id, raw in _mapping(report.get("candidates")).items():
        candidate = _mapping(raw)
        source = _mapping(candidate.get("source"))
        shadow = _mapping(candidate.get("shadow"))
        integrity = _mapping(shadow.get("input_integrity"))
        gate = _mapping(candidate_gates.get(candidate_id))
        records.append(
            StrategyShadowEvidence(
                candidate_id=str(candidate_id),
                status=str(candidate.get("status") or "insufficient_data"),
                spec_hash=str(shadow.get("spec_hash")) if shadow.get("spec_hash") else None,
                point_in_time_integrity_verified=bool(integrity.get("eligible_for_promotion_evidence") is True),
                independent_session_count=_int(source.get("independent_session_count")),
                evidence_status=_availability(candidate.get("status")),
                coverage=_shadow_coverage(candidate, shadow=shadow),
                top_n=_shadow_top_n(candidate, mode=mode),
                rank_delta_vs_production=_shadow_rank_delta(candidate, shadow=shadow),
                constraints=_shadow_constraints(gate),
                exposure=_shadow_exposure(candidate, gate=gate),
                promotion_gate=_shadow_promotion_gate(gate),
            )
        )
    return records[:20]


def _shadow_coverage(
    candidate: Mapping[str, object],
    *,
    shadow: Mapping[str, object],
) -> StrategyShadowCoverageEvidence:
    source = _mapping(candidate.get("source"))
    quality = _mapping(candidate.get("evaluation_quality"))
    run_evidence = _sequence_of_mappings(shadow.get("run_evidence"))
    expected_count = _optional_int(quality.get("expected_item_count"))
    ratio = None if expected_count == 0 else _float(quality.get("item_coverage_ratio"))
    scored_counts = [value for item in run_evidence if (value := _optional_int(item.get("scored_count"))) is not None]
    if ratio is None:
        status = "unavailable"
        reasons = [
            (
                "候选无预期评分行，覆盖率分母为 0；不会用 0 代替缺失证据"
                if expected_count == 0
                else "离线 artifact 未记录候选评分行覆盖率；不会用 0 代替缺失证据"
            )
        ]
    else:
        status = _availability(candidate.get("status"))
        reason_counts = _mapping(quality.get("exclusion_reason_counts"))
        reasons = [
            *[str(item) for item in _sequence(quality.get("rejection_reasons"))],
            *[f"{name}: {count}" for name, count in reason_counts.items()],
        ][:20]
    return StrategyShadowCoverageEvidence(
        status=status,
        independent_session_count=_optional_int(source.get("independent_session_count")),
        scored_run_count=(
            _optional_int(quality.get("evaluated_run_count"))
            if quality.get("evaluated_run_count") is not None
            else (len(run_evidence) if run_evidence else None)
        ),
        scored_item_count=(
            _optional_int(quality.get("scored_item_count")) if quality.get("scored_item_count") is not None else (sum(scored_counts) if scored_counts else None)
        ),
        item_coverage_ratio=ratio,
        reasons=reasons,
    )


def _shadow_top_n(
    candidate: Mapping[str, object],
    *,
    mode: str,
) -> list[StrategyShadowTopNEvidence]:
    cohorts = _sequence_of_mappings(candidate.get("cohorts"))
    stability = _sequence_of_mappings(candidate.get("stability"))
    output: list[StrategyShadowTopNEvidence] = []
    for top_n in (20, 50, 100):
        matching = [
            item
            for item in cohorts
            if _optional_int(item.get("top_n")) == top_n
            and _optional_int(item.get("horizon_trading_days")) == 5
            and _item_mode(item) == mode
            and _is_full_market_contract(item)
        ]
        if not matching:
            output.append(
                StrategyShadowTopNEvidence(
                    top_n=cast(Any, top_n),
                    status="unavailable",
                    insufficient_reasons=[f"离线 artifact 未记录 {mode} Top{top_n} 的 5 日候选证据"],
                )
            )
            continue
        item = max(
            matching,
            key=lambda row: _optional_int(row.get("independent_session_count")) or -1,
        )
        execution = _mapping(item.get("execution"))
        turnover_values = [
            value
            for row in stability
            if _optional_int(row.get("top_n")) == top_n and _item_mode(row) == mode and (value := _float(row.get("turnover_rate"))) is not None
        ]
        output.append(
            StrategyShadowTopNEvidence(
                top_n=cast(Any, top_n),
                status=_availability(item.get("status")),
                sample_size=_optional_int(item.get("sample_size")),
                independent_session_count=_optional_int(item.get("independent_session_count")),
                gross_return=_first_float(item, "session_average_return", "average_return"),
                net_return=_float(execution.get("average_net_return")),
                cost_drag=_float(execution.get("average_cost_drag")),
                turnover_rate=(sum(turnover_values) / len(turnover_values) if turnover_values else None),
                insufficient_reasons=[str(value) for value in _sequence(item.get("insufficient_reasons"))][:20],
            )
        )
    return output


def _shadow_rank_delta(
    candidate: Mapping[str, object],
    *,
    shadow: Mapping[str, object],
) -> StrategyShadowRankDeltaEvidence:
    raw = _shadow_rank_delta_source(candidate, shadow=shadow)
    if not raw:
        return StrategyShadowRankDeltaEvidence(
            status="unavailable",
            reasons=["离线 artifact 未持久化 v4-v5.x 逐股排名差；不会用 0 表示未比较"],
        )
    values = (
        _first_float(raw, "mean_rank_delta", "mean_delta"),
        _first_float(raw, "median_rank_delta", "median_delta"),
        _first_float(
            raw,
            "mean_absolute_rank_delta",
            "mean_absolute_delta",
            "mean_absolute",
        ),
        _first_float(
            raw,
            "maximum_absolute_rank_delta",
            "max_absolute_rank_delta",
            "max_absolute",
        ),
    )
    explicit_status = raw.get("status")
    status = _availability(explicit_status) if explicit_status is not None else ("available" if any(value is not None for value in values) else "unavailable")
    return _shadow_rank_delta_evidence(raw, status=status, values=values)


def _shadow_rank_delta_source(
    candidate: Mapping[str, object],
    *,
    shadow: Mapping[str, object],
) -> Mapping[str, object]:
    direct = _mapping(candidate.get("rank_delta_vs_production"))
    if direct:
        return direct
    shadow_direct = _mapping(shadow.get("rank_delta_vs_production"))
    if shadow_direct:
        return shadow_direct
    comparison = _mapping(candidate.get("production_comparison"))
    return _mapping(comparison.get("rank_delta")) or comparison


def _shadow_rank_delta_evidence(
    raw: Mapping[str, object],
    *,
    status: EvidenceAvailability,
    values: tuple[float | None, float | None, float | None, float | None],
) -> StrategyShadowRankDeltaEvidence:
    overlap = _mapping(raw.get("top_n_overlap"))
    reasons = [str(item) for key in ("unavailable_reasons", "insufficient_reasons", "reasons") for item in _sequence(raw.get(key))]
    return StrategyShadowRankDeltaEvidence(
        status=status,
        compared_run_count=_optional_int(raw.get("compared_run_count")),
        compared_item_count=_first_optional_int(
            raw,
            "compared_item_count",
            "common_symbol_count",
        ),
        candidate_ranking_count=_optional_int(raw.get("candidate_ranking_count")),
        production_ranking_count=_optional_int(raw.get("production_ranking_count")),
        common_symbol_count=_optional_int(raw.get("common_symbol_count")),
        missing_candidate_count=_first_optional_int(
            raw,
            "missing_candidate_count",
            "missing_candidate_symbol_count",
            "missing_from_candidate_count",
        ),
        missing_production_count=_first_optional_int(
            raw,
            "missing_production_count",
            "missing_production_symbol_count",
            "missing_from_production_count",
        ),
        mean_rank_delta=values[0],
        median_rank_delta=values[1],
        mean_absolute_rank_delta=values[2],
        maximum_absolute_rank_delta=values[3],
        top20_overlap_ratio=_overlap_ratio(raw, overlap, 20),
        top50_overlap_ratio=_overlap_ratio(raw, overlap, 50),
        top100_overlap_ratio=_overlap_ratio(raw, overlap, 100),
        reasons=list(dict.fromkeys(reasons))[:20],
    )


def _shadow_constraints(gate: Mapping[str, object]) -> StrategyShadowConstraintEvidence:
    criteria = _mapping(gate.get("criteria"))
    selected = {
        name: _mapping(value)
        for name, value in criteria.items()
        if any(token in str(name) for token in ("hysteresis", "constraint", "cost", "capacity", "tradability"))
    }
    if not selected:
        return StrategyShadowConstraintEvidence(
            status="unavailable",
            reasons=["离线 artifact 未记录候选约束门禁"],
        )
    failed = [name for name, item in selected.items() if item.get("passed") is not True]
    turnover = _mapping(selected.get("hysteresis_turnover_top100"))
    return StrategyShadowConstraintEvidence(
        status="available",
        passed=not failed,
        hysteresis_turnover_rate=_float(turnover.get("observed")),
        failed_constraints=failed[:20],
    )


def _shadow_exposure(
    candidate: Mapping[str, object],
    *,
    gate: Mapping[str, object],
) -> StrategyShadowExposureEvidence:
    records = _sequence_of_mappings(candidate.get("exposure_audit"))
    criterion = _mapping(_mapping(gate.get("criteria")).get("board_industry_liquidity_exposure"))
    differences = [
        abs(value)
        for record in records
        for dimension in ("board", "industry", "liquidity")
        for group in _sequence_of_mappings(record.get(dimension))
        if (value := _float(group.get("share_difference"))) is not None
    ]
    observed = _float(criterion.get("observed"))
    if observed is None and differences:
        observed = max(differences)
    threshold = _float(_mapping(criterion.get("threshold")).get("maximum_absolute_share_difference"))
    if not criterion and not records:
        return StrategyShadowExposureEvidence(
            status="unavailable",
            reasons=["离线 artifact 未记录候选板块、行业与流动性暴露审计"],
        )
    return StrategyShadowExposureEvidence(
        status="available",
        passed=(bool(criterion.get("passed")) if criterion else None),
        record_count=len(records) if records else None,
        maximum_absolute_share_difference=observed,
        threshold=threshold,
    )


def _shadow_promotion_gate(
    gate: Mapping[str, object],
) -> StrategyShadowPromotionGateEvidence:
    if not gate:
        return StrategyShadowPromotionGateEvidence(
            status="unavailable",
            reasons=["该版离线 artifact 未记录逐候选晋级门禁"],
        )
    return StrategyShadowPromotionGateEvidence(
        status="available",
        gate_version=_optional_text(gate.get("gate_version")),
        decision=_optional_text(gate.get("decision")),
        passed=(gate.get("passed") is True),
        failed_criteria=[str(item) for item in _sequence(gate.get("failed_criteria"))][:30],
    )


def _promotion(report: Mapping[str, object]) -> StrategyPromotionEvidence:
    raw = _mapping(report.get("promotion"))
    multiple = _mapping(raw.get("multiple_testing_control"))
    pbo = _mapping(multiple.get("pbo"))
    dsr = _mapping(multiple.get("deflated_sharpe_ratio"))
    observed = _int(raw.get("observed_independent_session_count"))
    required = max(1, _int(raw.get("required_independent_session_count"), 20))
    integrity = bool(raw.get("point_in_time_input_integrity_verified"))
    multiple_testing_ready = bool(multiple.get("ready"))
    blockers = [str(item) for item in _sequence(raw.get("blocking_reasons"))][:30]
    if observed < required:
        blockers.append(f"独立交易日仅 {observed}/{required}，不足以支持晋级")
    if not integrity:
        blockers.append("离线报告未提供可晋级的时点输入完整性证明")
    if not multiple_testing_ready:
        blockers.append("配对块检验/BH-FDR 尚未就绪")
    blockers.extend(_manual_review_blockers(report, raw))
    eligible = not blockers and bool(raw.get("eligible_for_human_review"))
    return StrategyPromotionEvidence(
        eligible_for_manual_review=eligible,
        observed_independent_session_count=observed,
        required_independent_session_count=required,
        point_in_time_input_integrity_verified=integrity,
        multiple_testing_method=str(multiple.get("method") or "deterministic-circular-moving-block-bootstrap-plus-benjamini-hochberg-fdr"),
        multiple_testing_ready=multiple_testing_ready,
        # Kept as a backward-compatible field. PBO is explicitly not computed.
        pbo_ready=False,
        pbo_status=cast(Any, pbo.get("status") or "not_computed"),
        deflated_sharpe_status=cast(Any, dsr.get("status") or "not_computed"),
        blockers=list(dict.fromkeys(blockers))[:30],
        conclusion=str(raw.get("conclusion") or "证据不足，不得晋级生产评分。"),
    )


def _manual_review_blockers(
    report: Mapping[str, object],
    promotion: Mapping[str, object],
) -> list[str]:
    blockers: list[str] = []
    candidates = _mapping(report.get("candidates"))
    gates = _mapping(promotion.get("candidate_gates"))
    candidate_ids = set(candidates)
    if report.get("status") != "ok":
        blockers.append("离线报告状态未达到 ok")
    if promotion.get("automatic_promotion") is not False:
        blockers.append("离线报告必须明确禁止自动晋级")
    if not candidate_ids or set(gates) != candidate_ids:
        blockers.append("候选与逐候选晋级门禁不完整")
    passed = _verified_passed_candidates(candidates, gates, blockers)
    declared = [str(item) for item in _sequence(promotion.get("eligible_candidates"))]
    if len(declared) != len(set(declared)) or set(declared) != passed:
        blockers.append("eligible_candidates 无法由逐候选门禁重建")
    if bool(promotion.get("eligible_for_human_review")) != bool(passed):
        blockers.append("人工复核聚合标志与逐候选门禁冲突")
    return blockers


def _verified_passed_candidates(
    candidates: Mapping[str, object],
    gates: Mapping[str, object],
    blockers: list[str],
) -> set[str]:
    passed: set[str] = set()
    for candidate_id, raw_gate in gates.items():
        gate = _mapping(raw_gate)
        criteria = _mapping(gate.get("criteria"))
        failed = {name for name, raw_criterion in criteria.items() if _mapping(raw_criterion).get("passed") is not True}
        declared_failed = {str(item) for item in _sequence(gate.get("failed_criteria"))}
        reconstructed = bool(criteria) and not failed
        if gate.get("passed") is not reconstructed or declared_failed != failed:
            blockers.append(f"候选 {candidate_id} 的聚合门禁无法重建")
            continue
        if reconstructed:
            candidate = _mapping(candidates.get(candidate_id))
            if candidate.get("status") != "ok":
                blockers.append(f"候选 {candidate_id} 状态与通过门禁冲突")
                continue
            passed.add(str(candidate_id))
    return passed


def _center_status(
    report: Mapping[str, object],
    promotion: StrategyPromotionEvidence,
) -> EvidenceCenterStatus:
    if promotion.eligible_for_manual_review:
        return "eligible_for_manual_review"
    if str(report.get("status")) == "blocked":
        return "blocked"
    return "insufficient_data"


def _sources(report: Mapping[str, object]) -> list[str]:
    production = _mapping(report.get("production"))
    source = _mapping(production.get("source"))
    values = [
        source.get("ranking_source"),
        source.get("forward_price_source"),
        "frozen_point_in_time_evidence",
    ]
    return list(dict.fromkeys(str(item) for item in values if item))[:30]


def _freshness_notes(
    row: sqlite3.Row | None,
    *,
    report: Mapping[str, object],
) -> list[str]:
    baseline_generated_at = _optional_text(report.get("generated_at")) or "未知"
    baseline = f"跨日期统计来自离线只读评估快照（生成于 {baseline_generated_at}）；" "刷新本页不会在线重算。"
    if row is None:
        return [baseline, "该策略版本尚无执行快照；先执行最新扫描或历史时点回放。"]
    return [
        baseline,
        f"组合证据冻结于 data_as_of={row['data_as_of']}，数据日={row['data_date']}。",
        "证据中心只读取已发布扫描和其后实际持久化的完整交易日，不读取未来数据。",
    ]


def _limitations(report: Mapping[str, object], *, has_execution: bool) -> list[str]:
    production = _mapping(report.get("production"))
    limitations = [str(item) for item in _sequence(production.get("limitations"))]
    prefix = [
        "跨日期统计是生产评分/Shadow 的基线证据，不等同于当前自定义 StrategySpec 已被验证有效。",
        "Alpha、confidence、risk、tradability 是序数研究分，不是收益概率。",
        "不连接券商、不自动下单，也不提供自动晋级生产评分的入口。",
    ]
    if not has_execution:
        prefix.append("当前策略版本尚无独立执行证据。")
    return list(dict.fromkeys([*prefix, *limitations]))[:30]


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _load_offline_evaluation_report() -> Mapping[str, object]:
    try:
        value = decode_json_bytes(read_regular_file(_OFFLINE_REPORT, max_bytes=_OFFLINE_REPORT_MAX_BYTES))
    except ArtifactIOError as exc:
        raise RuntimeError("离线证据报告读取或 JSON 完整性校验失败，请重新生成") from exc
    if not isinstance(value, dict):
        raise RuntimeError("离线证据报告根节点必须是对象，请重新生成")
    _verify_offline_evaluation_report(value)
    return value


def _verify_offline_evaluation_report(report: Mapping[str, object]) -> None:
    if set(report) != _OFFLINE_REPORT_KEYS:
        raise RuntimeError("离线证据报告字段集合与 compact v2 合同不一致")
    if _stable_digest(report) != _OFFLINE_REPORT_DIGEST:
        raise RuntimeError("离线证据报告内容摘要与保留基线不一致")
    if report.get("schema_version") != "market-scan-shadow-comparison-v2":
        raise RuntimeError("离线证据报告 schema 版本不受支持")
    if report.get("status") not in {"ok", "insufficient_data", "blocked"}:
        raise RuntimeError("离线证据报告状态无效")
    projection = _mapping(report.get("artifact_projection"))
    if projection.get("schema_version") != "market-scan-shadow-comparison-compact-v1":
        raise RuntimeError("离线证据报告 projection schema 版本不受支持")
    generated_at = _aware_datetime(report.get("generated_at"))
    if generated_at is None:
        raise RuntimeError("离线证据报告 generated_at 必须包含时区")
    _verify_offline_candidate_sets(report)


def _verify_offline_candidate_sets(report: Mapping[str, object]) -> None:
    candidates = _mapping(report.get("candidates"))
    promotion = _mapping(report.get("promotion"))
    gates = _mapping(promotion.get("candidate_gates"))
    multiple = _mapping(promotion.get("multiple_testing_control"))
    results = _mapping(multiple.get("candidate_results"))
    candidate_ids = set(candidates)
    if not candidate_ids or set(gates) != candidate_ids or set(results) != candidate_ids:
        raise RuntimeError("离线证据报告候选、门禁与多重检验集合不一致")
    if _int(multiple.get("candidate_count"), -1) != len(candidate_ids):
        raise RuntimeError("离线证据报告多重检验候选计数不一致")
    derived = _promotion(report)
    if derived.eligible_for_manual_review != bool(promotion.get("eligible_for_human_review")):
        raise RuntimeError("离线证据报告人工复核状态无法由逐候选证据重建")
    if promotion.get("automatic_promotion") is not False:
        raise RuntimeError("离线证据报告不得开启自动晋级")


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _item_mode(item: Mapping[str, object]) -> object:
    return item.get("mode") or _mapping(item.get("dimensions")).get("mode")


def _is_full_market_contract(item: Mapping[str, object]) -> bool:
    dimensions = _mapping(item.get("dimensions"))
    return len(dimensions) == 3 and dimensions.get("scope") != "TOP100快速更新评分"


def _optional_text(value: object) -> str | None:
    return str(value) if value not in {None, ""} else None


def _availability(value: object) -> EvidenceAvailability:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"ok", "available", "eligible_for_manual_review"}:
        return "available"
    if normalized in {"insufficient", "insufficient_data", "blocked"}:
        return "insufficient_data"
    return "unavailable"


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _sequence_of_mappings(value: object) -> list[Mapping[str, object]]:
    return [item for item in _sequence(value) if isinstance(item, Mapping)]


def _int(value: object, default: int = 0) -> int:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return default


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _first_optional_int(value: Mapping[str, object], *keys: str) -> int | None:
    for key in keys:
        parsed = _optional_int(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _float(value: object) -> float | None:
    try:
        return float(cast(Any, value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_float(value: Mapping[str, object], *keys: str) -> float | None:
    for key in keys:
        parsed = _float(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _overlap_ratio(
    raw: Mapping[str, object],
    overlap: Mapping[str, object],
    top_n: int,
) -> float | None:
    direct = _first_float(
        raw,
        f"top{top_n}_overlap_ratio",
        f"top_{top_n}_overlap_ratio",
        f"top{top_n}_overlap",
        f"top_{top_n}_overlap",
    )
    if direct is not None:
        return direct
    return _first_float(overlap, str(top_n), f"top{top_n}", f"top_{top_n}")


def _float_list(value: object) -> list[float]:
    return [number for item in _sequence(value) if (number := _float(item)) is not None][:2]


def _stable_digest(value: object) -> str:
    return strategy_evidence_digest(value)


__all__ = ["StrategyEvidenceService"]
