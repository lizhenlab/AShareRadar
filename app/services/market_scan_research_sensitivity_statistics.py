"""Descriptive account statistics; never infer capacity or select a winning cell."""

from __future__ import annotations

from collections import Counter
from math import fsum
from statistics import fmean

from app.services.market_scan_research_portfolio_models import ResearchPortfolioDay, ResearchPortfolioResult


def sensitivity_account_summary(account: ResearchPortfolioResult) -> dict[str, object]:
    ratios = [day.market_value / day.nav if day.market_value is not None and day.nav is not None and day.nav > 0 else None
              for day in account.days]
    observed = [value for value in ratios if value is not None]
    end = account.days[-1]
    return {
        "input_digest": account.input_digest, "result_digest": account.result_digest,
        "provenance_status": account.provenance_status, "total_return": account.total_return,
        "maximum_drawdown": account.maximum_drawdown, "final_nav": end.nav, "final_cash": end.cash,
        "fees": account.total_fees, "fees_per_initial_cash": account.total_fees / account.config.initial_cash,
        "same_executed_path_fee_addback_return": None if account.total_return is None else account.total_return + account.total_fees / account.config.initial_cash,
        "buy_count": sum(trade.side == "buy" for trade in account.trades),
        "sell_count": sum(trade.side == "sell" for trade in account.trades),
        "gross_traded": fsum(trade.gross_amount for trade in account.trades),
        "remaining_position_count": len(account.final_positions),
        "expected_entry_slots": account.expected_entry_slots, "filled_entry_slots": account.filled_entry_slots,
        "unknown_entry_slots": account.unknown_entry_slots, "entry_fill_coverage": account.entry_fill_coverage,
        "entry_decision_evidence_coverage": account.entry_decision_evidence_coverage,
        "valuation_coverage": account.valuation_coverage,
        "mean_invested_nav_fraction": fmean(observed) if len(observed) == len(ratios) else None,
        "observed_invested_fraction_days": len(observed), "declared_days": len(ratios),
        "event_reason_counts": dict(sorted(Counter(event.reason for event in account.events).items())),
        "daily": [{"session_date": day.session_date, "nav": day.nav, "cash": day.cash, "market_value": day.market_value,
                   "daily_return": day.daily_return, "invested_nav_fraction": ratio, "fees": day.fees,
                   "gross_traded": day.gross_traded, "unresolved_reasons": list(day.unresolved_reasons)}
                  for day, ratio in zip(account.days, ratios, strict=True)],
    }


def sensitivity_account_comparison(candidate: ResearchPortfolioResult, reference: ResearchPortfolioResult) -> dict[str, object]:
    dates = [day.session_date for day in candidate.days]
    if dates != [day.session_date for day in reference.days] or candidate.config != reference.config:
        raise ValueError("sensitivity comparisons require identical calendars and execution settings")
    pairs = [_daily_difference(left, right) for left, right in zip(candidate.days[1:], reference.days[1:], strict=True)]
    complete = bool(pairs) and all(value is not None for value in pairs)
    complete = complete and candidate.total_return is not None and reference.total_return is not None
    return {"reference": "production_v5_same_scenario", "reference_result_digest": reference.result_digest,
            "status": "complete" if complete else "incomplete", "session_dates": dates[1:],
            "daily_net_return_improvement": pairs,
            "mean_daily_net_return_improvement": fmean(value for value in pairs if value is not None) if complete else None,
            "inference": "descriptive_only; no p-values or scenario selection", "promotion_eligible": False}


def _daily_difference(left: ResearchPortfolioDay, right: ResearchPortfolioDay) -> float | None:
    if left.daily_return is None or right.daily_return is None:
        return None
    return left.daily_return - right.daily_return
