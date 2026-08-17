"""Strict contracts for the read-only executable-candidate shadow projection."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from math import isclose
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.strategy_lab import StrategySpecInput


class _StrictShadowModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class ExecutableShadowRunEvidence(_StrictShadowModel):
    run_id: int = Field(ge=1)
    status: Literal["success", "degraded"]
    mode: Literal["official"]
    scope: str = Field(min_length=1)
    data_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    quote_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    scan_rule_version: str = Field(min_length=1)
    production_score_rule_version: str = Field(min_length=1)
    production_score_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_count: int = Field(ge=0)
    successful_result_count: int = Field(ge=0)
    verified_point_in_time_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.successful_result_count > self.result_count:
            raise ValueError("成功结果数量不能超过冻结结果总数")
        if self.verified_point_in_time_count > self.successful_result_count:
            raise ValueError("可验证时点证据数量不能超过成功结果数量")
        return self


class ExecutableShadowGatePolicy(_StrictShadowModel):
    exclude_st: Literal[True] = True
    exclude_new: Literal[True] = True
    suspension_evidence: Literal["frozen_daily_amount_and_reason_proxy"]
    price_limit_evidence: Literal["frozen_daily_single_price_proxy"]
    minimum_listing_days: int = Field(ge=0)
    minimum_history_sessions: int = Field(ge=61)
    minimum_amount_cny: float = Field(ge=0)
    minimum_tradability_score: float = Field(ge=0, le=100)
    maximum_risk_score: float = Field(ge=0, le=100)
    adv_evidence_status: Literal["unavailable"] = "unavailable"
    capacity_basis: Literal["frozen_session_amount_participation_proxy"]
    maximum_notional_share_of_session_amount: float = Field(gt=0, le=0.05)


class ExecutableShadowExposureAudit(_StrictShadowModel):
    selected_count: int = Field(ge=0)
    selected_weight: float = Field(ge=0, le=1)
    top10_weight: float = Field(ge=0, le=1)
    industry_weights: dict[str, float]
    board_weights: dict[str, float]
    average_risk_score: float | None = Field(default=None, ge=0, le=100)
    average_tradability_score: float | None = Field(default=None, ge=0, le=100)
    estimated_round_trip_cost_cny: float = Field(ge=0)
    estimated_turnover: float = Field(ge=0, le=2)


class ExecutableShadowSummary(_StrictShadowModel):
    status: Literal["ready", "no_trade", "blocked"]
    no_trade: bool
    no_trade_reasons: list[str]
    evaluated_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    adjusted_count: int = Field(ge=0)
    unfilled_count: int = Field(ge=0)
    target_invested_weight: float = Field(ge=0, le=1)
    estimated_turnover: float = Field(ge=0, le=2)
    estimated_round_trip_cost_cny: float = Field(ge=0)
    residual_cash_cny: float = Field(ge=0)
    evidence_verified_count: int = Field(ge=0)
    replacement_attempt_count: int = Field(ge=0)
    pool_exhausted: bool
    underinvested_reason: str | None = None
    notes: list[str]


class ExecutableShadowCandidate(_StrictShadowModel):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    code: str = Field(pattern=r"^\d{6}$")
    name: str
    board: str
    industry: str | None = None
    original_rank: int | None = Field(default=None, ge=1)
    utility_rank: int | None = Field(default=None, ge=1)
    utility_score: float | None = Field(default=None, ge=0, le=100)
    alpha_1d: float | None = Field(default=None, ge=0, le=100)
    alpha_5d: float | None = Field(default=None, ge=0, le=100)
    alpha_20d: float | None = Field(default=None, ge=0, le=100)
    confidence: float | None = Field(default=None, ge=0, le=100)
    risk: float | None = Field(default=None, ge=0, le=100)
    tradability: float | None = Field(default=None, ge=0, le=100)
    status: Literal["selected", "rejected", "constraint_adjusted", "unfilled"]
    target_weight: float = Field(ge=0, le=1)
    target_quantity: int = Field(ge=0)
    estimated_gross_amount_cny: float = Field(ge=0)
    estimated_round_trip_cost_cny: float = Field(ge=0)
    evidence_verified: bool
    hard_filter_failures: list[str]
    reasons: list[str]
    rank_change_reason: str


class ExecutableCandidateShadowReport(_StrictShadowModel):
    schema_version: Literal["market-scan-executable-candidate-shadow-v2"] = "market-scan-executable-candidate-shadow-v2"
    status: Literal["research_shadow"] = "research_shadow"
    efficacy_status: Literal["not_generated"] = "not_generated"
    production_effect: Literal["none"] = "none"
    production_ranking_mutated: Literal[False] = False
    database_write_performed: Literal[False] = False
    evidence: ExecutableShadowRunEvidence
    strategy_contract_version: Literal["executable-candidate-shadow-spec-v2"] = "executable-candidate-shadow-spec-v2"
    strategy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_spec: StrategySpecInput
    gate_policy: ExecutableShadowGatePolicy
    summary: ExecutableShadowSummary
    selected: list[ExecutableShadowCandidate] = Field(max_length=100)
    candidate_preview: list[ExecutableShadowCandidate] = Field(max_length=100)
    candidate_total: int = Field(ge=0)
    exposure_audit: ExecutableShadowExposureAudit
    draft_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    limitations: list[str] = Field(min_length=1, max_length=30)
    canonical_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_shadow_boundary(self) -> Self:
        _validate_shadow_counts(self)
        _validate_shadow_selected(self)
        _validate_shadow_summary(self)
        if self.canonical_digest != executable_candidate_shadow_digest(self):
            raise ValueError("可执行候选 Shadow 摘要校验失败")
        return self


def _validate_shadow_counts(report: ExecutableCandidateShadowReport) -> None:
    summary = report.summary
    if report.candidate_total < len(report.candidate_preview):
        raise ValueError("候选总数不能小于预览数量")
    if report.candidate_total != report.evidence.result_count:
        raise ValueError("候选总数必须与冻结结果总数一致")
    if report.candidate_total != summary.evaluated_count:
        raise ValueError("候选总数必须与组合评估数量一致")
    if summary.rejected_count + summary.selected_count + summary.unfilled_count != summary.evaluated_count:
        raise ValueError("组合候选状态计数不能重建评估总数")
    if summary.adjusted_count > summary.selected_count:
        raise ValueError("约束调整数量不能超过入选数量")


def _validate_shadow_selected(report: ExecutableCandidateShadowReport) -> None:
    summary = report.summary
    exposure = report.exposure_audit
    if len({item.symbol for item in report.selected}) != len(report.selected):
        raise ValueError("Shadow 入选列表不能包含重复股票")
    if any(item.status not in {"selected", "constraint_adjusted"} for item in report.selected):
        raise ValueError("Shadow 入选列表只能包含已入选或约束调整股票")
    if (summary.selected_count, exposure.selected_count) != (len(report.selected), len(report.selected)):
        raise ValueError("组合摘要、暴露审计与入选列表数量必须一致")
    if summary.evidence_verified_count != report.evidence.verified_point_in_time_count:
        raise ValueError("组合摘要与批次证据的已验证数量必须一致")


def _validate_shadow_summary(report: ExecutableCandidateShadowReport) -> None:
    summary = report.summary
    exposure = report.exposure_audit
    comparisons = (
        (summary.target_invested_weight, exposure.selected_weight, "入选权重"),
        (summary.estimated_turnover, exposure.estimated_turnover, "预计换手"),
        (
            summary.estimated_round_trip_cost_cny,
            exposure.estimated_round_trip_cost_cny,
            "预计往返成本",
        ),
    )
    for left, right, label in comparisons:
        if not isclose(left, right, rel_tol=0, abs_tol=1e-8):
            raise ValueError(f"组合摘要与暴露审计的{label}必须一致")
    if summary.no_trade != (summary.status == "no_trade"):
        raise ValueError("no_trade 标记必须与组合状态一致")
    if summary.pool_exhausted and summary.underinvested_reason is None:
        raise ValueError("候选池耗尽时必须给出未充分投资原因")
    if summary.target_invested_weight < 0.999999 and summary.underinvested_reason is None:
        raise ValueError("未充分投资时必须给出结构化原因")


def executable_candidate_shadow_digest(
    value: BaseModel | Mapping[str, object],
) -> str:
    """Return the content digest while excluding the digest field itself."""

    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = deepcopy(dict(value))
    else:
        raise TypeError("可执行候选 Shadow 摘要输入必须是模型或映射")
    payload.pop("canonical_digest", None)
    return sha256_hex(canonical_json_bytes(payload))


__all__ = [
    "ExecutableCandidateShadowReport",
    "ExecutableShadowCandidate",
    "ExecutableShadowExposureAudit",
    "ExecutableShadowGatePolicy",
    "ExecutableShadowRunEvidence",
    "ExecutableShadowSummary",
    "executable_candidate_shadow_digest",
]
