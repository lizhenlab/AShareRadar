from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, sha256_hex
from tools.audit_market_scan_frontier import read_frontier_object
from app.services.market_scan_research_holdings import (
    audit_research_holdings_payload,
)
from app.services.market_scan_research_portfolio import (
    ResearchPortfolioConfig, ResearchSignalBatch, ResearchSignalCandidate,
    replay_research_portfolio, research_portfolio_payload,
)
from tests.test_market_scan_research_portfolio import DATES, SYMBOL, batch, replay, row


def classification(symbol: str = SYMBOL, **changes: object) -> dict[str, object]:
    record = {
        "symbol": symbol, "as_of": "2026-08-24T08:00:00+08:00",
        "effective_from": "2026-08-01", "effective_through": "2026-08-31",
        "source_id": "fixture-classifications", "classification_version": "fixture-v1",
        "industry": "消费", "market": symbol[-2:], "board": "main",
        **changes,
    }
    return {**record, "row_digest": sha256_hex(canonical_json_bytes(record))}


def sidecar(result, records=None):
    payload = {
        "schema_version": "market-scan-research-holdings-classifications-v1",
        "portfolio_input_digest": result.input_digest,
        "portfolio_result_digest": result.result_digest,
        "rows": records if records is not None else [classification()],
    }
    return {**payload, "classification_digest": sha256_hex(canonical_json_bytes(payload))}


def audit(result, records=None):
    evidence = sidecar(result, records)
    return audit_research_holdings_payload(
        research_portfolio_payload(result), evidence, expected_portfolio_digest=result.result_digest,
        expected_classification_digest=evidence["classification_digest"],
    )


def test_actual_buy_and_sell_follow_holdings_market_values_and_conserve_nav() -> None:
    result = replay()
    before = asdict(result)
    report = audit(result)
    first, held, exited = report["days"][:3]
    assert first["cash_weight"] == 1 and first["position_count"] == 0
    assert held["positions"][0]["quantity"] == result.trades[0].quantity == 900
    assert held["known_market_value"] == 9000
    assert held["cash_weight"] + sum(item["nav_weight"] for item in held["positions"]) == pytest.approx(1)
    assert held["exposures"]["industry"][0]["nav_weight"] == pytest.approx(9000 / result.days[1].nav)
    assert held["concentration"]["symbol_hhi_equity"] == 1
    assert held["concentration"]["industry_hhi_equity"] == 1
    assert exited["position_count"] == 0 and exited["cash_weight"] == 1
    assert report["promotion_eligible"] is False
    assert report["provenance"]["classification_authority"] == "self_asserted_pit_metadata"
    assert report["provenance"]["portfolio_authority"] == "digest_bound_not_independently_replayed"
    assert asdict(result) == before
    json.dumps(report, allow_nan=False)


def test_locked_exit_continues_to_consume_market_value_and_block_new_entry() -> None:
    rows = tuple(row(day, exit_state="locked_limit" if day in DATES[2:4] else "executable") for day in DATES)
    result = replay(batches=(batch(DATES[0]), batch(DATES[2])), rows=rows)
    report = audit(result)
    for index in (2, 3):
        held = report["days"][index]
        assert held["known_market_value"] == 9000
        assert held["positions"][0]["exit_overdue"] is True
        assert held["cash_weight"] < 1
    assert report["days"][4]["position_count"] == 0
    assert result.trades[-1].exit_delay_sessions == 2


@pytest.mark.parametrize("missing_mark", [False, True])
def test_multiple_actual_holdings_use_market_values_instead_of_name_counts(missing_mark: bool) -> None:
    symbols = (SYMBOL, "000001.SZ")
    candidates = tuple(ResearchSignalCandidate(symbol, rank, "c" * 64) for rank, symbol in enumerate(symbols, 1))
    rows = tuple(row(day, symbol=symbol, price=price, previous=price, exit_state="locked_limit")
                 for day in DATES for symbol, price in zip(symbols, (10, 30), strict=True)
                 if not (missing_mark and symbol == symbols[1] and day == DATES[2]))
    result = replay_research_portfolio(
        (ResearchSignalBatch("two-holdings", DATES[0], "b" * 64, candidates),), DATES,
        synthetic_rows=rows, config=ResearchPortfolioConfig(initial_cash=20_000, top_n=2, horizon=1),
    )
    records = [classification(), classification(symbols[1], industry="金融", board="growth")]
    held = audit(result, records)["days"][2]
    assert held["position_count"] == 2
    assert held["positions"][0]["market_value"] == 4000
    if missing_mark:
        assert held["positions"][1]["market_value"] is None
        assert held["position_valuation_coverage"] == .5
        assert held["classification_coverage"]["industry"]["market_value_coverage"] is None
        assert held["known_market_value"] == 4000 and held["market_value"] is None
        assert held["exposures"]["industry"][1]["market_value"] is None
        return
    assert held["positions"][1]["market_value"] == 3000
    assert held["concentration"]["symbol_hhi_equity"] == pytest.approx((4 / 7) ** 2 + (3 / 7) ** 2)
    for dimension in ("industry", "market", "board"):
        assert sum(item["nav_weight"] for item in held["exposures"][dimension]) + held["cash_weight"] == pytest.approx(1)
        assert sorted(item["market_value"] for item in held["exposures"][dimension]) == [3000, 4000]
        assert held["classification_coverage"][dimension]["market_value_coverage"] == 1


def test_unfilled_order_is_cash_and_does_not_create_exposure() -> None:
    rows = tuple(row(day, entry_state="locked_limit" if day == DATES[1] else "executable") for day in DATES)
    report = audit(replay(rows=rows))
    assert all(day["position_count"] == 0 and day["cash_weight"] == 1 for day in report["days"])


def test_unknown_classification_is_independent_bucket_with_explicit_coverage() -> None:
    held = audit(replay(), [])["days"][1]
    assert held["classification_coverage"]["industry"]["market_value_coverage"] == 0
    assert held["exposures"]["industry"][0]["classification"] is None
    assert held["exposures"]["industry"][0]["market_value"] == 9000
    assert held["concentration"]["industry_hhi_equity"] is None
    assert held["concentration"]["industry_bucket_hhi_equity"] == 1
    assert held["concentration"]["maximum_industry_nav_weight"] is None


@pytest.mark.parametrize("changes", [
    {"as_of": "2026-08-25T15:00:01+08:00"},
    {"as_of": "2026-08-25T07:00:01Z"},
    {"effective_from": "2026-08-26"},
    {"effective_through": "2026-08-24"},
])
def test_future_or_ineffective_classification_never_backfills(changes: dict[str, object]) -> None:
    held = audit(replay(), [classification(**changes)])["days"][1]
    assert held["positions"][0]["classification_row_digest"] is None
    assert held["classification_coverage"]["industry"]["market_value_coverage"] == 0


def test_latest_available_classification_is_order_invariant() -> None:
    result = replay()
    old = classification()
    new = classification(as_of="2026-08-25T07:00:00Z", industry="可选消费")
    left = audit(result, [old, new])["days"]
    right = audit(result, [new, old])["days"]
    assert left == right
    assert left[1]["positions"][0]["classification_row_digest"] == new["row_digest"]


def test_missing_valuation_preserves_quantity_without_fabricated_weights() -> None:
    result = replay(rows=tuple(row(day) for day in DATES if day != DATES[2]))
    held = audit(result)["days"][2]
    assert held["position_count"] == 1 and held["positions"][0]["quantity"] == 900
    assert held["nav"] is None and held["cash_weight"] is None
    assert held["known_market_value"] == 0 and held["unvalued_position_count"] == 1
    assert held["positions"][0]["market_value"] is None
    assert held["position_valuation_coverage"] == 0
    assert held["classification_coverage"]["industry"]["market_value_coverage"] is None
    assert held["exposures"]["industry"][0]["market_value"] is None
    assert all(value is None for value in held["concentration"].values())


def test_incomplete_entry_evidence_does_not_claim_complete_cash_account() -> None:
    result = replay(rows=tuple(row(day) for day in DATES if day != DATES[1]))
    held = audit(result)["days"][1]
    assert held["cash"] == 20_000 and held["position_count"] == 0
    assert held["cash_weight"] is None and held["account_valuation_complete"] is False
    assert held["position_valuation_coverage"] is None


def test_cli_decoded_payload_matches_audit_and_rejects_tampering(tmp_path) -> None:
    result = replay()
    evidence = sidecar(result)
    payload = research_portfolio_payload(result)
    kwargs = {"expected_portfolio_digest": result.result_digest,
              "expected_classification_digest": evidence["classification_digest"]}
    assert audit_research_holdings_payload(payload, evidence, **kwargs) == audit(result)
    portfolio_path, sidecar_path = tmp_path / "portfolio.json", tmp_path / "classifications.json"
    portfolio_path.write_text(json.dumps(payload))
    sidecar_path.write_text(json.dumps(evidence))
    assert audit_research_holdings_payload(read_frontier_object(portfolio_path), read_frontier_object(sidecar_path), **kwargs) == audit(result)
    payload["days"][1]["cash"] += 1
    with pytest.raises(ValueError, match="digest"):
        audit_research_holdings_payload(payload, evidence, **kwargs)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, "9000"])
def test_invalid_valuation_scalar_is_rejected(value: object) -> None:
    result = replay()
    changed_position = replace(result.days[1].positions[0], market_value=value)
    changed_day = replace(result.days[1], positions=(changed_position,))
    changed = replace(result, days=(result.days[0], changed_day, *result.days[2:]))
    with pytest.raises(ValueError):
        audit(changed)


@pytest.mark.parametrize("changes", [
    {"as_of": "2026-08-24 08:00:00"}, {"as_of": "invalid"},
    {"effective_from": "2026-02-30"}, {"effective_from": "2026-09-01"},
    {"market": "SZ"}, {"source_id": ""}, {"classification_version": ""},
    {"symbol": "not-a-symbol"}, {"industry": ""}, {"board": True},
])
def test_invalid_classification_contract_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        audit(replay(), [classification(**changes)])


def test_duplicate_or_conflicting_same_time_classifications_rejected() -> None:
    for other in (classification(), classification(industry="另一行业")):
        with pytest.raises(ValueError, match="ambiguous|duplicate"):
            audit(replay(), [classification(), other])


def test_classification_and_portfolio_pins_cannot_be_rebound_or_self_claim_authority() -> None:
    result = replay()
    evidence = sidecar(result)
    bad = deepcopy(evidence)
    bad["rows"][0]["industry"] = "changed"
    with pytest.raises(ValueError, match="digest"):
        audit_research_holdings_payload(research_portfolio_payload(result), bad, expected_portfolio_digest=result.result_digest, expected_classification_digest=evidence["classification_digest"])
    for field in ("portfolio_input_digest", "portfolio_result_digest"):
        bad = {**evidence, field: "d" * 64}
        bad["classification_digest"] = sha256_hex(canonical_json_bytes({k: v for k, v in bad.items() if k != "classification_digest"}))
        with pytest.raises(ValueError, match="binding"):
            audit_research_holdings_payload(research_portfolio_payload(result), bad, expected_portfolio_digest=result.result_digest, expected_classification_digest=bad["classification_digest"])
    with pytest.raises(ValueError):
        audit_research_holdings_payload(research_portfolio_payload(result), {**evidence, "official": True}, expected_portfolio_digest=result.result_digest, expected_classification_digest=evidence["classification_digest"])
    with pytest.raises(ValueError, match="digest"):
        audit_research_holdings_payload(research_portfolio_payload(result), evidence,
                                       expected_portfolio_digest="d" * 64,
                                       expected_classification_digest=evidence["classification_digest"])


def test_resealed_cash_and_position_mismatches_still_fail_conservation() -> None:
    result = replay()
    for changed_day in (replace(result.days[1], cash=result.days[1].cash + 1),
                        replace(result.days[1], market_value=8000),
                        replace(result.days[1], positions=())):
        changed = replace(result, result_digest="", days=(result.days[0], changed_day, *result.days[2:]))
        changed = replace(changed, result_digest=sha256_hex(canonical_json_bytes(research_portfolio_payload(changed))))
        with pytest.raises(ValueError):
            audit(changed)


def test_cli_input_rejects_duplicate_fields_and_unknown_nested_fields(tmp_path) -> None:
    result = replay()
    evidence = sidecar(result)
    kwargs = {"expected_portfolio_digest": result.result_digest,
              "expected_classification_digest": evidence["classification_digest"]}
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"days":[],"days":[]}')
    with pytest.raises(ArtifactIOError):
        read_frontier_object(duplicate)
    payload = research_portfolio_payload(result)
    payload["days"][0]["unexpected"] = "ignored?"
    with pytest.raises(ValueError):
        audit_research_holdings_payload(payload, evidence, **kwargs)


@pytest.mark.parametrize("corruption", [
    "schema", "authority", "total_fees", "final_positions", "calendar", "missing_calendar",
    "trade_date", "trade_sleeve", "trade_symbol", "trade_digest", "trade_price", "sell_quantity", "same_day_sell",
    "position_quantity", "position_date", "missing_mark_pair", "zero_mark", "missing_nav", "negative_cash", "fractional_cent",
])
def test_resealed_account_must_still_satisfy_real_trade_and_valuation_contracts(corruption: str) -> None:
    result = replay()
    changes = {
        "schema": {"schema_version": "unknown"}, "authority": {"promotion_eligible": True},
        "total_fees": {"total_fees": 0}, "final_positions": {"final_positions": result.days[1].positions},
        "calendar": {"days": tuple(reversed(result.days))}, "missing_calendar": {"days": result.days[:2] + result.days[3:]},
    }
    trades = {
        "trade_date": (0, {"session_date": "2026-08-01"}), "trade_sleeve": (0, {"sleeve": -1}),
        "trade_symbol": (0, {"symbol": "invalid"}), "trade_digest": (0, {"source_digest": "invalid"}),
        "trade_price": (0, {"price": 0}), "sell_quantity": (1, {"quantity": 800, "gross_amount": 8000}),
        "same_day_sell": (1, {"session_date": result.trades[0].session_date}),
    }
    position_changes = {
        "position_quantity": {"quantity": 800}, "position_date": {"target_exit_date": result.days[1].session_date},
        "missing_mark_pair": {"mark_price": None}, "zero_mark": {"mark_price": 0},
    }
    if corruption in changes:
        result = replace(result, **changes[corruption])
    elif corruption in trades:
        index, fields = trades[corruption]
        rows = list(result.trades)
        rows[index] = replace(rows[index], **fields)
        result = replace(result, trades=tuple(rows))
    else:
        held = result.days[1]
        if corruption in position_changes:
            held = replace(held, positions=(replace(held.positions[0], **position_changes[corruption]),))
        else:
            held = replace(held, **{"missing_nav": {"nav": None}, "negative_cash": {"cash": -1}, "fractional_cent": {"cash": 1.001}}[corruption])
        result = replace(result, days=(result.days[0], held, *result.days[2:]))
    result = replace(result, result_digest="")
    result = replace(result, result_digest=sha256_hex(canonical_json_bytes(research_portfolio_payload(result))))
    with pytest.raises(ValueError):
        audit(result)
