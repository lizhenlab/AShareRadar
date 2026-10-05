"""Net scenarios preserve frozen cash budgets and require strict official tokens."""

from dataclasses import replace
from datetime import datetime
import hashlib
import json

import pytest

from app.services.market_scan_official_execution import (
    load_verified_official_execution_session, load_verified_official_execution_source_registry,
    seal_official_execution_raw_file_receipt, seal_official_execution_session_artifact,
    seal_official_execution_source_registry,
)
from app.services.strategy_template_net_returns import evaluate_strategy_template_net_returns
from app.services import strategy_template_net_returns as net_returns
from app.services.strategy_template_tracking import strategy_template_tracking_contracts
from app.services.strategy_template_tracking_metrics import (
    STRATEGY_TEMPLATE_TRACKING_IDS, StrategyTrackingPosition, StrategyTrackingSelection, StrategyTrackingSession,
)
from app.services.trading_calendar import ASHARE_TIMEZONE, TradingCalendarCoverageError, next_trade_dates
from datetime import date
from tests.test_market_scan_research_portfolio import row


DATES = ("2026-08-24", "2026-08-25", "2026-08-26")
AS_OF = datetime(2026, 8, 28, 16, tzinfo=ASHARE_TIMEZONE)
SYMBOL = "600519.SH"


def _session(*, positions=None, status="ready", day=DATES[0], run_id=1, rule="fixture-rule"):
    holdings = positions if positions is not None else (StrategyTrackingPosition(SYMBOL, "fixture", None, .1, 10.0),)
    selections = tuple(StrategyTrackingSelection(
        template_id, status, 10, 1 if holdings else 0, holdings, sum(item.target_weight for item in holdings),
        900_000.0, 100.0, "c" * 64, "d" * 64, "missing" if holdings else "no_selection", None,
    ) for template_id in STRATEGY_TEMPLATE_TRACKING_IDS)
    return StrategyTrackingSession(run_id, day, DATES[1], rule, "a" * 64, "b" * 64, day + "T16:00:00+08:00", selections)


@pytest.fixture
def templates():
    return strategy_template_tracking_contracts()[1]


def _official(tmp_path, rows, *, generated_at=None, acquired_at=None):
    uri = "https://licensed.example.test/daily"
    registry_payload = seal_official_execution_source_registry([{
        "source_id": "synthetic", "dataset_id": "fixture", "provider_legal_name": "Fixture Licensed Provider",
        "markets": ["SH"], "authority_basis": "exchange_direct_subscription", "delivery_channel": "https",
        "source_base_uri": uri, "license_reference": "fixture-license", "license_document_sha256": "e" * 64,
        "valid_from": "2026-01-01", "valid_through": "2026-12-31", "research_use_authorized": True,
    }], registered_at="2026-08-01T09:00:00+08:00")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_payload))
    registry = load_verified_official_execution_source_registry(registry_path, expected_registry_digest=registry_payload["registry_digest"])
    sessions = []
    for day in sorted({item.session_date for item in rows}):
        raw = b"strict loader fixture" + day.encode()
        relative = day + ".raw"
        (tmp_path / relative).write_bytes(raw)
        receipt = seal_official_execution_raw_file_receipt({
            "source_id": "synthetic", "dataset_id": "fixture", "market": "SH", "session_date": day,
            "relative_path": relative, "byte_size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "source_uri": uri + "/" + relative, "available_at": day + "T15:01:00+08:00",
            "acquired_at": acquired_at or day + "T15:02:00+08:00", "parser_version": "fixture-v1", "license_reference": "fixture-license",
        })
        source_rows = [{**item.model_dump(mode="json"), "receipt_digest": receipt["receipt_digest"]} for item in rows if item.session_date == day]
        artifact = seal_official_execution_session_artifact(
            session_date=day, generated_at=generated_at or day + "T15:06:00+08:00",
            source_registry_digest=registry.registry_digest, receipts=[receipt], rows=source_rows,
        )
        artifact_path = tmp_path / (day + ".json")
        artifact_path.write_text(json.dumps(artifact))
        sessions.append(load_verified_official_execution_session(artifact_path, registry=registry, raw_file_root=tmp_path))
    return tuple(sessions)


def _report(templates, *, session=None, official=(), **kwargs):
    return evaluate_strategy_template_net_returns(
        (session or _session(),), templates, as_of=kwargs.pop("as_of", AS_OF), horizon=kwargs.pop("horizon", 1),
        notional_cash_cny=kwargs.pop("notional_cash_cny", 1_000_000.0), official_sessions=official, **kwargs,
    )


def _first(report):
    return report["cohorts"][0]["sessions"][0]["selections"][0]


def test_strict_tokens_execute_t1_with_fees_inside_original_weight_budget(tmp_path, templates):
    rows = [row(day, price=11 if day == DATES[2] else 10, amount=200_000_000) for day in DATES]
    report = _report(templates, official=_official(tmp_path, rows))
    session = report["cohorts"][0]["sessions"][0]
    assert (session["entry_date"], session["exit_date"]) == (DATES[1], DATES[2])
    assert report["execution_evidence"]["provenance_status"] == "official_raw_file_verified"
    assert report["continuous_portfolio_simulation"] is False and report["promotion_eligible"] is False
    selected = _first(report)
    base, stress = selected["base"], selected["stress"]
    bought = base["positions"][0]
    assert selected["status"] == "available"
    assert bought["budget_cny"] == 100_000 and bought["quantity"] == 9_900
    assert bought["buy_amount_cny"] + bought["buy_fees_cny"] <= bought["budget_cny"]
    assert selected["net_return"] == pytest.approx(bought["pnl_cny"] / 1_000_000)
    assert selected["net_return"] < .01  # Uninvested 90% remains cash; no renormalization.
    assert stress["total_fees_cny"] > base["total_fees_cny"]
    assert selected["stress_net_return"] < selected["net_return"]
    assert base["residual_cash_cny"] == pytest.approx(1_000_000 + bought["pnl_cny"])
    assert -.001 < base["independent_batch_max_drawdown"] < 0
    policy, costs = templates[0]["strategy_spec"]["execution_policy"], report["cost_specs"][0]
    assert costs["base"]["commission_rate_pct"] == policy["commission_rate"] * 100
    assert costs["base"]["slippage_buy_pct"] == policy["buy_slippage_bps"] / 100
    assert costs["stress"]["stamp_duty_sell_pct"] == costs["base"]["stamp_duty_sell_pct"]
    assert costs["stress"]["transfer_fee_pct"] == costs["base"]["transfer_fee_pct"]


def test_no_official_evidence_is_unavailable_not_zero_and_raw_rows_are_rejected(templates):
    first = _first(_report(templates))
    assert first["net_return"] is None and first["stress_net_return"] is None
    assert first["reason_codes"] == ["official_execution_evidence_unavailable"]
    with pytest.raises(ValueError, match="strict-loader"):
        _report(templates, official=(row(DATES[0]),))


@pytest.mark.parametrize("change,reason", [
    ({"state": "suspended"}, "entry_session_suspended"),
    ({"entry_state": "locked_limit"}, "entry_locked_limit"),
    ({"entry_state": "capacity_exceeded"}, "entry_capacity_exceeded"),
])
def test_proven_unfilled_entry_stays_cash_without_replacement(tmp_path, templates, change, reason):
    official = _official(tmp_path, [row(DATES[1], **change)])
    first = _first(_report(templates, official=official))
    assert first["status"] == "available" and first["net_return"] == 0
    assert first["base"]["unfilled_count"] == 1 and first["base"]["filled_count"] == 0
    assert first["base"]["independent_batch_max_drawdown"] == 0
    assert first["reason_codes"] == [reason]


@pytest.mark.parametrize("mutation,reason", [
    ("missing_entry", "entry_session_evidence_missing"),
    ("missing_previous", "previous_session_missing"),
    ("missing_exit", "holding_path_evidence_missing"),
    ("action", "corporate_action_ledger_required"),
    ("blocked_exit", "exit_locked_limit"),
    ("reference", "previous_close_reference_conflict"),
    ("exit_capacity", "exit_prior_session_capacity_exceeded"),
])
def test_any_unresolved_position_invalidates_whole_original_basket(tmp_path, templates, mutation, reason):
    rows = [row(day) for day in DATES]
    if mutation.startswith("missing_"):
        index = {"missing_entry": 1, "missing_previous": 0, "missing_exit": 2}[mutation]
        rows.pop(index)
    elif mutation == "action":
        rows[2] = row(DATES[2], action="effective_event")
    elif mutation == "blocked_exit":
        rows[2] = row(DATES[2], exit_state="locked_limit")
    elif mutation == "reference":
        rows[2] = row(DATES[2], previous=9)
    else:
        rows[1] = row(DATES[1], amount=100)
    rows.extend(row(day, symbol="600001.SH") for day in DATES)
    positions = (StrategyTrackingPosition(SYMBOL, "one", None, .1, 10), StrategyTrackingPosition("600001.SH", "two", None, .1, 10))
    first = _first(_report(templates, session=_session(positions=positions), official=_official(tmp_path, rows)))
    assert first["status"] == "unavailable" and first["net_return"] is None
    assert first["base"]["unavailable_count"] == 1
    assert first["base"]["positions"][1]["status"] == "available"
    assert first["base"]["positions"][1]["budget_cny"] == 100_000
    assert first["base"]["independent_batch_max_drawdown"] is None
    assert reason in first["reason_codes"]


def test_cost_and_lot_capacity_use_prior_turnover_not_future_turnover(tmp_path, templates):
    rows = [row(day, amount=100 if day == DATES[0] else 1e12) for day in DATES]
    first = _first(_report(templates, official=_official(tmp_path, rows)))
    assert first["net_return"] == 0 and first["base"]["filled_count"] == 0
    assert first["reason_codes"] == ["cash_or_prior_capacity_below_minimum_lot"]


def test_empty_no_trade_is_cash_but_blocked_is_never_cash(templates):
    cash = _first(_report(templates, session=_session(positions=(), status="no_trade")))
    assert cash["net_return"] == 0 and cash["base"]["independent_batch_max_drawdown"] == 0
    assert cash["reason_codes"] == ["no_selection"]
    blocked = _first(_report(templates, session=_session(positions=(), status="blocked")))
    assert blocked["status"] == "blocked" and blocked["net_return"] is None


@pytest.mark.parametrize("field", ["generated_at", "acquired_at"])
def test_future_evidence_cannot_be_seen_by_historical_report(tmp_path, templates, field):
    official = _official(tmp_path, [row(day) for day in DATES], **{field: "2026-09-01T16:00:00+08:00"})
    first = _first(_report(templates, official=official))
    assert first["status"] == "unavailable"
    assert first["reason_codes"] == ["execution_evidence_after_as_of"]


def test_future_cached_exit_does_not_bypass_d_plus_h_plus_one_maturity(tmp_path, templates):
    official = _official(tmp_path, [row(day) for day in DATES])
    first = _first(_report(templates, official=official, as_of=datetime(2026, 8, 25, 16, tzinfo=ASHARE_TIMEZONE)))
    assert first["status"] == "pending" and first["net_return"] is None
    assert first["reason_codes"] == ["outcome_not_mature"]


def test_contract_cohorts_stay_separate_and_duplicate_sources_are_rejected(tmp_path, templates):
    official = _official(tmp_path, [row(day) for day in DATES])
    sessions = (_session(), _session(run_id=2, rule="second-contract"))
    report = evaluate_strategy_template_net_returns(sessions, templates, as_of=AS_OF, horizon=1,
                                                   notional_cash_cny=1_000_000, official_sessions=official)
    assert len(report["cohorts"]) == 2
    with pytest.raises(ValueError, match="duplicate"):
        _report(templates, official=official + official)


@pytest.mark.parametrize("kwargs", [
    {"horizon": True}, {"horizon": 2}, {"notional_cash_cny": True}, {"notional_cash_cny": float("nan")},
    {"notional_cash_cny": float("inf")}, {"notional_cash_cny": 10 ** 400}, {"notional_cash_cny": 10000.001},
    {"as_of": AS_OF.replace(tzinfo=None)}, {"as_of": datetime(2100, 1, 1, tzinfo=ASHARE_TIMEZONE)},
])
def test_invalid_report_inputs_reject_without_execution(templates, kwargs):
    with pytest.raises(ValueError):
        _report(templates, **kwargs)


def test_tampered_template_weights_symbol_and_late_publication_fail_closed(templates):
    invalid = [{**item, "strategy_fingerprint": "0" * 64} for item in templates]
    with pytest.raises(ValueError, match="fingerprint"):
        _report(invalid)
    for position in (StrategyTrackingPosition(SYMBOL, "x", None, float("nan"), 10),
                     StrategyTrackingPosition("wrong", "x", None, .1, 10)):
        with pytest.raises(ValueError):
            _report(templates, session=_session(positions=(position,)))
    with pytest.raises(ValueError, match="publication|published"):
        _report(templates, session=replace(_session(), published_at="2026-08-25T10:00:00+08:00"))


def test_readonly_engine_does_not_mutate_selection_and_changes_only_labels(tmp_path, templates):
    frozen = _session()
    before = repr(frozen)
    base = _report(templates, session=frozen, official=_official(tmp_path, [row(day, amount=200_000_000) for day in DATES]))
    changed = _report(templates, session=frozen, official=_official(tmp_path, [row(day, price=11 if day == DATES[2] else 10, amount=200_000_000) for day in DATES]))
    assert repr(frozen) == before
    assert _first(base)["status"] == _first(changed)["status"] == "available"
    one, two = _first(base)["base"]["positions"][0], _first(changed)["base"]["positions"][0]
    assert (one["symbol"], one["target_weight"], one["quantity"]) == (two["symbol"], two["target_weight"], two["quantity"])
    assert _first(base)["net_return"] != _first(changed)["net_return"]
    assert base["execution_evidence"]["manifest_digest"] != changed["execution_evidence"]["manifest_digest"]


def test_cost_pressure_resizes_lots_without_changing_frozen_weight(tmp_path, templates):
    official = _official(tmp_path, [row(day, amount=200_000_000) for day in DATES])
    first = _first(_report(templates, official=official, notional_cash_cny=1_001_000))
    base, stress = first["base"]["positions"][0], first["stress"]["positions"][0]
    assert base["target_weight"] == stress["target_weight"] == .1
    assert base["budget_cny"] == stress["budget_cny"] == 100_100
    assert base["quantity"] == 10_000 and stress["quantity"] == 9_900


def test_cash_selection_waits_for_calendar_maturity(templates, monkeypatch):
    frozen = _session(positions=(), status="no_trade")
    pending = _first(_report(templates, session=frozen, as_of=datetime(2026, 8, 25, 16, tzinfo=ASHARE_TIMEZONE)))
    assert pending["status"] == "pending" and pending["net_return"] is None
    def uncovered(value, count, **kwargs):
        assert kwargs == {"allow_auto_refresh": False}
        if count > 1:
            raise TradingCalendarCoverageError("fixture")
        return next_trade_dates(value, count, **kwargs)
    monkeypatch.setattr(net_returns, "next_trade_dates", uncovered)
    unavailable = _first(_report(templates, session=frozen))
    assert unavailable["status"] == "unavailable" and unavailable["net_return"] is None
    assert unavailable["reason_codes"] == ["calendar_unavailable"]


def test_signal_qfq_price_does_not_reuse_adjusted_share_quantity(tmp_path, templates):
    official = _official(tmp_path, [row(day) for day in DATES])
    normal = _first(_report(templates, official=official))
    rebased = _session(positions=(StrategyTrackingPosition(SYMBOL, "rebased", None, .1, 2.5),))
    comparison = _first(_report(templates, session=rebased, official=official))
    assert normal == comparison


def test_suspended_previous_day_has_no_usable_capacity_or_reference(tmp_path, templates):
    official = _official(tmp_path, [row(day, state="suspended" if day == DATES[0] else "trading") for day in DATES])
    first = _first(_report(templates, official=official))
    assert first["status"] == "unavailable" and first["base"]["filled_count"] == 0
    assert first["reason_codes"] == ["previous_session_missing"]


@pytest.mark.parametrize("horizon", [1, 5, 10, 20])
def test_supported_horizons_use_exact_entry_then_t1_compliant_exit(templates, horizon):
    report = _report(templates, horizon=horizon)
    session = report["cohorts"][0]["sessions"][0]
    assert session["entry_date"] == DATES[1]
    assert session["exit_date"] == next_trade_dates(date.fromisoformat(DATES[0]), horizon + 1, allow_auto_refresh=False)[-1].isoformat()


def test_intermediate_close_drawdown_is_independent_batch_and_not_endpoint_only(tmp_path, templates):
    days = (DATES[0], *(value.isoformat() for value in next_trade_dates(date.fromisoformat(DATES[0]), 6, allow_auto_refresh=False)))
    prices = [10, 10, 8, 10, 10, 10, 10]
    official = _official(tmp_path, [row(day, price=prices[i], previous=prices[max(0, i - 1)], amount=200_000_000) for i, day in enumerate(days)])
    first = _first(_report(templates, horizon=5, official=official, as_of=datetime(2026, 9, 2, 16, tzinfo=ASHARE_TIMEZONE)))
    assert first["status"] == "available"
    assert first["base"]["independent_batch_max_drawdown"] < -.0198
    assert first["base"]["independent_batch_max_drawdown"] < first["net_return"]


def test_signal_on_weekend_and_overallocated_cash_fail_closed(templates):
    with pytest.raises(ValueError, match="canonical date"):
        _report(templates, session=_session(day="2026-08-23"))
    positions = tuple(StrategyTrackingPosition(f"60000{index}.SH", "fixture", None, .50000000001, 10) for index in range(2))
    original = _session(positions=positions)
    selections = tuple(replace(item, target_invested_weight=1.0) for item in original.selections)
    with pytest.raises(ValueError, match="exceed capital"):
        _report(templates, session=replace(original, selections=selections))


@pytest.mark.parametrize("end_price,reason", [(1e307, "nonfinite_exit_amount"), (10.0, "nonfinite_position_valuation")])
def test_finite_extreme_official_prices_never_overflow_into_a_return(tmp_path, templates, end_price, reason):
    days = (DATES[0], *(value.isoformat() for value in next_trade_dates(date.fromisoformat(DATES[0]), 6, allow_auto_refresh=False)))
    prices = [10, 10, 1e307, 10, 10, 10, end_price]
    official = _official(tmp_path, [row(day, price=prices[i], previous=prices[max(0, i - 1)], amount=200_000_000) for i, day in enumerate(days)])
    first = _first(_report(templates, horizon=5, official=official, as_of=datetime(2026, 9, 2, 16, tzinfo=ASHARE_TIMEZONE)))
    assert first["status"] == "unavailable" and first["net_return"] is None
    assert first["reason_codes"] == [reason]


def test_missing_intermediate_session_is_not_replaced_by_a_later_bar(tmp_path, templates):
    days = (DATES[0], *(value.isoformat() for value in next_trade_dates(date.fromisoformat(DATES[0]), 6, allow_auto_refresh=False)))
    official = _official(tmp_path, [row(day) for index, day in enumerate(days) if index != 2])
    first = _first(_report(templates, horizon=5, official=official, as_of=datetime(2026, 9, 2, 16, tzinfo=ASHARE_TIMEZONE)))
    assert first["net_return"] is None and first["base"]["filled_count"] == 1
    assert first["reason_codes"] == ["holding_path_evidence_missing"]


def test_opaque_official_tokens_cannot_be_bypassed_with_json_verified_flags(templates):
    with pytest.raises(ValueError, match="strict-loader"):
        _report(templates, official=({"verified": True, "official": True, "rows": [row(DATES[1]).model_dump()]},))


def test_official_state_contract_rejects_unknown_and_nonfinite_data():
    with pytest.raises(ValueError):
        row(DATES[1], entry_state="unknown")
    with pytest.raises(ValueError):
        row(DATES[1], price=float("nan"))


def test_duplicate_frozen_sessions_cannot_multiply_return_observations(templates):
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_strategy_template_net_returns((_session(), _session()), templates,
                                               as_of=AS_OF, horizon=1, notional_cash_cny=1_000_000)


@pytest.mark.parametrize("price", [1e-308, 5e-324])
def test_extremely_small_official_prices_cannot_crash_or_emit_inexact_quantities(tmp_path, templates, price):
    official = _official(tmp_path, [row(day, price=price, previous=price) for day in DATES])
    report = _report(templates, official=official)
    first = _first(report)
    assert first["status"] == "unavailable" and first["net_return"] is None
    assert first["reason_codes"] == ["position_quantity_not_representable"]
    assert first["base"]["positions"][0]["quantity"] == 0
    assert first["base"]["filled_count"] == 0
    assert report["maximum_exact_quantity"] == 2 ** 53 - 1
    json.dumps(report, allow_nan=False)
