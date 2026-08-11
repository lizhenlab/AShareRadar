from __future__ import annotations

import pytest

from app.models.market import Kline
from app.services.market_scan_probability_labels import (
    PROBABILITY_EXECUTION_MODEL,
    PROBABILITY_LABEL_VERSION,
    ProbabilityLabelConfig,
    build_probability_label_outcomes,
    probability_label_contract,
)


def test_probability_label_uses_next_open_and_holding_horizon_close_with_costs() -> None:
    rows = _rows(
        ("2026-01-05", 100, 100, 101, 99, 1_000),
        ("2026-01-06", 100, 101, 103, 99, 1_000),
        ("2026-01-07", 105, 110, 112, 104, 1_000),
    )

    outcome = _labels(rows, horizons=(1,))[1]

    assert outcome.status == "modelled"
    assert outcome.reason == "target_close"
    assert outcome.entry_date == "2026-01-06"
    assert outcome.exit_date == "2026-01-07"
    assert outcome.entry_price == 100
    assert outcome.exit_price == 110
    assert outcome.gross_return == pytest.approx(0.10)
    assert outcome.net_return is not None and 0 < outcome.net_return < outcome.gross_return
    assert outcome.cost_drag is not None and outcome.cost_drag > 0
    assert outcome.label == 1
    assert outcome.rule_profile_verified is True
    assert outcome.daily_bar_model_limited is True


def test_probability_label_never_assumes_locked_limit_entry_or_exit() -> None:
    locked_entry = _rows(
        ("2026-01-05", 100, 100, 100, 100, 1_000),
        ("2026-01-06", 110, 110, 110, 110, 1_000),
        ("2026-01-07", 111, 112, 113, 110, 1_000),
    )
    assert _labels(locked_entry, horizons=(1,))[1].reason == "locked_limit_up"
    assert _labels(locked_entry, horizons=(1,))[1].status == "unfilled"

    locked_exit = _rows(
        ("2026-01-05", 100, 100, 101, 99, 1_000),
        ("2026-01-06", 100, 100, 101, 99, 1_000),
        ("2026-01-07", 90, 90, 90, 90, 1_000),
    )
    outcome = _labels(locked_exit, horizons=(1,))[1]
    assert outcome.status == "unfilled"
    assert outcome.reason == "locked_limit_down"
    assert outcome.net_return is None


def test_probability_label_rejects_suspension_capacity_and_incomplete_horizon() -> None:
    suspended = _rows(
        ("2026-01-05", 100, 100, 101, 99, 1_000),
        ("2026-01-06", 100, 100, 100, 100, 0),
        ("2026-01-07", 100, 101, 102, 99, 1_000),
    )
    assert _labels(suspended, horizons=(1,))[1].reason == "suspended_or_zero_volume"

    capacity = _labels(
        _rows(
            ("2026-01-05", 100, 100, 101, 99, 1_000),
            ("2026-01-06", 100, 101, 102, 99, 1_000),
            ("2026-01-07", 101, 102, 103, 100, 1_000),
        ),
        horizons=(1,),
        amount=1_000,
    )[1]
    assert capacity.status == "unfilled"
    assert capacity.reason == "daily_capacity_limit"

    incomplete = _labels(
        _rows(
            ("2026-01-05", 100, 100, 101, 99, 1_000),
            ("2026-01-06", 100, 101, 102, 99, 1_000),
        ),
        horizons=(1,),
    )[1]
    assert incomplete.status == "data_unavailable"
    assert incomplete.reason == "target_date_missing"


def test_probability_label_contract_is_versioned_and_rejects_conflicting_bars() -> None:
    contract = probability_label_contract(ProbabilityLabelConfig(horizons=(1, 5, 20)))

    assert contract["label_version"] == PROBABILITY_LABEL_VERSION
    assert contract["execution_model"] == PROBABILITY_EXECUTION_MODEL
    assert contract["horizons"] == [1, 5, 20]
    assert contract["cost_model_version"]
    assert set(contract["target_definitions"]) == {
        "absolute_net_return_positive",
        "equal_weight_market_net_excess_positive",
    }

    duplicate = _rows(
        ("2026-01-05", 100, 100, 101, 99, 1_000),
        ("2026-01-05", 100, 101, 102, 99, 1_000),
        ("2026-01-06", 100, 101, 102, 99, 1_000),
    )
    with pytest.raises(ValueError, match="conflicting probability label bar"):
        _labels(duplicate, horizons=(1,))


def test_probability_label_fails_closed_when_effective_rule_profile_is_unverified() -> None:
    rows = _rows(
        ("2026-01-05", 100, 100, 101, 99, 1_000),
        ("2026-01-06", 100, 101, 102, 99, 1_000),
        ("2026-01-07", 101, 102, 103, 100, 1_000),
    )

    outcome = build_probability_label_outcomes(
        symbol="600001.SH",
        market="SH",
        list_date=None,
        is_st=False,
        quote_date="2026-01-05",
        amount=1_000_000_000,
        rows=rows,
        eligible_dates=("2026-01-06", "2026-01-07"),
        config=ProbabilityLabelConfig(horizons=(1,)),
    )[1]

    assert outcome.status == "data_unavailable"
    assert outcome.reason == "entry_rule_profile_degraded"
    assert outcome.net_return is None
    assert outcome.rule_profile_verified is False


def _labels(
    rows: list[Kline],
    *,
    horizons: tuple[int, ...],
    amount: float = 1_000_000_000,
):
    return build_probability_label_outcomes(
        symbol="600001.SH",
        market="SH",
        list_date="2020-01-02",
        is_st=False,
        quote_date="2026-01-05",
        amount=amount,
        rows=rows,
        eligible_dates=tuple(row.date for row in rows if row.date > "2026-01-05"),
        config=ProbabilityLabelConfig(horizons=horizons),
    )


def _rows(*values: tuple[str, float, float, float, float, float]) -> list[Kline]:
    return [
        Kline(
            date=row_date,
            open=open_price,
            close=close,
            high=high,
            low=low,
            volume=volume,
            adjustment_mode="qfq",
            data_version="probability-test-v1",
            contract_version="daily-kline.v1",
            source="test",
        )
        for row_date, open_price, close, high, low, volume in values
    ]
