from __future__ import annotations

from copy import deepcopy

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.market_scan_research_holdings import audit_research_holdings_payload
from app.services.market_scan_research_portfolio import research_portfolio_payload
from tests.test_market_scan_research_holdings import sidecar
from tests.test_market_scan_research_portfolio import DATES, replay, row
from tools import audit_market_scan_frontier as cli


def _audit_resealed(payload, evidence):
    payload["result_digest"] = ""
    payload["result_digest"] = sha256_hex(canonical_json_bytes(payload))
    evidence = {**evidence, "portfolio_result_digest": payload["result_digest"]}
    evidence.pop("classification_digest")
    evidence["classification_digest"] = sha256_hex(canonical_json_bytes(evidence))
    return audit_research_holdings_payload(
        payload, evidence, expected_portfolio_digest=payload["result_digest"],
        expected_classification_digest=evidence["classification_digest"],
    )


@pytest.mark.parametrize("corruption", ["trade_balance", "daily_balances", "sleeve_count", "initial_transfer"])
def test_self_resealed_ledger_must_conserve_each_fixed_cash_sleeve(corruption):
    result = replay()
    payload = research_portfolio_payload(result)
    if corruption == "trade_balance":
        payload["trades"][0]["sleeve_cash_after"] = 0
    elif corruption == "daily_balances":
        payload["days"][1]["sleeve_cash"].reverse()
    elif corruption == "sleeve_count":
        payload["days"][1]["sleeve_cash"] = [payload["days"][1]["cash"]]
    else:
        payload["days"][0]["sleeve_cash"][0] -= 1
        payload["days"][0]["sleeve_cash"][1] += 1

    with pytest.raises(ValueError, match="sleeve"):
        _audit_resealed(payload, sidecar(result))


@pytest.mark.parametrize("field", ["fees", "gross_traded"])
def test_self_resealed_daily_activity_must_equal_its_actual_trades(field):
    result = replay()
    payload = research_portfolio_payload(result)
    payload["days"][1][field] += 1

    with pytest.raises(ValueError, match="daily"):
        _audit_resealed(payload, sidecar(result))


def test_changing_cost_profile_cannot_relabel_old_fee_amounts_as_valid():
    result = replay()
    payload = research_portfolio_payload(result)
    payload["config"]["cost_profile"] = "stress"

    with pytest.raises(ValueError, match="cost profile"):
        _audit_resealed(payload, sidecar(result))


@pytest.mark.parametrize("cost_profile", ["base", "conservative", "stress"])
def test_unmodified_shared_cash_ledger_remains_byte_stable_after_audit(cost_profile):
    result = replay(cost_profile=cost_profile)
    payload = research_portfolio_payload(result)
    evidence = sidecar(result)
    before = deepcopy(payload), deepcopy(evidence)

    report = audit_research_holdings_payload(
        payload, evidence, expected_portfolio_digest=result.result_digest,
        expected_classification_digest=evidence["classification_digest"],
    )

    assert (payload, evidence) == before
    assert report["portfolio_result_digest"] == result.result_digest
    assert report["days"][1]["nav"] == result.days[1].nav


def test_holdings_cli_rejects_self_resealed_cash_corruption_without_publishing(tmp_path, capsys):
    result = replay()
    payload = research_portfolio_payload(result)
    payload["trades"][0]["sleeve_cash_after"] = 0
    payload["result_digest"] = ""
    payload["result_digest"] = sha256_hex(canonical_json_bytes(payload))
    evidence = sidecar(result)
    evidence["portfolio_result_digest"] = payload["result_digest"]
    evidence.pop("classification_digest")
    evidence["classification_digest"] = sha256_hex(canonical_json_bytes(evidence))
    portfolio, classifications, output = (tmp_path / name for name in ("portfolio.json", "sidecar.json", "report.json"))
    portfolio.write_bytes(canonical_json_bytes(payload))
    classifications.write_bytes(canonical_json_bytes(evidence))
    before = portfolio.read_bytes(), classifications.read_bytes()

    code = cli.main(["holdings", "--portfolio", str(portfolio), "--portfolio-digest", payload["result_digest"],
                     "--classifications", str(classifications), "--classification-digest", evidence["classification_digest"],
                     "--output", str(output)])

    assert code == 1 and not output.exists()
    assert "trade sleeve cash" in capsys.readouterr().err
    assert (portfolio.read_bytes(), classifications.read_bytes()) == before


def test_declared_sleeve_count_is_checked_before_allocating_its_cash_array(monkeypatch):
    from app.services import market_scan_research_holdings_validation as validation

    result = replay(rows=tuple(row(day, entry_state="locked_limit") for day in DATES))
    payload = research_portfolio_payload(result)
    payload["config"]["horizon"] = 1_000_000_000

    def forbidden_allocation(_config):
        pytest.fail("untrusted horizon must not allocate cash before its serialized sleeve count is checked")

    monkeypatch.setattr(validation, "_initial_cash_sleeves", forbidden_allocation)
    with pytest.raises(ValueError, match="initial sleeve count"):
        _audit_resealed(payload, sidecar(result))


def test_purchase_cannot_borrow_cash_from_another_sleeve_even_if_total_cash_covers_it():
    result = replay()
    payload = research_portfolio_payload(result)
    # The original 900-share purchase costs 9006.89. A two-sleeve account with
    # 18000 total has enough account cash but only 9000 in the buying sleeve.
    payload["config"]["initial_cash"] = 18000
    for day in payload["days"]:
        day["cash"] -= 2000
        day["nav"] -= 2000
        day["sleeve_cash"] = [0, day["cash"]]
    payload["days"][0]["sleeve_cash"] = [9000, 9000]
    for trade in payload["trades"]:
        trade["sleeve_cash_after"] = 0

    with pytest.raises(ValueError, match="running sleeve cash"):
        _audit_resealed(payload, sidecar(result))
