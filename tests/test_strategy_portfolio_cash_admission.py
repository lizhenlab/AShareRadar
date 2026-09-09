"""Cash and minimum-position constraints hold for the actual planned orders."""

from contextlib import closing
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

import pytest

from app.models.strategy_execution import PortfolioDraft, StrategyExecutionRequest
from app.models.strategy_lab import StrategyExecutionPolicy, StrategyPortfolioConstraints, StrategySpecUpdate
from app.models.paper_trading import PaperInstrumentMetadata
from app.repositories.strategy_execution import StrategyExecutionRepository
from app.repositories.strategy_lab import StrategyLabRepository
from app.services.paper_trading_rules import resolve_trade_rule_profile
from app.services.strategy_execution import StrategyExecutionService
from app.services.strategy_lab import StrategyLabService
from app.services.strategy_portfolio_allocation import affordable_portfolio_purchase, reference_buy_debit
from tests.test_strategy_automation_atomic_completion import _isolated_environment
from tests.market_scan_test_support import SCAN_AS_OF


def _buy_debit(candidate, policy):
    gross = Decimal(str(candidate.estimated_gross_amount_cny))
    commission = max(gross * Decimal(str(policy.commission_rate)), Decimal(str(policy.minimum_commission_cny)))
    rate = Decimal(str(policy.transfer_fee_rate)) + Decimal(str(policy.buy_slippage_bps)) / 10_000
    return gross + (commission + gross * rate).quantize(Decimal("0.01"))


def test_public_execution_and_plan_fit_cash_after_reference_buy_costs(tmp_path):
    with _isolated_environment(tmp_path) as (cache, service, strategy_id, _run_id):
        policy = cache.domain_services.strategy_lab.get(strategy_id).spec.execution_policy
        draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id, notional_cash_cny=1_000_000.0))
        debit = sum((_buy_debit(item, policy) for item in draft.selected), Decimal(0))
        assert draft.selected
        assert debit <= Decimal("1000000")
        assert draft.summary.residual_cash_cny == float(Decimal("1000000") - debit)
        plan = cache.domain_services.strategy_automation.create_simulation_plan(draft.context.execution_id)
        assert [(x.symbol, x.target_quantity) for x in plan.orders] == [(x.symbol, x.target_quantity) for x in draft.selected]


def test_public_execution_checks_minimum_again_after_quantity_rounding(tmp_path):
    with _isolated_environment(tmp_path) as (cache, service, strategy_id, _run_id):
        minimum = cache.domain_services.strategy_lab.get(strategy_id).spec.portfolio_constraints.min_position_amount_cny
        draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id, notional_cash_cny=10_000.0))
        assert all(item.estimated_gross_amount_cny >= minimum for item in draft.selected)
        assert draft.summary.no_trade
        assert draft.summary.residual_cash_cny == 10_000.0
        assert draft.summary.pool_exhausted
        assert draft.summary.replacement_attempt_count == 4
        plan = cache.domain_services.strategy_automation.create_simulation_plan(draft.context.execution_id)
        assert plan.status == "no_trade" and not plan.orders


def _old_execution(tmp_path):
    path = Path(__file__).parent / "fixtures/strategy_portfolio_allocation_v1.json"
    fixture = json.loads(path.read_text())
    database = tmp_path / "old-allocation.sqlite3"
    with closing(sqlite3.connect(database)) as conn:
        conn.executescript("\n".join(fixture["sqlite_dump"]))
    return StrategyExecutionRepository(database), fixture, database


def test_real_old_execution_stays_readable_without_recomputing_cash(tmp_path):
    repository, fixture, database = _old_execution(tmp_path)
    before = database.read_bytes()
    restored = repository.draft(fixture["execution_id"])
    assert restored == PortfolioDraft.model_validate(fixture["draft"])
    assert restored.summary.residual_cash_cny == 0.0
    assert sum(row.estimated_gross_amount_cny for row in restored.selected) == pytest.approx(999_790.32)
    assert database.read_bytes() == before


def test_new_allocation_has_distinct_identity_and_does_not_rewrite_old_execution(tmp_path):
    repository, fixture, database = _old_execution(tmp_path)
    service = StrategyExecutionService(
        repository, StrategyLabService(StrategyLabRepository(database)), market_clock=lambda: SCAN_AS_OF,
    )
    old = repository.draft(fixture["execution_id"])
    request = StrategyExecutionRequest(strategy_id=fixture["strategy_id"])
    current = service.execute(request)
    repeated = service.execute(request)
    assert current.context.execution_fingerprint != old.context.execution_fingerprint
    assert current.context.execution_fingerprint == repeated.context.execution_fingerprint
    assert current.result_digest == repeated.result_digest
    assert current.context.cost_rule_fingerprint == old.context.cost_rule_fingerprint
    assert current.context.source_snapshot_digest == old.context.source_snapshot_digest
    assert current.selected != old.selected
    assert repository.draft(fixture["execution_id"]) == old


def _zero_cost_policy():
    return StrategyExecutionPolicy(
        commission_rate=0, minimum_commission_cny=0, transfer_fee_rate=0,
        sell_stamp_duty_rate=0, buy_slippage_bps=0, sell_slippage_bps=0,
    )


def _update_strategy(cache, strategy_id, **changes):
    current = cache.domain_services.strategy_lab.get(strategy_id)
    cache.domain_services.strategy_lab.update(
        strategy_id, StrategySpecUpdate(spec=current.spec.model_copy(update=changes), expected_revision=1, confirmed=True),
    )


def test_post_rounding_minimum_failure_refills_with_an_affordable_lower_rank(tmp_path):
    with _isolated_environment(tmp_path) as (cache, service, strategy_id, _run_id):
        current = cache.domain_services.strategy_lab.get(strategy_id)
        constraints = current.spec.portfolio_constraints.model_copy(update={"min_position_amount_cny": 4_900.0})
        _update_strategy(cache, strategy_id, portfolio_constraints=constraints)
        draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id, notional_cash_cny=10_000.0))
        assert [row.symbol for row in draft.selected] == ["920001.BJ", "688001.SH"]
        assert all(row.estimated_gross_amount_cny >= 4_900 for row in draft.selected)
        assert draft.summary.replacement_attempt_count == 1
        assert draft.summary.pool_exhausted is False
        rejected = next(row for row in draft.candidate_preview if row.symbol == "300001.SZ")
        assert rejected.status == "unfilled"
        assert any("实际金额低于最小持仓金额" in reason for reason in rejected.reasons)


def test_custom_weight_tolerance_cannot_spend_more_than_shared_cash(tmp_path):
    capital = 1_019_999.99
    weights = {"920001.BJ": 513_000 / capital, "688001.SH": 507_000 / capital}
    assert 1 < sum(weights.values()) <= 1.0000001
    constraints = StrategyPortfolioConstraints(
        stock_count=2, weighting_method="custom", custom_weights=weights,
        max_stock_weight=.6, max_industry_positions=2, max_industry_weight=1,
        max_board_weight=1, min_position_amount_cny=0, max_notional_share_of_daily_amount=.05,
    )
    with _isolated_environment(tmp_path) as (cache, service, strategy_id, _run_id):
        _update_strategy(cache, strategy_id, portfolio_constraints=constraints, execution_policy=_zero_cost_policy())
        draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id, notional_cash_cny=capital))
        assert [(row.symbol, row.target_quantity) for row in draft.selected] == [("920001.BJ", 50_000), ("688001.SH", 49_999)]
        gross = sum(Decimal(str(row.estimated_gross_amount_cny)) for row in draft.selected)
        assert gross <= Decimal(str(capital))
        assert draft.summary.residual_cash_cny == float(Decimal(str(capital)) - gross)
        assert all(row.target_weight <= .6 for row in draft.selected)


@pytest.mark.parametrize("symbol,minimum,step", [
    ("600001.SH", 100, 100), ("300001.SZ", 100, 100),
    ("688001.SH", 200, 1), ("920001.BJ", 100, 1),
])
@pytest.mark.parametrize("policy", [
    _zero_cost_policy(), StrategyExecutionPolicy(),
    StrategyExecutionPolicy(commission_rate=.02, minimum_commission_cny=1_000),
])
def test_buy_budget_respects_real_board_units_and_exact_fee_boundaries(symbol, minimum, step, policy):
    profile = resolve_trade_rule_profile(symbol, date(2026, 7, 17), PaperInstrumentMetadata(
        symbol=symbol, name="合成股票", market=symbol.split(".")[1], list_date="2000-01-01",
        is_st=False, status_effective_date="2026-07-17", source="synthetic",
    ))
    assert (profile.min_buy_quantity, profile.buy_quantity_step) == (minimum, step)
    gross = float(Decimal("13.37") * minimum)
    exact_cash = reference_buy_debit(policy, gross)
    kwargs = dict(policy=policy, price=13.37, gross_limit=gross, minimum_quantity=minimum, quantity_step=step)
    below = affordable_portfolio_purchase(cash_budget=float(exact_cash - Decimal(".01")), **kwargs)
    exact = affordable_portfolio_purchase(cash_budget=float(exact_cash), **kwargs)
    assert below.quantity == 0
    assert (exact.quantity, exact.gross_amount) == (minimum, gross)


def test_affordable_purchase_is_maximal_and_search_work_is_bounded(monkeypatch):
    from app.services import strategy_portfolio_allocation as allocation
    policy = StrategyExecutionPolicy(commission_rate=.02, minimum_commission_cny=1_000, buy_slippage_bps=1_000)
    observed = 0
    original = allocation.reference_buy_debit
    def counted(*args):
        nonlocal observed
        observed += 1
        return original(*args)
    monkeypatch.setattr(allocation, "reference_buy_debit", counted)
    result = allocation.affordable_portfolio_purchase(
        policy, price=.01, cash_budget=1_000_000_000, gross_limit=1_000_000_000,
        minimum_quantity=100, quantity_step=1,
    )
    assert original(policy, result.gross_amount) <= Decimal("1000000000")
    assert original(policy, float(Decimal(".01") * (result.quantity + 1))) > Decimal("1000000000")
    assert observed <= 40
