"""Typed contracts for the evidence-first full-market strategy laboratory."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


STRATEGY_SPEC_SCHEMA_VERSION: Literal[1] = 1

StrategyBoard = Literal["sh_main", "star", "sz_main", "chinext", "beijing"]
StrategyProfile = Literal["conservative", "balanced", "aggressive", "custom"]
StrategyFilterOperator = Literal["eq", "ne", "gt", "gte", "lt", "lte", "between", "in"]
StrategyMetricKind = Literal["number", "boolean", "category"]
StrategyObjectiveDirection = Literal["maximize", "minimize"]
StrategyWeightingMethod = Literal["equal", "risk_adjusted", "custom"]
StrategyCostProfile = Literal["base", "conservative", "stress"]
StrategyRunCadence = Literal["manual", "daily_after_close", "trading_day_intraday"]

NameText = Annotated[str, Field(min_length=1, max_length=80)]
DescriptionText = Annotated[str, Field(max_length=1000)]
MetricName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
SourceName = Annotated[str, Field(min_length=1, max_length=80)]
SymbolText = Annotated[str, Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


def _all_boards() -> list[StrategyBoard]:
    return ["sh_main", "star", "sz_main", "chinext", "beijing"]


class StrategyUniverse(_StrictModel):
    boards: list[StrategyBoard] = Field(default_factory=_all_boards, min_length=1, max_length=5)

    @field_validator("boards")
    @classmethod
    def validate_unique_boards(cls, value: list[StrategyBoard]) -> list[StrategyBoard]:
        if len(value) != len(set(value)):
            raise ValueError("股票板块不能重复")
        return value


class StrategyExclusions(_StrictModel):
    exclude_st: bool = True
    exclude_new: bool = False
    min_listing_days: int = Field(default=120, ge=0, le=10_000)
    exclude_suspended: bool = True
    min_history_sessions: int = Field(default=61, ge=1, le=1_500)
    min_data_quality_score: int = Field(default=70, ge=0, le=100)
    min_amount_cny: float = Field(default=0.0, ge=0, le=1_000_000_000_000_000, allow_inf_nan=False)


FilterScalar = bool | int | float | str
FilterValue = FilterScalar | list[str] | list[int] | list[float]


class StrategyHardFilter(_StrictModel):
    field: MetricName
    operator: StrategyFilterOperator
    value: FilterValue
    period_sessions: int | None = Field(default=None, ge=1, le=1_500)

    @model_validator(mode="after")
    def validate_operator_value_shape(self) -> Self:
        is_list = isinstance(self.value, list)
        if self.operator == "between" and (not isinstance(self.value, list) or len(self.value) != 2):
            raise ValueError("between 过滤条件必须提供两个边界值")
        if self.operator == "in" and (not is_list or not self.value):
            raise ValueError("in 过滤条件必须提供非空列表")
        if self.operator not in {"between", "in"} and is_list:
            raise ValueError(f"{self.operator} 过滤条件不能使用列表值")
        return self


class StrategyObjectives(_StrictModel):
    alpha_1d: float = Field(default=0.05, ge=0, le=1, allow_inf_nan=False)
    alpha_5d: float = Field(default=0.20, ge=0, le=1, allow_inf_nan=False)
    alpha_20d: float = Field(default=0.35, ge=0, le=1, allow_inf_nan=False)
    confidence: float = Field(default=0.15, ge=0, le=1, allow_inf_nan=False)
    risk: float = Field(default=0.15, ge=0, le=1, allow_inf_nan=False)
    tradability: float = Field(default=0.10, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_non_zero_total(self) -> Self:
        if self.total_weight <= 0:
            raise ValueError("至少需要一个非零策略目标权重")
        return self

    @property
    def total_weight(self) -> float:
        return sum(
            (
                self.alpha_1d,
                self.alpha_5d,
                self.alpha_20d,
                self.confidence,
                self.risk,
                self.tradability,
            )
        )


class StrategyPortfolioConstraints(_StrictModel):
    stock_count: int = Field(default=20, ge=1, le=100)
    weighting_method: StrategyWeightingMethod = "equal"
    max_stock_weight: float = Field(default=0.10, gt=0, le=1, allow_inf_nan=False)
    max_industry_positions: int = Field(default=3, ge=1, le=100)
    max_industry_weight: float = Field(default=0.30, gt=0, le=1, allow_inf_nan=False)
    max_board_weight: float = Field(default=0.50, gt=0, le=1, allow_inf_nan=False)
    min_position_amount_cny: float = Field(default=5_000.0, ge=0, le=1_000_000_000, allow_inf_nan=False)
    max_notional_share_of_daily_amount: float = Field(default=0.001, gt=0, le=0.05, allow_inf_nan=False)
    custom_weights: dict[SymbolText, float] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def validate_custom_weights(self) -> Self:
        _validate_weighting_method(self)
        _validate_custom_weight_values(self)
        _validate_weight_capacity(self)
        return self


def _validate_weighting_method(value: StrategyPortfolioConstraints) -> None:
    if value.weighting_method == "custom" and not value.custom_weights:
        raise ValueError("custom 权重方式必须提供 custom_weights")
    if value.weighting_method != "custom" and value.custom_weights:
        raise ValueError("只有 custom 权重方式可以提供 custom_weights")


def _validate_custom_weight_values(value: StrategyPortfolioConstraints) -> None:
    weights = value.custom_weights.values()
    if any(weight <= 0 or weight > 1 for weight in weights):
        raise ValueError("自定义权重必须位于 (0, 1] 区间")
    if any(weight > value.max_stock_weight for weight in value.custom_weights.values()):
        raise ValueError("自定义权重不能超过单股权重上限")
    if sum(value.custom_weights.values()) > 1.0000001:
        raise ValueError("自定义权重合计不能超过 1")


def _validate_weight_capacity(value: StrategyPortfolioConstraints) -> None:
    if len(value.custom_weights) > value.stock_count:
        raise ValueError("自定义权重股票数量不能超过组合股票数")
    if value.weighting_method != "custom" and value.max_stock_weight * value.stock_count < 0.999999:
        raise ValueError("股票数量与单股权重上限无法构成满仓组合")


class StrategyRebalancePolicy(_StrictModel):
    hold_sessions: int = Field(default=5, ge=1, le=60)
    cadence: StrategyRunCadence = "manual"
    rebalance_every_sessions: int = Field(default=5, ge=1, le=60)
    buy_utility_threshold: float = Field(default=0.0, ge=0, le=100, allow_inf_nan=False)
    hold_utility_threshold: float = Field(default=0.0, ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_hysteresis(self) -> Self:
        if self.hold_utility_threshold > self.buy_utility_threshold:
            raise ValueError("持有阈值不能高于买入阈值")
        return self


class StrategyExecutionPolicy(_StrictModel):
    t_plus_one: Literal[True] = True
    respect_price_limits: Literal[True] = True
    respect_suspensions: Literal[True] = True
    cost_profile: StrategyCostProfile = "base"
    commission_rate: float = Field(default=0.0003, ge=0, le=0.02, allow_inf_nan=False)
    minimum_commission_cny: float = Field(default=5.0, ge=0, le=1_000, allow_inf_nan=False)
    sell_stamp_duty_rate: float = Field(default=0.0005, ge=0, le=0.02, allow_inf_nan=False)
    transfer_fee_rate: float = Field(default=0.00001, ge=0, le=0.01, allow_inf_nan=False)
    buy_slippage_bps: float = Field(default=5.0, ge=0, le=1_000, allow_inf_nan=False)
    sell_slippage_bps: float = Field(default=5.0, ge=0, le=1_000, allow_inf_nan=False)


class StrategyEvidencePolicy(_StrictModel):
    minimum_quality_score: int = Field(default=70, ge=0, le=100)
    maximum_market_data_age_days: int = Field(default=1, ge=0, le=365)
    maximum_fundamental_data_age_days: int = Field(default=120, ge=0, le=730)
    allowed_sources: list[SourceName] = Field(default_factory=list, max_length=30)
    blocked_sources: list[SourceName] = Field(default_factory=list, max_length=30)
    require_verified_point_in_time_evidence: bool = True

    @field_validator("allowed_sources", "blocked_sources")
    @classmethod
    def validate_unique_sources(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("证据来源不能重复")
        return value

    @model_validator(mode="after")
    def validate_source_sets(self) -> Self:
        overlap = set(self.allowed_sources) & set(self.blocked_sources)
        if overlap:
            raise ValueError(f"证据来源不能同时允许和禁止：{', '.join(sorted(overlap))}")
        return self


class StrategySpecInput(_StrictModel):
    name: NameText
    description: DescriptionText = ""
    schema_version: Literal[1] = STRATEGY_SPEC_SCHEMA_VERSION
    universe: StrategyUniverse = Field(default_factory=StrategyUniverse)
    exclusions: StrategyExclusions = Field(default_factory=StrategyExclusions)
    hard_filters: list[StrategyHardFilter] = Field(default_factory=list, max_length=30)
    objectives: StrategyObjectives = Field(default_factory=StrategyObjectives)
    profile: StrategyProfile = "balanced"
    portfolio_constraints: StrategyPortfolioConstraints = Field(default_factory=StrategyPortfolioConstraints)
    rebalance_policy: StrategyRebalancePolicy = Field(default_factory=StrategyRebalancePolicy)
    execution_policy: StrategyExecutionPolicy = Field(default_factory=StrategyExecutionPolicy)
    evidence_policy: StrategyEvidencePolicy = Field(default_factory=StrategyEvidencePolicy)

    @model_validator(mode="after")
    def validate_profile_objectives(self) -> Self:
        if self.profile == "custom" and "objectives" not in self.model_fields_set:
            raise ValueError("custom 策略画像必须显式提供 objectives")
        if self.profile != "custom":
            expected = strategy_profile_objectives(self.profile)
            if "objectives" in self.model_fields_set and self.objectives != expected:
                raise ValueError("命名策略画像不能覆盖固定目标权重；请改用 custom")
            self.objectives = expected
        return self

    @field_validator("name", "description")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 and character not in {"\t", "\n"} for character in value):
            raise ValueError("名称或描述不能包含控制字符")
        return value

    @field_validator("hard_filters")
    @classmethod
    def validate_unique_filters(cls, value: list[StrategyHardFilter]) -> list[StrategyHardFilter]:
        identities = [(item.field, item.operator, item.period_sessions, repr(item.value)) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("硬过滤条件不能重复")
        return value


def strategy_profile_objectives(profile: StrategyProfile) -> StrategyObjectives:
    """Return explicit deterministic objective weights for a named profile."""
    presets = {
        "conservative": {
            "alpha_1d": 0.03,
            "alpha_5d": 0.12,
            "alpha_20d": 0.25,
            "confidence": 0.20,
            "risk": 0.25,
            "tradability": 0.15,
        },
        "balanced": {
            "alpha_1d": 0.05,
            "alpha_5d": 0.20,
            "alpha_20d": 0.35,
            "confidence": 0.15,
            "risk": 0.15,
            "tradability": 0.10,
        },
        "aggressive": {
            "alpha_1d": 0.10,
            "alpha_5d": 0.25,
            "alpha_20d": 0.40,
            "confidence": 0.10,
            "risk": 0.05,
            "tradability": 0.10,
        },
    }
    if profile == "custom":
        raise ValueError("custom 策略画像没有隐式目标权重")
    return StrategyObjectives.model_validate(presets[profile])


class StrategySpecCreate(_StrictModel):
    spec: StrategySpecInput
    confirmed: Literal[True]


class StrategySpecUpdate(_StrictModel):
    spec: StrategySpecInput
    expected_revision: int = Field(ge=1)
    confirmed: Literal[True]


class StrategySpecCopyRequest(_StrictModel):
    name: NameText
    revision: int | None = Field(default=None, ge=1)
    confirmed: Literal[True]


class StrategySpecArchiveRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    archived: bool = True


class StrategySpec(BaseModel):
    strategy_id: int = Field(ge=1)
    strategy_version: int = Field(ge=1)
    revision: int = Field(ge=1)
    current_revision: int = Field(ge=1)
    archived: bool
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: StrategySpecInput
    created_at: str
    updated_at: str
    version_created_at: str


class StrategySpecPage(BaseModel):
    items: list[StrategySpec]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    page_count: int = Field(ge=0)


class StrategyMetricDefinition(BaseModel):
    name: str
    label: str
    kind: StrategyMetricKind
    unit: str
    direction: StrategyObjectiveDirection | None = None
    allowed_operators: list[StrategyFilterOperator]
    allowed_periods: list[int]
    source_field: str


class StrategyCompiledExpression(BaseModel):
    field: str
    source_field: str
    operator: StrategyFilterOperator
    value: FilterValue
    period_sessions: int | None = None
    display: str


class StrategyExecutionPlan(BaseModel):
    dry_run: Literal[True] = True
    executable: bool
    blocked_reasons: list[str]
    board_labels: list[str]
    expressions: list[StrategyCompiledExpression]
    required_fields: list[str]
    objective_order: list[str]
    portfolio_summary: list[str]
    execution_summary: list[str]
    estimated_universe: str
    estimated_work: str
    will_start_scan: Literal[False] = False


class StrategyCompileRequest(_StrictModel):
    spec: StrategySpecInput
    dry_run: Literal[True] = True


class StrategyCompileResponse(BaseModel):
    normalized_spec: StrategySpecInput
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_plan: StrategyExecutionPlan
    warnings: list[str]
    ambiguities: list[str]
    unsupported_clauses: list[str]


class StrategyNaturalLanguageRequest(_StrictModel):
    text: str = Field(min_length=2, max_length=1000)
    name: NameText | None = None

    @field_validator("text")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 and character not in {"\t", "\n"} for character in value):
            raise ValueError("策略文本不能包含控制字符")
        return value


class StrategyNaturalLanguageResponse(BaseModel):
    original_text: str
    draft: StrategySpecInput
    applied_defaults: list[str]
    ambiguities: list[str]
    unsupported_clauses: list[str]
    compile: StrategyCompileResponse
    requires_confirmation: Literal[True] = True


class StrategyVersionSummary(BaseModel):
    strategy_id: int = Field(ge=1)
    revision: int = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str
    created_at: str


class StrategyVersionPage(BaseModel):
    items: list[StrategyVersionSummary]
    total: int = Field(ge=0)


class StrategyVersionDiff(BaseModel):
    strategy_id: int = Field(ge=1)
    left_revision: int = Field(ge=1)
    right_revision: int = Field(ge=1)
    left_fingerprint: str
    right_fingerprint: str
    changed_paths: list[str]


__all__ = [
    "STRATEGY_SPEC_SCHEMA_VERSION",
    "StrategyCompileRequest",
    "StrategyCompileResponse",
    "StrategyExecutionPlan",
    "StrategyHardFilter",
    "StrategyMetricDefinition",
    "StrategyNaturalLanguageRequest",
    "StrategyNaturalLanguageResponse",
    "StrategySpec",
    "StrategySpecArchiveRequest",
    "StrategySpecCopyRequest",
    "StrategySpecCreate",
    "StrategySpecInput",
    "StrategySpecPage",
    "StrategySpecUpdate",
    "StrategyVersionDiff",
    "StrategyVersionPage",
    "StrategyVersionSummary",
]
