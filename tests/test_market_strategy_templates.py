from __future__ import annotations

from copy import deepcopy

from pydantic import ValidationError
import pytest

from app.models.market_strategy_templates import (
    MarketStrategyTemplate,
    MarketStrategyTemplateCatalog,
)
from app.services.market_strategy_templates import (
    market_strategy_catalog_digest,
    market_strategy_template_catalog,
    market_strategy_template_digest,
)
from app.services.strategy_compiler import compile_strategy_spec


AVAILABLE_IDS = {
    "balanced_multi_horizon",
    "bounded_medium_trend",
    "capacity_first",
    "daily_continuation",
    "defensive_liquidity",
    "pullback_continuation",
}
SHADOW_IDS = {
    "industry_relative_strength",
    "medium_momentum",
    "short_reversal",
}
UNAVAILABLE_IDS = {
    "crowding_risk",
    "dividend_low_vol",
    "event_revision",
    "quality_growth",
    "value_garp",
}


def test_catalog_contract_order_identity_statuses_and_production_boundary() -> None:
    catalog = market_strategy_template_catalog()

    assert catalog.schema_version == "full-market-strategy-template-catalog-v1"
    assert catalog.as_of_date == "2026-08-12"
    assert catalog.selection_mode == "exclusive"
    assert catalog.production_rule_version == "full-market-score-v4"
    assert catalog.production_effect == "none"
    assert catalog.official_session_count == 2
    identities = [(item.template_id, item.version) for item in catalog.templates]
    assert identities == sorted(identities)
    assert len(identities) == len(set(identities)) == 14
    assert _ids(catalog, "available_for_draft") == AVAILABLE_IDS
    assert _ids(catalog, "shadow_only") == SHADOW_IDS
    assert _ids(catalog, "unavailable") == UNAVAILABLE_IDS


def test_digests_are_canonical_deterministic_and_change_with_semantics() -> None:
    first = market_strategy_template_catalog()
    second = market_strategy_template_catalog()
    assert first == second
    assert first.catalog_digest == market_strategy_catalog_digest(first)
    assert all(item.template_digest == market_strategy_template_digest(item) for item in first.templates)

    item_payload = first.templates[0].model_dump(mode="json")
    reversed_item = dict(reversed(list(item_payload.items())))
    assert market_strategy_template_digest(item_payload) == market_strategy_template_digest(reversed_item)
    changed_item = deepcopy(item_payload)
    changed_item["objective"] = str(changed_item["objective"]) + "（语义变化）"
    assert market_strategy_template_digest(changed_item) != first.templates[0].template_digest

    catalog_payload = first.model_dump(mode="json")
    catalog_payload["official_session_count"] = 3
    assert market_strategy_catalog_digest(catalog_payload) != first.catalog_digest


def test_available_drafts_are_strict_custom_and_compile_executable() -> None:
    catalog = market_strategy_template_catalog()
    allowed_filter_fields = {"alpha_1d", "alpha_5d", "alpha_20d", "risk", "tradability", "amount", "return_pct"}
    for item in catalog.templates:
        if item.availability != "available_for_draft":
            assert item.strategy_spec is None
            continue
        assert item.strategy_spec is not None
        assert item.contract_status == "verified"
        assert item.efficacy_status == "not_generated"
        assert item.regime_evidence_status == "not_generated"
        assert item.strategy_spec.profile == "custom"
        assert item.horizon.formation_sessions == 61
        assert "objectives" in item.strategy_spec.model_fields_set
        assert item.strategy_spec.rebalance_policy.rebalance_every_sessions == item.strategy_spec.rebalance_policy.hold_sessions
        assert {value.field for value in item.strategy_spec.hard_filters} <= allowed_filter_fields
        compiled = compile_strategy_spec(item.strategy_spec)
        assert compiled.execution_plan.executable is True
        assert compiled.execution_plan.will_start_scan is False
        assert compiled.unsupported_clauses == []
        assert item.required_fields == compiled.execution_plan.required_fields
        assert {
            "derived.listing_board",
            "market_scan_result.industry",
            "market_scan_result.price",
            "market_scan_result.status",
        } <= set(item.required_fields)
        assert any("样本外验证" in note for note in item.limitations)
        assert any("日K成交额" in note and "代理" in note for note in item.limitations)


def test_model_rejects_cross_state_specs_missing_fields_duplicates_and_unknowns() -> None:
    catalog = market_strategy_template_catalog()
    available = _payload(catalog, "balanced_multi_horizon")
    available["strategy_spec"] = None
    with pytest.raises(ValidationError, match="必须提供 strategy_spec"):
        MarketStrategyTemplate.model_validate(available)

    shadow = _payload(catalog, "short_reversal")
    shadow["strategy_spec"] = _payload(catalog, "daily_continuation")["strategy_spec"]
    with pytest.raises(ValidationError, match="不能提供 strategy_spec"):
        MarketStrategyTemplate.model_validate(shadow)

    unavailable = _payload(catalog, "value_garp")
    unavailable["missing_fields"] = []
    with pytest.raises(ValidationError, match="必须明确 missing_fields"):
        MarketStrategyTemplate.model_validate(unavailable)

    unknown = _payload(catalog, "value_garp")
    unknown["probability"] = 0.75
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MarketStrategyTemplate.model_validate(unknown)

    catalog_payload = catalog.model_dump(mode="json")
    catalog_payload["templates"] = list(reversed(catalog_payload["templates"]))
    with pytest.raises(ValidationError, match="确定性排序"):
        MarketStrategyTemplateCatalog.model_validate(catalog_payload)


def test_models_replay_digests_and_reject_resealed_unknown_draft_fields() -> None:
    catalog = market_strategy_template_catalog()
    tampered_item = _payload(catalog, "balanced_multi_horizon")
    tampered_item["objective"] = "已被篡改"
    with pytest.raises(ValidationError, match="摘要与内容不一致"):
        MarketStrategyTemplate.model_validate(tampered_item)

    tampered_catalog = catalog.model_dump(mode="json")
    tampered_catalog["templates"][0]["objective"] = "目录内语义已被篡改"
    tampered_catalog["templates"][0]["template_digest"] = market_strategy_template_digest(tampered_catalog["templates"][0])
    with pytest.raises(ValidationError, match="目录摘要与内容不一致"):
        MarketStrategyTemplateCatalog.model_validate(tampered_catalog)

    unknown_metric = _payload(catalog, "daily_continuation")
    spec = unknown_metric["strategy_spec"]
    assert isinstance(spec, dict)
    filters = spec["hard_filters"]
    assert isinstance(filters, list)
    filters[0]["field"] = "unknown_metric"
    unknown_metric["template_digest"] = market_strategy_template_digest(unknown_metric)
    with pytest.raises(ValidationError, match="未注册的字段或周期"):
        MarketStrategyTemplate.model_validate(unknown_metric)


def test_catalog_rejects_same_template_id_at_multiple_versions() -> None:
    catalog = market_strategy_template_catalog()
    payload = catalog.model_dump(mode="json")
    first = deepcopy(payload["templates"][0])
    second = deepcopy(payload["templates"][1])
    second["template_id"] = first["template_id"]
    second["version"] = 2
    second["template_digest"] = market_strategy_template_digest(second)
    payload["templates"] = [first, second]
    payload["catalog_digest"] = market_strategy_catalog_digest(payload)

    with pytest.raises(ValidationError, match="ID 必须全局唯一"):
        MarketStrategyTemplateCatalog.model_validate(payload)


def test_unavailable_routes_expose_null_spec_and_explicit_missing_contract() -> None:
    catalog = market_strategy_template_catalog()
    for item in catalog.templates:
        assert item.efficacy_status not in {"pass", "passed"}
        if item.availability == "shadow_only":
            assert item.horizon.formation_sessions == 61
        if item.availability == "unavailable":
            assert item.strategy_spec is None
            assert item.contract_status == "unavailable"
            assert item.efficacy_status == "unavailable"
            assert item.missing_fields
            assert set(item.missing_fields) <= set(item.required_fields)

    crowding = next(item for item in catalog.templates if item.template_id == "crowding_risk")
    assert {"amount", "tradability"} <= set(crowding.required_fields)
    assert {"amount", "tradability"}.isdisjoint(crowding.missing_fields)
    assert set(crowding.missing_fields) == {
        "pit_common_holdings",
        "pit_fund_flow",
        "crowding_score",
        "capacity_score",
    }


def _ids(catalog: MarketStrategyTemplateCatalog, status: str) -> set[str]:
    return {item.template_id for item in catalog.templates if item.availability == status}


def _payload(catalog: MarketStrategyTemplateCatalog, template_id: str) -> dict[str, object]:
    return next(item.model_dump(mode="json") for item in catalog.templates if item.template_id == template_id)
