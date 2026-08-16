"""Strict read-only contracts for the full-market strategy template catalog."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.strategy_lab import StrategySpecInput


MARKET_STRATEGY_TEMPLATE_CATALOG_SCHEMA_VERSION: Literal["full-market-strategy-template-catalog-v1"] = "full-market-strategy-template-catalog-v1"
MARKET_STRATEGY_TEMPLATE_AS_OF_DATE: Literal["2026-08-12"] = "2026-08-12"

TemplateAvailability = Literal["available_for_draft", "shadow_only", "unavailable"]
TemplateContractStatus = Literal["verified", "unavailable"]
TemplateEfficacyStatus = Literal["not_generated", "insufficient_data", "unavailable"]
TemplateId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
RequiredFieldText = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.]{1,199}$")]
DigestText = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NoteText = Annotated[str, Field(min_length=1, max_length=500)]

_DRAFT_FILTER_PERIODS: dict[str, frozenset[int | None]] = {
    "alpha_1d": frozenset({None}),
    "alpha_5d": frozenset({None}),
    "alpha_20d": frozenset({None}),
    "risk": frozenset({None}),
    "tradability": frozenset({None}),
    "amount": frozenset({None}),
    "return_pct": frozenset({1, 5, 20, 60}),
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class MarketStrategyTemplateHorizon(_StrictModel):
    formation_sessions: int = Field(ge=1, le=1_500)
    holding_sessions: int = Field(ge=1, le=60)
    rebalance_sessions: int = Field(ge=1, le=60)
    label: Annotated[str, Field(min_length=1, max_length=80)]


class MarketStrategyTemplate(_StrictModel):
    template_id: TemplateId
    version: int = Field(ge=1)
    name: Annotated[str, Field(min_length=1, max_length=80)]
    family: Annotated[str, Field(min_length=1, max_length=80)]
    objective: Annotated[str, Field(min_length=1, max_length=1_000)]
    horizon: MarketStrategyTemplateHorizon
    availability: TemplateAvailability
    strategy_spec: StrategySpecInput | None
    contract_status: TemplateContractStatus
    efficacy_status: TemplateEfficacyStatus
    regime_evidence_status: Literal["not_generated"] = "not_generated"
    required_fields: list[RequiredFieldText] = Field(min_length=1, max_length=50)
    missing_fields: list[RequiredFieldText] = Field(default_factory=list, max_length=50)
    gate_reasons: list[NoteText] = Field(min_length=1, max_length=30)
    regime_hypotheses: list[NoteText] = Field(min_length=1, max_length=20)
    cost_notes: list[NoteText] = Field(min_length=1, max_length=20)
    risk_notes: list[NoteText] = Field(min_length=1, max_length=20)
    limitations: list[NoteText] = Field(min_length=1, max_length=30)
    template_digest: DigestText

    @field_validator(
        "required_fields",
        "missing_fields",
        "gate_reasons",
        "regime_hypotheses",
        "cost_notes",
        "risk_notes",
        "limitations",
    )
    @classmethod
    def validate_unique_list_values(cls, value: list[object]) -> list[object]:
        if len(value) != len({str(item) for item in value}):
            raise ValueError("策略模板列表字段不能包含重复项")
        return value

    @model_validator(mode="after")
    def validate_availability_contract(self) -> Self:
        required = set(self.required_fields)
        if not set(self.missing_fields) <= required:
            raise ValueError("missing_fields 必须是 required_fields 的子集")
        if self.availability == "available_for_draft":
            self._validate_available_draft()
        elif self.availability == "shadow_only":
            self._validate_shadow_only()
        else:
            self._validate_unavailable()
        if self.template_digest != _model_digest(self, "template_digest"):
            raise ValueError("策略模板摘要与内容不一致")
        return self

    def _validate_available_draft(self) -> None:
        if self.strategy_spec is None:
            raise ValueError("available_for_draft 模板必须提供 strategy_spec")
        if self.contract_status != "verified" or self.efficacy_status != "not_generated":
            raise ValueError("可载入草案必须是 verified/not_generated")
        if self.missing_fields:
            raise ValueError("可载入草案不能声明缺失字段")
        if self.strategy_spec.name != self.name:
            raise ValueError("可载入草案的名称必须与模板名称一致")
        if self.strategy_spec.profile != "custom":
            raise ValueError("可载入模板必须使用 custom 画像并显式冻结目标权重")
        for item in self.strategy_spec.hard_filters:
            periods = _DRAFT_FILTER_PERIODS.get(item.field)
            if periods is None or item.period_sessions not in periods:
                raise ValueError("可载入模板包含未注册的字段或周期")
        policy = self.strategy_spec.rebalance_policy
        if policy.hold_sessions != self.horizon.holding_sessions:
            raise ValueError("模板持有周期必须与 StrategySpec 一致")
        if policy.rebalance_every_sessions != self.horizon.rebalance_sessions:
            raise ValueError("模板调仓周期必须与 StrategySpec 一致")

    def _validate_shadow_only(self) -> None:
        if self.strategy_spec is not None:
            raise ValueError("shadow_only 模板不能提供 strategy_spec")
        if self.contract_status != "verified" or self.efficacy_status != "insufficient_data":
            raise ValueError("影子模板必须是 verified/insufficient_data")
        if self.missing_fields:
            raise ValueError("影子模板的已冻结研究合同不能声明缺失字段")

    def _validate_unavailable(self) -> None:
        if self.strategy_spec is not None:
            raise ValueError("unavailable 模板不能提供 strategy_spec")
        if self.contract_status != "unavailable" or self.efficacy_status != "unavailable":
            raise ValueError("不可用模板必须是 unavailable/unavailable")
        if not self.missing_fields:
            raise ValueError("不可用模板必须明确 missing_fields")


class MarketStrategyTemplateCatalog(_StrictModel):
    schema_version: Literal["full-market-strategy-template-catalog-v1"] = MARKET_STRATEGY_TEMPLATE_CATALOG_SCHEMA_VERSION
    as_of_date: Literal["2026-08-12"] = MARKET_STRATEGY_TEMPLATE_AS_OF_DATE
    selection_mode: Literal["exclusive"] = "exclusive"
    production_rule_version: Literal["full-market-score-v4"] = "full-market-score-v4"
    production_effect: Literal["none"] = "none"
    official_session_count: Literal[2] = 2
    templates: list[MarketStrategyTemplate] = Field(min_length=1, max_length=50)
    catalog_digest: DigestText

    @model_validator(mode="after")
    def validate_template_identity_and_order(self) -> Self:
        identities = [(item.template_id, item.version) for item in self.templates]
        template_ids = [item.template_id for item in self.templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("目录内策略模板 ID 必须全局唯一")
        if identities != sorted(identities):
            raise ValueError("策略模板必须按 template_id/version 确定性排序")
        if self.catalog_digest != _model_digest(self, "catalog_digest"):
            raise ValueError("策略模板目录摘要与内容不一致")
        return self


def _model_digest(value: BaseModel, digest_field: str) -> str:
    payload = value.model_dump(mode="json", exclude={digest_field})
    return sha256_hex(canonical_json_bytes(payload))


__all__ = [
    "MARKET_STRATEGY_TEMPLATE_AS_OF_DATE",
    "MARKET_STRATEGY_TEMPLATE_CATALOG_SCHEMA_VERSION",
    "MarketStrategyTemplate",
    "MarketStrategyTemplateCatalog",
    "MarketStrategyTemplateHorizon",
    "TemplateAvailability",
    "TemplateContractStatus",
    "TemplateEfficacyStatus",
]
