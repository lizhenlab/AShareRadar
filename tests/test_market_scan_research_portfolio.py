from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import pytest

from app.services.market_scan_official_execution import (
    OfficialExecutionSessionRow,
    load_verified_official_execution_session,
    load_verified_official_execution_source_registry,
    seal_official_execution_raw_file_receipt,
    seal_official_execution_session_artifact,
    seal_official_execution_session_row,
    seal_official_execution_source_registry,
)
from app.services.market_scan_research_portfolio import (
    ResearchPortfolioConfig,
    ResearchSignalBatch,
    ResearchSignalCandidate,
    replay_research_portfolio,
    research_portfolio_payload,
)


DATES = ("2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31")
SYMBOL = "600519.SH"


def row(day: str, *, symbol: str = SYMBOL, price: float = 10, previous: float = 10,
        exit_state: str = "executable", state: str = "trading", action: str = "none",
        entry_state: str = "executable", amount: float = 100_000_000) -> OfficialExecutionSessionRow:
    raw = {
        "symbol": symbol, "code": symbol[:6], "market": symbol[-2:], "session_date": day,
        "observed_at": day + "T15:05:00+08:00", "source_id": "synthetic", "dataset_id": "fixture",
        "receipt_digest": "a" * 64, "source_record_id": symbol + day,
        "exchange_session_state": state, "entry_execution_state": entry_state,
        "entry_reason_code": "fixture_entry", "exit_execution_state": exit_state,
        "exit_reason_code": "fixture_exit",
        "instrument_rules": {
            "effective_date": day, "board": "main", "is_st": False, "listing_status": "listed",
            "board_rule_id": "fixture", "st_rule_id": "fixture", "delisting_rule_id": "fixture",
            "minimum_buy_quantity": 100, "buy_quantity_step": 100, "sell_quantity_step": 1,
            "price_limit_pct": .1,
        },
        "corporate_action": {
            "status": action, "event_id": "event" if action == "effective_event" else None,
            "previous_close": float(previous), "reference_price": float(previous),
            "reference_price_rule_id": "fixture",
        },
        "bar": {
            "adjustment_mode": "none", "open": float(price), "close": float(price),
            "high": float(price), "low": float(price), "volume": 1_000_000., "amount": float(amount),
        },
    }
    if state != "trading":
        raw["bar"] = {"adjustment_mode": "none"}
        raw["entry_execution_state"] = "suspended" if state == "suspended" else "rule_ineligible"
        raw["exit_execution_state"] = raw["entry_execution_state"]
    return OfficialExecutionSessionRow.model_validate(seal_official_execution_session_row(raw))


def batch(day: str, *, symbol: str = SYMBOL, batch_id: str | None = None) -> ResearchSignalBatch:
    return ResearchSignalBatch(batch_id or day, day, "b" * 64, (ResearchSignalCandidate(symbol, 1, "c" * 64),))


def replay(*, batches: tuple[ResearchSignalBatch, ...] | None = None,
           rows: tuple[OfficialExecutionSessionRow, ...] | None = None,
           sessions: tuple[str, ...] = DATES, **config: object):
    return replay_research_portfolio(
        batches or (batch(DATES[0]),), sessions,
        synthetic_rows=rows or tuple(row(day) for day in DATES),
        config=ResearchPortfolioConfig(initial_cash=20_000, top_n=1, horizon=1, **config),
    )


def test_cash_conservation_fixed_close_horizon_and_fee_budget() -> None:
    result = replay()
    buy, sell = result.trades
    assert (buy.session_date, sell.session_date) == (DATES[1], DATES[2])
    assert buy.quantity == 900  # 1,000 shares would consume the cash needed for fees.
    assert buy.gross_amount + buy.fees <= 10_000
    assert result.days[-1].cash == pytest.approx(20_000 - buy.fees - sell.fees)
    assert result.days[-1].nav == result.days[-1].cash
    assert result.days[-1].positions == ()
    assert result.total_fees == buy.fees + sell.fees
    assert result.provenance_status == "synthetic"
    assert result.promotion_eligible is False


def test_overlapping_symbol_does_not_double_allocate_shared_cash() -> None:
    result = replay(batches=(batch(DATES[0]), batch(DATES[1])))
    assert sum(trade.side == "buy" for trade in result.trades) == 1
    assert any(event.reason == "symbol_already_held" for event in result.events)
    assert all(day.cash >= 0 for day in result.days)


def test_locked_exit_remains_in_account_and_blocks_sleeve_reuse() -> None:
    rows = tuple(row(day, exit_state="locked_limit" if day in DATES[2:4] else "executable") for day in DATES)
    result = replay(batches=(batch(DATES[0]), batch(DATES[2])), rows=rows)
    assert result.days[2].positions[0].quantity == 900
    assert result.days[3].positions[0].quantity == 900
    assert result.trades[-1].session_date == DATES[4]
    assert result.trades[-1].exit_delay_sessions == 2
    assert any(event.reason == "sleeve_still_invested" for event in result.events)


def test_missing_mark_never_drops_position_or_invents_drawdown() -> None:
    rows = tuple(row(day) for day in DATES if day != DATES[2])
    result = replay(rows=rows)
    day = result.days[2]
    assert day.nav is None and day.market_value is None and day.drawdown is None
    assert day.positions[0].quantity == 900 and day.positions[0].market_value is None
    assert result.maximum_drawdown is None
    assert len(result.trades) == 1  # The missing session also lacks corporate-action evidence.
    assert "corporate_action_history_incomplete" in result.days[-1].unresolved_reasons


def test_calendar_gap_rejected_and_missing_previous_bar_cannot_shift_entry() -> None:
    with pytest.raises(ValueError, match="complete.*calendar"):
        replay(sessions=DATES[:2] + DATES[3:])
    result = replay(rows=tuple(row(day) for day in DATES[1:]))
    assert result.trades == ()
    assert any(event.reason == "previous_session_missing" for event in result.events)
    assert result.total_return is None and result.maximum_drawdown is None
    assert result.unknown_entry_slots == 1 and result.entry_decision_evidence_coverage == 0


def test_corporate_action_does_not_continue_unchanged_shares() -> None:
    rows = tuple(row(day, action="effective_event" if day == DATES[2] else "none") for day in DATES)
    result = replay(rows=rows)
    assert len(result.trades) == 1
    assert result.days[-1].positions[0].quantity == 900
    assert result.days[-1].nav is None and result.maximum_drawdown is None
    assert "corporate_action_ledger_required" in result.days[-1].unresolved_reasons


def test_replay_order_is_canonical_and_input_changes_are_bound() -> None:
    result = replay()
    reordered = replay(rows=tuple(row(day) for day in reversed(DATES)))
    assert asdict(result) == asdict(reordered)
    changed = replay(batches=(ResearchSignalBatch("different", DATES[0], "b" * 64, batch(DATES[0]).candidates),))
    assert changed.input_digest != result.input_digest
    assert changed.result_digest != result.result_digest


def test_daily_nav_drawdown_counts_actual_shared_cash() -> None:
    rows = tuple(row(day, price=9 if day in DATES[1:3] else 10,
                     previous=9 if day in DATES[2:4] else 10) for day in DATES)
    result = replay(rows=rows)
    assert result.maximum_drawdown is not None
    assert result.maximum_drawdown == pytest.approx(result.days[-1].nav / 20_000 - 1)
    assert result.days[1].nav == pytest.approx(20_000 - result.trades[0].fees)


@pytest.mark.parametrize("settings", [
    {"initial_cash": float("nan")}, {"initial_cash": True}, {"initial_cash": 0}, {"initial_cash": 1.001},
    {"top_n": 0}, {"top_n": True}, {"top_n": 1.5}, {"horizon": -1}, {"cost_profile": "other"},
    {"max_participation_rate": float("nan")}, {"max_participation_rate": True}, {"max_participation_rate": 2},
    {"allocation": "unknown"},
])
def test_config_rejects_nonfinite_or_ambiguous_money(settings: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ResearchPortfolioConfig(**settings)


@pytest.mark.parametrize("bad_batch", [
    ResearchSignalBatch("", DATES[0], "b" * 64, ()),
    ResearchSignalBatch("id", "2026-08-23", "b" * 64, ()),
    ResearchSignalBatch("id", DATES[0], "bad", ()),
    ResearchSignalBatch("id", DATES[0], "b" * 64, (ResearchSignalCandidate("bad", 1, "c" * 64),)),
    ResearchSignalBatch("id", DATES[0], "b" * 64, (ResearchSignalCandidate(SYMBOL, 0, "c" * 64),)),
    ResearchSignalBatch("id", DATES[0], "b" * 64, (ResearchSignalCandidate(SYMBOL, True, "c" * 64),)),
    ResearchSignalBatch("id", DATES[0], "b" * 64, (ResearchSignalCandidate(SYMBOL, 1, "bad"),)),
    ResearchSignalBatch("id", DATES[0], "b" * 64, batch(DATES[0]).candidates * 2),
])
def test_invalid_frozen_identities_rejected(bad_batch: ResearchSignalBatch) -> None:
    with pytest.raises(ValueError):
        replay(batches=(bad_batch,))


def test_duplicate_batches_dates_rows_and_short_calendar_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        replay(batches=(batch(DATES[0]), batch(DATES[0])))
    with pytest.raises(ValueError, match="one frozen"):
        replay(batches=(batch(DATES[0]), batch(DATES[0], batch_id="different")))
    with pytest.raises(ValueError, match="duplicate"):
        replay(rows=(row(DATES[0]), row(DATES[0])))
    with pytest.raises(ValueError, match="calendar"):
        replay(sessions=DATES[:1])
    with pytest.raises(ValueError, match="calendar"):
        replay(sessions=tuple(reversed(DATES)))


@pytest.mark.parametrize("change,expected", [
    ({"entry_state": "locked_limit"}, "entry_locked_limit"),
    ({"state": "suspended"}, "session_suspended"),
    ({"previous": 9}, "previous_close_reference_conflict"),
    ({"action": "effective_event"}, "corporate_action_ledger_required"),
])
def test_entry_admission_retains_cash(change: dict[str, object], expected: str) -> None:
    rows = tuple(row(day, **change) if day == DATES[1] else row(day) for day in DATES)
    result = replay(rows=rows)
    assert result.trades == () and result.days[-1].cash == 20_000
    assert result.events[0].reason == expected


def test_capacity_uses_prior_turnover_and_keeps_fees_inside_budget() -> None:
    rows = tuple(row(day, amount=10 if day == DATES[0] else 100_000_000) for day in DATES)
    result = replay(rows=rows)
    assert result.trades == ()
    assert result.events[0].reason == "cash_or_prior_capacity_below_minimum_lot"


def test_suspension_has_no_fabricated_nav_but_can_resume_with_complete_action_history() -> None:
    rows = tuple(row(day, state="suspended" if day == DATES[2] else "trading") for day in DATES)
    result = replay(rows=rows)
    assert result.days[2].nav is None and result.days[2].positions
    assert result.trades[-1].session_date == DATES[4]
    assert result.days[-1].nav is not None
    assert result.maximum_drawdown is None and result.total_return is None


def test_missing_entry_evidence_and_unfilled_frozen_ranks_are_cash() -> None:
    empty = ResearchSignalBatch("empty", DATES[0], "b" * 64, ())
    result = replay(batches=(empty,))
    assert result.events[0].reason == "missing_frozen_slots_retain_cash"
    assert result.entry_fill_coverage == 0
    assert result.days[-1].cash == 20_000 and result.days[-1].nav is None
    missing = replay(rows=tuple(row(day) for day in DATES if day != DATES[1]))
    assert missing.events[0].reason == "session_evidence_missing"
    assert missing.maximum_drawdown is None and missing.total_return is None
    assert missing.entry_decision_evidence_coverage == 0 and missing.valuation_coverage < 1
    unavailable = replay_research_portfolio((batch(DATES[0]),), DATES)
    assert unavailable.provenance_status == "unavailable" and not unavailable.trades
    assert unavailable.total_return is None and unavailable.maximum_drawdown is None
    assert unavailable.days[-1].cash == unavailable.config.initial_cash and unavailable.days[-1].nav is None


def test_last_day_signal_is_pending_and_does_not_enter_early() -> None:
    result = replay(batches=(batch(DATES[-1]),))
    assert result.trades == ()
    assert result.events[0].reason == "entry_session_after_window"
    assert result.total_return is None and result.maximum_drawdown is None


def test_cost_model_cannot_be_applied_before_effective_date() -> None:
    sessions = ("2023-08-24", "2023-08-25")
    with pytest.raises(ValueError, match="cost profile"):
        replay_research_portfolio((batch(sessions[0]),), sessions)


def test_real_price_loss_drawdown_uses_held_quantity_and_shared_cash() -> None:
    rows = tuple(row(day, price=9 if day >= DATES[2] else 10,
                     previous=9 if day >= DATES[3] else 10) for day in DATES)
    result = replay(rows=rows)
    assert result.total_return == pytest.approx((-900 - result.total_fees) / 20_000)
    assert result.maximum_drawdown == result.total_return
    payload = research_portfolio_payload(result)
    assert isinstance(payload["days"], list)
    payload["result_digest"] = ""
    from app.artifacts.io import canonical_json_bytes, sha256_hex
    assert sha256_hex(canonical_json_bytes(payload)) == result.result_digest


def _official_sessions(tmp_path: Path):
    source_uri = "https://licensed.example.test/daily"
    registry_payload = seal_official_execution_source_registry([{
        "source_id": "synthetic", "dataset_id": "fixture", "provider_legal_name": "Fixture Licensed Provider",
        "markets": ["SH"], "authority_basis": "exchange_direct_subscription", "delivery_channel": "https",
        "source_base_uri": source_uri, "license_reference": "fixture-license", "license_document_sha256": "e" * 64,
        "valid_from": "2026-01-01", "valid_through": "2026-12-31", "research_use_authorized": True,
    }], registered_at="2026-08-01T09:00:00+08:00")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_payload))
    registry = load_verified_official_execution_source_registry(registry_path, expected_registry_digest=registry_payload["registry_digest"])
    sessions = []
    for day in DATES:
        raw = b"synthetic raw delivery bytes for loader test" + day.encode()
        relative = day + ".raw"
        (tmp_path / relative).write_bytes(raw)
        receipt = seal_official_execution_raw_file_receipt({
            "source_id": "synthetic", "dataset_id": "fixture", "market": "SH", "session_date": day,
            "relative_path": relative, "byte_size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "source_uri": source_uri + "/" + relative, "available_at": day + "T15:01:00+08:00",
            "acquired_at": day + "T15:02:00+08:00", "parser_version": "fixture-v1", "license_reference": "fixture-license",
        })
        raw_row = row(day).model_dump(mode="json")
        raw_row["receipt_digest"] = receipt["receipt_digest"]
        artifact = seal_official_execution_session_artifact(
            session_date=day, generated_at=day + "T15:06:00+08:00", source_registry_digest=registry.registry_digest,
            receipts=[receipt], rows=[raw_row],
        )
        artifact_path = tmp_path / (day + ".json")
        artifact_path.write_text(json.dumps(artifact))
        sessions.append(load_verified_official_execution_session(artifact_path, registry=registry, raw_file_root=tmp_path))
    return tuple(sessions)


def test_official_provenance_requires_existing_strict_loader_tokens(tmp_path: Path) -> None:
    sessions = _official_sessions(tmp_path)
    result = replay_research_portfolio(
        (batch(DATES[0]),), DATES, official_sessions=sessions,
        config=ResearchPortfolioConfig(initial_cash=20_000, top_n=1, horizon=1),
    )
    assert result.provenance_status == "official_raw_file_verified"
    assert len(result.source_artifact_digests) == len(DATES)
    assert result.days[-1].cash == replay().days[-1].cash
    assert result.promotion_eligible is False
    with pytest.raises(ValueError, match="cannot be mixed"):
        replay_research_portfolio((batch(DATES[0]),), DATES, official_sessions=sessions, synthetic_rows=(row(DATES[0]),))
    with pytest.raises(ValueError, match="strict-loader"):
        replay_research_portfolio((batch(DATES[0]),), DATES, official_sessions=(sessions[0].artifact,))
    sessions[0].session_date = DATES[1]
    with pytest.raises(ValueError, match="identity changed"):
        replay_research_portfolio((batch(DATES[0]),), DATES, official_sessions=sessions)


def test_different_symbols_share_fixed_sleeves_without_borrowing_from_future_sales() -> None:
    second = "600000.SH"
    rows = tuple(row(day, symbol=symbol) for day in DATES for symbol in (SYMBOL, second))
    result = replay(rows=rows, batches=(batch(DATES[0]), batch(DATES[1], symbol=second)))
    buys = [trade for trade in result.trades if trade.side == "buy"]
    assert len(buys) == 2 and {trade.sleeve for trade in buys} == {0, 1}
    assert all(trade.quantity == 900 for trade in buys)
    assert [trade.side for trade in result.trades if trade.session_date == DATES[2]] == ["buy", "sell"]
    assert result.days[-1].cash == pytest.approx(20_000 - result.total_fees)
    assert result.entry_fill_coverage == 1


def test_sell_capacity_exhaustion_holds_position_until_actual_capacity_is_available() -> None:
    rows = tuple(row(day, amount=10 if day == DATES[1] else 100_000_000) for day in DATES)
    result = replay(rows=rows)
    assert len(result.days[2].positions) == 1
    assert result.trades[-1].session_date == DATES[3]
    assert any(event.reason == "exit_prior_session_capacity_exceeded" for event in result.events)


def test_daily_sell_quantity_rule_change_blocks_exit() -> None:
    changed = row(DATES[2]).model_dump(mode="json")
    changed["instrument_rules"]["sell_quantity_step"] = 200
    resealed = OfficialExecutionSessionRow.model_validate(seal_official_execution_session_row(changed))
    result = replay(rows=tuple(resealed if day == DATES[2] else row(day) for day in DATES))
    assert result.trades[-1].session_date == DATES[3]
    assert any(event.reason == "exit_quantity_rule_conflict" for event in result.events)


def test_slot_ranking_order_and_rounding_never_redistribute_missing_candidate_weight() -> None:
    second = "600000.SH"
    candidates = (ResearchSignalCandidate(SYMBOL, 1, "c" * 64), ResearchSignalCandidate(second, 3, "d" * 64))
    original = ResearchSignalBatch("ordered", DATES[0], "b" * 64, candidates)
    config = ResearchPortfolioConfig(initial_cash=20_000.01, top_n=2, horizon=1)
    rows = tuple(row(day, symbol=symbol) for day in DATES for symbol in (SYMBOL, second))
    result = replay_research_portfolio((original,), DATES, synthetic_rows=rows, config=config)
    reversed_batch = ResearchSignalBatch("ordered", DATES[0], "b" * 64, tuple(reversed(candidates)))
    reverse = replay_research_portfolio((reversed_batch,), DATES, synthetic_rows=rows, config=config)
    assert result == reverse
    assert result.trades[0].quantity == 400
    assert result.expected_entry_slots == 2 and result.filled_entry_slots == 1
    assert result.days[0].cash == 20_000.01
    assert result.days[-1].cash == pytest.approx(20_000.01 - result.total_fees)


def test_unmatured_position_is_marked_without_fabricated_final_liquidation() -> None:
    config = ResearchPortfolioConfig(initial_cash=20_000, top_n=1, horizon=5)
    result = replay_research_portfolio((batch(DATES[0]),), DATES[:3], synthetic_rows=tuple(row(day) for day in DATES), config=config)
    assert len(result.trades) == 1
    assert len(result.final_positions) == 1
    assert result.final_positions[0].target_exit_date > DATES[2]
    assert result.days[-1].nav is not None


def test_frozen_universe_uses_each_batch_actual_count_and_binds_allocation_policy() -> None:
    second, third = "600000.SH", "600001.SH"
    larger = ResearchSignalBatch("larger", DATES[0], "b" * 64, (
        ResearchSignalCandidate(SYMBOL, 5, "c" * 64), ResearchSignalCandidate(second, 9, "d" * 64),
    ))
    smaller = batch(DATES[1], symbol=third)
    rows = tuple(row(day, symbol=symbol) for day in DATES for symbol in (SYMBOL, second, third))
    config = ResearchPortfolioConfig(initial_cash=20_000, horizon=1, top_n=1, allocation="frozen-universe")
    result = replay_research_portfolio((larger, smaller), DATES, synthetic_rows=rows, config=config)
    buys = [trade for trade in result.trades if trade.side == "buy"]
    assert [trade.quantity for trade in buys] == [400, 400, 900]
    assert result.expected_entry_slots == 3 and result.filled_entry_slots == 3
    assert result.entry_fill_coverage == 1
    top_n = replay_research_portfolio((larger, smaller), DATES, synthetic_rows=rows,
                                     config=ResearchPortfolioConfig(initial_cash=20_000, horizon=1, top_n=1))
    assert result.input_digest != top_n.input_digest
    empty = ResearchSignalBatch("empty", DATES[0], "b" * 64, ())
    with pytest.raises(ValueError, match="nonempty universe"):
        replay_research_portfolio((empty,), DATES, synthetic_rows=rows, config=config)


def test_unresolved_entry_stops_later_batches_from_spending_unknown_account_cash() -> None:
    second = "600000.SH"
    rows = tuple(row(day, symbol=symbol) for day in DATES for symbol in (SYMBOL, second)
                 if not (day == DATES[1] and symbol == SYMBOL))
    result = replay(rows=rows, batches=(batch(DATES[0]), batch(DATES[1], symbol=second)))
    assert result.trades == ()
    assert result.events[-1].reason == "account_entry_history_incomplete"
    assert result.unknown_entry_slots == 2 and result.entry_decision_evidence_coverage == 0
    assert result.days[-1].cash == 20_000 and result.days[-1].nav is None


def test_verified_unfilled_entry_remains_known_cash_with_complete_decision_evidence() -> None:
    rows = tuple(row(day, entry_state="locked_limit" if day == DATES[1] else "executable") for day in DATES)
    result = replay(rows=rows)
    assert result.unknown_entry_slots == 0 and result.entry_decision_evidence_coverage == 1
    assert result.total_return == 0 and result.maximum_drawdown == 0
    assert result.valuation_coverage == 1


def test_minimum_exit_fee_cannot_make_a_sleeve_negative_or_borrow_another_sleeve() -> None:
    rows = tuple(row(day, price=.01 if day >= DATES[2] else 10,
                     previous=.01 if day >= DATES[3] else 10) for day in DATES)
    result = replay_research_portfolio((batch(DATES[0]),), DATES, synthetic_rows=rows,
                                     config=ResearchPortfolioConfig(initial_cash=2010.42, top_n=1, horizon=1))
    assert len(result.trades) == 1
    assert result.trades[0].sleeve_cash_after == 0
    assert all(min(day.sleeve_cash) >= 0 for day in result.days)
    assert result.final_positions[0].quantity == 100
    assert any(event.reason == "exit_fee_cash_shortfall" for event in result.events)
