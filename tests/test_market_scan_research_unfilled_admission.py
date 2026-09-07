from __future__ import annotations

import pytest

from app.services.market_scan_research_portfolio import ResearchSignalBatch, ResearchSignalCandidate
from tests.test_market_scan_research_portfolio import DATES, SYMBOL, batch, replay, row


def test_consecutive_verified_suspensions_do_not_poison_later_cash_decisions() -> None:
    healthy = "600000.SH"
    later = ResearchSignalBatch("healthy", DATES[2], "b" * 64, (ResearchSignalCandidate(healthy, 1, "c" * 64),))
    batches = (batch(DATES[0]), batch(DATES[1]), later)
    rows = tuple(row(day, state="suspended" if day in DATES[1:3] else "trading") for day in DATES)
    rows += tuple(row(day, symbol=healthy) for day in DATES)
    result = replay(batches=batches, rows=rows)
    assert result.schema_version == "shared-cash-official-daily-ledger-v3"
    assert [event.reason for event in result.events] == ["session_suspended", "session_suspended"]
    assert result.unknown_entry_slots == 0 and result.entry_decision_evidence_coverage == 1
    assert [trade.symbol for trade in result.trades] == [healthy, healthy]
    assert result.days[-1].nav == pytest.approx(20_000 - result.total_fees)
    assert result.total_return is not None and result.maximum_drawdown is not None


@pytest.mark.parametrize("state,entry,reason", [
    ("suspended", "executable", "session_suspended"),
    ("trading", "locked_limit", "entry_locked_limit"),
    ("trading", "capacity_exceeded", "entry_capacity_exceeded"),
])
@pytest.mark.parametrize("previous", ["missing", "conflicting"])
def test_verified_nonentry_state_establishes_cash_without_inventing_a_price(state, entry, reason, previous) -> None:
    rows = tuple(row(day, state=state, entry_state=entry, previous=9 if previous == "conflicting" else 10)
                 if day == DATES[1] else row(day) for day in DATES if day != DATES[0] or previous != "missing")
    result = replay(rows=rows)
    assert result.trades == () and result.events[0].reason == reason
    assert result.unknown_entry_slots == 0 and result.entry_decision_evidence_coverage == 1
    assert all(day.nav == 20_000 for day in result.days)
    assert result.total_return == 0 and result.maximum_drawdown == 0


@pytest.mark.parametrize("missing_day", [DATES[0], DATES[1]])
def test_executable_or_unknown_entry_still_requires_the_complete_price_reference(missing_day) -> None:
    result = replay(rows=tuple(row(day) for day in DATES if day != missing_day))
    assert result.unknown_entry_slots == 1 and result.trades == ()
    assert result.total_return is None and result.maximum_drawdown is None
    assert result.days[-1].nav is None


def test_suspended_existing_position_is_never_reclassified_as_cash() -> None:
    rows = tuple(row(day, state="suspended" if day in DATES[2:4] else "trading") for day in DATES)
    result = replay(rows=rows)
    assert result.trades[0].side == "buy" and result.trades[0].symbol == SYMBOL
    assert all(result.days[index].positions and result.days[index].nav is None for index in (2, 3))
    assert result.trades[-1].side == "sell" and result.trades[-1].session_date == DATES[5]
    assert result.total_return is None and result.maximum_drawdown is None


def test_nonentry_shortcut_still_requires_integrity_checked_session_state() -> None:
    forged = row(DATES[1]).model_copy(update={"entry_execution_state": "locked_limit"})
    rows = tuple(forged if day == DATES[1] else row(day) for day in DATES)
    with pytest.raises(ValueError, match="digest"):
        replay(rows=rows)
