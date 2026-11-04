"""Production direction follows prices while the vendor return remains auditable."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.models.market import Kline
from app.models.market_scan import MarketScanMode, MarketScanResultItem, MarketScanRun
from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_scan_execution_quote import (
    market_scan_execution_quote_evidence_digest,
    verify_market_scan_execution_quote_evidence,
)
from app.repositories.market_scan_rule_contracts import register_market_scan_rule_contract
from app.services import market_scan_production_replay as production_replay
from app.services import market_scan_scoring as scoring
from app.services.market_scan_manager import market_scan_rule_contract
from app.services.market_scan_probability_source import (
    is_current_writable_production_score_contract,
    is_registered_production_score_contract,
)
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence_context
from app.services.trading_calendar import trading_dates_between
from tests.test_market_scan_raw_score_replay import _snapshot
from tests.test_market_scan_scoring import AS_OF, DATA_DATE, PREOPEN_AS_OF, _item, _persisted_item, _quote


OLD_V4_HASH = "2fca8cd2e3a6dbefbb2c424dd7fb07d7ed8e1b88c35334e8e1256d9d5e1d7f88"
MODES = ("official", "preopen", "intraday")


def _observations(*, mode: MarketScanMode, actual_return: float, reported_return: float):
    # Use the trusted calendar, rather than weekdays, to freeze exactly 61
    # consecutive completed sessions including exchange holiday boundaries.
    days = trading_dates_between(date(2026, 1, 1), DATA_DATE, allow_auto_refresh=False)[-61:]
    assert len(days) == 61
    previous_close = 10.0
    price = previous_close * (1 + actual_return / 100)
    rows = [
        Kline(
            date=day.isoformat(), open=previous_close, close=previous_close,
            high=previous_close * 1.02, low=previous_close * .98,
            volume=4_000_000 if index >= len(days) - 5 else 1_000_000,
            adjustment_mode="qfq", as_of=f"{DATA_DATE.isoformat()}T15:00:00",
            data_version=f"canonical-return|qfq|{DATA_DATE.isoformat()}", source="test-qfq",
        )
        for index, day in enumerate(days)
    ]
    if mode != "intraday":
        rows[-1] = rows[-1].model_copy(update={
            "close": price, "high": max(previous_close, price) * 1.02,
            "low": min(previous_close, price) * .98,
        })
    as_of = AS_OF if mode == "official" else PREOPEN_AS_OF.replace(hour=10 if mode == "intraday" else 8)
    quote_date = as_of.date() if mode == "intraday" else DATA_DATE
    quote = _quote().model_copy(update={
        "price": price, "prev_close": previous_close, "open": previous_close,
        "high": max(previous_close, price) * 1.02, "low": min(previous_close, price) * .98,
        "change": price - previous_close, "change_pct": reported_return,
        "timestamp": f"{quote_date.isoformat()} {'10:00:00' if mode == 'intraday' else '15:00:00'}",
    })
    return quote, rows, as_of, quote_date


def _score(*, mode: MarketScanMode, actual_return: float, reported_return: float):
    quote, rows, as_of, quote_date = _observations(
        mode=mode, actual_return=actual_return, reported_return=reported_return,
    )
    result = scoring.score_market_scan_item(
        _item(), quote, rows, as_of=as_of, completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE, expected_quote_date=quote_date,
        min_history_rows=61, min_data_quality_score=50, mode=mode,
        rule_version=f"full-market-scan-v6:{'a' * 64}", quote_observed_at=quote.timestamp,
    )
    return result, quote, as_of, quote_date


def _context(result, quote, as_of, quote_date, mode):
    item = _persisted_item(result).model_copy(update={
        "quote_observed_at": quote.timestamp, "updated_at": as_of.isoformat(),
        "tags": list(result.tags),
    })
    evidence = item.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    return item, evidence, dict(
        expected_data_date=DATA_DATE.isoformat(), expected_quote_date=quote_date.isoformat(),
        expected_as_of=as_of.isoformat(), expected_mode=mode,
    )


def _rank_signature(result):
    return result.trend_score, result.leader_score, result.raw_score, result.score, tuple(result.tags)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("actual_return", [-.01, 0.0, .01])
def test_tolerated_vendor_direction_cannot_change_production_score_or_tags(mode, actual_return):
    outputs = []
    for error in (-.29, 0.0, .29):
        reported = actual_return + error
        result, quote, as_of, quote_date = _score(mode=mode, actual_return=actual_return, reported_return=reported)
        item, evidence, context = _context(result, quote, as_of, quote_date, mode)
        inputs = result.score_details["inputs"]
        assert result.status == "success"
        assert result.data_quality_score == 100
        assert result.volume_ratio == 2.29
        assert result.change_pct == inputs["change_pct"] == reported
        assert evidence["payload"]["quote_change_pct"] == reported
        assert evidence["payload"]["quote_prev_close"] == quote.prev_close
        assert inputs["scoring_change_pct"] == round((quote.price - quote.prev_close) / quote.prev_close * 100, 12)
        assert verify_market_scan_point_in_time_evidence_context(evidence, item=item, **context)
        assert scoring.replay_score_details(result.score_details).raw_score == result.raw_score
        outputs.append(_rank_signature(result))
    assert outputs[0] == outputs[1] == outputs[2]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("actual_return,expected_tag", [(4.9, False), (5.1, True)])
def test_strong_gain_tag_uses_canonical_return_on_both_sides_of_reported_threshold(mode, actual_return, expected_tag):
    signatures = []
    for error in (-.29, 0.0, .29):
        result, _, _, _ = _score(mode=mode, actual_return=actual_return, reported_return=actual_return + error)
        assert ("涨幅强" in result.tags) is expected_tag
        signatures.append(_rank_signature(result))
    assert signatures[0] == signatures[1] == signatures[2]


@pytest.mark.parametrize("mode", MODES)
def test_canonical_return_preserves_small_real_direction_and_declared_precision(mode):
    for actual in (-.0000001, .0000001, .1234567890128):
        result, quote, _, _ = _score(mode=mode, actual_return=actual, reported_return=-.1 if actual > 0 else .1)
        canonical = result.score_details["inputs"]["scoring_change_pct"]
        assert canonical == round((quote.price - quote.prev_close) / quote.prev_close * 100, 12)
        assert canonical * actual > 0


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("reported", [-.3, .3])
def test_exact_vendor_return_tolerance_remains_accepted(mode, reported):
    result, _, _, _ = _score(mode=mode, actual_return=0.0, reported_return=reported)
    assert result.score_details["inputs"]["scoring_change_pct"] == 0.0


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("reported", [-.3000001, .3000001])
def test_out_of_tolerance_vendor_return_is_still_missing(mode, reported):
    with pytest.raises(scoring.MarketScanDataMissing, match="涨跌幅.*偏差过大"):
        _score(mode=mode, actual_return=0.0, reported_return=reported)


def _reseal(evidence):
    serialized = json.dumps(evidence["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    evidence["payload_digest"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _reseal_execution_quote(evidence):
    identity = {
        name: evidence[name]
        for name in ("symbol", "quote_date", "provider_event_at", "source", "fallback_used", "bar")
    }
    evidence["normalized_quote_digest"] = sha256_hex(canonical_json_bytes(identity))
    evidence["evidence_digest"] = market_scan_execution_quote_evidence_digest(evidence)
    assert verify_market_scan_execution_quote_evidence(evidence) == evidence


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("field", ["source", "fallback_used", "provider_event_at", "captured_at", "amount"])
def test_current_context_rejects_resealed_quote_from_another_observation(mode, field):
    result, quote, as_of, quote_date = _score(mode=mode, actual_return=0.0, reported_return=.29)
    item, evidence, context = _context(result, quote, as_of, quote_date, mode)
    original_pit = deepcopy(evidence)
    assert verify_market_scan_point_in_time_evidence_context(evidence, item=item, **context)
    execution = item.score_details["execution_quote_evidence"]
    if field == "source":
        execution[field] = "another-valid-source"
    elif field == "fallback_used":
        execution[field] = not execution[field]
    elif field == "provider_event_at":
        execution[field] = (datetime.fromisoformat(execution[field]) - timedelta(minutes=1)).isoformat()
    elif field == "captured_at":
        execution[field] = (datetime.fromisoformat(execution[field]) + timedelta(seconds=1)).isoformat()
    else:
        execution["bar"][field] += 1.0
    _reseal_execution_quote(execution)
    assert evidence == original_pit
    assert not verify_market_scan_point_in_time_evidence_context(evidence, item=item, **context)


@pytest.mark.parametrize("mode", MODES)
def test_current_quote_binding_accepts_equivalent_timestamp_offsets(mode):
    result, quote, as_of, quote_date = _score(mode=mode, actual_return=0.0, reported_return=.29)
    item, evidence, context = _context(result, quote, as_of, quote_date, mode)
    original_pit = deepcopy(evidence)
    execution = item.score_details["execution_quote_evidence"]
    for field in ("provider_event_at", "captured_at"):
        execution[field] = datetime.fromisoformat(execution[field]).astimezone(timezone.utc).isoformat()
    _reseal_execution_quote(execution)
    assert evidence == original_pit
    assert verify_market_scan_point_in_time_evidence_context(evidence, item=item, **context)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("value", ["missing", None, True, "10.0", 0.0, -10.0, 10.01])
def test_current_context_rejects_missing_invalid_or_resealed_previous_close(mode, value):
    result, quote, as_of, quote_date = _score(mode=mode, actual_return=0.0, reported_return=.29)
    item, evidence, context = _context(result, quote, as_of, quote_date, mode)
    assert verify_market_scan_point_in_time_evidence_context(evidence, item=item, **context)
    if value == "missing":
        evidence["payload"].pop("quote_prev_close")
    else:
        evidence["payload"]["quote_prev_close"] = value
    _reseal(evidence)
    assert not verify_market_scan_point_in_time_evidence_context(evidence, item=item, **context)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("value", ["missing", None, True, "0.0", .000001])
def test_current_context_rejects_missing_or_changed_canonical_scoring_input(mode, value):
    result, quote, as_of, quote_date = _score(mode=mode, actual_return=0.0, reported_return=-.29)
    item, evidence, context = _context(result, quote, as_of, quote_date, mode)
    assert verify_market_scan_point_in_time_evidence_context(evidence, item=item, **context)
    if value == "missing":
        item.score_details["inputs"].pop("scoring_change_pct")
    else:
        item.score_details["inputs"]["scoring_change_pct"] = value
    assert not verify_market_scan_point_in_time_evidence_context(evidence, item=item, **context)


@pytest.mark.parametrize("mode", MODES)
def test_existing_full_production_snapshot_helper_carries_canonical_input(monkeypatch, mode):
    item, run = _snapshot(monkeypatch, mutation="none", mode=mode)
    evidence = item.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    payload = evidence["payload"]
    assert item.score_details["inputs"]["scoring_change_pct"] == round(
        (payload["quote_price"] - payload["quote_prev_close"]) / payload["quote_prev_close"] * 100, 12,
    )
    scoring.verify_persisted_market_scan_result(item, run)


def _old_v4_snapshot():
    path = Path(__file__).parent / "fixtures/market_scan_trend_v4_canonical_predecessor.json"
    value = json.loads(path.read_text())
    return MarketScanResultItem.model_validate(value["item"]), MarketScanRun.model_validate(value["run"])


def test_frozen_trend_v4_preserves_old_hash_and_reads_without_new_price_admission(monkeypatch):
    item, run = _old_v4_snapshot()
    before = deepcopy(item.model_dump())
    evidence = item.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    assert item.score_details["score_spec_hash"] == OLD_V4_HASH
    assert "quote_prev_close" not in evidence["payload"]
    assert "scoring_change_pct" not in item.score_details["inputs"]
    assert scoring.stable_score_spec_hash(scoring.market_scan_score_spec_trend_v4(min_data_quality_score=50)) == OLD_V4_HASH
    monkeypatch.setattr(scoring, "score_market_scan_item", lambda *_args, **_kwargs: pytest.fail("historical row was scored again"))
    monkeypatch.setattr(production_replay, "canonical_price_change_pct", lambda *_args: pytest.fail("old trend required new canonical input"))
    scoring.verify_persisted_market_scan_result(
        item, run, expected_score_rule_version="full-market-score-v5", expected_score_spec_hash=OLD_V4_HASH,
    )
    assert scoring.replay_score_details(item.score_details).raw_score == item.raw_score == 88.3484
    assert item.model_dump() == before


def test_frozen_trend_v4_is_registered_readable_but_not_current_writable():
    item, _ = _old_v4_snapshot()
    assert is_registered_production_score_contract("full-market-score-v5", OLD_V4_HASH)
    assert not is_current_writable_production_score_contract("full-market-score-v5", OLD_V4_HASH)
    assert not scoring.is_current_market_scan_score_spec(item.score_details["score_spec"], OLD_V4_HASH)
    settings = SimpleNamespace(
        market_scan_min_data_quality_score=50, market_scan_kline_limit=120,
        market_scan_min_history_rows=61, market_scan_new_stock_days=120,
    )
    contract = market_scan_rule_contract(settings)
    assert scoring.stable_score_spec_hash(contract["score_spec"]) != OLD_V4_HASH
    contract["score_spec"] = deepcopy(item.score_details["score_spec"])
    with sqlite3.connect(":memory:") as conn, pytest.raises(ValueError, match="当前可写"):
        register_market_scan_rule_contract(
            conn, rule_version=f"full-market-scan-v6:{scoring.stable_score_spec_hash(contract)}",
            contract=contract, stamp=AS_OF.isoformat(),
        )


def test_old_v4_evidence_cannot_be_relabelled_as_current_without_previous_close():
    item, run = _old_v4_snapshot()
    item.score_details["score_spec"] = scoring.market_scan_score_spec(min_data_quality_score=50)
    item.score_details["score_spec_hash"] = scoring.stable_score_spec_hash(item.score_details["score_spec"])
    item.score_details["inputs"]["scoring_change_pct"] = 5.0
    evidence = item.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    assert not verify_market_scan_point_in_time_evidence_context(
        evidence, item=item, expected_data_date=run.data_date, expected_quote_date=run.quote_date,
        expected_as_of=run.as_of, expected_mode=run.mode,
    )
