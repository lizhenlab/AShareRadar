from copy import deepcopy

import pytest

from app.services.market_scan_allocation import plan_market_scan_allocation
from app.services.market_scan_allocation_contracts import allocation_digest, allocation_source_digests
from app.services.paper_trading_costs import resolve_cost_profile


def policy(**changes):
    return {"schema_version": "market-scan-allocation-policy-v1", "allocation": "fixed-candidate-slots",
            "top_n": 1, "max_symbol_weight": .5, "max_industry_weight": .5, "max_participation_rate": .01,
            "cost_profile": "base", "cost_profile_id": resolve_cost_profile("base").profile_id,
            "max_input_age_seconds": 3600, "max_quote_age_seconds": 300,
            "max_classification_age_days": 30, "existing_breach_policy": "block-new-orders", **changes}


def instrument(symbol="600001.SH", industry="same-sector", **changes):
    return {"symbol": symbol, "price": 10.0, "valuation_observed_at": "2026-08-25T09:29:00+08:00",
            "industry": industry, "classification_observed_at": "2026-08-24T08:00:00+08:00",
            "classification_effective_from": "2026-08-01", "classification_effective_through": "2026-08-31",
            "minimum_buy_quantity": 100, "buy_quantity_step": 100, "capacity_amount": 100000000.0,
            "capacity_session_date": "2026-08-24", "capacity_observed_at": "2026-08-24T16:00:00+08:00", **changes}


def inputs(*, held=True, cash=21000.0):
    holdings = [{"position_id": "old-a", "symbol": "600001.SH", "sleeve": 0, "quantity": 900}] if held else []
    return {"schema_version": "market-scan-allocation-input-v1", "decision_at": "2026-08-25T09:30:00+08:00",
            "account": {"source_id": "account-fixture", "observed_as_of": "2026-08-25T09:25:00+08:00",
                        "holdings_complete": True, "cash": cash, "positions": holdings},
            "candidates": {"source_id": "rank-fixture", "observed_as_of": "2026-08-25T09:20:00+08:00",
                           "rows": [{"symbol": "600002.SH", "frozen_rank": 1}]},
            "market": {"source_id": "market-fixture", "observed_as_of": "2026-08-25T09:30:00+08:00",
                       "rows": [instrument(), instrument("600002.SH")]}}


def plan(value=None, settings=None):
    value = inputs() if value is None else value
    settings = policy() if settings is None else settings
    return plan_market_scan_allocation(value, settings, expected_source_digests=allocation_source_digests(value),
                                       expected_policy_digest=allocation_digest(settings))


def test_old_same_industry_holding_consumes_whole_account_capacity():
    report = plan()
    assert report["status"] == "planned"
    assert report["proposed_orders"][0]["quantity"] == 500
    assert report["after"]["industry_exposures"][0]["nav_weight"] <= .5
    assert report["before"]["market_value"] == 9000
    assert report["promotion_eligible"] is False and report["replay_integrated"] is False


def test_existing_breach_blocks_new_orders_even_in_another_sector():
    value = inputs(cash=6000.0)
    value["market"]["rows"][1]["industry"] = "other"
    report = plan(value)
    assert report["status"] == "blocked" and report["proposed_orders"] == []
    assert "existing_risk_limit_breach" in report["blocking_reasons"]


@pytest.mark.parametrize("field", ["price", "industry"])
def test_unknown_old_exposure_blocks_all_additions(field):
    value = inputs()
    value["market"]["rows"][0][field] = None
    report = plan(value)
    assert report["status"] == "blocked" and not report["proposed_orders"]
    assert report["after"]["cash"] == value["account"]["cash"]


def test_unknown_candidate_is_rejected_without_refill_or_budget_redistribution():
    value = inputs(held=False, cash=20000.0)
    value["candidates"]["rows"] = [{"symbol": "600001.SH", "frozen_rank": 1}, {"symbol": "600002.SH", "frozen_rank": 2}]
    value["market"]["rows"][0]["industry"] = None
    report = plan(value, policy(top_n=2, max_industry_weight=1.0, max_symbol_weight=1.0))
    assert [row["symbol"] for row in report["proposed_orders"]] == ["600002.SH"]
    assert report["proposed_orders"][0]["gross_amount"] + report["proposed_orders"][0]["estimated_fees"] <= 10000


def test_independent_pins_and_strict_json_contract_are_required():
    value, settings = inputs(), policy()
    pins = allocation_source_digests(value)
    changed = deepcopy(value)
    changed["account"]["cash"] += 1
    with pytest.raises(ValueError, match="pin"):
        plan_market_scan_allocation(changed, settings, expected_source_digests=pins, expected_policy_digest=allocation_digest(settings))
    with pytest.raises(ValueError):
        plan({**value, "verified": True})


def two_candidates():
    value = inputs(held=False, cash=20000.0)
    value["candidates"]["rows"] = [{"symbol": "600001.SH", "frozen_rank": 1}, {"symbol": "600002.SH", "frozen_rank": 2}]
    value["market"]["rows"] = [instrument(symbol, industry=symbol, price=1.0, minimum_buy_quantity=1, buy_quantity_step=1)
                               for symbol in ("600001.SH", "600002.SH")]
    return value


def test_all_later_fees_are_reserved_before_first_order_weight_is_decided():
    report = plan(two_candidates(), policy(top_n=2, max_symbol_weight=.3, max_industry_weight=.3))
    assert len(report["proposed_orders"]) == 2
    assert report["proposed_orders"][0]["quantity"] <= 5995
    assert all(row["nav_weight"] <= .3 for row in report["after"]["symbol_exposures"])
    assert report["after"]["nav"] >= report["conservative_nav_floor"]
    fees = sum(row["estimated_fees"] for row in report["proposed_orders"])
    gross = sum(row["gross_amount"] for row in report["proposed_orders"])
    assert report["after"]["cash"] == pytest.approx(20000 - gross - fees)
    assert report["after"]["nav"] == pytest.approx(20000 - fees)


def test_cross_sleeve_duplicate_symbol_quantities_are_aggregated_before_risk():
    value = inputs()
    value["account"]["positions"] = [{"position_id": "a", "symbol": "600001.SH", "sleeve": 0, "quantity": 400},
                                       {"position_id": "b", "symbol": "600001.SH", "sleeve": 3, "quantity": 500}]
    report = plan(value)
    assert report["before"]["symbol_exposures"][0]["quantity"] == 900
    assert report["proposed_orders"][0]["quantity"] == 500


def test_old_risk_near_limit_cannot_be_pushed_over_by_fees_on_other_orders():
    value = inputs(cash=9000.0)
    value["market"]["rows"][1]["industry"] = "other"
    report = plan(value)
    assert report["status"] == "blocked" and not report["proposed_orders"]
    assert "existing_risk_limit_after_fee_reserve" in report["blocking_reasons"]


@pytest.mark.parametrize("row_index, field, stamp, rejects_input", [
    (0, "valuation_observed_at", "2026-08-25T09:31:00+08:00", True),
    (1, "classification_observed_at", "2026-08-25T09:31:00+08:00", True),
    (0, "valuation_observed_at", "2026-08-25T09:20:00+08:00", False),
    (1, "valuation_observed_at", "2026-08-25T09:20:00+08:00", False),
    (0, "classification_observed_at", "2026-06-01T08:00:00+08:00", False),
    (1, "classification_observed_at", None, False),
])
def test_future_stale_and_missing_evidence_cannot_enter_new_risk(row_index, field, stamp, rejects_input):
    value = inputs()
    value["market"]["rows"][row_index][field] = stamp
    if rejects_input:
        with pytest.raises(ValueError, match="observed after"):
            plan(value)
        return
    report = plan(value)
    assert report["proposed_orders"] == []
    assert report["status"] == ("blocked" if row_index == 0 else "no_orders")


@pytest.mark.parametrize("changes", [
    {"capacity_amount": None}, {"capacity_session_date": "2026-08-21"},
    {"capacity_observed_at": "2026-08-24T10:00:00+08:00"},
    {"classification_effective_from": "2026-08-26"}, {"classification_effective_through": "2026-08-24"},
])
def test_unavailable_capacity_and_ineffective_classification_reject_candidate(changes):
    value = inputs()
    value["market"]["rows"][1].update(changes)
    assert plan(value)["proposed_orders"] == []


def test_prior_capacity_and_minimum_lot_remain_separate_from_total_cash():
    value = inputs()
    value["market"]["rows"][1]["capacity_amount"] = 100000.0
    report = plan(value)
    assert report["proposed_orders"][0]["gross_amount"] == 1000
    value["market"]["rows"][1]["capacity_amount"] = 99999.99
    assert plan(value)["proposed_orders"] == []


@pytest.mark.parametrize("mutation", ["duplicate_position", "duplicate_candidate", "rank_gap", "duplicate_market", "future_source", "stale_source",
                                        "incomplete_holdings", "cash_precision", "boolean_quantity", "nonfinite_price", "lot_step", "extra_rank_score"])
def test_conflicting_and_ambiguous_source_contracts_fail_closed(mutation):
    value = inputs()
    if mutation == "duplicate_position":
        value["account"]["positions"] *= 2
    elif mutation == "duplicate_candidate":
        value["candidates"]["rows"] *= 2
    elif mutation == "rank_gap":
        value["candidates"]["rows"][0]["frozen_rank"] = 2
    elif mutation == "duplicate_market":
        value["market"]["rows"].append(deepcopy(value["market"]["rows"][0]))
    elif mutation in {"future_source", "stale_source"}:
        value["account"]["observed_as_of"] = "2026-08-25T09:31:00+08:00" if mutation == "future_source" else "2026-08-24T09:00:00+08:00"
    elif mutation == "incomplete_holdings":
        value["account"]["holdings_complete"] = 1
    elif mutation == "cash_precision":
        value["account"]["cash"] = 21000.001
    elif mutation == "boolean_quantity":
        value["account"]["positions"][0]["quantity"] = True
    elif mutation == "nonfinite_price":
        value["market"]["rows"][0]["price"] = float("nan")
    elif mutation == "lot_step":
        value["market"]["rows"][1].update(minimum_buy_quantity=100, buy_quantity_step=30)
    else:
        value["candidates"]["rows"][0]["expected_return"] = 99
    with pytest.raises(ValueError):
        plan(value)


@pytest.mark.parametrize("changes", [{"cost_profile_id": "false"}, {"top_n": 2}, {"max_symbol_weight": True},
                                       {"max_industry_weight": 0.0}, {"max_participation_rate": 1.1}, {"existing_breach_policy": "ignore"}])
def test_policy_identity_and_limits_are_frozen_and_strict(changes):
    with pytest.raises(ValueError):
        plan(settings=policy(**changes))


def test_plan_is_deterministic_and_keeps_inputs_unchanged():
    value, settings = inputs(), policy()
    original = deepcopy(value), deepcopy(settings)
    first = plan(value, settings)
    assert first == plan(value, settings)
    assert original == (value, settings)
    assert first["result_digest"] == allocation_digest({key: val for key, val in first.items() if key != "result_digest"})


def test_admission_and_report_share_the_same_captured_source_bytes(monkeypatch):
    from app.services import market_scan_allocation_validation as validation
    value, settings = inputs(), policy()
    frozen_digest = allocation_digest(value)
    source_pins = allocation_source_digests(value)
    original = validation.allocation_source_digests

    def mutate_after_hash(captured):
        digests = original(captured)
        value["account"]["cash"] = 1.0
        return digests

    monkeypatch.setattr(validation, "allocation_source_digests", mutate_after_hash)
    result = plan_market_scan_allocation(value, settings, expected_source_digests=source_pins, expected_policy_digest=allocation_digest(settings))
    assert result["before"]["cash"] == 21000
    assert result["input_digest"] == frozen_digest


def test_preserved_policy_digest_matches_its_original_json_number_types():
    settings = policy(max_symbol_weight=1, max_industry_weight=1)
    result = plan(settings=settings)
    assert result["policy_digest"] == allocation_digest(result["policy"])
    assert type(result["policy"]["max_symbol_weight"]) is int


def test_exact_cash_for_minimum_lot_is_not_lost_to_a_cent_of_excess_fee_reserve():
    value = inputs(held=False, cash=125.02)
    value["market"]["rows"] = [instrument("600002.SH", price=1.2)]
    result = plan(value, policy(max_symbol_weight=1.0, max_industry_weight=1.0))
    assert result["proposed_orders"][0]["quantity"] == 100
    assert result["reserved_fee_upper_bound"] == 5.02
    assert result["after"]["cash"] == 0


def test_unknown_or_unaffordable_candidate_reserves_no_phantom_fee():
    value = two_candidates()
    value["market"]["rows"][0]["industry"] = None
    value["market"]["rows"][1].update(price=1000.0, minimum_buy_quantity=100, buy_quantity_step=100)
    result = plan(value, policy(top_n=2))
    assert result["reserved_fee_upper_bound"] == 0
    assert result["proposed_orders"] == []
