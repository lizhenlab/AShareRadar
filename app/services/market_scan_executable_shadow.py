"""Read-only executable-candidate projection over one frozen full-market run."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal, Protocol, cast

from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MarketScanRun,
)
from app.models.market_scan_executable_shadow import (
    ExecutableCandidateShadowReport,
    ExecutableShadowCandidate,
    ExecutableShadowExposureAudit,
    ExecutableShadowGatePolicy,
    ExecutableShadowRunEvidence,
    executable_candidate_shadow_digest,
)
from app.models.market_scan_snapshot import (
    FrozenFullMarketSnapshotIntegrity,
    validate_frozen_full_market_snapshot,
)
from app.models.strategy_execution import (
    PortfolioCandidate,
    StrategyExecutionMarketScanMode,
    StrategyExecutionRequest,
)
from app.models.strategy_lab import (
    StrategyEvidencePolicy,
    StrategyExecutionPolicy,
    StrategyExclusions,
    StrategyHardFilter,
    StrategyObjectives,
    StrategyPortfolioConstraints,
    StrategyRebalancePolicy,
    StrategySpec,
    StrategySpecInput,
)
from app.repositories.strategy_execution import FrozenMarketScan
from app.services.strategy_compiler import strategy_spec_fingerprint
from app.services.strategy_portfolio import PortfolioComputation, build_portfolio_draft


EXECUTABLE_SHADOW_MINIMUM_AMOUNT_CNY = 100_000_000.0
EXECUTABLE_SHADOW_MINIMUM_TRADABILITY = 55.0
EXECUTABLE_SHADOW_MAXIMUM_RISK = 55.0
EXECUTABLE_SHADOW_MAXIMUM_AMOUNT_SHARE = 0.001


class ExecutableShadowRepositoryProtocol(Protocol):
    def frozen_scan(
        self,
        *,
        run_id: int | None,
        data_date: str | None,
        mode: StrategyExecutionMarketScanMode,
    ) -> FrozenMarketScan: ...


class MarketScanExecutableShadowService:
    """Build a research-only projection without saving an execution or changing ranks."""

    def __init__(self, repository: ExecutableShadowRepositoryProtocol) -> None:
        self._repository = repository

    def project(
        self,
        run_id: int,
        *,
        notional_cash_cny: float = 1_000_000.0,
    ) -> ExecutableCandidateShadowReport:
        frozen = self._repository.frozen_scan(
            run_id=run_id,
            data_date=None,
            mode="official",
        )
        integrity = validate_frozen_full_market_snapshot(frozen.run, frozen.items)
        spec = executable_candidate_shadow_spec()
        fingerprint = strategy_spec_fingerprint(spec)
        strategy = _virtual_strategy(spec, fingerprint=fingerprint, run=frozen.run)
        computation = build_portfolio_draft(
            strategy,
            frozen.run,
            frozen.items,
            StrategyExecutionRequest(
                strategy_id=1,
                kind="historical_replay",
                run_id=run_id,
                mode="official",
                notional_cash_cny=notional_cash_cny,
            ),
        )
        return _shadow_report(
            frozen,
            spec=spec,
            fingerprint=fingerprint,
            computation=computation,
            integrity=integrity,
        )


def _shadow_report(
    frozen: FrozenMarketScan,
    *,
    spec: StrategySpecInput,
    fingerprint: str,
    computation: PortfolioComputation,
    integrity: FrozenFullMarketSnapshotIntegrity,
) -> ExecutableCandidateShadowReport:
    selected = [item for item in computation.candidates if item.status in {"selected", "constraint_adjusted"}]
    payload = {
        "schema_version": "market-scan-executable-candidate-shadow-v2",
        "status": "research_shadow",
        "efficacy_status": "not_generated",
        "production_effect": "none",
        "production_ranking_mutated": False,
        "database_write_performed": False,
        "evidence": _run_evidence(
            frozen.run,
            computation.candidates,
            integrity,
        ).model_dump(mode="json"),
        "strategy_contract_version": "executable-candidate-shadow-spec-v2",
        "strategy_fingerprint": fingerprint,
        "strategy_spec": spec.model_dump(mode="json"),
        "gate_policy": _gate_policy(spec).model_dump(mode="json"),
        "summary": computation.summary.model_dump(mode="json"),
        "selected": [_shadow_candidate(item).model_dump(mode="json") for item in selected],
        "candidate_preview": [_shadow_candidate(item).model_dump(mode="json") for item in computation.candidates[:100]],
        "candidate_total": len(computation.candidates),
        "exposure_audit": _exposure_audit(
            selected,
            computation.summary.estimated_turnover,
        ).model_dump(mode="json"),
        "draft_result_digest": computation.result_digest,
        "limitations": _limitations(),
    }
    payload["canonical_digest"] = executable_candidate_shadow_digest(payload)
    return ExecutableCandidateShadowReport.model_validate(payload)


def executable_candidate_shadow_spec() -> StrategySpecInput:
    """Return the immutable v1 policy used only by the read-only shadow projection."""

    return StrategySpecInput(
        name="全市场可执行候选榜 Shadow v1",
        description=("先应用冻结时点证据、ST/新股、日线停牌与涨跌停代理、流动性和容量约束，" "再计算多目标研究顺序；不修改生产趋势榜。"),
        exclusions=_shadow_exclusions(),
        hard_filters=_shadow_hard_filters(),
        profile="custom",
        objectives=_shadow_objectives(),
        portfolio_constraints=_shadow_portfolio_constraints(),
        rebalance_policy=StrategyRebalancePolicy(
            hold_sessions=5,
            cadence="manual",
            rebalance_every_sessions=5,
        ),
        execution_policy=StrategyExecutionPolicy(
            cost_profile="conservative",
            buy_slippage_bps=10.0,
            sell_slippage_bps=10.0,
        ),
        evidence_policy=StrategyEvidencePolicy(
            minimum_quality_score=80,
            maximum_market_data_age_days=1,
            require_verified_point_in_time_evidence=True,
        ),
    )


def _shadow_exclusions() -> StrategyExclusions:
    return StrategyExclusions(
        exclude_st=True,
        exclude_new=True,
        min_listing_days=120,
        exclude_suspended=True,
        min_history_sessions=61,
        min_data_quality_score=80,
        min_amount_cny=EXECUTABLE_SHADOW_MINIMUM_AMOUNT_CNY,
    )


def _shadow_hard_filters() -> list[StrategyHardFilter]:
    return [
        StrategyHardFilter(
            field="risk",
            operator="lte",
            value=EXECUTABLE_SHADOW_MAXIMUM_RISK,
        ),
        StrategyHardFilter(
            field="tradability",
            operator="gte",
            value=EXECUTABLE_SHADOW_MINIMUM_TRADABILITY,
        ),
    ]


def _shadow_objectives() -> StrategyObjectives:
    return StrategyObjectives(
        alpha_1d=0.05,
        alpha_5d=0.20,
        alpha_20d=0.25,
        confidence=0.05,
        risk=0.25,
        tradability=0.20,
    )


def _shadow_portfolio_constraints() -> StrategyPortfolioConstraints:
    return StrategyPortfolioConstraints(
        stock_count=30,
        weighting_method="risk_adjusted",
        max_stock_weight=0.05,
        max_industry_positions=3,
        max_industry_weight=0.20,
        max_board_weight=0.50,
        min_position_amount_cny=10_000.0,
        max_notional_share_of_daily_amount=EXECUTABLE_SHADOW_MAXIMUM_AMOUNT_SHARE,
    )


def _require_frozen_official_full_market(run: MarketScanRun) -> None:
    if run.status not in {"success", "degraded"}:
        raise ValueError("可执行候选 Shadow 只接受已发布的冻结批次")
    if run.mode != "official":
        raise ValueError("可执行候选 Shadow 只接受盘后正式批次")
    if run.scope != MARKET_SCAN_FULL_MARKET_SCOPE:
        raise ValueError("可执行候选 Shadow 只接受完整全市场批次")


def _virtual_strategy(
    spec: StrategySpecInput,
    *,
    fingerprint: str,
    run: MarketScanRun,
) -> StrategySpec:
    return StrategySpec(
        strategy_id=1,
        strategy_version=1,
        revision=1,
        current_revision=1,
        archived=False,
        fingerprint=fingerprint,
        spec=spec,
        created_at=run.as_of,
        updated_at=run.as_of,
        version_created_at=run.as_of,
    )


def _run_evidence(
    run: MarketScanRun,
    candidates: list[PortfolioCandidate],
    integrity: FrozenFullMarketSnapshotIntegrity,
) -> ExecutableShadowRunEvidence:
    verified = sum(item.evidence_verified for item in candidates if item.original_rank is not None)
    return ExecutableShadowRunEvidence(
        run_id=run.id,
        status=cast(Literal["success", "degraded"], run.status),
        mode="official",
        scope=run.scope,
        data_date=run.data_date,
        quote_date=run.quote_date,
        scan_rule_version=run.rule_version,
        production_score_rule_version=integrity.production_score_rule_version,
        production_score_spec_hash=integrity.production_score_spec_hash,
        result_count=integrity.result_count,
        successful_result_count=integrity.success_count,
        verified_point_in_time_count=verified,
    )


def _gate_policy(spec: StrategySpecInput) -> ExecutableShadowGatePolicy:
    exclusions = spec.exclusions
    constraints = spec.portfolio_constraints
    return ExecutableShadowGatePolicy(
        suspension_evidence="frozen_daily_amount_and_reason_proxy",
        price_limit_evidence="frozen_daily_single_price_proxy",
        minimum_listing_days=exclusions.min_listing_days,
        minimum_history_sessions=exclusions.min_history_sessions,
        minimum_amount_cny=exclusions.min_amount_cny,
        minimum_tradability_score=EXECUTABLE_SHADOW_MINIMUM_TRADABILITY,
        maximum_risk_score=EXECUTABLE_SHADOW_MAXIMUM_RISK,
        capacity_basis="frozen_session_amount_participation_proxy",
        maximum_notional_share_of_session_amount=(constraints.max_notional_share_of_daily_amount),
    )


def _exposure_audit(
    selected: list[PortfolioCandidate],
    estimated_turnover: float,
) -> ExecutableShadowExposureAudit:
    industry_weights: defaultdict[str, float] = defaultdict(float)
    board_weights: defaultdict[str, float] = defaultdict(float)
    for item in selected:
        industry_weights[item.industry or "未知行业"] += item.target_weight
        board_weights[item.board] += item.target_weight
    total_weight = sum(item.target_weight for item in selected)
    return ExecutableShadowExposureAudit(
        selected_count=len(selected),
        selected_weight=round(total_weight, 8),
        top10_weight=round(
            sum(sorted((item.target_weight for item in selected), reverse=True)[:10]),
            8,
        ),
        industry_weights={key: round(value, 8) for key, value in sorted(industry_weights.items())},
        board_weights={key: round(value, 8) for key, value in sorted(board_weights.items())},
        average_risk_score=_weighted_score(selected, "risk"),
        average_tradability_score=_weighted_score(selected, "tradability"),
        estimated_round_trip_cost_cny=round(
            sum(item.estimated_round_trip_cost_cny for item in selected),
            2,
        ),
        estimated_turnover=estimated_turnover,
    )


def _weighted_score(
    selected: list[PortfolioCandidate],
    field: Literal["risk", "tradability"],
) -> float | None:
    values = [(float(value), item.target_weight) for item in selected if (value := item.risk if field == "risk" else item.tradability) is not None]
    weight = sum(item[1] for item in values)
    if not values or weight <= 0:
        return None
    return round(sum(value * item_weight for value, item_weight in values) / weight, 8)


def _shadow_candidate(item: PortfolioCandidate) -> ExecutableShadowCandidate:
    return ExecutableShadowCandidate(
        symbol=item.symbol,
        code=item.code,
        name=item.name,
        board=item.board,
        industry=item.industry,
        original_rank=item.original_rank,
        utility_rank=item.utility_rank,
        utility_score=item.utility_score,
        alpha_1d=item.alpha_1d,
        alpha_5d=item.alpha_5d,
        alpha_20d=item.alpha_20d,
        confidence=item.confidence,
        risk=item.risk,
        tradability=item.tradability,
        status=item.status,
        target_weight=item.target_weight,
        target_quantity=item.target_quantity,
        estimated_gross_amount_cny=item.estimated_gross_amount_cny,
        estimated_round_trip_cost_cny=item.estimated_round_trip_cost_cny,
        evidence_verified=item.evidence_verified,
        hard_filter_failures=item.hard_filter_failures,
        reasons=item.reasons,
        rank_change_reason=item.rank_change_reason,
    )


def _limitations() -> list[str]:
    return [
        "该投影完全属于Shadow研究，收益有效性尚未生成，不能作为投资建议或自动交易授权。",
        "来源批次的生产评分与原始排名保持不变；本结果仅提供独立候选顺序。",
        "Alpha、风险、置信和可交易性均为冻结截面序数分，不是收益率或上涨概率。",
        "停牌与一字状态仅使用冻结日K成交额、单一价格和原因文本代理，不能证明盘口排队成交。",
        "当前没有可信的历史ADV序列；容量只按冻结当日成交额参与率估算。",
        "行业分类存在混合粒度；行业权重约束不能替代正式行业中性风险模型。",
        "成本仅为声明的佣金、税费和滑点情景，未建模实时价差、冲击或订单簿深度。",
    ]


__all__ = [
    "MarketScanExecutableShadowService",
    "executable_candidate_shadow_spec",
]
