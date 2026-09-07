from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterator
import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.models.market import Kline, Quote
from app.models.market_scan import MarketScanResultItem, MarketScanResultWrite
from app.repositories.market_scan_result_validation import (
    validate_production_result_write,
    validate_result_write,
)
from app.repositories.market_scan_rule_contracts import register_market_scan_rule_contract
from app.services.market_scan_execution_quote import MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY
from app.services.market_scan_manager import market_scan_rule_contract
from app.services.market_scan_pressure import MarketScanPressureController
from app.services.market_scan_score_contract import stable_score_spec_hash
from app.services.market_scan_scoring import MarketScanDataMissing
from app.services.market_scan_skip_contract import MARKET_SCAN_SKIP_EVIDENCE_KEY
from app.services.market_scan_stock_evaluation import MarketScanStockEvaluator
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.market_scan_validation import (
    failed_scan_result_for_exception,
    raise_batch_outcome_error,
)
from app.utils.provider_errors import ProviderChainUnavailable
from tests.test_market_scan_scoring import AS_OF, DATA_DATE, _item, _quote, _rows
from tests.test_market_scan_skip_contract import _case, _running_repository


OBSERVED_AT = AS_OF.isoformat(sep=" ")


def _settings() -> Settings:
    return Settings(
        scheduler_enabled=False,
        market_scan_retry_attempts=1,
        market_scan_min_history_rows=61,
        market_scan_min_data_quality_score=50,
    )


class _Hub:
    def __init__(self, rows: list[Kline], *, error: BaseException | None = None) -> None:
        self.settings = _settings()
        self.rows = rows
        self.error = error

    async def kline(self, symbol: str, **_kwargs: object) -> list[Kline]:
        if self.error is not None:
            raise self.error
        return self.rows if symbol == "600519.SH" else _rows(DATA_DATE, 80)


def _evaluator(hub: _Hub) -> MarketScanStockEvaluator:
    return MarketScanStockEvaluator(
        hub,
        MarketScanPressureController(2, retry_backoff_seconds=0),
        Counter(),
        sensitive_values=(),
        monotonic=lambda: 0.0,
        kline_prefetch=None,
    )


async def _scan_one(
    evaluator: MarketScanStockEvaluator,
    item: MarketScanResultItem,
    quote: Quote | None,
    rule_version: str,
    *,
    observed_at: str = OBSERVED_AT,
) -> MarketScanResultWrite:
    return await evaluator.scan_one(
        item, quote, quote_error="报价不可用", quote_observed_at=observed_at,
        semaphore=asyncio.Semaphore(2), cancel_event=asyncio.Event(), as_of=AS_OF,
        cutoff=DATA_DATE, expected_data_date=DATA_DATE, expected_quote_date=DATA_DATE,
        mode="official", rule_version=rule_version, prefetched_cache=None,
    )


@pytest.fixture
def production_contract() -> Iterator[tuple[sqlite3.Connection, sqlite3.Row, str]]:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE market_scan_rule_contract (rule_version TEXT PRIMARY KEY, "
            "contract_json TEXT, production_score_rule_version TEXT, "
            "production_score_spec_hash TEXT, created_at TEXT)"
        )
        contract = market_scan_rule_contract(_settings())
        rule_version = f"full-market-scan-v6:{stable_score_spec_hash(contract)}"
        register_market_scan_rule_contract(
            conn, rule_version=rule_version, contract=contract, stamp=OBSERVED_AT,
        )
        run = conn.execute(
            "SELECT ? AS rule_version, 'official' AS mode, ? AS quote_date, ? AS scope",
            (rule_version, DATA_DATE.isoformat(), FULL_MARKET_SCOPE),
        ).fetchone()
        yield conn, run, rule_version


def _isolated_missing_result(
    rows: list[Kline],
    quote: Quote | None,
    production_contract: tuple[sqlite3.Connection, sqlite3.Row, str],
    *,
    observed_at: str = OBSERVED_AT,
) -> MarketScanResultWrite:
    conn, run, rule_version = production_contract

    async def scenario() -> list[MarketScanResultWrite | BaseException]:
        evaluator = _evaluator(_Hub(rows))
        good_item = _item().model_copy(update={"symbol": "600520.SH", "code": "600520"})
        return await asyncio.gather(
            _scan_one(evaluator, _item(), quote, rule_version, observed_at=observed_at),
            _scan_one(evaluator, good_item, _quote(code="600520"), rule_version),
            return_exceptions=True,
        )

    outcomes = asyncio.run(scenario())
    raise_batch_outcome_error(outcomes)
    missing, success = outcomes
    assert isinstance(missing, MarketScanResultWrite)
    assert isinstance(success, MarketScanResultWrite)
    assert missing.status == "missing"
    assert success.status == "success"
    assert missing.score is None and missing.raw_score is None
    validate_result_write(missing)
    validate_production_result_write(missing, run, conn)
    return missing


@pytest.mark.parametrize("quote_present", [False, True])
@pytest.mark.parametrize("invalid_bar", ["price_conflict", "fallback_conflict", "demo"])
def test_rejected_bars_remain_one_missing_stock_in_a_healthy_batch(
    production_contract: tuple[sqlite3.Connection, sqlite3.Row, str],
    quote_present: bool,
    invalid_bar: str,
) -> None:
    rows = _rows(DATA_DATE, 80)
    if invalid_bar == "demo":
        rows[-1] = rows[-1].model_copy(update={"source": "demo"})
        message = "演示日K不能用于全市场生产评分"
    else:
        update = {"close": rows[-1].close + 0.01} if invalid_bar == "price_conflict" else {"fallback_used": True}
        rows.append(rows[-1].model_copy(update=update))
        message = "存在冲突日K"

    result = _isolated_missing_result(rows, _quote() if quote_present else None, production_contract)

    assert message in (result.error or "")
    assert result.data_date is None
    assert result.kline_source is None
    assert result.adjustment_mode is None
    assert result.kline_fallback_used is False
    assert (MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY in result.score_details) is quote_present


@pytest.mark.parametrize(
    ("quote_update", "observed_at", "error_fragment"),
    [
        ({"code": "000001", "market": "SZ"}, OBSERVED_AT, "行情代码不匹配"),
        ({"timestamp": "bad"}, OBSERVED_AT, "报价时间"),
        ({"high": 1.0}, OBSERVED_AT, "OHLC"),
        ({}, "bad", "captured_at"),
    ],
)
def test_rejected_quote_evidence_does_not_abort_batch_or_claim_valid_quote(
    production_contract: tuple[sqlite3.Connection, sqlite3.Row, str],
    quote_update: dict[str, object],
    observed_at: str,
    error_fragment: str,
) -> None:
    quote = _quote().model_copy(update=quote_update)

    result = _isolated_missing_result(
        _rows(DATA_DATE, 80), quote, production_contract, observed_at=observed_at,
    )

    assert error_fragment in (result.error or "")
    assert result.quote_timestamp is None
    assert result.quote_observed_at is None
    assert result.quote_source is None
    assert result.quote_fallback_used is False
    assert MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY not in result.score_details
    assert result.data_date == DATA_DATE.isoformat()


@pytest.mark.parametrize(("quote_fallback", "kline_fallback"), [(True, False), (False, True), (True, True)])
def test_failure_keeps_valid_fallback_evidence_bound_to_outer_fields(
    production_contract: tuple[sqlite3.Connection, sqlite3.Row, str],
    quote_fallback: bool,
    kline_fallback: bool,
) -> None:
    rows = _rows(DATA_DATE, 20)
    rows[0] = rows[0].model_copy(update={"fallback_used": kline_fallback})

    result = _isolated_missing_result(rows, _quote(fallback_used=quote_fallback), production_contract)

    assert "完整前复权日K不足" in (result.error or "")
    assert result.quote_fallback_used is quote_fallback
    assert result.kline_fallback_used is kline_fallback
    assert ("quote_fallback" in result.degradation_reasons) is quote_fallback
    assert ("kline_fallback" in result.degradation_reasons) is kline_fallback
    assert result.quote_timestamp == _quote().timestamp
    assert MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY in result.score_details


def test_real_scan_one_preserves_typed_skip_evidence_for_repository_write(tmp_path: Path) -> None:
    repo, run, item, settings, rule_version = _running_repository(tmp_path)
    item, quote, rows = _case("new_listing_insufficient_history", item=item)
    hub = _Hub(rows)
    hub.settings = settings

    result = asyncio.run(_scan_one(_evaluator(hub), item, quote, rule_version, observed_at="2026-07-17 15:01:00"))

    assert result.status == "skipped"
    assert MARKET_SCAN_SKIP_EVIDENCE_KEY in result.score_details
    assert MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY in result.score_details
    updated = repo.save_result_batch(run.id, [result])
    assert updated.skipped_count == 1
    assert updated.missing_count == 0


class _StopScan(BaseException):
    pass


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, _StopScan, ProviderChainUnavailable])
def test_scan_one_keeps_cancellation_and_systemic_failure_semantics(error_type: type[BaseException]) -> None:
    error = error_type("stop scan")

    with pytest.raises(error_type):
        asyncio.run(_scan_one(_evaluator(_Hub([], error=error)), _item(), _quote(), "test-rule"))


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, _StopScan, RuntimeError])
@pytest.mark.parametrize("stage", ["completed_market_scan_klines", "build_market_scan_execution_quote_evidence"])
def test_failure_evidence_recovery_does_not_swallow_unexpected_or_fatal_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
    stage: str,
) -> None:
    def abort(*_args: object, **_kwargs: object) -> None:
        raise error_type("do not convert")

    monkeypatch.setattr(f"app.services.market_scan_validation.{stage}", abort)

    with pytest.raises(error_type, match="do not convert"):
        failed_scan_result_for_exception(
            item=_item(), quote=_quote(), rows=_rows(DATA_DATE, 80), cutoff=DATA_DATE,
            exc=MarketScanDataMissing("original error"), sensitive_values=(),
            quote_observed_at=OBSERVED_AT, mode="official", quote_date=DATA_DATE,
        )
