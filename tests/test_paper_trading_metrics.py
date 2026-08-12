from __future__ import annotations

from app.models.paper_trading import PaperEquityPoint, PaperStrategy, PaperTradingRun
from app.repositories.paper_trading_metrics import (
    calculate_closed_metrics,
    calculate_risk_metrics,
    cost_profile_from_run,
    drawdown_durations,
    exposure_metrics,
    latest_metrics,
    payoff_ratio,
    sample_warning,
)
from app.services.paper_trading_costs import resolve_cost_profile


def test_closed_latest_exposure_and_sample_metrics_cover_empty_and_mixed_samples() -> None:
    closed = [
        _strategy(1, realized_pnl=200),
        _strategy(2, realized_pnl=-100),
        _strategy(3, realized_pnl=0),
    ]

    metrics = calculate_closed_metrics(closed)
    assert metrics.win_count == 1
    assert metrics.win_rate == 33.33
    assert metrics.average_win == 200
    assert metrics.average_loss == -100
    assert metrics.payoff_ratio == 2
    assert metrics.expectancy == 33.33
    assert metrics.profit_factor == 2
    assert payoff_ratio(None, -100) is None
    assert payoff_ratio(100, None) is None
    assert payoff_ratio(100, 0) is None

    latest = latest_metrics(1_000_000, [])
    assert latest.cash_balance == 1_000_000
    assert latest.total_equity == 1_000_000
    assert exposure_metrics([]) == (0, 0)
    assert sample_warning(5, 59) == "仅有 59 个收益观察值，暂不计算年化风险指标"
    assert sample_warning(5, 60) is None


def test_risk_metrics_distinguish_sample_shortage_constant_returns_and_available_series() -> None:
    short = calculate_risk_metrics(1_000_000, [_equity(0, 1_000_000)], closed_count=5)
    assert short["risk_metric_status"] == "unavailable"
    assert "60个" in str(short["risk_metric_message"])

    constant = [_equity(index, 1_000_000) for index in range(60)]
    no_volatility = calculate_risk_metrics(1_000_000, constant, closed_count=5)
    assert no_volatility["risk_metric_status"] == "unavailable"
    assert "波动" in str(no_volatility["risk_metric_message"])

    varied = [
        _equity(
            index,
            1_000_000 + index * 2_000 + (-8_000 if index % 7 == 0 else 0),
            drawdown=-2.5 if index % 7 == 0 else 0,
        )
        for index in range(60)
    ]
    available = calculate_risk_metrics(1_000_000, varied, closed_count=5)
    assert available["risk_metric_status"] == "available"
    assert available["sharpe_ratio"] is not None
    assert available["sortino_ratio"] is not None
    assert available["calmar_ratio"] is not None


def test_risk_metrics_handle_zero_baseline_positive_only_returns_and_empty_return_series() -> None:
    positive = [_equity(index, 1_000_000 + index * (index + 1)) for index in range(60)]
    result = calculate_risk_metrics(1_000_000, positive, closed_count=5)
    assert result["risk_metric_status"] == "available"
    assert result["sortino_ratio"] is None
    assert result["calmar_ratio"] is None

    zeros = [_equity(index, 0) for index in range(60)]
    no_returns = calculate_risk_metrics(0, zeros, closed_count=5)
    assert no_returns["risk_metric_status"] == "unavailable"

    positive_from_zero = [_equity(index, 100 + index * (index + 1)) for index in range(60)]
    zero_baseline = calculate_risk_metrics(0, positive_from_zero, closed_count=5)
    assert zero_baseline["risk_metric_status"] == "available"
    assert zero_baseline["calmar_ratio"] is None


def test_drawdown_duration_reports_recovered_and_unrecovered_troughs() -> None:
    recovered = [
        _equity(0, 100, drawdown=0),
        _equity(1, 98, drawdown=-2),
        _equity(2, 95, drawdown=-5),
        _equity(3, 97, drawdown=-3),
        _equity(4, 101, drawdown=0),
    ]
    assert drawdown_durations(recovered) == (3, 2)

    unrecovered = [
        _equity(0, 100, drawdown=0),
        _equity(1, 99, drawdown=-1),
        _equity(2, 96, drawdown=-4),
    ]
    assert drawdown_durations(unrecovered) == (2, None)


def test_cost_profile_from_run_prefers_snapshot_then_id_and_falls_back_to_base() -> None:
    stress = resolve_cost_profile("stress")
    snapshotted = _run(
        configuration={"cost_profile": stress.model_dump(mode="json")},
        cost_profile_id="missing",
    )
    assert cost_profile_from_run(snapshotted) == stress

    invalid_snapshot = _run(
        configuration={"cost_profile": {"profile_id": "broken"}},
        cost_profile_id=stress.profile_id,
    )
    assert cost_profile_from_run(invalid_snapshot) == stress

    unknown = _run(configuration={"cost_profile": "legacy"}, cost_profile_id="unknown")
    assert cost_profile_from_run(unknown).profile_id == resolve_cost_profile("base").profile_id
    assert cost_profile_from_run(None).profile_id == resolve_cost_profile("base").profile_id


def _equity(index: int, total: float, *, drawdown: float = 0) -> PaperEquityPoint:
    return PaperEquityPoint(
        id=index + 1,
        run_id=1,
        as_of_date=f"2026-01-{index + 1:02d}",
        cash_balance=total,
        market_value=0,
        estimated_exit_friction=0,
        total_equity=total,
        gross_equity=total,
        cumulative_cost=0,
        realized_pnl=total - 1_000_000,
        unrealized_pnl=0,
        return_pct=0,
        gross_return_pct=0,
        exposure_pct=index % 10,
        drawdown_pct=drawdown,
        created_at="2026-01-01 16:00:00",
    )


def _strategy(strategy_id: int, *, realized_pnl: float) -> PaperStrategy:
    return PaperStrategy(
        id=strategy_id,
        plan_id=strategy_id,
        plan_revision=1,
        advice_id=strategy_id,
        symbol=f"600{strategy_id:03d}.SH",
        activation_market_time="2026-01-01 09:30:00",
        allocation_pct=10,
        snapshot_market_time="2025-12-31 15:00:00",
        snapshot_price=100,
        snapshot_adjustment_mode="qfq",
        snapshot_data_version="test-v1",
        snapshot_contract_version="daily-kline-v1",
        target_price=110,
        stop_price=90,
        horizon_days=5,
        status="closed",
        realized_pnl=realized_pnl,
        created_at="2026-01-01 09:30:00",
        updated_at="2026-01-10 15:00:00",
    )


def _run(*, configuration: dict[str, object], cost_profile_id: str) -> PaperTradingRun:
    return PaperTradingRun.model_validate(
        {
            "id": 1,
            "as_of": "2026-01-10 16:00:00",
            "rule_version": "paper-v1",
            "cost_profile_id": cost_profile_id,
            "strategy_count": 0,
            "execution_count": 0,
            "closed_count": 0,
            "data_unavailable_count": 0,
            "configuration": configuration,
            "message": "test",
            "created_at": "2026-01-10 16:00:00",
        }
    )
