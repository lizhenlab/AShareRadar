from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

from app.config import Settings
from app.models.market_scan import (
    MarketScanCoverage,
    MarketScanPublicationSummary,
    MarketScanResultItem,
    MarketScanResultWrite,
    MarketScanRun,
    MarketScanScoreDistribution,
    MarketScanSeed,
)
from app.models.schemas import Kline
from app.services.cache import SQLiteCache
from app.services.market_scan_completion import (
    MARKET_SCAN_SCORE_DISTRIBUTION_POLICY,
    MarketScanFinalizer,
    assess_market_scan_score_distribution,
    completion_status,
    publication_blockers,
)
from app.services.market_scan_contracts import (
    MarketScanCacheProtocol,
    MarketScanDataHubProtocol,
    MarketScanSettingsProtocol,
)
from app.services.market_scan_execution import MarketScanExecutor
from app.services.market_scan_manager import market_scan_rule_version
from app.services import market_scan_manager
from app.services.market_scan_scoring import (
    MarketScanReplayError,
    market_scan_score_spec,
    rank_score_details,
    replay_score_details,
    score_market_scan_item,
    stable_score_spec_hash,
    verify_score_details,
)
from tests.factories import make_kline, make_quote


AS_OF = datetime(2026, 7, 17, 16, 30)
DATA_DATE = date(2026, 7, 17)


def test_score_spec_hash_is_canonical_and_covers_every_ranking_contract_dimension() -> None:
    spec = market_scan_score_spec(min_data_quality_score=50)
    reordered = {key: spec[key] for key in reversed(spec)}

    assert stable_score_spec_hash(spec) == stable_score_spec_hash(reordered)
    assert len(stable_score_spec_hash(spec)) == 64

    mutations = []
    changed_penalty = deepcopy(spec)
    changed_penalty["final_score"]["quality_penalty_per_missing_point"] = 0.14
    mutations.append(changed_penalty)
    changed_refinement = deepcopy(spec)
    changed_refinement["ranking"]["refinement"]["weights"]["ma_alignment"] = 0.39
    mutations.append(changed_refinement)
    changed_profile = deepcopy(spec)
    changed_profile["leader_profile"]["profile_id"] = "full-market-trend-only-v-next"
    mutations.append(changed_profile)
    changed_algorithm = deepcopy(spec)
    changed_algorithm["algorithms"]["trend_score"] = "trend-score-v-next"
    mutations.append(changed_algorithm)
    changed_rounding = deepcopy(spec)
    changed_rounding["rounding"]["mode"] = "half-up"
    mutations.append(changed_rounding)
    changed_tie_break = deepcopy(spec)
    changed_tie_break["ranking"]["tie_break"][-1] = ["symbol", "desc"]
    mutations.append(changed_tie_break)

    original_hash = stable_score_spec_hash(spec)
    assert all(stable_score_spec_hash(candidate) != original_hash for candidate in mutations)


def test_rule_version_is_an_opaque_stable_hash_not_a_partial_config_string(tmp_path: Path) -> None:
    settings = Settings(cache_path=tmp_path / "rule.sqlite3", scheduler_enabled=False)

    first = market_scan_rule_version(settings)
    second = market_scan_rule_version(settings)

    assert first == second
    prefix, digest = first.split(":", 1)
    assert prefix == "full-market-scan-v5"
    assert len(digest) == 64
    assert "kline_limit=" not in first


def test_rule_version_hash_covers_score_distribution_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(cache_path=tmp_path / "rule-distribution.sqlite3", scheduler_enabled=False)
    baseline = market_scan_rule_version(settings)
    changed = replace(
        MARKET_SCAN_SCORE_DISTRIBUTION_POLICY,
        degraded_top100_tie_ratio_at_least=0.45,
    )

    monkeypatch.setattr(market_scan_manager, "MARKET_SCAN_SCORE_DISTRIBUTION_POLICY", changed)

    assert market_scan_manager.market_scan_rule_version(settings) != baseline


def test_scoring_replay_details_survive_sqlite_round_trip(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    run = cache.create_market_scan_run(
        trigger="manual",
        rule_version=market_scan_rule_version(cache.settings),
        as_of="2026-07-17 16:30:00",
        data_date=DATA_DATE.isoformat(),
        scope="test",
    )
    cache.start_market_scan_run(run.id)
    cache.seed_market_scan_results(
        run.id,
        [
            MarketScanSeed(
                "600001.SH",
                "600001",
                "SH",
                "沪市样本",
                industry="测试行业",
                list_date="2000-01-01",
            )
        ],
        excluded_count=0,
    )
    pending = cache.pending_market_scan_items(run.id)[0]
    result = score_market_scan_item(
        pending,
        _quote(),
        _rows(DATA_DATE),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        rule_version=run.rule_version,
    )

    cache.save_market_scan_result_batch(run.id, [result])
    restored = _results(cache, run.id).items[0]

    assert restored.metrics == result.metrics
    assert restored.score_details == result.score_details
    assert restored.score_details["run_rule_version"] == run.rule_version
    assert restored.score_details["score_spec_hash"] == stable_score_spec_hash(
        restored.score_details["score_spec"]
    )
    assert restored.score_details["inputs"]["amount"] == pytest.approx(800_000_000)
    assert restored.score_details["components"]["leader_score"]["rule_deltas"] == {}
    assert restored.score_details["components"]["rank_refinement"]["score"] <= 1
    assert restored.score_details["components"]["final_score"]["rounded"] == restored.score
    assert restored.score_details["ranking"]["tie_break_values"]["symbol"] == "600001.SH"


def test_persisted_score_details_replay_scores_and_sqlite_rank_exactly(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    run = cache.create_market_scan_run(
        trigger="manual",
        rule_version=market_scan_rule_version(cache.settings),
        as_of="2026-07-17 16:30:00",
        data_date=DATA_DATE.isoformat(),
        scope="test",
    )
    cache.start_market_scan_run(run.id)
    cache.seed_market_scan_results(
        run.id,
        [
            MarketScanSeed(
                "600001.SH", "600001", "SH", "沪市样本", industry="测试行业", list_date="2000-01-01"
            ),
            MarketScanSeed(
                "000001.SZ", "000001", "SZ", "深市样本", industry="测试行业", list_date="2000-01-01"
            ),
            MarketScanSeed(
                "920001.BJ", "920001", "BJ", "北交样本", industry="测试行业", list_date="2000-01-01"
            ),
        ],
        excluded_count=0,
    )
    changes = {"600001.SH": 5.2, "000001.SZ": 2.4, "920001.BJ": -1.0}
    writes = []
    for item in cache.pending_market_scan_items(run.id):
        quote = _quote().model_copy(
            update={
                "code": item.code,
                "market": item.market,
                "name": item.name,
                "change_pct": changes[item.symbol],
            }
        )
        writes.append(
            score_market_scan_item(
                item,
                quote,
                _rows(DATA_DATE),
                as_of=AS_OF,
                completed_cutoff=DATA_DATE,
                expected_data_date=DATA_DATE,
                min_history_rows=60,
                min_data_quality_score=0,
                rule_version=run.rule_version,
            )
        )
    cache.save_market_scan_result_batch(run.id, writes)
    cache.finish_market_scan_run(run.id, "success", message="测试重放")
    restored = _results(cache, run.id).items

    for item in restored:
        replay = replay_score_details(item.score_details)
        assert replay.leader_score == item.leader_score
        assert replay.final_score == item.score
        assert verify_score_details(
            item.score_details,
            expected_leader_score=item.leader_score,
            expected_final_score=item.score,
        ) == replay

    replayed_ranks = rank_score_details(
        [(item.symbol, item.score_details) for item in restored]
    )
    assert replayed_ranks == {item.symbol: item.rank for item in restored}


def test_score_replay_rejects_corruption_and_unknown_schemas_or_algorithms(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    run = cache.create_market_scan_run(
        trigger="manual",
        rule_version=market_scan_rule_version(cache.settings),
        as_of="2026-07-17 16:30:00",
        data_date=DATA_DATE.isoformat(),
        scope="test",
    )
    cache.start_market_scan_run(run.id)
    cache.seed_market_scan_results(
        run.id,
        [MarketScanSeed("600001.SH", "600001", "SH", "沪市样本", list_date="2000-01-01")],
        excluded_count=0,
    )
    details = score_market_scan_item(
        cache.pending_market_scan_items(run.id)[0],
        _quote(),
        _rows(DATA_DATE),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        rule_version=run.rule_version,
    ).score_details

    unknown_schema = deepcopy(details)
    unknown_schema["schema_version"] = 999
    with pytest.raises(MarketScanReplayError, match="未知.*schema"):
        replay_score_details(unknown_schema)

    corrupted = deepcopy(details)
    corrupted["inputs"]["amount"] = float("nan")
    with pytest.raises(MarketScanReplayError, match="损坏|有限"):
        replay_score_details(corrupted)

    unknown_algorithm = deepcopy(details)
    unknown_algorithm["score_spec"]["algorithms"]["final_score"] = "unknown-final-v9"
    unknown_algorithm["score_spec_hash"] = stable_score_spec_hash(unknown_algorithm["score_spec"])
    with pytest.raises(MarketScanReplayError, match="未知.*算法"):
        replay_score_details(unknown_algorithm)

    with pytest.raises(MarketScanReplayError, match="不一致"):
        verify_score_details(
            details,
            expected_leader_score=0,
            expected_final_score=0,
        )


def test_publication_requires_global_and_sh_sz_bj_coverage() -> None:
    summary = MarketScanPublicationSummary(
        coverages=(
            MarketScanCoverage("ALL", total_count=100, success_count=96),
            MarketScanCoverage("SH", total_count=40, success_count=40),
            MarketScanCoverage("SZ", total_count=55, success_count=55),
            MarketScanCoverage("BJ", total_count=5, success_count=1),
        )
    )

    blockers = publication_blockers(summary)

    assert len(blockers) == 1
    assert "BJ" in blockers[0]
    assert "1/5" in blockers[0]


def test_systemic_same_day_lag_blocks_publication_without_reclassifying_individual_rows(
    tmp_path: Path,
) -> None:
    cache = _cache(tmp_path)
    run = _seed_running_run(cache)
    stale_date = "2026-07-16"
    writes = [
        _successful_write("600001.SH"),
        _successful_write("000001.SZ"),
        _successful_write("920001.BJ"),
        MarketScanResultWrite(
            symbol="600002.SH",
            status="missing",
            error="当日报价存在有效成交，但日K同日滞后",
            data_date=stale_date,
        ),
        MarketScanResultWrite(
            symbol="000002.SZ",
            status="skipped",
            reason="日K同日滞后，可能停牌",
            data_date=stale_date,
        ),
        MarketScanResultWrite(
            symbol="920002.BJ",
            status="skipped",
            reason="日K同日滞后，可能停牌",
            data_date=stale_date,
        ),
    ]
    cache.save_market_scan_result_batch(run.id, writes)
    current = cache.market_scan_run(run.id)

    summary = cache.market_scan_repo.publication_summary(run.id)
    status, message = completion_status(current, publication_summary=summary)
    by_symbol = {item.symbol: item.status for item in _results(cache, run.id).items}

    assert summary.systemic_stale_cluster is not None
    assert summary.systemic_stale_cluster.data_date == stale_date
    assert summary.systemic_stale_cluster.count == 3
    assert summary.systemic_stale_cluster.markets == ("BJ", "SH", "SZ")
    assert status == "failed"
    assert "系统性同日滞后" in message
    assert by_symbol["600002.SH"] == "missing"
    assert by_symbol["000002.SZ"] == "skipped"


def test_quote_snapshot_span_blocks_when_one_market_exceeds_the_limit(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    run = _seed_running_run(cache)
    cache.save_market_scan_result_batch(
        run.id,
        [
            _successful_write("600001.SH", quote_timestamp="2026-07-17 15:00:00"),
            _successful_write("600002.SH", quote_timestamp="2026-07-17 15:16:00"),
            _successful_write("000001.SZ", quote_timestamp="2026-07-17 15:08:00"),
            _successful_write("000002.SZ", quote_timestamp="2026-07-17 15:08:30"),
            _successful_write("920001.BJ", quote_timestamp="2026-07-17 15:09:00"),
            _successful_write("920002.BJ", quote_timestamp="2026-07-17 15:09:20"),
        ],
    )
    current = cache.market_scan_run(run.id)

    summary = cache.market_scan_repo.publication_summary(run.id)
    status, message = completion_status(current, publication_summary=summary)

    assert summary.snapshot_span_seconds == 16 * 60
    assert status == "failed"
    assert "快照跨度" in message


def test_quote_snapshot_span_blocks_cross_market_time_mixing(
    tmp_path: Path,
) -> None:
    cache = _cache(tmp_path)
    run = _seed_running_run(cache)
    cache.save_market_scan_result_batch(
        run.id,
        [
            _successful_write("600001.SH", quote_timestamp="2026-07-17 16:15:00"),
            _successful_write("600002.SH", quote_timestamp="2026-07-17 16:15:45"),
            _successful_write("000001.SZ", quote_timestamp="2026-07-17 16:15:10"),
            _successful_write("000002.SZ", quote_timestamp="2026-07-17 16:15:59"),
            _successful_write("920001.BJ", quote_timestamp="2026-07-17 15:35:00"),
            _successful_write("920002.BJ", quote_timestamp="2026-07-17 15:35:50"),
        ],
    )

    summary = cache.market_scan_repo.publication_summary(run.id)
    status, message = completion_status(
        cache.market_scan_run(run.id),
        publication_summary=summary,
    )

    assert summary.snapshot_started_at == "2026-07-17 15:35:00"
    assert summary.snapshot_finished_at == "2026-07-17 16:15:59"
    assert summary.snapshot_span_seconds == 40 * 60 + 59
    assert publication_blockers(summary)
    assert status == "failed"
    assert "快照跨度" in message


def test_snapshot_span_normalizes_equivalent_aware_and_naive_market_times(
    tmp_path: Path,
) -> None:
    cache = _cache(tmp_path)
    run = _seed_running_run(cache)
    cache.save_market_scan_result_batch(
        run.id,
        [
            _successful_write("600001.SH", quote_timestamp="2026-07-17 15:00:00"),
            _successful_write("600002.SH", quote_timestamp="2026-07-17T15:00:00+08:00"),
            _successful_write("000001.SZ", quote_timestamp="2026-07-17 15:05:00"),
            _successful_write("000002.SZ", quote_timestamp="2026-07-17T07:05:00Z"),
            _successful_write("920001.BJ", quote_timestamp="2026-07-17 15:10:00"),
            _successful_write("920002.BJ", quote_timestamp="2026-07-17T15:10:00+08:00"),
        ],
    )

    summary = cache.market_scan_repo.publication_summary(run.id)

    assert summary.snapshot_span_seconds == 10 * 60
    assert summary.invalid_snapshot_timestamps == ()
    assert publication_blockers(summary) == ()


def test_unparseable_snapshot_timestamp_is_a_publication_blocker(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    run = _seed_running_run(cache, one_per_market=True)
    cache.save_market_scan_result_batch(
        run.id,
        [
            _successful_write("600001.SH"),
            _successful_write("000001.SZ"),
            _successful_write("920001.BJ", quote_timestamp="not-a-market-time"),
        ],
    )

    summary = cache.market_scan_repo.publication_summary(run.id)
    status, message = completion_status(
        cache.market_scan_run(run.id),
        publication_summary=summary,
    )

    assert summary.invalid_snapshot_timestamps == ("not-a-market-time",)
    assert status == "failed"
    assert "报价时间不可解析" in message


def test_history_skips_are_diagnostic_and_do_not_reduce_publishable_coverage(
    tmp_path: Path,
) -> None:
    cache = _cache(tmp_path)
    run = cache.create_market_scan_run(
        trigger="manual",
        rule_version=market_scan_rule_version(cache.settings),
        as_of="2026-07-17 16:30:00",
        data_date=DATA_DATE.isoformat(),
        scope="test",
    )
    cache.start_market_scan_run(run.id)
    bj_symbols = [f"{920000 + index:06d}.BJ" for index in range(330)]
    cache.seed_market_scan_results(
        run.id,
        [
            MarketScanSeed("600001.SH", "600001", "SH", "沪市样本"),
            MarketScanSeed("000001.SZ", "000001", "SZ", "深市样本"),
            *[MarketScanSeed(symbol, symbol[:6], "BJ", f"北交样本{index}") for index, symbol in enumerate(bj_symbols)],
        ],
        excluded_count=0,
    )
    cache.save_market_scan_result_batch(
        run.id,
        [
            _successful_write("600001.SH"),
            _successful_write("000001.SZ"),
            *[_successful_write(symbol) for symbol in bj_symbols[:309]],
            *[
                MarketScanResultWrite(
                    symbol=symbol,
                    status="skipped",
                    reason="历史数据不足 60 根，暂不参与排名",
                )
                for symbol in bj_symbols[309:]
            ],
        ],
    )
    current = cache.market_scan_run(run.id)

    summary = cache.market_scan_repo.publication_summary(run.id)
    bj = summary.coverage_for("BJ")
    status, message = completion_status(current, publication_summary=summary)

    assert bj is not None
    assert (bj.total_count, bj.success_count, bj.missing_count, bj.skipped_count) == (
        309,
        309,
        0,
        21,
    )
    assert bj.coverage_ratio == 1
    assert publication_blockers(summary) == ()
    assert status == "degraded"
    assert "跳过 21" in message
    assert cache.finish_market_scan_run(run.id, status, message=message).status == "degraded"


def test_excessive_market_skips_cannot_hide_coverage_failure(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    run = _seed_running_run(cache)
    cache.save_market_scan_result_batch(
        run.id,
        [
            _successful_write("600001.SH"),
            _successful_write("600002.SH"),
            _successful_write("000001.SZ"),
            _successful_write("000002.SZ"),
            _successful_write("920001.BJ"),
            MarketScanResultWrite(
                symbol="920002.BJ",
                status="skipped",
                reason="历史数据不足 60 根，暂不参与排名",
            ),
        ],
    )

    summary = cache.market_scan_repo.publication_summary(run.id)
    bj = summary.coverage_for("BJ")
    status, message = completion_status(
        cache.market_scan_run(run.id),
        publication_summary=summary,
    )

    assert bj is not None
    assert bj.coverage_ratio == 1
    assert bj.eligible_ratio == 0.5
    assert status == "failed"
    assert "BJ 有效样本占比不足" in message


def test_pending_rows_cannot_be_published_when_eligible_coverage_is_complete(
    tmp_path: Path,
) -> None:
    cache = _cache(tmp_path)
    run = _seed_running_run(cache)
    cache.save_market_scan_result_batch(
        run.id,
        [
            _successful_write("600001.SH"),
            _successful_write("600002.SH"),
            _successful_write("000001.SZ"),
            _successful_write("000002.SZ"),
            _successful_write("920001.BJ"),
        ],
    )
    current = cache.market_scan_run(run.id)
    summary = cache.market_scan_repo.publication_summary(run.id)

    status, message = completion_status(current, publication_summary=summary)

    assert publication_blockers(summary) == ()
    assert status == "failed"
    assert "尚有 1 只待处理" in message


def test_market_scan_uses_minimal_runtime_checkable_protocols(tmp_path: Path) -> None:
    cache = _cache(tmp_path)

    async def stock_pool(**_kwargs):
        return []

    async def partial_quotes_with_errors(_symbols, use_cache=True):
        del use_cache
        return [], ()

    async def kline(_symbol, **_kwargs):
        return []

    hub = SimpleNamespace(
        settings=cache.settings,
        cache=cache,
        stock_pool=stock_pool,
        partial_quotes_with_errors=partial_quotes_with_errors,
        kline=kline,
    )

    assert isinstance(cache.settings, MarketScanSettingsProtocol)
    assert isinstance(cache, MarketScanCacheProtocol)
    assert isinstance(hub, MarketScanDataHubProtocol)
    assert MarketScanExecutor(hub).cache is cache  # type: ignore[arg-type]


def test_market_scan_high_risk_modules_are_in_the_explicit_mypy_scope() -> None:
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    configured = set(config["tool"]["mypy"]["files"])

    assert {
        "app/services/datahub_klines.py",
        "app/services/leader_scoring.py",
        "app/services/market_scan_contracts.py",
        "app/services/market_scan_replay.py",
        "app/services/market_scan_validation.py",
    } <= configured


def test_runtime_guard_rejects_trading_date_drift_before_processing_pending_rows() -> None:
    current = datetime(2026, 7, 20, 16, 30)
    executor = _guard_executor(now=lambda: current)
    executor._load_or_seed_pending = _return_one_pending  # type: ignore[method-assign]
    executor._process_pending = _unexpected_process_pending  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="运行期间完整交易日.*2026-07-20"):
        asyncio.run(executor.execute(_run(), asyncio.Event()))


def test_full_scan_wall_clock_budget_stops_without_fabricating_missing_results() -> None:
    executor = _guard_executor(wall_clock_budget_seconds=0.01)
    executor._load_or_seed_pending = _return_one_pending  # type: ignore[method-assign]
    executor._process_pending = _slow_process_pending  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="墙钟预算"):
        asyncio.run(executor.execute(_run(), asyncio.Event()))


def _cache(tmp_path: Path) -> SQLiteCache:
    settings = Settings(cache_path=tmp_path / "market-scan-trust.sqlite3", scheduler_enabled=False)
    return SQLiteCache(settings=settings)


def _seed_running_run(cache: SQLiteCache, *, one_per_market: bool = False) -> MarketScanRun:
    run = cache.create_market_scan_run(
        trigger="manual",
        rule_version=market_scan_rule_version(cache.settings),
        as_of="2026-07-17 16:30:00",
        data_date=DATA_DATE.isoformat(),
        scope="test",
    )
    cache.start_market_scan_run(run.id)
    seeds = [
        MarketScanSeed("600001.SH", "600001", "SH", "沪市一号"),
        MarketScanSeed("000001.SZ", "000001", "SZ", "深市一号"),
        MarketScanSeed("920001.BJ", "920001", "BJ", "北交一号"),
    ]
    if not one_per_market:
        seeds.extend(
            (
                MarketScanSeed("600002.SH", "600002", "SH", "沪市二号"),
                MarketScanSeed("000002.SZ", "000002", "SZ", "深市二号"),
                MarketScanSeed("920002.BJ", "920002", "BJ", "北交二号"),
            )
        )
    cache.seed_market_scan_results(run.id, seeds, excluded_count=0)
    return cache.market_scan_run(run.id)


def _successful_write(
    symbol: str,
    *,
    quote_timestamp: str = "2026-07-17 15:00:00",
) -> MarketScanResultWrite:
    return MarketScanResultWrite(
        symbol=symbol,
        status="success",
        score=80,
        trend_score=75,
        leader_score=78,
        data_quality_score=90,
        price=10.0,
        change_pct=1.0,
        turnover_rate=4.0,
        volume_ratio=1.2,
        amount=800_000_000,
        metrics={"ma20": 9.5},
        reason="测试评分",
        data_date=DATA_DATE.isoformat(),
        quote_timestamp=quote_timestamp,
        quote_source="test",
        kline_source="test",
        adjustment_mode="qfq",
    )


def _results(cache: SQLiteCache, run_id: int):
    return cache.market_scan_results(
        run_id,
        page=1,
        page_size=100,
        status=None,
        market=None,
        industry=None,
        is_st=None,
        is_new=None,
        min_data_quality_score=None,
        keyword=None,
        sort="rank",
        order="asc",
    )


def _quote():
    return make_quote(
        price=10.3,
        prev_close=10.0,
        high=10.5,
        low=9.9,
        change_pct=3.0,
        turnover_rate=4.2,
        timestamp="2026-07-17 15:00:00",
    ).model_copy(
        update={
            "code": "600001",
            "market": "SH",
            "name": "沪市样本",
            "amount": 800_000_000,
        }
    )


def _rows(latest: date, count: int = 80) -> list[Kline]:
    days: list[date] = []
    cursor = latest
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    first_close = 10.3 - (count - 1) * 0.03
    return [
        make_kline(
            date=day.isoformat(),
            close=first_close + index * 0.03,
            volume=1_000_000 + index * 10_000,
            source="测试前复权日K",
            as_of=latest.isoformat(),
            data_version=f"test|qfq|{latest.isoformat()}",
        )
        for index, day in enumerate(days)
    ]


def _run() -> MarketScanRun:
    return MarketScanRun(
        id=1,
        status="running",
        trigger="manual",
        rule_version="test",
        as_of="2026-07-17 16:30:00",
        data_date="2026-07-17",
        scope="test",
        total_count=1,
        excluded_count=0,
        processed_count=0,
        success_count=0,
        missing_count=0,
        skipped_count=0,
        retry_count=0,
        progress_pct=0,
        coverage_pct=0,
        created_at="2026-07-17 16:30:00",
        updated_at="2026-07-17 16:30:00",
    )


def _guard_executor(
    *,
    now=lambda: AS_OF,
    wall_clock_budget_seconds: float = 60,
) -> MarketScanExecutor:
    hub = SimpleNamespace(
        cache=SimpleNamespace(),
        settings=SimpleNamespace(
            market_scan_wall_clock_budget_seconds=wall_clock_budget_seconds,
        ),
    )
    return MarketScanExecutor(hub, now=now)  # type: ignore[arg-type]


async def _return_one_pending(*_args) -> list[MarketScanResultItem]:
    return [
        MarketScanResultItem(
            run_id=1,
            symbol="600001.SH",
            code="600001",
            market="SH",
            name="测试",
            status="pending",
            updated_at="2026-07-17 16:30:00",
        )
    ]


async def _unexpected_process_pending(*_args) -> tuple[str, ...]:
    raise AssertionError("trading-date drift must stop before row processing")


async def _slow_process_pending(*_args) -> tuple[str, ...]:
    await asyncio.sleep(0.1)
    return ()


def test_constant_raw_scores_fail_publication_with_auditable_metrics() -> None:
    raw_scores = [52.5] * 200
    cache = _CompletionCache(raw_scores)

    persisted = asyncio.run(
        MarketScanFinalizer(cache).finish_completed(
            _distribution_run(len(raw_scores)),
            degraded_count=0,
            warnings=(),
        )
    )

    assert persisted is True
    assert cache.finished is not None
    status, message, error = cache.finished
    assert status == "failed"
    assert "raw_score 全部相同" in message
    assert MARKET_SCAN_SCORE_DISTRIBUTION_POLICY.version in message
    assert "distinct ratio 0.50%" in message
    assert "最大并列组 200/200（100.00%）" in message
    assert error is not None and "raw_score 全部相同" in error


def test_top_100_fully_saturated_at_100_fails_publication() -> None:
    raw_scores = [100.0] * 100 + [99.0 - index * 0.1 for index in range(200)]
    distribution = _score_distribution(raw_scores)

    assessment = assess_market_scan_score_distribution(distribution)
    status, message = completion_status(
        _distribution_run(len(raw_scores)),
        score_distribution=distribution,
    )

    assert assessment.status == "failed"
    assert assessment.reasons == ("前100名 raw_score 全部饱和在 100",)
    assert distribution.saturation_ratio == 1 / 3
    assert distribution.top100_tie_ratio == 1
    assert status == "failed"
    assert "前100名 raw_score 全部饱和在 100" in message


def test_large_top_tie_degrades_instead_of_failing_publication() -> None:
    raw_scores = [80.0] * 60 + [79.0 - index * 0.1 for index in range(140)]
    distribution = _score_distribution(raw_scores)

    assessment = assess_market_scan_score_distribution(distribution)
    status, message = completion_status(
        _distribution_run(len(raw_scores)),
        score_distribution=distribution,
    )

    assert assessment.status == "degraded"
    assert distribution.top100_tie_ratio == 0.6
    assert status == "degraded"
    assert "前100并列占比达到 60.00%" in message


def test_normal_raw_score_distribution_passes_without_false_positive() -> None:
    raw_scores = [20.0 + index * 0.1 for index in range(500)]
    distribution = _score_distribution(raw_scores)

    assessment = assess_market_scan_score_distribution(distribution)
    status, message = completion_status(
        _distribution_run(len(raw_scores)),
        score_distribution=distribution,
    )

    assert assessment.status == "pass"
    assert distribution.distinct_raw_score_ratio == 1
    assert distribution.max_tie_group_ratio == 1 / 500
    assert distribution.saturation_ratio == 0
    assert distribution.top100_tie_ratio == 0
    assert status == "success"
    assert MARKET_SCAN_SCORE_DISTRIBUTION_POLICY.version in message


def test_many_small_top_ties_are_measured_as_all_tied_rows() -> None:
    raw_scores = [100 - index // 2 * 0.001 for index in range(100)] + [80 - index * 0.01 for index in range(100)]
    distribution = _score_distribution(raw_scores)

    assessment = assess_market_scan_score_distribution(distribution)

    assert distribution.top100_max_tie_group_count == 2
    assert distribution.top100_tied_count == 100
    assert distribution.top100_tie_ratio == 1
    assert assessment.status == "degraded"


class _CompletionCache:
    def __init__(self, raw_scores: list[float]) -> None:
        self.raw_scores = raw_scores
        self.finished: tuple[str, str, str | None] | None = None

    def market_scan_results(self, run_id: int, **kwargs: object) -> SimpleNamespace:
        assert run_id == 1
        assert kwargs["status"] == "success"
        assert kwargs["sort"] == "raw_score"
        return SimpleNamespace(items=[SimpleNamespace(raw_score=value) for value in self.raw_scores])

    def finish_market_scan_run(
        self,
        run_id: int,
        status: str,
        *,
        message: str,
        error: str | None = None,
    ) -> None:
        assert run_id == 1
        self.finished = (status, message, error)


def _distribution_run(count: int) -> MarketScanRun:
    return MarketScanRun(
        id=1,
        status="running",
        trigger="manual",
        rule_version="test-rule-v1",
        as_of="2026-07-29 16:30:00",
        data_date="2026-07-29",
        scope="test",
        total_count=count,
        excluded_count=0,
        processed_count=count,
        success_count=count,
        missing_count=0,
        skipped_count=0,
        retry_count=0,
        progress_pct=100,
        coverage_pct=100,
        created_at="2026-07-29 16:30:00",
        updated_at="2026-07-29 16:31:00",
    )


def _score_distribution(raw_scores: list[float]) -> MarketScanScoreDistribution:
    return MarketScanScoreDistribution.from_raw_scores(
        raw_scores,
        expected_count=len(raw_scores),
        policy=MARKET_SCAN_SCORE_DISTRIBUTION_POLICY,
    )
