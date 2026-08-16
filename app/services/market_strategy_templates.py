"""Pure, deterministic catalog of full-market research strategy templates."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_strategy_templates import (
    MARKET_STRATEGY_TEMPLATE_AS_OF_DATE,
    MARKET_STRATEGY_TEMPLATE_CATALOG_SCHEMA_VERSION,
    MarketStrategyTemplate,
    MarketStrategyTemplateCatalog,
)
from app.models.strategy_lab import (
    StrategyHardFilter,
    StrategyObjectives,
    StrategyPortfolioConstraints,
    StrategyRebalancePolicy,
    StrategySpecInput,
)
from app.services.strategy_compiler import compile_strategy_spec


_COMMON_LIMITATIONS = [
    "该模板仅为研究草案，不代表上涨概率、投资建议或已验证收益。",
    "所有数值阈值均为未经过样本外验证的产品初始值。",
    "截至2026-08-12仅有2个独立official交易日，不足以检验有效性或市场状态稳健性。",
    "停牌与一字状态当前使用冻结日K成交额和原因文本代理，不等同交易所逐时停复牌或排队成交证据。",
]
_COMMON_COST_NOTES = [
    "若进入可执行草案，将采用StrategySpec v1声明的成本与滑点假设；当前未接入真实盘口排队成交。",
]
_COMMON_RISK_NOTES = [
    "alpha字段是截面序数研究分，不是上涨概率，缺失值不得补零。",
]
_COMMON_GATE_REASONS = [
    "PIT字段合同已验证，可载入Strategy Lab研究草案。",
    "有效性证据尚未生成，不得标记为通过或用于生产排名。",
]


def market_strategy_template_catalog() -> MarketStrategyTemplateCatalog:
    """Return a new validated catalog without reading providers or databases."""
    templates = sorted(
        [*_available_templates(), *_research_route_templates()],
        key=lambda item: (item.template_id, item.version),
    )
    payload = {
        "schema_version": MARKET_STRATEGY_TEMPLATE_CATALOG_SCHEMA_VERSION,
        "as_of_date": MARKET_STRATEGY_TEMPLATE_AS_OF_DATE,
        "selection_mode": "exclusive",
        "production_rule_version": "full-market-score-v4",
        "production_effect": "none",
        "official_session_count": 2,
        "templates": [item.model_dump(mode="json") for item in templates],
    }
    payload["catalog_digest"] = market_strategy_catalog_digest(payload)
    return MarketStrategyTemplateCatalog.model_validate(payload)


def market_strategy_template_digest(
    value: MarketStrategyTemplate | Mapping[str, object],
) -> str:
    """Hash the canonical template payload while excluding its own digest."""
    payload = _plain_payload(value)
    payload.pop("template_digest", None)
    return sha256_hex(canonical_json_bytes(payload))


def market_strategy_catalog_digest(
    value: MarketStrategyTemplateCatalog | Mapping[str, object],
) -> str:
    """Hash the canonical catalog payload while excluding its own digest."""
    payload = _plain_payload(value)
    payload.pop("catalog_digest", None)
    return sha256_hex(canonical_json_bytes(payload))


def _plain_payload(value: object) -> dict[str, object]:
    if isinstance(value, (MarketStrategyTemplate, MarketStrategyTemplateCatalog)):
        return value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise TypeError("策略模板摘要输入必须是模型或映射")
    return deepcopy(dict(value))


def _available_templates() -> list[MarketStrategyTemplate]:
    return [
        _balanced_multi_horizon(),
        _daily_continuation(),
        _bounded_medium_trend(),
        _pullback_continuation(),
        _defensive_liquidity(),
        _capacity_first(),
    ]


def _balanced_multi_horizon() -> MarketStrategyTemplate:
    spec = _draft_spec(
        name="多周期均衡研究草案",
        description="联合1/5/20日序数分、风险与可交易性；阈值未经样本外验证。",
        filters=[
            _filter("alpha_5d", "gte", 45.0),
            _filter("alpha_20d", "gte", 35.0),
            _filter("risk", "lte", 65.0),
            _filter("tradability", "gte", 20.0),
            _filter("amount", "gte", 50_000_000.0),
        ],
        objectives=(0.10, 0.25, 0.30, 0.10, 0.15, 0.10),
        holding=5,
        rebalance=5,
    )
    return _available_template("balanced_multi_horizon", "多周期", "平衡多个研究周期与风险容量约束。", 61, spec)


def _daily_continuation() -> MarketStrategyTemplate:
    spec = _draft_spec(
        name="次日延续研究草案",
        description="观察短周期动量延续并限制追涨幅度；阈值未经样本外验证。",
        filters=[
            _filter("alpha_1d", "gte", 50.0),
            _filter("alpha_5d", "gte", 45.0),
            _filter("return_pct", "between", [0.0, 7.0], period=1),
            _filter("risk", "lte", 70.0),
            _filter("tradability", "gte", 30.0),
            _filter("amount", "gte", 80_000_000.0),
        ],
        objectives=(0.35, 0.25, 0.10, 0.10, 0.08, 0.12),
        holding=1,
        rebalance=1,
    )
    return _available_template("daily_continuation", "短期延续", "形成次日延续候选并约束过热和流动性。", 61, spec)


def _bounded_medium_trend() -> MarketStrategyTemplate:
    spec = _draft_spec(
        name="有界中期趋势研究草案",
        description="保留中期趋势并排除极端涨幅与高风险；阈值未经样本外验证。",
        filters=[
            _filter("alpha_20d", "gte", 45.0),
            _filter("return_pct", "between", [0.0, 30.0], period=20),
            _filter("return_pct", "gte", 0.0, period=60),
            _filter("risk", "lte", 55.0),
            _filter("tradability", "gte", 25.0),
            _filter("amount", "gte", 50_000_000.0),
        ],
        objectives=(0.05, 0.20, 0.40, 0.10, 0.15, 0.10),
        holding=10,
        rebalance=10,
    )
    return _available_template("bounded_medium_trend", "中期趋势", "研究受涨幅和风险边界约束的中期趋势。", 61, spec)


def _pullback_continuation() -> MarketStrategyTemplate:
    spec = _draft_spec(
        name="回撤后延续研究草案",
        description="在中期序数分较强时观察单日回撤后的延续；阈值未经样本外验证。",
        filters=[
            _filter("alpha_5d", "gte", 45.0),
            _filter("alpha_20d", "gte", 45.0),
            _filter("return_pct", "between", [-6.0, 0.0], period=1),
            _filter("risk", "lte", 65.0),
            _filter("tradability", "gte", 20.0),
            _filter("amount", "gte", 50_000_000.0),
        ],
        objectives=(0.10, 0.25, 0.35, 0.10, 0.10, 0.10),
        holding=5,
        rebalance=5,
    )
    return _available_template("pullback_continuation", "趋势回撤", "研究趋势背景下的有界单日回撤。", 61, spec)


def _defensive_liquidity() -> MarketStrategyTemplate:
    spec = _draft_spec(
        name="防御流动性研究草案",
        description="优先低风险、成交容量与中期稳定性；阈值未经样本外验证。",
        filters=[
            _filter("risk", "lte", 40.0),
            _filter("tradability", "gte", 35.0),
            _filter("amount", "gte", 100_000_000.0),
            _filter("alpha_20d", "gte", 35.0),
            _filter("return_pct", "gte", -5.0, period=20),
        ],
        objectives=(0.03, 0.12, 0.25, 0.15, 0.30, 0.15),
        holding=10,
        rebalance=10,
    )
    return _available_template("defensive_liquidity", "防御", "优先风险控制和可交易容量的防御性候选。", 61, spec)


def _capacity_first() -> MarketStrategyTemplate:
    spec = _draft_spec(
        name="容量优先研究草案",
        description="以成交额和可交易性约束组合容量；阈值未经样本外验证。",
        filters=[
            _filter("amount", "gte", 200_000_000.0),
            _filter("tradability", "gte", 55.0),
            _filter("risk", "lte", 65.0),
            _filter("alpha_5d", "gte", 40.0),
        ],
        objectives=(0.05, 0.20, 0.20, 0.10, 0.15, 0.30),
        holding=5,
        rebalance=5,
        stock_count=30,
        max_stock_weight=0.05,
    )
    return _available_template("capacity_first", "容量", "先控制容量与可交易性，再观察短中周期序数分。", 61, spec)


def _draft_spec(
    *,
    name: str,
    description: str,
    filters: list[StrategyHardFilter],
    objectives: tuple[float, float, float, float, float, float],
    holding: int,
    rebalance: int,
    stock_count: int = 20,
    max_stock_weight: float = 0.10,
) -> StrategySpecInput:
    return StrategySpecInput(
        name=name,
        description=description,
        hard_filters=filters,
        profile="custom",
        objectives=StrategyObjectives(
            alpha_1d=objectives[0],
            alpha_5d=objectives[1],
            alpha_20d=objectives[2],
            confidence=objectives[3],
            risk=objectives[4],
            tradability=objectives[5],
        ),
        portfolio_constraints=StrategyPortfolioConstraints(
            stock_count=stock_count,
            max_stock_weight=max_stock_weight,
        ),
        rebalance_policy=StrategyRebalancePolicy(
            hold_sessions=holding,
            rebalance_every_sessions=rebalance,
        ),
    )


def _filter(
    field: str,
    operator: str,
    value: float | list[float],
    *,
    period: int | None = None,
) -> StrategyHardFilter:
    return StrategyHardFilter.model_validate({"field": field, "operator": operator, "value": value, "period_sessions": period})


def _available_template(
    template_id: str,
    family: str,
    objective: str,
    formation: int,
    spec: StrategySpecInput,
) -> MarketStrategyTemplate:
    compiled = compile_strategy_spec(spec)
    if not compiled.execution_plan.executable or compiled.unsupported_clauses:
        raise ValueError("可载入策略模板必须通过确定性编译")
    fields = compiled.execution_plan.required_fields
    payload = _base_payload(
        template_id=template_id,
        name=spec.name,
        family=family,
        objective=objective,
        formation=formation,
        holding=spec.rebalance_policy.hold_sessions,
        rebalance=spec.rebalance_policy.rebalance_every_sessions,
    )
    payload.update(
        availability="available_for_draft",
        strategy_spec=spec.model_dump(mode="json"),
        contract_status="verified",
        efficacy_status="not_generated",
        required_fields=fields,
        missing_fields=[],
        gate_reasons=_COMMON_GATE_REASONS,
        regime_hypotheses=["候选阈值可能随市场状态变化，当前尚未生成分状态证据。"],
    )
    return _materialize(payload)


def _research_route_templates() -> list[MarketStrategyTemplate]:
    return [
        _shadow_route("short_reversal", "短期反转", "研究短期反转在A股T+1和成本约束下的净效应。", ["return_pct", "risk", "tradability", "amount"]),
        _shadow_route("medium_momentum", "中期动量", "研究跳过近期窗口后的中期动量与状态依赖。", ["alpha_20d", "return_pct", "risk", "tradability"]),
        _shadow_route(
            "industry_relative_strength",
            "行业相对强度",
            "研究行业内横截面强弱及行业暴露约束。",
            ["industry", "industry_relative_strength", "alpha_20d", "tradability"],
        ),
        _unavailable_route("value_garp", "价值与GARP", "研究估值与增长匹配。", ["pe_ttm", "peg", "earnings_growth_pit"]),
        _unavailable_route("quality_growth", "质量成长", "研究盈利质量和可持续成长。", ["roe_pit", "cashflow_quality_pit", "earnings_growth_pit"]),
        _unavailable_route(
            "dividend_low_vol", "红利低波", "研究PIT股息率和低波组合。", ["dividend_yield_pit", "dividend_event_pit", "free_float_market_cap_pit"]
        ),
        _unavailable_route(
            "event_revision", "事件与预期修正", "研究公告事件和分析师预期修正。", ["announcement_event_pit", "analyst_revision_pit", "consensus_estimate_pit"]
        ),
        _unavailable_route(
            "crowding_risk",
            "拥挤与容量",
            "研究拥挤、容量压力和交易成本非线性。",
            ["pit_common_holdings", "pit_fund_flow", "crowding_score", "capacity_score"],
            present_fields=["amount", "tradability"],
        ),
    ]


def _shadow_route(
    template_id: str,
    family: str,
    objective: str,
    required_fields: list[str],
) -> MarketStrategyTemplate:
    payload = _base_payload(
        template_id=template_id,
        name=f"{family}研究路线",
        family=family,
        objective=objective,
        formation=61,
        holding=5,
        rebalance=5,
    )
    payload.update(
        availability="shadow_only",
        strategy_spec=None,
        contract_status="verified",
        efficacy_status="insufficient_data",
        required_fields=required_fields,
        missing_fields=[],
        gate_reasons=["相关PIT字段合同可用于影子研究，但仅2个独立official交易日。", "尚无足够样本外证据，不生成可执行StrategySpec。"],
        regime_hypotheses=["效应方向和强度可能依赖市场状态，需按状态分层验证。"],
    )
    return _materialize(payload)


def _unavailable_route(
    template_id: str,
    family: str,
    objective: str,
    missing_fields: list[str],
    present_fields: list[str] | None = None,
) -> MarketStrategyTemplate:
    payload = _base_payload(
        template_id=template_id,
        name=f"{family}研究路线",
        family=family,
        objective=objective,
        formation=252,
        holding=20,
        rebalance=20,
    )
    payload.update(
        availability="unavailable",
        strategy_spec=None,
        contract_status="unavailable",
        efficacy_status="unavailable",
        required_fields=[*(present_fields or []), *missing_fields],
        missing_fields=missing_fields,
        gate_reasons=["当前冻结全市场数据不具备全部所需PIT字段，禁止代理、补零或静默降级。"],
        regime_hypotheses=["数据合同建立前不生成市场状态假设证据。"],
    )
    return _materialize(payload)


def _base_payload(
    *,
    template_id: str,
    name: str,
    family: str,
    objective: str,
    formation: int,
    holding: int,
    rebalance: int,
) -> dict[str, object]:
    return {
        "template_id": template_id,
        "version": 1,
        "name": name,
        "family": family,
        "objective": objective,
        "horizon": {
            "formation_sessions": formation,
            "holding_sessions": holding,
            "rebalance_sessions": rebalance,
            "label": f"形成{formation}日/持有{holding}日/每{rebalance}日调仓",
        },
        "regime_evidence_status": "not_generated",
        "cost_notes": list(_COMMON_COST_NOTES),
        "risk_notes": list(_COMMON_RISK_NOTES),
        "limitations": list(_COMMON_LIMITATIONS),
    }


def _materialize(payload: dict[str, object]) -> MarketStrategyTemplate:
    value = deepcopy(payload)
    value["template_digest"] = market_strategy_template_digest(value)
    return MarketStrategyTemplate.model_validate(value)


__all__ = [
    "market_strategy_catalog_digest",
    "market_strategy_template_catalog",
    "market_strategy_template_digest",
]
