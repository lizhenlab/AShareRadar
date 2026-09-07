from dataclasses import replace

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.market_scan_research_holdings import audit_research_holdings_payload
from app.services.market_scan_research_portfolio import research_portfolio_payload
from tests.test_market_scan_research_holdings import sidecar
from tests.test_market_scan_research_portfolio import DATES, replay, row


def audit(result):
    result = replace(result, result_digest="")
    result = replace(result, result_digest=sha256_hex(canonical_json_bytes(research_portfolio_payload(result))))
    evidence = sidecar(result)
    return audit_research_holdings_payload(research_portfolio_payload(result), evidence,
                                          expected_portfolio_digest=result.result_digest,
                                          expected_classification_digest=evidence["classification_digest"])


def test_rehashed_target_cannot_hide_overdue_position():
    result = replay(rows=tuple(row(day, exit_state="locked_limit" if day in DATES[2:4] else "executable") for day in DATES))
    changed = tuple(replace(day, positions=tuple(replace(item, target_exit_date="2026-12-31") for item in day.positions)) for day in result.days)
    with pytest.raises(ValueError, match="target exit"):
        audit(replace(result, days=changed))


def test_in_window_target_uses_frozen_account_sessions(monkeypatch):
    from app.services import market_scan_research_holdings_validation as validation
    result = replay()
    monkeypatch.setattr(validation, "next_trade_dates", lambda *_: pytest.fail("frozen sessions already contain target"))
    assert audit(result)["days"][1]["positions"][0]["target_exit_date"] == DATES[2]


def test_outside_window_target_is_checked_against_trusted_calendar():
    result = replay(sessions=DATES[:2], rows=tuple(row(day) for day in DATES[:2]))
    assert audit(result)["days"][1]["positions"][0]["target_exit_date"] == DATES[2]
    changed_position = replace(result.final_positions[0], target_exit_date=DATES[3])
    with pytest.raises(ValueError, match="target exit"):
        audit(replace(result, final_positions=(changed_position,), days=(result.days[0], replace(result.days[1], positions=(changed_position,)))))


def test_exit_delay_must_match_frozen_sessions():
    result = replay(rows=tuple(row(day, exit_state="locked_limit" if day in DATES[2:4] else "executable") for day in DATES))
    trades = tuple(replace(trade, exit_delay_sessions=0) if trade.side == "sell" else trade for trade in result.trades)
    with pytest.raises(ValueError, match="exit delay"):
        audit(replace(result, trades=trades))
