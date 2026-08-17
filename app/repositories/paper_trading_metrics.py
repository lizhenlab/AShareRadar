"""Pure performance and position calculations for paper-trading snapshots."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import sqrt
from statistics import fmean, pstdev

from app.models.paper_trading import (
    PaperCostProfile,
    PaperEquityPoint,
    PaperPosition,
    PaperStrategy,
    PaperTrade,
    PaperTradingAccount,
    PaperTradingPerformance,
    PaperTradingRun,
)
from app.models.paper_trading_costs import available_cost_profiles, resolve_cost_profile, trade_costs


@dataclass(frozen=True)
class ClosedMetrics:
    win_count: int
    win_rate: float | None
    average_win: float | None
    average_loss: float | None
    payoff_ratio: float | None
    expectancy: float | None
    profit_factor: float | None


@dataclass(frozen=True)
class LatestMetrics:
    cash_balance: float
    market_value: float
    total_equity: float
    gross_equity: float
    realized_pnl: float
    unrealized_pnl: float
    total_cost: float
    total_return_pct: float
    gross_return_pct: float
    benchmark_return_pct: float | None
    excess_return_pct: float | None


def calculate_performance(
    account: PaperTradingAccount,
    strategies: list[PaperStrategy],
    equity: list[PaperEquityPoint],
    trades: list[PaperTrade],
) -> PaperTradingPerformance:
    closed = [item for item in strategies if item.status == "closed"]
    outcomes = calculate_closed_metrics(closed)
    counts = Counter(item.status for item in strategies)
    latest = latest_metrics(account.initial_cash, equity)
    risk_values = calculate_risk_metrics(account.initial_cash, equity, len(closed))
    drawdown_duration, recovery = drawdown_durations(equity)
    average_exposure, maximum_exposure = exposure_metrics(equity)
    gross_pnl = latest.gross_equity - account.initial_cash
    modeled_cost_drag = latest.gross_equity - latest.total_equity
    return PaperTradingPerformance(
        strategy_count=len(strategies),
        pending_count=counts["pending"],
        open_count=counts["open"],
        closed_count=counts["closed"],
        skipped_count=counts["skipped"],
        expired_count=counts["expired"],
        data_unavailable_count=counts["data_unavailable"],
        win_count=outcomes.win_count,
        win_rate_pct=outcomes.win_rate,
        cash_balance=latest.cash_balance,
        market_value=latest.market_value,
        total_equity=latest.total_equity,
        gross_equity=latest.gross_equity,
        realized_pnl=latest.realized_pnl,
        unrealized_pnl=latest.unrealized_pnl,
        gross_pnl=round(gross_pnl, 2),
        total_cost=latest.total_cost,
        cost_drag_pct=round(modeled_cost_drag / account.initial_cash * 100, 4),
        cost_to_gross_profit_pct=(
            round(modeled_cost_drag / gross_pnl * 100, 4)
            if gross_pnl > 0
            else None
        ),
        total_return_pct=latest.total_return_pct,
        gross_return_pct=latest.gross_return_pct,
        benchmark_return_pct=latest.benchmark_return_pct,
        excess_return_pct=latest.excess_return_pct,
        max_drawdown_pct=min((item.drawdown_pct for item in equity), default=0),
        max_drawdown_duration_sessions=drawdown_duration,
        recovery_duration_sessions=recovery,
        average_win=outcomes.average_win,
        average_loss=outcomes.average_loss,
        payoff_ratio=outcomes.payoff_ratio,
        expectancy=outcomes.expectancy,
        profit_factor=outcomes.profit_factor,
        turnover_pct=round(sum(item.gross_amount for item in trades) / account.initial_cash * 100, 4),
        average_exposure_pct=average_exposure,
        maximum_exposure_pct=maximum_exposure,
        return_observation_count=len(equity),
        sample_warning=sample_warning(len(closed), len(equity)),
        **risk_values,
    )


def calculate_closed_metrics(closed: list[PaperStrategy]) -> ClosedMetrics:
    values = [float(item.realized_pnl or 0) for item in closed]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    average_win = round(fmean(wins), 2) if wins else None
    average_loss = round(fmean(losses), 2) if losses else None
    payoff = payoff_ratio(average_win, average_loss)
    return ClosedMetrics(
        win_count=len(wins),
        win_rate=round(len(wins) / len(closed) * 100, 2) if closed else None,
        average_win=average_win,
        average_loss=average_loss,
        payoff_ratio=payoff,
        expectancy=round(fmean(values), 2) if values else None,
        profit_factor=round(sum(wins) / abs(sum(losses)), 4) if losses else None,
    )


def payoff_ratio(average_win: float | None, average_loss: float | None) -> float | None:
    if average_win is None or average_loss is None or average_loss == 0:
        return None
    return round(average_win / abs(average_loss), 4)


def latest_metrics(initial_cash: float, equity: list[PaperEquityPoint]) -> LatestMetrics:
    if not equity:
        return LatestMetrics(initial_cash, 0, initial_cash, initial_cash, 0, 0, 0, 0, 0, None, None)
    item = equity[-1]
    return LatestMetrics(
        item.cash_balance,
        item.market_value,
        item.total_equity,
        item.gross_equity,
        item.realized_pnl,
        item.unrealized_pnl,
        item.cumulative_cost,
        item.return_pct,
        item.gross_return_pct,
        item.benchmark_return_pct,
        item.excess_return_pct,
    )


def exposure_metrics(equity: list[PaperEquityPoint]) -> tuple[float, float]:
    values = [item.exposure_pct for item in equity]
    if not values:
        return 0, 0
    return round(fmean(values), 4), round(max(values), 4)


def sample_warning(closed_count: int, observation_count: int) -> str | None:
    if closed_count < 5:
        return f"仅有 {closed_count} 笔已平仓策略，胜率和盈亏统计只作描述性参考"
    if observation_count < 60:
        return f"仅有 {observation_count} 个收益观察值，暂不计算年化风险指标"
    return None


def calculate_risk_metrics(
    initial_cash: float,
    equity: list[PaperEquityPoint],
    closed_count: int,
) -> dict[str, object]:
    if len(equity) < 60 or closed_count < 5:
        return {
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "calmar_ratio": None,
            "risk_metric_status": "unavailable",
            "risk_metric_message": "至少需要60个收益观察值和5笔已平仓策略",
        }
    values = [initial_cash, *[item.total_equity for item in equity]]
    returns = [current / previous - 1 for previous, current in zip(values, values[1:], strict=False) if previous > 0]
    if len(returns) < 2 or pstdev(returns) == 0:
        return {
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "calmar_ratio": None,
            "risk_metric_status": "unavailable",
            "risk_metric_message": "收益序列缺少可计算的波动",
        }
    mean_return = fmean(returns)
    volatility = pstdev(returns)
    downside = sqrt(fmean([min(item, 0) ** 2 for item in returns]))
    maximum_drawdown = abs(min((item.drawdown_pct for item in equity), default=0)) / 100
    annualized = (equity[-1].total_equity / initial_cash) ** (252 / len(returns)) - 1 if initial_cash > 0 else 0
    return {
        "sharpe_ratio": round(mean_return / volatility * sqrt(252), 4),
        "sortino_ratio": round(mean_return / downside * sqrt(252), 4) if downside > 0 else None,
        "calmar_ratio": round(annualized / maximum_drawdown, 4) if maximum_drawdown > 0 else None,
        "risk_metric_status": "available",
        "risk_metric_message": "按日收益观察值年化；无风险利率按0处理",
    }


def drawdown_durations(equity: list[PaperEquityPoint]) -> tuple[int, int | None]:
    longest = 0
    current = 0
    trough_index: int | None = None
    minimum = 0.0
    for index, item in enumerate(equity):
        if item.drawdown_pct < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
        if item.drawdown_pct < minimum:
            minimum = item.drawdown_pct
            trough_index = index
    if trough_index is None:
        return longest, 0
    recovery_index = next(
        (index for index in range(trough_index + 1, len(equity)) if equity[index].drawdown_pct >= 0),
        None,
    )
    return longest, recovery_index - trough_index if recovery_index is not None else None


def build_positions(
    strategies: list[PaperStrategy],
    profile: PaperCostProfile,
) -> list[PaperPosition]:
    positions: list[PaperPosition] = []
    for item in strategies:
        if item.status != "open" or not item.entry_date or item.entry_price is None or item.last_price is None:
            continue
        gross_cost = item.entry_price * item.quantity
        cost_basis = gross_cost + item.buy_friction
        market_value = item.last_price * item.quantity
        exit_friction = trade_costs(profile, side="sell", gross_amount=market_value).total
        unrealized = market_value - exit_friction - cost_basis
        positions.append(
            PaperPosition(
                strategy_id=item.id,
                symbol=item.symbol,
                quantity=item.quantity,
                entry_date=item.entry_date,
                entry_price=item.entry_price,
                cost_basis=round(cost_basis, 2),
                last_price=item.last_price,
                market_value=round(market_value, 2),
                estimated_exit_friction=round(exit_friction, 2),
                unrealized_pnl=round(unrealized, 2),
                return_pct=round(unrealized / cost_basis * 100, 4) if cost_basis else 0,
                target_price=item.normalized_target_price or item.target_price,
                stop_price=item.normalized_stop_price or item.stop_price,
                held_sessions=item.held_sessions,
                pending_exit_reason=item.pending_exit_reason,
            )
        )
    return positions


def cost_profile_from_run(run: PaperTradingRun | None) -> PaperCostProfile:
    if run is not None:
        raw = run.configuration.get("cost_profile")
        if isinstance(raw, dict):
            try:
                return PaperCostProfile.model_validate(raw)
            except ValueError:
                pass
        for profile in available_cost_profiles():
            if profile.profile_id == run.cost_profile_id:
                return profile
    return resolve_cost_profile("base")


def optional_metric_delta(right: float | None, left: float | None) -> float | None:
    if right is None or left is None:
        return None
    return round(float(right) - float(left), 4)


__all__ = [
    "ClosedMetrics",
    "LatestMetrics",
    "build_positions",
    "calculate_closed_metrics",
    "calculate_performance",
    "calculate_risk_metrics",
    "cost_profile_from_run",
    "drawdown_durations",
    "exposure_metrics",
    "latest_metrics",
    "optional_metric_delta",
    "payoff_ratio",
    "sample_warning",
]
