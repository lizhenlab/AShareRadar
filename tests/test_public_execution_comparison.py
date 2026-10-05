"""Every frozen pair survives public-fact comparison without execution inference."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.services import public_execution_comparison as comparison


DAY = "2026-09-18"
AS_OF = datetime(2026, 9, 20, 1, tzinfo=UTC)
STAMP = "2026-09-18T10:00:00+00:00"


def fact(symbol="600519.SH", **overrides):
    return {"symbol": symbol, "session_date": DAY, "trade_state": "trading", "is_st": False,
            "reference_price": 10.0, "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
            "volume": 1000.0, "amount": 11000.0, "adjustment_mode": "unadjusted",
            "volume_unit": "shares", "amount_unit": "CNY", **overrides}


def notice(symbol="600519.SH", **overrides):
    market = symbol[-2:] if symbol else "SZ"
    return {"provider": "sse_public_notice" if market == "SH" else "szse_public_notice", "market": market,
            "session_date": DAY, "product_code": symbol[:6] if symbol else "159001", "symbol": symbol,
            "instrument_type": "stock" if symbol else "other_or_unknown", "notice_type": "suspension_notice",
            "raw_start_time": "20260918", "raw_end_time": "", "raw_period": "AM", "raw_row": {"fixture": "original"}, **overrides}


def source(rows=(), **overrides):
    return {"status": "success", "observed_at": STAMP, "digest": "a" * 64, "rows": list(rows), **overrides}


def report(sources=None, pairs=None, as_of=AS_OF):
    return comparison.compare_public_execution_facts(
        [("600519.SH", DAY)] if pairs is None else pairs, {} if sources is None else sources, as_of=as_of,
    )


def test_keeps_requested_order_missing_bj_and_future_pairs_without_inference():
    pairs = [("000001.SZ", DAY), ("920002.BJ", DAY), ("600519.SH", "2026-09-21"), ("600519.SH", DAY)]
    result = report({("baostock", DAY): source([fact("000001.SZ")])}, pairs)
    assert [(row["symbol"], row["session_date"]) for row in result["rows"]] == pairs
    assert [row["status"] for row in result["rows"]] == ["reported", "unsupported", "pending", "missing"]
    assert result["required_pair_count"] == 4 and result["status"] == "partial_research_only"
    assert result["pair_counts"] == {"reported": 1, "missing": 1, "pending": 1, "unsupported": 1, "error": 0}
    assert result["official_execution_admitted"] is result["point_in_time_verified"] is False
    assert result["provider_calls"] == result["runtime_writes"] == 0
    assert all(row["reported_trade_state"] == "unknown" for row in result["rows"][1:])


def test_empty_notices_are_not_normal_trading_evidence_and_missing_vendor_retains_notice():
    row = report({("sse_public_notice", DAY): source()})["rows"][0]
    assert row["status"] == "missing" and row["reported_trade_state"] == "unknown"
    assert row["notice_status"] == "no_matching_notice" and row["notices"] == []
    row = report({("sse_public_notice", DAY): source([notice()])})["rows"][0]
    assert row["notice_status"] == "matched" and row["notices"][0]["raw_period"] == "AM"
    assert row["status"] == "missing" and row["reported_trade_state"] == "unknown"


def test_normal_vendor_state_and_intraday_notice_are_parallel_facts_not_inferred_conflict():
    raw = {("baostock", DAY): source([fact(reference_price=8.5)]),
           ("sse_public_notice", DAY): source([notice(), notice(raw_period="PM")], digest="b" * 64)}
    before = deepcopy(raw)
    row = report(raw)["rows"][0]
    assert row["status"] == "reported" and row["reported_trade_state"] == "trading"
    assert row["baostock_fact"]["reference_price"] == 8.5 and len(row["notices"]) == 2
    assert row["source_evidence"]["sse_public_notice"]["digest"] == "b" * 64
    assert row["source_evidence"]["baostock"]["observed_at"] == STAMP
    assert "executable" not in row and "upper_limit" not in row
    row["notices"][0]["raw_row"]["fixture"] = "mutated report"
    assert raw == before


def test_other_product_notices_are_never_matched_to_stocks():
    raw = {("baostock", DAY): source([fact("000001.SZ")]),
           ("szse_public_notice", DAY): source([notice(None)]),
           ("sse_public_notice", DAY): source([notice()])}
    row = report(raw, [("000001.SZ", DAY)])["rows"][0]
    assert row["status"] == "reported" and row["notice_status"] == "no_matching_notice"
    assert row["notices"] == [] and set(row["source_evidence"]) == {"baostock", "szse_public_notice"}


def test_resumption_notice_is_preserved_without_inventing_trade_state():
    row = report({("szse_public_notice", DAY): source([notice("301266.SZ", notice_type="resumption_notice",
                   raw_start_time="", raw_end_time="2026-09-18 开市", raw_period="取消停牌")])}, [("301266.SZ", DAY)])["rows"][0]
    assert row["status"] == "missing" and row["reported_trade_state"] == "unknown"
    assert row["notices"][0]["notice_type"] == "resumption_notice"


def test_declared_suspension_retains_unknown_volume_and_amount():
    suspended = fact(trade_state="suspended", open=10, high=10, low=10, close=10, volume=None, amount=None)
    row = report({("baostock", DAY): source([suspended])})["rows"][0]
    assert row["status"] == "reported" and row["reported_trade_state"] == "suspended"
    assert row["baostock_fact"]["volume"] is row["baostock_fact"]["amount"] is None


def test_single_price_zero_volume_does_not_infer_suspension_or_limit_lock():
    flat = fact(open=10, high=10, low=10, close=10, volume=0, amount=0)
    row = report({("baostock", DAY): source([flat])})["rows"][0]
    assert row["reported_trade_state"] == "trading" and row["status"] == "reported"
    assert row["baostock_fact"] == flat


def test_future_source_is_not_used_even_when_its_session_has_finished():
    raw = {("baostock", DAY): source([fact()], observed_at="2026-09-21T00:00:00Z"),
           ("sse_public_notice", DAY): source([notice()], observed_at="2026-09-21T00:00:00Z")}
    row = report(raw)["rows"][0]
    assert row["status"] == row["notice_status"] == "pending"
    assert row["reason"] == "source_observed_after_as_of" and row["baostock_fact"] is None and row["notices"] == []
    assert row["source_evidence"]["baostock"]["digest"] == "a" * 64


def test_future_pairs_never_read_facts_and_use_shanghai_calendar_date():
    day = "2026-09-21"
    row = report({("baostock", day): source([object()])}, [("600519.SH", day)])["rows"][0]
    assert row["status"] == "pending" and row["reason"] == "session_after_as_of"
    as_of = datetime(2026, 9, 17, 23, tzinfo=UTC)  # Already Sep 18 in Shanghai.
    row = report(as_of=as_of)["rows"][0]
    assert row["status"] == "missing"


def test_daily_observation_before_session_close_is_rejected():
    row = report({("baostock", DAY): source([fact()], observed_at="2026-09-18T06:59:59Z")})["rows"][0]
    assert row["status"] == "error" and row["reason"] == "source_observed_before_session_close"


@pytest.mark.parametrize("status", ["missing", "pending", "error"])
def test_unavailable_source_never_uses_attached_rows(status):
    row = report({("baostock", DAY): source([fact()], status=status, observed_at=None, digest=None)})["rows"][0]
    assert row["status"] == status and row["baostock_fact"] is None


@pytest.mark.parametrize("changes,reason", [
    ({"status": []}, "invalid_source_status"), ({"status": "verified"}, "invalid_source_status"),
    ({"digest": 42}, "invalid_source_digest"), ({"digest": ""}, "invalid_source_digest"),
    ({"digest": None}, "source_digest_unavailable"),
    ({"observed_at": None}, "invalid_source_observed_at"), ({"observed_at": "bad"}, "invalid_source_observed_at"),
    ({"observed_at": "2026-09-18T18:00:00"}, "invalid_source_observed_at"),
    ({"observed_at": "2026-09-18T18:00:00+08:00"}, "invalid_source_observed_at"),
])
def test_malformed_source_metadata_is_safe_error(changes, reason):
    row = report({("baostock", DAY): source([fact()], **changes)})["rows"][0]
    assert row["status"] == "error" and row["reason"] == reason and row["baostock_fact"] is None


@pytest.mark.parametrize("rows", [
    [fact(), fact()], [fact(session_date="2026-09-17")], [fact("920002.BJ")], [fact(trade_state="unknown")],
    [fact(is_st="0")], [fact(high=float("nan"))], [fact(amount=float("inf"))], [fact(close=10 ** 400)],
    [fact(volume=None)], [fact(volume=True)], [fact(reference_price=0)], [fact(low=20)],
    [fact(adjustment_mode="qfq")], [fact(amount_unit="thousand_CNY")],
    [fact(trade_state="suspended")], [fact(trade_state=[], is_st=False)], [None], {},
])
def test_invalid_normalized_vendor_batch_never_yields_partial_success(rows):
    row = report({("baostock", DAY): source(rows=[]) | {"rows": rows}})["rows"][0]
    assert row["status"] == "error" and row["reason"] == "invalid_normalized_source_rows"


@pytest.mark.parametrize("invalid", [
    notice(session_date="2026-09-17"), notice(market="SZ"), notice(symbol="000001.SZ"),
    notice(instrument_type="other_or_unknown"), notice(provider="other"),
])
def test_invalid_notices_do_not_erase_valid_vendor_facts(invalid):
    row = report({("baostock", DAY): source([fact()]), ("sse_public_notice", DAY): source([invalid])})["rows"][0]
    assert row["status"] == "reported" and row["notice_status"] == "error" and row["notices"] == []


@pytest.mark.parametrize("pairs", [
    [("600519.SH", DAY), ("600519.SH", DAY)], [("600519", DAY)], [("600519.sh", DAY)],
    [("600519.SH", "20260918")], [("600519.SH", "invalid")], ["600519.SH"], [("600519.SH",)],
])
def test_invalid_pairs_fail_without_dropping_or_normalizing_members(pairs):
    with pytest.raises(ValueError):
        report(pairs=pairs)


def test_limits_are_explicit_and_empty_requirements_never_claim_coverage(monkeypatch):
    result = report(pairs=[])
    assert result["status"] == "needs_source" and result["required_pair_count"] == 0 and result["rows"] == []
    monkeypatch.setattr(comparison, "_MAX_PAIRS", 1)
    with pytest.raises(ValueError, match="too many"):
        report(pairs=[("600519.SH", DAY), ("000001.SZ", DAY)])
    monkeypatch.setattr(comparison, "_MAX_SOURCE_ROWS", 1)
    assert report({("baostock", DAY): source([fact()])})["rows"][0]["status"] == "error"
    with pytest.raises(ValueError):
        report({("unregistered", DAY): source()})
    with pytest.raises(ValueError):
        report(as_of=AS_OF.replace(tzinfo=None))
