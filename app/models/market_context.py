"""Auditable current-session context, separate from historical score evidence."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class ContextObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str
    name: str
    change_pct: FiniteFloat
    event_at: str
    observed_at: str
    source: str


class MarketContextScore(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    rule_version: Literal["current-market-context.v1", "current-market-context.v2"] = "current-market-context.v1"
    horizon: Literal["same-session-relative-strength"] = "same-session-relative-strength"
    historical_calibration_eligible: Literal[False] = False
    symbol: str
    base_score: int = Field(ge=0, le=100)
    reliability_score: int = Field(ge=0, le=100)
    score: int = Field(ge=0, le=100)
    raw_score: FiniteFloat = Field(ge=0, le=100)
    evaluated_at: str
    stock_event_at: str
    market: ContextObservation | None = None
    industry: ContextObservation | None = None
    stock_excess_pct: FiniteFloat | None = None
    industry_excess_pct: FiniteFloat | None = None
    relative_strength_score: FiniteFloat | None = Field(default=None, ge=0, le=100,
        description="个股相对主行业涨跌的截断映射，50表示无超额；不是第二个独立基础分。")
    relative_weight: FiniteFloat = Field(default=0, ge=0, le=1,
        description="v2表示原方向幅度的最大相对修正比例；v1表示基础分与相对强弱分的混合权重。")
    relative_adjustment: FiniteFloat | None = Field(default=None, ge=-10, le=10,
        description="v2行业相对修正后、门控前与基础分的差值；缺失行业为0，旧v1记录为null。")
    before_gates_score: FiniteFloat = Field(ge=0, le=100)
    pre_reliability_score: FiniteFloat = Field(ge=0, le=100)
    market_multiplier: FiniteFloat = Field(ge=0.5, le=1)
    industry_multiplier: FiniteFloat = Field(ge=0.7, le=1)
    unavailable_reasons: list[str] = Field(default_factory=list)
    note: str = "当日相对强弱修正规则，不是风险调整 alpha、上涨概率或收益预测；参数尚未经样本外收益验证。"
