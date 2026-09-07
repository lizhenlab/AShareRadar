"""Cash boundaries use currency amounts, not binary floating-point noise."""

from decimal import Decimal

import pytest

from app.models.paper_trading import CostProfileName, PaperCostOverrides
from app.services.market_scan_evaluation_execution import affordable_execution_purchase
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs


@pytest.mark.parametrize("price", [1.09, 2.18, 4.11])
@pytest.mark.parametrize("profile_name", ["base", "conservative", "stress"])
def test_exact_cash_including_fees_buys_one_lot(price: float, profile_name: CostProfileName) -> None:
    profile = resolve_cost_profile(profile_name)
    amount = float(Decimal(str(price)) * 100)
    fees = trade_costs(profile, side="buy", gross_amount=amount).total
    cash = float(Decimal(str(amount)) + Decimal(str(fees)))

    quantity, gross, actual_fees = affordable_execution_purchase(cash, price, 100, 100, profile)

    assert quantity == 100
    assert gross == amount
    assert actual_fees == fees
    assert Decimal(str(gross)) + Decimal(str(actual_fees)) == Decimal(str(cash))


@pytest.mark.parametrize("price", [1.09, 2.18, 4.11])
@pytest.mark.parametrize("profile_name", ["base", "conservative", "stress"])
def test_one_cent_short_cannot_buy_a_lot(price: float, profile_name: CostProfileName) -> None:
    profile = resolve_cost_profile(profile_name)
    amount = float(Decimal(str(price)) * 100)
    fees = trade_costs(profile, side="buy", gross_amount=amount).total
    cash = float(Decimal(str(amount)) + Decimal(str(fees)) - Decimal("0.01"))

    assert affordable_execution_purchase(cash, price, 100, 100, profile) == (0, 0.0, 0.0)


@pytest.mark.parametrize("price", [0.07, 0.14, 1.13, 1.14])
def test_zero_cost_quantity_bound_preserves_an_exact_lot(price: float) -> None:
    profile = resolve_cost_profile("base", PaperCostOverrides(
        commission_rate_pct=0, minimum_commission=0, transfer_fee_pct=0, slippage_buy_pct=0,
    ))
    cash = float(Decimal(str(price)) * 100)

    assert affordable_execution_purchase(cash, price, 100, 100, profile) == (100, cash, 0.0)


@pytest.mark.parametrize("cash,quantity", [(114.019, 0), (114.02, 100), (114.021, 100)])
def test_cash_is_not_rounded_up_to_fund_a_trade(cash: float, quantity: int) -> None:
    result = affordable_execution_purchase(cash, 1.09, 100, 100, resolve_cost_profile("base"))

    assert result[0] == quantity
    assert Decimal(str(result[1])) + Decimal(str(result[2])) <= Decimal(str(cash))
