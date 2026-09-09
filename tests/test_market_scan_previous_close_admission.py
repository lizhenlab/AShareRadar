"""Completed quotes must use the same previous close as their daily evidence."""

import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.services.market_scan_pressure import MarketScanPressureController
from app.services.market_scan_scoring import MarketScanDataMissing, score_market_scan_item
from app.models.market_scan import MarketScanResultItem, MarketScanRun
from app.repositories.market_scan_rule_contracts import register_market_scan_rule_contract
from app.services.market_scan_manager import market_scan_rule_contract
from app.services import market_scan_scoring as scoring
from app.services.market_scan_stock_evaluation import MarketScanStockEvaluator
from tests.test_market_scan_completed_snapshot_trend import _load_legacy_read_fixture
from tests.test_market_scan_input_admission import _assert_current_score_contract_and_frozen_dimension_v4
from tests.test_market_scan_repository import _results
from tests.test_market_scan_scoring import AS_OF, DATA_DATE, PREOPEN_AS_OF, _item, _quote, _rows


def _inputs(previous=10.0, *, price=10.0, bar_previous=None):
    rows = [row.model_copy(update={
        "open": price, "close": price, "high": price + .1, "low": price - .1,
        "volume": 1_000_000.0,
    }) for row in _rows(DATA_DATE, 80)]
    if bar_previous is not None:
        rows[-2] = rows[-2].model_copy(update={
            "open": bar_previous, "close": bar_previous,
            "high": bar_previous + .1, "low": bar_previous - .1,
        })
    quote = _quote().model_copy(update={
        "price": price, "open": price, "high": price + .1, "low": price - .1,
        "prev_close": previous, "change": price - previous,
        "change_pct": (price - previous) / previous * 100, "volume": 1_000_000.0,
    })
    return quote, rows


def _score(quote, rows, *, mode="official"):
    return score_market_scan_item(
        _item(), quote, rows, as_of=AS_OF if mode == "official" else PREOPEN_AS_OF,
        mode=mode, completed_cutoff=DATA_DATE, expected_data_date=DATA_DATE,
        expected_quote_date=DATA_DATE, min_history_rows=61, min_data_quality_score=50,
    )


@pytest.mark.parametrize("mode", ["official", "preopen"])
@pytest.mark.parametrize("previous", [9.1, 11.0])
def test_self_consistent_quote_cannot_disagree_with_completed_previous_bar(mode, previous):
    quote, rows = _inputs(previous)
    assert rows[-1].close == quote.price
    assert (quote.price - quote.prev_close) / quote.prev_close * 100 == quote.change_pct
    with pytest.raises(MarketScanDataMissing, match="昨收价.*前一交易日日K收盘价"):
        _score(quote, rows, mode=mode)


class _Hub:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.settings = SimpleNamespace(
            market_scan_retry_attempts=1, market_scan_min_history_rows=61,
            market_scan_min_data_quality_score=50, market_scan_new_stock_days=120,
            market_scan_kline_limit=120, market_scan_symbol_timeout_seconds=5,
        )

    async def kline(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        return self.rows


@pytest.mark.parametrize("mode", ["official", "preopen"])
@pytest.mark.parametrize("previous,expected_status", [(9.1, "missing"), (10.0, "success")])
def test_stock_evaluator_classifies_previous_close_conflict_as_missing(mode, previous, expected_status):
    quote, rows = _inputs(previous)
    hub = _Hub(rows)
    evaluator = MarketScanStockEvaluator(
        hub, MarketScanPressureController(2, retry_backoff_seconds=0), Counter(),
        sensitive_values=(), monotonic=lambda: 0.0, kline_prefetch=None,
    )

    async def scan():
        return await evaluator.scan_one(
            _item(), quote, quote_error=None, quote_observed_at=quote.timestamp,
            semaphore=asyncio.Semaphore(2), cancel_event=asyncio.Event(),
            as_of=AS_OF if mode == "official" else PREOPEN_AS_OF,
            cutoff=DATA_DATE, expected_data_date=DATA_DATE, expected_quote_date=DATA_DATE,
            mode=mode, rule_version="synthetic-admission-check", prefetched_cache=None,
        )

    result = asyncio.run(scan())
    assert len(hub.calls) == 1
    assert result.status == expected_status
    if expected_status == "missing":
        assert result.score is None and result.raw_score is None
        assert "昨收价" in result.error
    else:
        assert result.data_quality_score == 100
        assert result.raw_score == 60


@pytest.mark.parametrize("mode", ["official", "preopen"])
@pytest.mark.parametrize("price,bar_previous,accepted", [
    (2.0, 1.98, True), (2.0, 1.97999999, False),
    (100.0, 99.5, True), (100.0, 99.49999999, False),
])
def test_previous_close_reuses_inclusive_price_tolerances(mode, price, bar_previous, accepted):
    quote, rows = _inputs(price, price=price, bar_previous=bar_previous)
    if accepted:
        result = _score(quote, rows, mode=mode)
        assert result.status == "success" and result.data_quality_score == 100
    else:
        with pytest.raises(MarketScanDataMissing, match="昨收价"):
            _score(quote, rows, mode=mode)


def test_intraday_still_binds_previous_close_to_last_completed_bar():
    quote, rows = _inputs(10.0, bar_previous=7.0)
    quote = quote.model_copy(update={
        "timestamp": "2026-07-20 10:00:00", "price": 10.1,
        "high": 10.2, "change": .1, "change_pct": 1.0,
    })
    kwargs = dict(
        as_of=PREOPEN_AS_OF.replace(hour=10), mode="intraday", completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE, expected_quote_date=PREOPEN_AS_OF.date(),
        min_history_rows=61, min_data_quality_score=50,
    )
    result = score_market_scan_item(_item(), quote, rows, **kwargs)
    assert result.status == "success"
    assert result.score_details["components"]["score_dimensions"]["raw_features"]["return_1d_pct"] == 1.0
    inconsistent = quote.model_copy(update={"prev_close": 9.1, "change": 1.0, "change_pct": 1 / 9.1 * 100})
    with pytest.raises(MarketScanDataMissing, match="昨收价与上一完整日K收盘价"):
        score_market_scan_item(_item(), inconsistent, rows, **kwargs)


@pytest.mark.parametrize("count", [0, 1])
def test_previous_close_boundary_does_not_index_unavailable_history(count):
    quote, rows = _inputs(9.1)
    for mode in ("official", "preopen"):
        scoring._require_completed_quote_previous_close(quote, rows[:count], mode=mode)  # noqa: SLF001


def test_previous_close_uses_sorted_admitted_bars_not_raw_provider_order():
    quote, rows = _inputs()
    baseline = _score(quote, rows)
    future = rows[-1].model_copy(update={"date": "2026-07-20", "close": 10.05})
    shuffled = [future, rows[-1], *reversed(rows)]
    assert _score(quote, shuffled) == baseline


def _historical_fixture():
    return json.loads((Path(__file__).parent / "fixtures/market_scan_previous_close_admission_v4.json").read_text())


def _historical_snapshot():
    value = _historical_fixture()
    return MarketScanResultItem.model_validate(value["item"]), MarketScanRun.model_validate(value["run"])


def test_old_admission_changes_run_identity_without_changing_score_math():
    old = _historical_fixture()["run_contract"]
    current = market_scan_rule_contract(SimpleNamespace(
        market_scan_min_data_quality_score=50, market_scan_kline_limit=120,
        market_scan_min_history_rows=61, market_scan_new_stock_days=120,
    ))
    assert old["input_admission"]["contract_version"] == "market-scan-input-admission-v4"
    assert current["input_admission"]["contract_version"] == "market-scan-input-admission-v5"
    assert old["score_spec"] == scoring.market_scan_score_spec_dimension_v4(min_data_quality_score=50)
    assert scoring.stable_score_spec_hash(old["score_spec"]) == "17c0e6b9ed6de9b39cad0d9f9d1fe14c8638749dd08796bed827869d3554e2c7"
    _assert_current_score_contract_and_frozen_dimension_v4(current["score_spec"])
    admission_only_prior = deepcopy(current)
    admission_only_prior["input_admission"] = deepcopy(old["input_admission"])
    assert current["score_spec"] == admission_only_prior["score_spec"]
    assert scoring.stable_score_spec_hash(current) != scoring.stable_score_spec_hash(admission_only_prior)
    assert scoring.stable_score_spec_hash(current) != scoring.stable_score_spec_hash(old)
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(ValueError, match="当前可写 v5 评分规范"):
            register_market_scan_rule_contract(
                conn, rule_version=f"full-market-scan-v6:{scoring.stable_score_spec_hash(old)}",
                contract=old, stamp=AS_OF.isoformat(),
            )
        with pytest.raises(ValueError, match="准入"):
            register_market_scan_rule_contract(
                conn, rule_version=f"full-market-scan-v6:{scoring.stable_score_spec_hash(admission_only_prior)}",
                contract=admission_only_prior, stamp=AS_OF.isoformat(),
            )


def test_real_baseline_conflicting_previous_close_stays_readable_without_readmission(monkeypatch):
    item, run = _historical_snapshot()
    assert item.raw_score == 69.0
    assert item.change_pct == pytest.approx((10 - 9.1) / 9.1 * 100)
    frozen = deepcopy(item.score_details)
    with pytest.raises(MarketScanDataMissing, match="昨收价"):
        _score(*_inputs(9.1))
    monkeypatch.setattr(scoring, "_require_completed_quote_previous_close", lambda *_args, **_kwargs: pytest.fail("historical input was readmitted"))
    scoring.verify_persisted_market_scan_result(item, run)
    assert scoring.replay_score_details(item.score_details).raw_score == 69.0
    assert item.score_details == frozen


def test_sealed_old_admission_history_is_read_only(tmp_path, monkeypatch):
    snapshot = _historical_snapshot()
    repo, run_id, path = _load_legacy_read_fixture(tmp_path, snapshot=snapshot)
    before = path.read_bytes()
    monkeypatch.setattr(scoring, "_require_completed_quote_previous_close", lambda *_args, **_kwargs: pytest.fail("historical input was readmitted"))
    page = _results(repo, run_id)
    assert page.items[0].raw_score == 69.0
    assert page.items[0].score_details == snapshot[0].score_details
    assert path.read_bytes() == before
