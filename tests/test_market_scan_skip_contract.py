from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from inspect import signature
from pathlib import Path
import json
import sqlite3

import pytest

from app.config import Settings
from app.db.market_scan_action_source import (
    MarketScanActionSourceError,
    inspect_market_scan_action_source,
    market_scan_diagnostics_authorize_action,
    require_market_scan_action_source,
)
from app.db.market_scan_integrity import (
    MarketScanSnapshotSealError,
    market_scan_snapshot_digest,
    verify_market_scan_snapshot,
)
from app.models.market import Kline, Quote
from app.models.market_scan import (
    MarketScanCoverage,
    MarketScanMarketProgress,
    MarketScanMode,
    MarketScanPublicationDiagnostic,
    MarketScanResultItem,
    MarketScanResultWrite,
    MarketScanScoreDistribution,
    MarketScanScoreDistributionPolicy,
    MarketScanSeed,
)
from app.services.cache import SQLiteCache
from app.services.market_scan_execution import MarketScanExecutor
from app.services.market_scan_manager import market_scan_rule_contract
from app.services.market_scan_score_contract import stable_score_spec_hash
from app.services.market_scan_scoring import (
    MarketScanDataMissing,
    MarketScanSkipped,
    score_market_scan_item,
)
from app.services.market_scan_skip_pit import (
    build_market_scan_skip_pit,
    verify_market_scan_skip_pit,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.market_scan_validation import (
    MarketScanRuntimeGuard,
    failed_scan_result_for_exception,
    missing_quote_result,
)
from app.services.trading_calendar import trading_date_range
from app.repositories import market_scan_action_gate_replay
from app.repositories import market_scan_terminal_publication
from app.repositories.market_scan_action_gate_replay import (
    MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE,
    validate_current_action_gate_claim,
)
from app.repositories.market_scan_score_diagnostics import (
    read_success_score_observations,
    score_observation_from_canonical_result,
)
from tests.factories import make_kline, make_quote
from tests.market_scan_test_support import action_pass_publication_diagnostics
from tests.test_strategy_execution import _disable_market_scan_immutability


DATA_DATE = date(2026, 7, 17)
AS_OF = datetime(2026, 7, 17, 16, 30)
QUOTE_OBSERVED_AT = "2026-07-17 15:01:00"


def test_run82_v5_reclassification_keeps_every_market_above_publication_gates() -> None:
    coverages = {
        "ALL": MarketScanCoverage("ALL", 5_396, 5_381, 15, 147),
        "SH": MarketScanCoverage("SH", 2_272, 2_265, 7, 40),
        "SZ": MarketScanCoverage("SZ", 2_814, 2_806, 8, 82),
        "BJ": MarketScanCoverage("BJ", 310, 310, 0, 25),
    }

    assert sum(item.success_count for key, item in coverages.items() if key != "ALL") == 5_381
    assert sum(item.missing_count for key, item in coverages.items() if key != "ALL") == 15
    assert sum(item.skipped_count for key, item in coverages.items() if key != "ALL") == 147
    assert all(item.coverage_ratio >= 0.95 for item in coverages.values())
    assert all(item.eligible_ratio >= 0.90 for item in coverages.values())
    progress = (
        MarketScanMarketProgress(
            market="SH", total_count=2_312, processed_count=2_312,
            success_count=2_265, missing_count=7, skipped_count=40,
            coverage_pct=2_265 / 2_272 * 100,
        ),
        MarketScanMarketProgress(
            market="SZ", total_count=2_896, processed_count=2_896,
            success_count=2_806, missing_count=8, skipped_count=82,
            coverage_pct=2_806 / 2_814 * 100,
        ),
        MarketScanMarketProgress(
            market="BJ", total_count=335, processed_count=335,
            success_count=310, missing_count=0, skipped_count=25,
            coverage_pct=100.0,
        ),
    )
    assert sum(item.total_count for item in progress) == 5_543
    eligible = sum(item.total_count - item.skipped_count for item in progress)
    successes = sum(item.success_count for item in progress)
    assert successes / eligible == coverages["ALL"].coverage_ratio


@pytest.mark.parametrize("reason_code", ["official_session_gap", "new_listing_insufficient_history"])
@pytest.mark.parametrize("invalid_quote", ["zero_trade", "single_price"])
def test_typed_skip_cannot_hide_unrankable_liquidity(
    reason_code: str,
    invalid_quote: str,
) -> None:
    settings, rule_version = _settings_and_rule()
    item, quote, rows = _case(reason_code)
    if invalid_quote == "zero_trade":
        quote = quote.model_copy(update={"volume": 0.0, "amount": 0.0})
    else:
        quote = quote.model_copy(
            update={"open": quote.price, "high": quote.price, "low": quote.price}
        )

    with pytest.raises(MarketScanDataMissing):
        _score(item, quote, rows, settings=settings, rule_version=rule_version)


@pytest.mark.parametrize(
    "bar_update",
    [
        {"adjustment_mode": "unknown"},
        {"data_version": "unknown"},
        {"as_of": "2026-07-18 15:15:00"},
        {"fallback_used": True},
    ],
)
@pytest.mark.parametrize(
    "reason_code",
    ["official_session_gap", "new_listing_insufficient_history"],
)
def test_typed_skip_rejects_untrusted_partial_bars(
    bar_update: dict[str, object],
    reason_code: str,
) -> None:
    settings, rule_version = _settings_and_rule()
    item, quote, rows = _case(reason_code)
    rows = [row.model_copy(update=bar_update) for row in rows]

    with pytest.raises(MarketScanDataMissing):
        _score(item, quote, rows, settings=settings, rule_version=rule_version)


@pytest.mark.parametrize(
    ("snapshot_as_of", "expected"),
    [
        (DATA_DATE.isoformat(), True),
        (f"{DATA_DATE.isoformat()} 15:15:00", True),
        (f"{DATA_DATE.isoformat()}T15:15:00+08:00", True),
        ("2026-07-18", False),
        ("2026/07/17", False),
    ],
)
def test_skip_pit_kline_snapshot_time_contract(
    snapshot_as_of: str,
    expected: bool,
) -> None:
    item, quote, rows = _case("new_listing_insufficient_history")
    rows = [row.model_copy(update={"as_of": snapshot_as_of}) for row in rows]
    pit = build_market_scan_skip_pit(
        quote,
        rows,
        quote_observed_at=QUOTE_OBSERVED_AT,
    )

    assert verify_market_scan_skip_pit(
        pit,
        expected_symbol=item.symbol,
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=AS_OF.isoformat(sep=" "),
        expected_bar_dates=[row.date for row in rows],
        expected_quote_timestamp=quote.timestamp,
        expected_quote_observed_at=QUOTE_OBSERVED_AT,
        expected_quote_source=quote.source,
        expected_kline_source=rows[-1].source,
        expected_adjustment_mode="qfq",
    ) is expected


@pytest.mark.parametrize(
    "snapshot_as_of",
    [
        DATA_DATE.isoformat(),
        f"{DATA_DATE.isoformat()} 15:15:00",
        f"{DATA_DATE.isoformat()}T15:15:00+08:00",
    ],
)
@pytest.mark.parametrize(
    "reason_code",
    ["official_session_gap", "new_listing_insufficient_history"],
)
def test_typed_skip_accepts_supported_kline_snapshot_times(
    reason_code: str,
    snapshot_as_of: str,
) -> None:
    settings, rule_version = _settings_and_rule()
    item, quote, rows = _case(reason_code)
    rows = [row.model_copy(update={"as_of": snapshot_as_of}) for row in rows]

    result = _skip_result(
        item,
        quote,
        rows,
        settings=settings,
        rule_version=rule_version,
    )

    assert result.status == "skipped"
    assert result.score_details["skip_evidence"]


@pytest.mark.parametrize("mode", ["official", "intraday", "preopen"])
def test_new_listing_skip_is_replayable_in_every_scan_mode(
    mode: MarketScanMode,
) -> None:
    settings, rule_version = _settings_and_rule()
    as_of, data_date, quote_date, quote_observed_at, quote_timestamp = (
        _mode_context(mode)
    )
    item, quote, rows = _case(
        "new_listing_insufficient_history",
        data_date=data_date,
        quote_timestamp=quote_timestamp,
    )
    quote = _quote_for_mode(quote, mode)

    result = _skip_result(
        item,
        quote,
        rows,
        settings=settings,
        rule_version=rule_version,
        mode=mode,
        as_of=as_of,
        data_date=data_date,
        quote_date=quote_date,
        quote_observed_at=quote_observed_at,
    )

    evidence = result.score_details["skip_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["mode"] == mode


def test_intraday_new_listing_skip_rejects_previous_close_mismatch() -> None:
    settings, rule_version = _settings_and_rule()
    mode: MarketScanMode = "intraday"
    as_of, data_date, quote_date, quote_observed_at, quote_timestamp = (
        _mode_context(mode)
    )
    item, quote, rows = _case(
        "new_listing_insufficient_history",
        data_date=data_date,
        quote_timestamp=quote_timestamp,
    )
    quote = _quote_for_mode(quote, mode)
    mismatched_previous = round(float(quote.prev_close) * 0.95, 4)
    quote = quote.model_copy(
        update={
            "prev_close": mismatched_previous,
            "change": round(float(quote.price) - mismatched_previous, 4),
            "change_pct": round(
                (float(quote.price) - mismatched_previous)
                / mismatched_previous
                * 100,
                4,
            ),
        }
    )

    with pytest.raises(ValueError, match="跳过证据不满足规范"):
        _score(
            item,
            quote,
            rows,
            settings=settings,
            rule_version=rule_version,
            mode=mode,
            as_of=as_of,
            data_date=data_date,
            quote_date=quote_date,
            quote_observed_at=quote_observed_at,
        )


@pytest.mark.parametrize("snapshot_as_of", ["2026-07-18", "2026/07/17"])
@pytest.mark.parametrize(
    "reason_code",
    ["official_session_gap", "new_listing_insufficient_history"],
)
def test_typed_skip_rejects_future_or_noncanonical_date_only_snapshot(
    reason_code: str,
    snapshot_as_of: str,
) -> None:
    settings, rule_version = _settings_and_rule()
    item, quote, rows = _case(reason_code)
    rows = [row.model_copy(update={"as_of": snapshot_as_of}) for row in rows]

    with pytest.raises(MarketScanDataMissing):
        _score(item, quote, rows, settings=settings, rule_version=rule_version)


def test_missing_quote_never_creates_an_action_eligible_new_listing_skip() -> None:
    item, _quote, rows = _case("new_listing_insufficient_history")

    result = missing_quote_result(
        item,
        rows,
        cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        quote_error="provider missing",
        min_history_rows=61,
    )

    assert result.status == "missing"
    assert result.score_details == {}


@pytest.mark.parametrize("tamper", ["threshold", "bar_contract", "symbol"])
def test_current_write_rejects_resigned_skip_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    repo, run, item, settings, rule_version = _running_repository(tmp_path)
    _case_item, quote, rows = _case("new_listing_insufficient_history", item=item)
    result = _skip_result(item, quote, rows, settings=settings, rule_version=rule_version)
    details = deepcopy(result.score_details)
    evidence = details["skip_evidence"]
    assert isinstance(evidence, dict)
    if tamper == "threshold":
        evidence["required_history_rows"] = 999
    elif tamper == "symbol":
        evidence["symbol"] = "000001.SZ"
    else:
        facts = evidence["facts"]
        assert isinstance(facts, dict)
        pit = facts["pit"]
        assert isinstance(pit, dict)
        bars = pit["bar_contract"]
        assert isinstance(bars, list)
        bars[0][6] = "unknown"
        pit["bar_contract_digest"] = stable_score_spec_hash(bars)
    evidence_without_digest = dict(evidence)
    evidence_without_digest.pop("evidence_digest")
    evidence["evidence_digest"] = stable_score_spec_hash(evidence_without_digest)

    with pytest.raises(ValueError, match="可信|合同|证据"):
        repo.save_result_batch(
            run.id,
            [replace(result, score_details=details)],
        )


@pytest.mark.parametrize("mode", ["official", "intraday", "preopen"])
def test_current_write_accepts_a_fully_verified_new_listing_skip(
    tmp_path: Path,
    mode: MarketScanMode,
) -> None:
    repo, run, item, settings, rule_version = _running_repository(tmp_path, mode=mode)
    as_of, data_date, quote_date, quote_observed_at, quote_timestamp = (
        _mode_context(mode)
    )
    _case_item, quote, rows = _case(
        "new_listing_insufficient_history",
        item=item,
        data_date=data_date,
        quote_timestamp=quote_timestamp,
    )
    quote = _quote_for_mode(quote, mode)
    rows = [
        row.model_copy(update={"as_of": data_date.isoformat()})
        for row in rows
    ]
    result = _skip_result(
        item,
        quote,
        rows,
        settings=settings,
        rule_version=rule_version,
        mode=mode,
        as_of=as_of,
        data_date=data_date,
        quote_date=quote_date,
        quote_observed_at=quote_observed_at,
    )

    updated = repo.save_result_batch(run.id, [result])

    assert updated.skipped_count == 1
    assert updated.missing_count == 0


@pytest.mark.parametrize("mode", ["intraday", "preopen"])
def test_nonofficial_skip_publication_omits_action_receipt(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    diagnostics = action_pass_publication_diagnostics()
    validated: list[object] = []
    monkeypatch.setattr(
        market_scan_terminal_publication,
        "validate_persisted_production_skips",
        lambda _conn, run: validated.append(run),
    )
    monkeypatch.setattr(
        market_scan_terminal_publication,
        "validate_current_action_gate_claim",
        lambda *_args, **_kwargs: pytest.fail(
            "nonofficial publication with skips must remain action-ineligible"
        ),
    )
    run = {"mode": mode, "skipped_count": 1}

    with sqlite3.connect(":memory:") as conn:
        observed = market_scan_terminal_publication.validated_publication_diagnostics(
            conn,
            run,  # type: ignore[arg-type]
            status="degraded",
            diagnostics=diagnostics,
        )

    assert observed == diagnostics
    assert validated == [run]


def test_two_quote_batches_persist_both_justified_skips_at_one_sealed_decision_time(
    tmp_path: Path,
) -> None:
    settings = Settings(
        cache_path=tmp_path / "two-batch-skips.sqlite3",
        scheduler_enabled=False,
        market_scan_batch_size=1,
        market_scan_concurrency=1,
        market_scan_batch_retry_attempts=1,
        market_scan_retry_attempts=1,
        market_scan_min_data_quality_score=0,
        market_scan_min_history_rows=61,
        market_scan_new_stock_days=120,
    )
    cache = SQLiteCache(settings=settings)
    repo = cache.market_scan_repo
    contract = market_scan_rule_contract(settings)
    rule_version = f"full-market-scan-v6:{stable_score_spec_hash(contract)}"
    run = repo.create_run(
        trigger="manual",
        mode="official",
        rule_version=rule_version,
        as_of="2026-07-17 16:30:00",
        data_date=DATA_DATE.isoformat(),
        scope=FULL_MARKET_SCOPE,
        rule_contract=contract,
    )
    run = repo.start_run(run.id)
    repo.record_stock_pool_source(run.id, "provider-full-pool")
    repo.seed_results(
        run.id,
        [
            MarketScanSeed(
                "600519.SH",
                "600519",
                "SH",
                "新股",
                list_date="2026-06-10",
                is_new=True,
                metadata_source="akshare",
            ),
            MarketScanSeed(
                "600520.SH",
                "600520",
                "SH",
                "旧股",
                list_date="2001-08-27",
                is_new=False,
                metadata_source="akshare",
            ),
        ],
        excluded_count=0,
    )
    pending = {item.symbol: item for item in repo.pending_items(run.id)}
    _unused, new_quote, new_rows = _case(
        "new_listing_insufficient_history",
        item=pending["600519.SH"],
    )
    _unused, gap_quote, gap_rows = _case(
        "official_session_gap",
        item=pending["600520.SH"],
    )

    class Hub:
        def __init__(self) -> None:
            self.settings = settings
            self.cache = cache
            self.quotes = {
                "600519.SH": new_quote,
                "600520.SH": gap_quote,
            }
            self.rows = {
                "600519.SH": new_rows,
                "600520.SH": gap_rows,
            }

        async def partial_quotes_with_errors(
            self,
            symbols: list[str],
            *,
            use_cache: bool = True,
        ) -> tuple[list[Quote], tuple[str, ...]]:
            del use_cache
            return [self.quotes[symbol] for symbol in symbols], ()

        async def kline(
            self,
            symbol: str,
            limit: int = 120,
            use_cache: bool = True,
            *,
            allow_stale: bool = False,
            require_provider_response: bool = False,
        ) -> list[Kline]:
            del use_cache, allow_stale, require_provider_response
            return self.rows[symbol][-limit:]

    capture_times = iter(
        [
            datetime(2026, 7, 17, 16, 30, 0),
            datetime(2026, 7, 17, 16, 30, 1),
            datetime(2026, 7, 17, 16, 30, 2),
            datetime(2026, 7, 17, 16, 30, 3),
        ]
    )
    executor = MarketScanExecutor(
        Hub(),  # type: ignore[arg-type]
        now=lambda: next(capture_times),
        monotonic=lambda: 0.0,
    )
    runtime_guard = MarketScanRuntimeGuard(
        data_date=DATA_DATE,
        quote_date=DATA_DATE,
        mode="official",
        wall_clock_budget_seconds=1_800,
        now=lambda: datetime(2026, 7, 17, 16, 31),
        monotonic=lambda: 0.0,
        started_monotonic=0.0,
    )

    asyncio.run(
        executor._process_pending(  # noqa: SLF001
            run,
            list(pending.values()),
            asyncio.Event(),
            runtime_guard,
        )
    )

    persisted_run = repo.run(run.id)
    assert persisted_run.as_of == "2026-07-17 16:30:02"
    with sqlite3.connect(settings.cache_path) as conn:
        rows = conn.execute(
            """
            SELECT symbol, status, quote_observed_at, metrics_json
            FROM market_scan_result WHERE run_id = ? ORDER BY symbol
            """,
            (run.id,),
        ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("600519.SH", "skipped"),
        ("600520.SH", "skipped"),
    ]
    assert rows[0][2] != rows[1][2]
    evidence = [json.loads(row[3])["score_details"]["skip_evidence"] for row in rows]
    assert [item["reason_code"] for item in evidence] == [
        "new_listing_insufficient_history",
        "official_session_gap",
    ]
    assert {item["as_of"] for item in evidence} == {persisted_run.as_of}


def test_score_gate_cannot_publish_300_successes_plus_33_arbitrary_skips(
    tmp_path: Path,
) -> None:
    settings, rule_version = _settings_and_rule(cache_path=tmp_path / "skip-attack.sqlite3")
    cache = SQLiteCache(settings=settings)
    repo = cache.market_scan_repo
    contract = market_scan_rule_contract(settings)
    seeds = [
        MarketScanSeed(f"{600000 + index:06d}.SH", f"{600000 + index:06d}", "SH", f"S{index}")
        for index in range(333)
    ]
    run = repo.create_run(
        trigger="manual",
        mode="official",
        rule_version=rule_version,
        as_of="2026-07-17 16:30:00",
        data_date=DATA_DATE.isoformat(),
        scope=FULL_MARKET_SCOPE,
        rule_contract=contract,
    )
    repo.start_run(run.id)
    repo.record_stock_pool_source(run.id, "provider-full-pool")
    repo.seed_results(run.id, seeds, excluded_count=0)
    with sqlite3.connect(settings.cache_path) as conn:
        conn.execute(
            """
            UPDATE market_scan_result
            SET status = CASE WHEN code < '600300' THEN 'success' ELSE 'skipped' END,
                score = CASE WHEN code < '600300' THEN 50 END,
                raw_score = CASE WHEN code < '600300' THEN 50.0 END,
                trend_score = CASE WHEN code < '600300' THEN 50 END,
                leader_score = CASE WHEN code < '600300' THEN 50 END,
                data_quality_score = CASE WHEN code < '600300' THEN 100 END,
                price = CASE WHEN code < '600300' THEN 10.0 END,
                reason = CASE WHEN code < '600300' THEN 'direct fixture' ELSE 'arbitrary skip' END,
                data_date = ?, metrics_json = '{}'
            WHERE run_id = ?
            """,
            (DATA_DATE.isoformat(), run.id),
        )
    diagnostics = action_pass_publication_diagnostics()
    assert market_scan_diagnostics_authorize_action(diagnostics) is True

    with pytest.raises(ValueError, match="结构化证据|负载合同"):
        repo.finish_run(
            run.id,
            "degraded",
            message="attempted gaming",
            publication_diagnostics=diagnostics,
        )


def test_current_finish_rejects_a_self_signed_score_pass_without_real_coverage(
    tmp_path: Path,
) -> None:
    settings, rule_version = _settings_and_rule(cache_path=tmp_path / "action-skip.sqlite3")
    cache = SQLiteCache(settings=settings)
    repo = cache.market_scan_repo
    contract = market_scan_rule_contract(settings)
    seeds = [
        MarketScanSeed(
            "600519.SH", "600519", "SH", "新股",
            list_date="2026-06-10", is_new=True, metadata_source="akshare",
        ),
        MarketScanSeed(
            "600520.SH", "600520", "SH", "旧股",
            list_date="2001-01-01", is_new=False, metadata_source="akshare",
        ),
    ]
    run = repo.create_run(
        trigger="manual", mode="official", rule_version=rule_version,
        as_of="2026-07-17 16:30:00", data_date=DATA_DATE.isoformat(),
        scope=FULL_MARKET_SCOPE, rule_contract=contract,
    )
    repo.start_run(run.id)
    repo.record_stock_pool_source(run.id, "provider-full-pool")
    repo.seed_results(run.id, seeds, excluded_count=0)
    repo.begin_quote_capture(run.id, "2026-07-17T08:29:59Z")
    repo.seal_quote_capture(
        run.id, finished_at="2026-07-17T08:30:02Z",
        decision_as_of="2026-07-17 16:30:00", duration_ms=3_000, count=2,
    )
    pending = {item.symbol: item for item in repo.pending_items(run.id)}
    _item_value, skip_quote, skip_rows = _case(
        "new_listing_insufficient_history",
        item=pending["600519.SH"],
    )
    success_quote, success_rows = _success_case(pending["600520.SH"])
    skip_result = _skip_result(
        pending["600519.SH"], skip_quote, skip_rows,
        settings=settings, rule_version=rule_version,
    )
    success_result = _score(
        pending["600520.SH"], success_quote, success_rows,
        settings=settings, rule_version=rule_version,
    )
    success_result = replace(success_result, quote_observed_at=QUOTE_OBSERVED_AT)
    repo.save_result_batch(run.id, [skip_result, success_result])
    with pytest.raises(ValueError, match="发布通过声明"):
        repo.finish_run(
            run.id,
            "degraded",
            message="self-signed pass",
            publication_diagnostics=action_pass_publication_diagnostics(),
        )
    rejected = repo.run(run.id)
    assert rejected.status == "running"
    assert rejected.snapshot_digest is None


def test_action_gate_revalidates_every_skip_after_a_valid_publication(tmp_path: Path) -> None:
    repo, settings, run_id, skip_symbol, _diagnostics = _valid_action_source_run(tmp_path)
    with sqlite3.connect(settings.cache_path) as conn:
        conn.row_factory = sqlite3.Row
        inspection = inspect_market_scan_action_source(conn, run_id)
        assert inspection.eligible is True
        assert inspection.reason is None
        assert require_market_scan_action_source(conn, run_id)
        _disable_market_scan_immutability(conn)
        raw = conn.execute(
            "SELECT metrics_json FROM market_scan_result WHERE run_id = ? AND symbol = ?",
            (run_id, skip_symbol),
        ).fetchone()[0]
        payload = json.loads(raw)
        evidence = payload["score_details"]["skip_evidence"]
        evidence["required_history_rows"] = 999
        unsigned = dict(evidence)
        unsigned.pop("evidence_digest")
        evidence["evidence_digest"] = stable_score_spec_hash(unsigned)
        conn.execute(
            "UPDATE market_scan_result SET metrics_json = ? WHERE run_id = ? AND symbol = ?",
            (
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                run_id,
                skip_symbol,
            ),
        )
        with pytest.raises(MarketScanSnapshotSealError, match="摘要不一致"):
            inspect_market_scan_action_source(conn, run_id)
        conn.execute(
            "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
            (market_scan_snapshot_digest(conn, run_id), run_id),
        )
        with pytest.raises(MarketScanActionSourceError, match="未证实的跳过样本"):
            require_market_scan_action_source(conn, run_id)


def test_verified_snapshot_score_observations_match_sql_oracle(tmp_path: Path) -> None:
    _repo, settings, run_id, _skip_symbol, _diagnostics = _valid_action_source_run(
        tmp_path
    )
    observed = []

    def collect(row: Mapping[str, object]) -> None:
        observation = score_observation_from_canonical_result(row)
        if observation is not None:
            observed.append(observation)

    with sqlite3.connect(settings.cache_path) as conn:
        conn.row_factory = sqlite3.Row
        expected_digest = market_scan_snapshot_digest(conn, run_id)
        actual_digest = verify_market_scan_snapshot(
            conn,
            run_id,
            result_observer=collect,
        )
        expected = read_success_score_observations(conn, run_id)

    assert actual_digest == expected_digest
    assert tuple(observed) == expected
    assert len(observed) == 100


def test_public_action_replay_api_cannot_accept_caller_observations() -> None:
    replay_parameters = signature(
        market_scan_action_gate_replay.replay_current_action_gate_receipt
    ).parameters
    inspection_parameters = signature(inspect_market_scan_action_source).parameters

    assert tuple(replay_parameters) == (
        "conn",
        "run",
        "diagnostics_without_receipt",
    )
    assert tuple(inspection_parameters) == ("conn", "run_id")
    assert (
        "replay_current_action_gate_receipt_from_verified_observations"
        not in market_scan_action_gate_replay.__all__
    )


def test_current_finish_rejects_real_score_pass_when_capture_duration_exceeds_contract(
    tmp_path: Path,
) -> None:
    repo, _settings, run_id, _skip_symbol, diagnostics = _valid_action_source_run(
        tmp_path,
        capture_duration_ms=1_300_000,
        publish=False,
    )

    with pytest.raises(ValueError, match="报价采集耗时.*1200"):
        repo.finish_run(
            run_id,
            "degraded",
            message="self-signed snapshot pass",
            publication_diagnostics=diagnostics,
        )

    rejected = repo.run(run_id)
    assert rejected.status == "running"
    assert rejected.snapshot_digest is None


def test_current_finish_rejects_caller_supplied_canonical_receipt(
    tmp_path: Path,
) -> None:
    repo, _settings, run_id, _skip_symbol, diagnostics = _valid_action_source_run(
        tmp_path,
        publish=False,
    )
    forged = diagnostics.model_copy(
        update={
            "passed_gates": [
                *diagnostics.passed_gates,
                MarketScanPublicationDiagnostic(
                    code=MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE,
                    label="伪造重放回执",
                    detail=f"market-scan-publication-replay-v1:{'a' * 64}",
                    severity="info",
                ),
            ]
        }
    )

    with pytest.raises(ValueError, match="只能由持久化边界生成"):
        repo.finish_run(
            run_id,
            "degraded",
            message="caller supplied receipt",
            publication_diagnostics=forged,
        )


def test_current_action_source_requires_repository_generated_replay_receipt(
    tmp_path: Path,
) -> None:
    _repo, settings, run_id, _skip_symbol, _diagnostics = _valid_action_source_run(
        tmp_path
    )
    with sqlite3.connect(settings.cache_path) as conn:
        conn.row_factory = sqlite3.Row
        _disable_market_scan_immutability(conn)
        raw = conn.execute(
            "SELECT publication_diagnostics_json FROM market_scan_run WHERE id = ?",
            (run_id,),
        ).fetchone()[0]
        diagnostics = json.loads(raw)
        diagnostics["passed_gates"] = [
            item
            for item in diagnostics["passed_gates"]
            if item["code"] != MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE
        ]
        conn.execute(
            "UPDATE market_scan_run SET publication_diagnostics_json = ? WHERE id = ?",
            (json.dumps(diagnostics, separators=(",", ":"), sort_keys=True), run_id),
        )
        conn.execute(
            "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
            (market_scan_snapshot_digest(conn, run_id), run_id),
        )

        inspection = inspect_market_scan_action_source(conn, run_id)
        assert inspection.snapshot_digest
        assert inspection.eligible is False
        assert inspection.reason is not None and "重放回执" in inspection.reason
        with pytest.raises(MarketScanActionSourceError, match="重放回执"):
            require_market_scan_action_source(conn, run_id)


@pytest.mark.parametrize(
    "tamper",
    ["source_warning", "blocker", "malformed_detail", "wrong_digest"],
)
def test_action_source_inspection_rejects_misplaced_or_malformed_receipt(
    tmp_path: Path,
    tamper: str,
) -> None:
    _repo, settings, run_id, _skip_symbol, _diagnostics = _valid_action_source_run(
        tmp_path
    )
    with sqlite3.connect(settings.cache_path) as conn:
        conn.row_factory = sqlite3.Row
        _disable_market_scan_immutability(conn)
        raw = conn.execute(
            "SELECT publication_diagnostics_json FROM market_scan_run WHERE id = ?",
            (run_id,),
        ).fetchone()[0]
        diagnostics = json.loads(raw)
        receipt = next(
            item
            for item in diagnostics["passed_gates"]
            if item["code"] == MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE
        )
        if tamper == "malformed_detail":
            receipt["detail"] = "market-scan-publication-replay-v1:not-a-digest"
        elif tamper == "wrong_digest":
            replacement = "0" if receipt["detail"][-1] != "0" else "1"
            receipt["detail"] = f"{receipt['detail'][:-1]}{replacement}"
        else:
            diagnostics["passed_gates"].remove(receipt)
            destination = "source_warnings" if tamper == "source_warning" else "blockers"
            receipt["severity"] = "warning" if tamper == "source_warning" else "error"
            diagnostics[destination].append(receipt)
        conn.execute(
            "UPDATE market_scan_run SET publication_diagnostics_json = ? WHERE id = ?",
            (json.dumps(diagnostics, separators=(",", ":"), sort_keys=True), run_id),
        )
        conn.execute(
            "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
            (market_scan_snapshot_digest(conn, run_id), run_id),
        )

        inspection = inspect_market_scan_action_source(conn, run_id)
        assert inspection.eligible is False
        assert inspection.reason


def test_current_action_source_requires_immutable_rule_contract_registration(
    tmp_path: Path,
) -> None:
    _repo, settings, run_id, _skip_symbol, _diagnostics = _valid_action_source_run(
        tmp_path
    )
    with sqlite3.connect(settings.cache_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DROP TRIGGER trg_market_scan_rule_contract_immutable_delete")
        conn.execute("DELETE FROM market_scan_rule_contract")

        inspection = inspect_market_scan_action_source(conn, run_id)
        assert inspection.eligible is False
        assert inspection.reason is not None and "规则合同注册" in inspection.reason
        with pytest.raises(MarketScanActionSourceError, match="规则合同注册"):
            require_market_scan_action_source(conn, run_id)


def test_current_intraday_action_claim_cannot_authorize_any_skipped_row(
    tmp_path: Path,
) -> None:
    _repo, settings, run_id, _skip_symbol, diagnostics = _valid_action_source_run(
        tmp_path,
        publish=False,
    )
    with sqlite3.connect(settings.cache_path) as conn:
        conn.row_factory = sqlite3.Row
        persisted = conn.execute(
            "SELECT * FROM market_scan_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        simulated_intraday = dict(persisted)
        simulated_intraday["mode"] = "intraday"

        with pytest.raises(ValueError, match="非盘后正式扫描不能用跳过"):
            validate_current_action_gate_claim(
                conn,
                simulated_intraday,  # type: ignore[arg-type]
                diagnostics,
            )


def test_current_run_registration_rejects_a_relaxed_distribution_policy(
    tmp_path: Path,
) -> None:
    settings, _rule_version = _settings_and_rule(
        cache_path=tmp_path / "relaxed-policy.sqlite3"
    )
    repo = SQLiteCache(settings=settings).market_scan_repo
    contract = market_scan_rule_contract(settings)
    publication = contract["publication"]
    assert isinstance(publication, dict)
    policy = publication["score_distribution"]
    assert isinstance(policy, dict)
    policy["minimum_sample_count"] = 3
    relaxed_rule = f"full-market-scan-v6:{stable_score_spec_hash(contract)}"

    with pytest.raises(ValueError, match="固定策略"):
        repo.create_run(
            trigger="manual",
            mode="official",
            rule_version=relaxed_rule,
            as_of="2026-07-17 16:30:00",
            data_date=DATA_DATE.isoformat(),
            scope=FULL_MARKET_SCOPE,
            rule_contract=contract,
        )

    assert repo.list_runs(page=1, page_size=10).total == 0
def _settings_and_rule(*, cache_path: Path | None = None) -> tuple[Settings, str]:
    settings = Settings(
        cache_path=cache_path or Path("data/test-market-scan-skip.sqlite3"),
        scheduler_enabled=False,
        market_scan_min_data_quality_score=0,
        market_scan_min_history_rows=61,
        market_scan_new_stock_days=120,
    )
    contract = market_scan_rule_contract(settings)
    return settings, f"full-market-scan-v6:{stable_score_spec_hash(contract)}"


def _case(
    reason_code: str,
    *,
    item: MarketScanResultItem | None = None,
    data_date: date = DATA_DATE,
    quote_timestamp: str | None = None,
) -> tuple[MarketScanResultItem, object, list[object]]:
    if reason_code == "new_listing_insufficient_history":
        dates, _status = trading_date_range(date(2026, 6, 10), data_date)
        result_item = item or _item(list_date="2026-06-10", is_new=True)
    else:
        dates, _status = trading_date_range(date(2026, 4, 1), data_date)
        selected = list(dates[-62:])
        selected.pop(-10)
        dates = tuple(selected)
        result_item = item or _item(list_date="2001-08-27", is_new=False)
    rows = [
        make_kline(
            date=session.isoformat(),
            close=10 + index * 0.01,
            volume=1_000 + index,
            source="provider-qfq",
            as_of=f"{session.isoformat()}T15:15:00+08:00",
            data_version=f"pit-{session.isoformat()}",
        )
        for index, session in enumerate(dates)
    ]
    price = float(rows[-1].close)
    previous = price - 0.1
    quote = make_quote(
        price=price,
        prev_close=previous,
        high=price + 0.1,
        low=price - 0.2,
        change_pct=(price - previous) / previous * 100,
        turnover_rate=1.0,
        timestamp=quote_timestamp or "2026-07-17T15:00:00+08:00",
    ).model_copy(
        update={
            "code": result_item.code,
            "market": result_item.market,
            "name": result_item.name,
            "open": price - 0.05,
            "volume": 10_000.0,
            "amount": 100_000.0,
            "change": 0.1,
        }
    )
    return result_item, quote, rows


def _success_case(item: MarketScanResultItem):
    dates, _status = trading_date_range(date(2026, 4, 1), DATA_DATE)
    selected = dates[-61:]
    rows = [
        make_kline(
            date=session.isoformat(), close=10 + index * 0.01,
            volume=1_000 + index, source="provider-qfq",
            as_of=f"{session.isoformat()} 15:15:00",
            data_version=f"pit-{session.isoformat()}",
        )
        for index, session in enumerate(selected)
    ]
    price = float(rows[-1].close)
    previous = price - 0.1
    quote = make_quote(
        price=price, prev_close=previous, high=price + 0.1, low=price - 0.2,
        change_pct=(price - previous) / previous * 100, turnover_rate=1.0,
        timestamp="2026-07-17 15:00:00",
    ).model_copy(
        update={
            "code": item.code, "market": item.market, "name": item.name,
            "open": price - 0.05, "volume": 10_000.0,
            "amount": 100_000.0, "change": 0.1,
        }
    )
    return quote, rows


def _item(*, list_date: str, is_new: bool) -> MarketScanResultItem:
    return MarketScanResultItem(
        run_id=1,
        symbol="600519.SH",
        code="600519",
        market="SH",
        name="样本",
        list_date=list_date,
        is_new=is_new,
        metadata_source="akshare",
        status="pending",
        updated_at="2026-07-17 16:30:00",
    )


def _score(
    item,
    quote,
    rows,
    *,
    settings: Settings,
    rule_version: str,
    mode: MarketScanMode = "official",
    as_of: datetime = AS_OF,
    data_date: date = DATA_DATE,
    quote_date: date = DATA_DATE,
    quote_observed_at: str = QUOTE_OBSERVED_AT,
):
    return score_market_scan_item(
        item,
        quote,
        rows,
        as_of=as_of,
        completed_cutoff=data_date,
        expected_data_date=data_date,
        expected_quote_date=quote_date,
        min_history_rows=settings.market_scan_min_history_rows,
        min_data_quality_score=settings.market_scan_min_data_quality_score,
        mode=mode,
        rule_version=rule_version,
        quote_observed_at=quote_observed_at,
        new_stock_days=settings.market_scan_new_stock_days,
    )


def _skip_result(
    item,
    quote,
    rows,
    *,
    settings: Settings,
    rule_version: str,
    mode: MarketScanMode = "official",
    as_of: datetime = AS_OF,
    data_date: date = DATA_DATE,
    quote_date: date = DATA_DATE,
    quote_observed_at: str = QUOTE_OBSERVED_AT,
):
    with pytest.raises(MarketScanSkipped) as raised:
        _score(
            item,
            quote,
            rows,
            settings=settings,
            rule_version=rule_version,
            mode=mode,
            as_of=as_of,
            data_date=data_date,
            quote_date=quote_date,
            quote_observed_at=quote_observed_at,
        )
    result = failed_scan_result_for_exception(
        item=item,
        quote=quote,
        rows=rows,
        cutoff=data_date,
        exc=raised.value,
        sensitive_values=(),
    )
    return replace(result, quote_observed_at=quote_observed_at)


def _running_repository(
    tmp_path: Path,
    *,
    mode: MarketScanMode = "official",
):
    settings, rule_version = _settings_and_rule(cache_path=tmp_path / "skip.sqlite3")
    cache = SQLiteCache(settings=settings)
    repo = cache.market_scan_repo
    contract = market_scan_rule_contract(settings)
    as_of, data_date, quote_date, _observed_at, _quote_timestamp = _mode_context(mode)
    run = repo.create_run(
        trigger="manual",
        mode=mode,
        rule_version=rule_version,
        as_of=as_of.isoformat(sep=" "),
        data_date=data_date.isoformat(),
        quote_date=quote_date.isoformat(),
        scope=FULL_MARKET_SCOPE,
        rule_contract=contract,
    )
    repo.start_run(run.id)
    repo.record_stock_pool_source(run.id, "provider-full-pool")
    repo.seed_results(
        run.id,
        [
            MarketScanSeed(
                "600519.SH",
                "600519",
                "SH",
                "样本",
                list_date="2026-06-10",
                is_new=True,
                metadata_source="akshare",
            )
        ],
        excluded_count=0,
    )
    capture_started_at, capture_finished_at = _capture_times(mode)
    repo.begin_quote_capture(run.id, capture_started_at)
    repo.seal_quote_capture(
        run.id,
        finished_at=capture_finished_at,
        decision_as_of=as_of.isoformat(sep=" "),
        duration_ms=3_000,
        count=1,
    )
    return repo, run, repo.pending_items(run.id)[0], settings, rule_version


def _mode_context(
    mode: MarketScanMode,
) -> tuple[datetime, date, date, str, str]:
    if mode == "official":
        return (
            AS_OF,
            DATA_DATE,
            DATA_DATE,
            QUOTE_OBSERVED_AT,
            "2026-07-17T15:00:00+08:00",
        )
    if mode == "intraday":
        return (
            datetime(2026, 7, 17, 10, 30),
            date(2026, 7, 16),
            date(2026, 7, 17),
            "2026-07-17 10:30:00",
            "2026-07-17T10:29:30+08:00",
        )
    return (
        datetime(2026, 7, 17, 8, 30),
        date(2026, 7, 16),
        date(2026, 7, 16),
        "2026-07-17 08:30:00",
        "2026-07-16T15:00:00+08:00",
    )


def _capture_times(mode: MarketScanMode) -> tuple[str, str]:
    if mode == "intraday":
        return "2026-07-17T02:29:59Z", "2026-07-17T02:30:02Z"
    if mode == "preopen":
        return "2026-07-17T00:29:59Z", "2026-07-17T00:30:02Z"
    return "2026-07-17T08:29:59Z", "2026-07-17T08:30:02Z"


def _quote_for_mode(quote: Quote, mode: MarketScanMode) -> Quote:
    if mode != "intraday":
        return quote
    previous = float(quote.price)
    current = round(previous * 1.03, 4)
    return quote.model_copy(
        update={
            "prev_close": previous,
            "price": current,
            "open": round(previous * 1.01, 4),
            "high": round(current * 1.01, 4),
            "low": round(previous * 0.99, 4),
            "change": round(current - previous, 4),
            "change_pct": round((current - previous) / previous * 100, 4),
        }
    )


def _valid_action_source_run(
    tmp_path: Path,
    *,
    capture_duration_ms: int = 3_000,
    publish: bool = True,
):
    settings, rule_version = _settings_and_rule(
        cache_path=tmp_path / "valid-action-source.sqlite3"
    )
    cache = SQLiteCache(settings=settings)
    repo = cache.market_scan_repo
    contract = market_scan_rule_contract(settings)
    success_seeds = [
        *(
            MarketScanSeed(
                f"{600000 + index:06d}.SH",
                f"{600000 + index:06d}",
                "SH",
                f"SH{index}",
                list_date="2000-01-01",
                metadata_source="akshare",
            )
            for index in range(34)
        ),
        *(
            MarketScanSeed(
                f"{index + 1:06d}.SZ",
                f"{index + 1:06d}",
                "SZ",
                f"SZ{index}",
                list_date="2000-01-01",
                metadata_source="akshare",
            )
            for index in range(33)
        ),
        *(
            MarketScanSeed(
                f"{920000 + index:06d}.BJ",
                f"{920000 + index:06d}",
                "BJ",
                f"BJ{index}",
                list_date="2000-01-01",
                metadata_source="akshare",
            )
            for index in range(33)
        ),
    ]
    skip_seed = MarketScanSeed(
        "688999.SH",
        "688999",
        "SH",
        "新股跳过样本",
        list_date="2026-06-10",
        is_new=True,
        metadata_source="akshare",
    )
    run = repo.create_run(
        trigger="manual",
        mode="official",
        rule_version=rule_version,
        as_of="2026-07-17 16:30:00",
        data_date=DATA_DATE.isoformat(),
        scope=FULL_MARKET_SCOPE,
        rule_contract=contract,
    )
    repo.start_run(run.id)
    repo.record_stock_pool_source(run.id, "provider-full-pool")
    repo.seed_results(run.id, [*success_seeds, skip_seed], excluded_count=0)
    repo.begin_quote_capture(run.id, "2026-07-17T08:29:59Z")
    repo.seal_quote_capture(
        run.id,
        finished_at="2026-07-17T08:30:02Z",
        decision_as_of="2026-07-17 16:30:00",
        duration_ms=capture_duration_ms,
        count=101,
    )
    pending = {item.symbol: item for item in repo.pending_items(run.id)}
    writes: list[MarketScanResultWrite] = []
    for index, seed in enumerate(success_seeds):
        quote, rows = _varying_success_case(pending[seed.symbol], index=index)
        write = _score(
            pending[seed.symbol],
            quote,
            rows,
            settings=settings,
            rule_version=rule_version,
        )
        writes.append(replace(write, quote_observed_at=QUOTE_OBSERVED_AT))
    _unused, skip_quote, skip_rows = _case(
        "new_listing_insufficient_history",
        item=pending[skip_seed.symbol],
    )
    writes.append(
        _skip_result(
            pending[skip_seed.symbol],
            skip_quote,
            skip_rows,
            settings=settings,
            rule_version=rule_version,
        )
    )
    repo.save_result_batch(run.id, writes)
    policy = MarketScanScoreDistributionPolicy()
    distribution = MarketScanScoreDistribution.from_score_observations(
        repo.success_score_observations(run.id),
        expected_count=100,
        policy=policy,
    )
    assert policy.assess(distribution).status == "pass"
    diagnostics = action_pass_publication_diagnostics()
    diagnostics = diagnostics.model_copy(
        update={
            "passed_gates": [
                diagnostics.passed_gates[0].model_copy(
                    update={
                        "detail": distribution.audit_text().removeprefix(
                            "评分分布门禁 "
                        )
                    }
                )
            ]
        }
    )
    if publish:
        published = repo.finish_run(
            run.id,
            "degraded",
            message="100 success plus one justified skip",
            publication_diagnostics=diagnostics,
        )
        assert published.success_count == 100
        assert published.skipped_count == 1
    return repo, settings, run.id, skip_seed.symbol, diagnostics


def _varying_success_case(
    item: MarketScanResultItem,
    *,
    index: int | float,
) -> tuple[Quote, list[Kline]]:
    dates, _status = trading_date_range(date(2026, 4, 1), DATA_DATE)
    selected = dates[-61:]
    final_close = 10.5
    first_close = final_close * (1 - (index - 50) / 500)
    step = (final_close - first_close) / (len(selected) - 1)
    rows = [
        make_kline(
            date=session.isoformat(),
            close=first_close + position * step,
            volume=1_000_000 + position * 20_000,
            source="provider-qfq",
            as_of=f"{session.isoformat()}T15:15:00+08:00",
            data_version=f"pit-{session.isoformat()}",
        )
        for position, session in enumerate(selected)
    ]
    quote = make_quote(
        price=final_close,
        prev_close=10.0,
        high=10.8,
        low=9.9,
        change_pct=5.0,
        turnover_rate=4.5,
        timestamp="2026-07-17T15:00:00+08:00",
    ).model_copy(
        update={
            "code": item.code,
            "market": item.market,
            "name": item.name,
            "open": 10.1,
            "amount": 900_000_000.0,
            "change": 0.5,
        }
    )
    return quote, rows
