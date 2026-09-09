"""Opening execution evidence must not claim anything about a later close."""

from dataclasses import asdict
from datetime import date

import pytest

from app.models.paper_trading import PaperInstrumentMetadata
from app.services import market_scan_evaluation as evaluation
from app.services.market_scan_probability_labels import ProbabilityLabelConfig, build_probability_label_outcomes
from app.services.paper_trading_rules import assess_daily_tradeability, resolve_trade_rule_profile
from tests.test_market_scan_evaluation_execution_evidence import _rows


def _outcomes(rows):
    common = {
        "symbol": "600001.SH", "market": "SH", "list_date": "2020-01-02", "is_st": False,
        "quote_date": "2026-08-11", "amount": 200_000_000,
        "eligible_dates": ("2026-08-12", "2026-08-13"),
    }
    label = build_probability_label_outcomes(**common, rows=rows, config=ProbabilityLabelConfig(horizons=(1,)))[1]
    raw = [{**row.model_dump(), "point_in_time": int(row.point_in_time)} for row in rows]
    result = evaluation._execution_outcomes(
        **common, is_new=False, bars=raw, config=evaluation.EvaluationConfig(horizons=(1,)),
    )[1]
    return result, label


@pytest.mark.parametrize("opening_status", ["locked_limit_down", "locked_limit_up", "unavailable", "unknown"])
def test_fixed_close_ignores_only_opening_restriction_and_keeps_model_uncertainty(opening_status):
    rows = _rows()
    ordinary = _outcomes(rows)
    rows[-1] = rows[-1].model_copy(update={"open_execution_status": opening_status})
    observed = _outcomes(rows)
    for before, after in zip(ordinary, observed, strict=True):
        assert after.status == "modelled"
        assert after.model_limited is True
        assert asdict(after) == asdict(before)
    assert observed[1].daily_bar_model_limited is True


@pytest.mark.parametrize("update", [
    {"session_status": "suspended"},
    {"volume": 0},
    {"open": 9.45, "close": 9.45, "high": 9.45, "low": 9.45},
])
def test_close_still_rejects_session_suspension_zero_volume_and_daily_locked_limit(update):
    rows = _rows()
    rows[-1] = rows[-1].model_copy(update={"open_execution_status": "tradable", **update})
    for outcome in _outcomes(rows):
        assert outcome.status == "unfilled"
        assert outcome.net_return is None


@pytest.mark.parametrize("opening_status", ["locked_limit_up", "unavailable"])
def test_next_open_entry_remains_unfilled(opening_status):
    rows = _rows()
    rows[1] = rows[1].model_copy(update={"open_execution_status": opening_status})
    for outcome in _outcomes(rows):
        assert outcome.status == "unfilled"
        assert outcome.net_return is None


def test_shared_tradeability_requires_explicit_valid_phase_and_defaults_to_open():
    row = _rows()[-1].model_copy(update={"open_execution_status": "locked_limit_down"})
    profile = resolve_trade_rule_profile(
        "600001.SH", date.fromisoformat(row.date),
        metadata=PaperInstrumentMetadata(
            symbol="600001.SH", list_date="2020-01-02", is_st=False, status_effective_date=row.date,
        ),
    )
    assert assess_daily_tradeability(row, previous_close=10.5, profile=profile).can_sell is False
    close = assess_daily_tradeability(row, previous_close=10.5, profile=profile, execution_phase="close")
    assert close.can_sell and close.model_limited
    with pytest.raises(ValueError, match="execution phase"):
        assess_daily_tradeability(row, previous_close=10.5, profile=profile, execution_phase="clsoe")


def test_unverified_historical_rule_does_not_gain_probability_label_authority():
    result = build_probability_label_outcomes(
        symbol="600001.SH", market="SH", list_date=None, is_st=False, quote_date="2026-08-11",
        amount=200_000_000, rows=_rows(), eligible_dates=("2026-08-12", "2026-08-13"),
        config=ProbabilityLabelConfig(horizons=(1,)),
    )[1]
    assert result.status == "data_unavailable" and result.label is None
    assert result.rule_profile_verified is False


def test_demo_source_is_still_rejected_for_close_labels():
    rows = _rows()
    rows[-1] = rows[-1].model_copy(update={"source": "demo"})
    with pytest.raises(ValueError, match="demo probability label bar"):
        _outcomes(rows)
