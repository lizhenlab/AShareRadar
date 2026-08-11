"""Evidence-center orchestration over a retained offline evaluation artifact.

The expensive cross-date evaluator intentionally stays out of the request path. Its
report is generated explicitly by ``tools/evaluate_market_scan_shadow.py`` and this
service only compacts that immutable baseline together with one strategy execution.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence, cast

from app.models.strategy_evidence import (
    EvidenceCenterStatus,
    StrategyDimensionEvidence,
    StrategyEvidenceCenter,
    StrategyEvidenceCoverage,
    StrategyEvidenceExecution,
    StrategyPromotionEvidence,
    StrategyRankEvidence,
    StrategyShadowEvidence,
    StrategyTopNEvidence,
)
from app.models.strategy_execution import PortfolioCandidate, PortfolioDraftSummary
from app.repositories.strategy_evidence import StrategyEvidenceRepository
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
_OFFLINE_REPORT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "research"
    / "FULL_MARKET_SELECTION_SHADOW_V5_2026.json"
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
        execution_row, summary, candidates = self.repository.latest_execution(
            strategy_id,
            strategy.strategy_version,
            mode,
        )
        generated_at = audit_now_text()
        promotion = _promotion(report)
        status = _center_status(report, promotion)
        payload: dict[str, object] = {
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
            "execution": _execution_evidence(
                execution_row,
                summary,
                candidates,
            ).model_dump(mode="json"),
            "coverage": [item.model_dump(mode="json") for item in _coverage(candidates)],
            "dimensions": [item.model_dump(mode="json") for item in _dimension_evidence(candidates)],
            "top_n": [item.model_dump(mode="json") for item in _top_n(report, mode=mode)],
            "rank_evidence": [
                item.model_dump(mode="json") for item in _rank_evidence(report, mode=mode)
            ],
            "exposure_audit": _exposure_audit(report, mode=mode),
            "shadow_candidates": [item.model_dump(mode="json") for item in _shadow(report)],
            "promotion": promotion.model_dump(mode="json"),
            "data_sources": _sources(report),
            "freshness_notes": _freshness_notes(execution_row, report=report),
            "limitations": _limitations(report, has_execution=execution_row is not None),
        }
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


def _execution_evidence(
    row: sqlite3.Row | None,
    summary: PortfolioDraftSummary | None,
    candidates: Sequence[PortfolioCandidate],
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
        data_as_of=str(row["data_as_of"]),
        data_date=str(row["data_date"]),
        cost_rule_fingerprint=str(row["cost_rule_fingerprint"]),
        evidence_digest_verified=reconstructed == str(row["result_digest"]),
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
    records = [
        item
        for item in _sequence_of_mappings(production.get("exposure_audit"))
        if _item_mode(item) in {None, mode}
    ]
    return {
        "status": "available" if records else "insufficient_data",
        "latest": records[-1] if records else {},
        "record_count": len(records),
    }


def _shadow(report: Mapping[str, object]) -> list[StrategyShadowEvidence]:
    records = []
    for candidate_id, raw in _mapping(report.get("candidates")).items():
        candidate = _mapping(raw)
        source = _mapping(candidate.get("source"))
        shadow = _mapping(candidate.get("shadow"))
        integrity = _mapping(shadow.get("input_integrity"))
        records.append(
            StrategyShadowEvidence(
                candidate_id=str(candidate_id),
                status=str(candidate.get("status") or "insufficient_data"),
                spec_hash=str(shadow.get("spec_hash")) if shadow.get("spec_hash") else None,
                point_in_time_integrity_verified=bool(
                    integrity.get("eligible_for_promotion_evidence") is True
                ),
                independent_session_count=_int(source.get("independent_session_count")),
            )
        )
    return records[:20]


def _promotion(report: Mapping[str, object]) -> StrategyPromotionEvidence:
    raw = _mapping(report.get("promotion"))
    multiple = _mapping(raw.get("multiple_testing_control"))
    observed = _int(raw.get("observed_independent_session_count"))
    required = max(1, _int(raw.get("required_independent_session_count"), 20))
    integrity = bool(raw.get("point_in_time_input_integrity_verified"))
    pbo_ready = bool(multiple.get("ready"))
    blockers = [str(item) for item in _sequence(raw.get("blocking_reasons"))][:30]
    if observed < required:
        blockers.append(f"独立交易日仅 {observed}/{required}，不足以支持晋级")
    if not integrity:
        blockers.append("离线报告未提供可晋级的时点输入完整性证明")
    if not pbo_ready:
        blockers.append("多重检验/PBO 尚未就绪")
    return StrategyPromotionEvidence(
        eligible_for_manual_review=bool(raw.get("eligible_for_human_review")),
        observed_independent_session_count=observed,
        required_independent_session_count=required,
        point_in_time_input_integrity_verified=integrity,
        multiple_testing_method=str(multiple.get("method") or "preregistered-ablation-plus-PBO-before-promotion"),
        pbo_ready=pbo_ready,
        blockers=list(dict.fromkeys(blockers))[:30],
        conclusion=str(raw.get("conclusion") or "证据不足，不得晋级生产评分。"),
    )


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
    baseline = (
        f"跨日期统计来自离线只读评估快照（生成于 {baseline_generated_at}）；"
        "刷新本页不会在线重算。"
    )
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
        value = json.loads(_OFFLINE_REPORT.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            "离线证据报告不存在；请先运行 tools/evaluate_market_scan_shadow.py 生成报告"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("离线证据报告不是有效 JSON，请重新生成") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError("离线证据报告根节点必须是对象，请重新生成")
    return value


def _item_mode(item: Mapping[str, object]) -> object:
    return item.get("mode") or _mapping(item.get("dimensions")).get("mode")


def _optional_text(value: object) -> str | None:
    return str(value) if value not in {None, ""} else None


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _sequence_of_mappings(value: object) -> list[Mapping[str, object]]:
    return [item for item in _sequence(value) if isinstance(item, Mapping)]


def _int(value: object, default: int = 0) -> int:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return default


def _float(value: object) -> float | None:
    try:
        return float(cast(Any, value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_list(value: object) -> list[float]:
    return [number for item in _sequence(value) if (number := _float(item)) is not None][:2]


def _stable_digest(value: object) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


__all__ = ["StrategyEvidenceService"]
