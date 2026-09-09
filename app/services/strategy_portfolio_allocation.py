"""Settle reference-price purchases within cash and gross-position budgets."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN

from app.models.strategy_lab import StrategyExecutionPolicy


STRATEGY_ALLOCATION_CONTRACT_VERSION = "strategy-portfolio-allocation-v2-buy-cash-actual-minimum"
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class PlannedPurchase:
    quantity: int = 0
    gross_amount: float = 0.0


def reference_buy_debit(policy: StrategyExecutionPolicy, gross: float) -> Decimal:
    """Reference amount plus estimated buy fees/slippage, not broker fills."""
    amount = Decimal(str(gross))
    if amount == 0:
        return amount
    commission = max(amount * Decimal(str(policy.commission_rate)), Decimal(str(policy.minimum_commission_cny)))
    rate = Decimal(str(policy.transfer_fee_rate)) + Decimal(str(policy.buy_slippage_bps)) / 10_000
    cost = (commission + amount * rate).quantize(_CENT, rounding=ROUND_HALF_EVEN)
    return amount + cost


def affordable_portfolio_purchase(
    policy: StrategyExecutionPolicy,
    *,
    price: float,
    cash_budget: float,
    gross_limit: float,
    minimum_quantity: int,
    quantity_step: int,
) -> PlannedPurchase:
    """Find the largest legal quantity with bounded, monotone binary search."""
    if price <= 0 or cash_budget <= 0 or gross_limit <= 0:
        return PlannedPurchase()
    unit_price = Decimal(str(price))
    cash = Decimal(str(cash_budget))
    capacity = Decimal(str(gross_limit))
    maximum = int(min(cash, capacity) // unit_price)
    low, high = 0, (maximum - minimum_quantity) // quantity_step
    best = PlannedPurchase()
    while low <= high:
        middle = (low + high) // 2
        quantity = minimum_quantity + middle * quantity_step
        gross = (unit_price * quantity).quantize(_CENT, rounding=ROUND_HALF_EVEN)
        if gross <= capacity and reference_buy_debit(policy, float(gross)) <= cash:
            best = PlannedPurchase(quantity, float(gross))
            low = middle + 1
        else:
            high = middle - 1
    return best if best.gross_amount > 0 else PlannedPurchase()


def strategy_allocation_contract() -> dict[str, object]:
    return {
        "version": STRATEGY_ALLOCATION_CONTRACT_VERSION,
        "cash_budget": "reference-gross-plus-buy-commission-transfer-and-slippage",
        "gross_position_limits": "reference-amount-after-quantity-rounding",
        "minimum_position": "actual-gross-after-buy-budget-and-quantity-rounding",
        "currency_rounding": "decimal-half-even-cent-per-order-gross-and-buy-cost",
        "residual_cash": "initial-cash-minus-estimated-buy-debits",
        "sell_cost": "round-trip-estimate-only-not-reserved-from-initial-cash",
    }
