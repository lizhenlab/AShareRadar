from __future__ import annotations

import json

import pytest

from app.models.market_scan import MarketScanResultItem, MarketScanSeed
from app.models.strategy_execution import StrategyExecutionRequest
from app.models.strategy_lab import (
    StrategyEvidencePolicy,
    StrategyHardFilter,
    StrategyPortfolioConstraints,
    StrategyRebalancePolicy,
    StrategySpecCreate,
    StrategySpecInput,
    StrategySpecUpdate,
)
from app.services.cache import SQLiteCache
from app.services.market_scan_scoring import score_market_scan_item
from app.services.strategy_execution import StrategyExecutionService
from app.services.strategy_portfolio import strategy_board
from tests.market_scan_test_support import SCAN_AS_OF, SCAN_DATA_DATE, _daily_rows, _quote_for


def test_strategy_execution_uses_frozen_dimensions_preserves_rank_and_paginates(tmp_path) -> None:
    cache, service, strategy_id, run_id = _environment(tmp_path)

    draft = service.execute(
        StrategyExecutionRequest(
            strategy_id=strategy_id,
            kind="latest_scan",
            notional_cash_cny=1_000_000.0,
        )
    )

    assert draft.context.strategy_id == strategy_id
    assert draft.context.strategy_version == 1
    assert draft.context.market_scan_run_id == run_id
    assert draft.context.rule_version == "full-market-score-v4-test"
    assert draft.context.data_date == SCAN_DATA_DATE.isoformat()
    assert draft.context.point_in_time is True
    assert len(draft.context.strategy_fingerprint) == 64
    assert len(draft.context.execution_fingerprint) == 64
    assert len(draft.context.cost_rule_fingerprint) == 64
    assert draft.summary.status == "ready"
    assert draft.summary.selected_count == 2
    assert draft.summary.estimated_round_trip_cost_cny > 0
    assert all(item.evidence_verified for item in draft.selected)
    assert all(item.original_rank is not None for item in draft.selected)
    assert all(item.utility_rank is not None for item in draft.selected)
    assert all("生产原始排名" in item.rank_change_reason for item in draft.selected)
    assert any(item.pareto_front for item in draft.candidate_preview)
    assert draft.candidate_total == 4

    first_page = service.candidates(
        draft.context.execution_id,
        page=1,
        page_size=2,
        status=None,
    )
    second_page = service.candidates(
        draft.context.execution_id,
        page=2,
        page_size=2,
        status=None,
    )
    assert first_page.total == 4
    assert first_page.page_count == 2
    assert len(first_page.items) == len(second_page.items) == 2
    assert {item.symbol for item in first_page.items}.isdisjoint(
        item.symbol for item in second_page.items
    )
    assert cache.strategy_execution_service is service


def test_historical_replay_uses_exact_published_date_and_same_strategy_fingerprint(tmp_path) -> None:
    _cache, service, strategy_id, run_id = _environment(tmp_path)
    latest = service.execute(
        StrategyExecutionRequest(strategy_id=strategy_id, kind="latest_scan")
    )
    replay = service.execute(
        StrategyExecutionRequest(
            strategy_id=strategy_id,
            kind="historical_replay",
            run_id=run_id,
            mode="official",
        )
    )

    assert replay.context.kind == "historical_replay"
    assert replay.context.strategy_fingerprint == latest.context.strategy_fingerprint
    assert replay.context.data_as_of == latest.context.data_as_of
    assert replay.result_digest != latest.result_digest
    assert service.executions(strategy_id=strategy_id, page=1, page_size=20).total == 2

    with pytest.raises(ValueError, match="模式不匹配"):
        service.execute(
            StrategyExecutionRequest(
                strategy_id=strategy_id,
                kind="historical_replay",
                run_id=run_id,
                mode="intraday",
            )
        )


def test_execution_fingerprint_uses_resolved_semantics_not_optional_selector_spelling(tmp_path) -> None:
    _cache, service, strategy_id, run_id = _environment(tmp_path)
    by_run = service.execute(
        StrategyExecutionRequest(
            strategy_id=strategy_id,
            revision=1,
            kind="historical_replay",
            run_id=run_id,
        )
    )
    by_date = service.execute(
        StrategyExecutionRequest(
            strategy_id=strategy_id,
            kind="historical_replay",
            data_date=by_run.context.data_date,
        )
    )

    assert by_run.context.execution_fingerprint == by_date.context.execution_fingerprint
    assert by_run.result_digest == by_date.result_digest


def test_portfolio_draft_returns_no_trade_when_constraints_make_every_order_unfillable(tmp_path) -> None:
    cache, service, strategy_id, _run_id = _environment(tmp_path)
    current = cache.strategy_lab_service.get(strategy_id)
    impossible = current.spec.model_copy(
        update={
            "portfolio_constraints": StrategyPortfolioConstraints(
                    stock_count=2,
                    max_stock_weight=0.5,
                    max_industry_positions=2,
                    max_industry_weight=1.0,
                    max_board_weight=1.0,
                    min_position_amount_cny=100_000.0,
                max_notional_share_of_daily_amount=0.000001,
            )
        }
    )
    cache.strategy_lab_service.update(
        strategy_id,
        StrategySpecUpdate(spec=impossible, expected_revision=1, confirmed=True),
    )

    draft = service.execute(
        StrategyExecutionRequest(
            strategy_id=strategy_id,
            revision=2,
            kind="latest_scan",
            notional_cash_cny=10_000.0,
        )
    )

    assert draft.summary.status == "no_trade"
    assert draft.summary.no_trade is True
    assert draft.summary.selected_count == 0
    assert draft.summary.unfilled_count > 0
    assert any("没有候选" in reason for reason in draft.summary.no_trade_reasons)


def test_hard_filter_failures_explain_minimum_change_without_mutating_original_rank(tmp_path) -> None:
    cache, service, strategy_id, _run_id = _environment(tmp_path)
    current = cache.strategy_lab_service.get(strategy_id)
    filtered = current.spec.model_copy(
        update={
            "hard_filters": [
                StrategyHardFilter(field="amount", operator="gte", value=10_000_000_000.0)
            ]
        }
    )
    updated = cache.strategy_lab_service.update(
        strategy_id,
        StrategySpecUpdate(spec=filtered, expected_revision=1, confirmed=True),
    )
    draft = service.execute(
        StrategyExecutionRequest(strategy_id=strategy_id, revision=updated.revision)
    )

    assert draft.summary.no_trade is True
    candidate = draft.candidate_preview[0]
    assert candidate.original_rank is not None
    assert candidate.utility_rank is None
    assert any("成交额" in failure for failure in candidate.hard_filter_failures)
    assert any("amount 至少提高" in change for change in candidate.minimum_changes)
    assert candidate.rank_change_reason.startswith("生产原始排名")


def test_custom_and_risk_adjusted_weighting_are_executed_not_only_serialized(tmp_path) -> None:
    cache, service, strategy_id, _run_id = _environment(tmp_path)
    current = cache.strategy_lab_service.get(strategy_id)
    custom = current.spec.model_copy(
        update={
            "portfolio_constraints": StrategyPortfolioConstraints(
                stock_count=2,
                weighting_method="custom",
                max_stock_weight=0.5,
                max_industry_positions=2,
                max_industry_weight=1.0,
                max_board_weight=1.0,
                custom_weights={"600001.SH": 0.2, "688001.SH": 0.3},
            )
        }
    )
    custom_version = cache.strategy_lab_service.update(
        strategy_id,
        StrategySpecUpdate(spec=custom, expected_revision=1, confirmed=True),
    )
    custom_draft = service.execute(
        StrategyExecutionRequest(strategy_id=strategy_id, revision=custom_version.revision)
    )
    custom_weights = {item.symbol: item.target_weight for item in custom_draft.selected}

    assert set(custom_weights) == {"600001.SH", "688001.SH"}
    assert custom_weights["600001.SH"] == pytest.approx(0.2, abs=0.001)
    assert custom_weights["688001.SH"] == pytest.approx(0.3, abs=0.001)
    assert any("自定义权重未包含" in reason for item in custom_draft.candidate_preview for reason in item.reasons)

    with cache._connect() as conn:  # noqa: SLF001 - test-only frozen snapshot mutation
        for symbol, risk in (("600001.SH", 10.0), ("688001.SH", 20.0)):
            row = conn.execute(
                "SELECT metrics_json FROM market_scan_result WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            payload = json.loads(str(row["metrics_json"]))
            payload["score_details"]["components"]["score_dimensions"]["scores"]["risk"] = risk
            conn.execute(
                "UPDATE market_scan_result SET metrics_json = ? WHERE symbol = ?",
                (json.dumps(payload, ensure_ascii=False), symbol),
            )
    risk_spec = custom_version.spec.model_copy(
        update={
            "portfolio_constraints": StrategyPortfolioConstraints(
                stock_count=2,
                weighting_method="risk_adjusted",
                max_stock_weight=0.8,
                max_industry_positions=2,
                max_industry_weight=1.0,
                max_board_weight=1.0,
            )
        }
    )
    risk_version = cache.strategy_lab_service.update(
        strategy_id,
        StrategySpecUpdate(spec=risk_spec, expected_revision=2, confirmed=True),
    )
    risk_draft = service.execute(
        StrategyExecutionRequest(strategy_id=strategy_id, revision=risk_version.revision)
    )
    risk_weights = {item.symbol: item.target_weight for item in risk_draft.selected}

    assert risk_weights["600001.SH"] > risk_weights["688001.SH"]
    assert any("risk_adjusted" in note for note in risk_draft.summary.notes)


def test_hysteresis_and_source_whitelist_are_deterministic_admission_gates(tmp_path) -> None:
    cache, service, strategy_id, _run_id = _environment(tmp_path)
    current = cache.strategy_lab_service.get(strategy_id)
    hysteresis = current.spec.model_copy(
        update={
            "rebalance_policy": StrategyRebalancePolicy(
                hold_sessions=5,
                rebalance_every_sessions=5,
                buy_utility_threshold=80,
                hold_utility_threshold=70,
            )
        }
    )
    version = cache.strategy_lab_service.update(
        strategy_id,
        StrategySpecUpdate(spec=hysteresis, expected_revision=1, confirmed=True),
    )
    draft = service.execute(
        StrategyExecutionRequest(
            strategy_id=strategy_id,
            revision=version.revision,
            current_weights={"600001.SH": 0.5},
        )
    )

    assert [item.symbol for item in draft.selected] == ["600001.SH"]
    assert any("持有阈值" in reason for reason in draft.selected[0].reasons)
    assert any(
        "新买入阈值" in failure
        for item in draft.candidate_preview
        if item.symbol != "600001.SH"
        for failure in item.hard_filter_failures
    )

    frozen = service.repository.frozen_scan(run_id=None, data_date=None, mode="official")
    allowed_sources = sorted(
        {
            str(source)
            for item in frozen.items
            for source in (item.quote_source, item.kline_source, item.metadata_source)
            if source
        }
    )
    with cache._connect() as conn:  # noqa: SLF001 - test-only missing provenance case
        conn.execute(
            "UPDATE market_scan_result SET metadata_source = NULL WHERE symbol = '600001.SH'",
        )
    source_spec = version.spec.model_copy(
        update={
            "rebalance_policy": StrategyRebalancePolicy(),
            "evidence_policy": StrategyEvidencePolicy(allowed_sources=allowed_sources),
        }
    )
    source_version = cache.strategy_lab_service.update(
        strategy_id,
        StrategySpecUpdate(spec=source_spec, expected_revision=2, confirmed=True),
    )
    source_draft = service.execute(
        StrategyExecutionRequest(strategy_id=strategy_id, revision=source_version.revision)
    )
    candidate = next(item for item in source_draft.candidate_preview if item.symbol == "600001.SH")

    assert any("缺少：元数据" in failure for failure in candidate.hard_filter_failures)


@pytest.mark.parametrize(
    ("code", "market", "expected"),
    [
        ("600001", "SH", "sh_main"),
        ("688001", "SH", "star"),
        ("000001", "SZ", "sz_main"),
        ("300001", "SZ", "chinext"),
        ("920001", "BJ", "beijing"),
    ],
)
def test_strategy_board_mapping_is_explicit(code: str, market: str, expected: str) -> None:
    assert strategy_board(code, market) == expected


def _environment(tmp_path) -> tuple[SQLiteCache, StrategyExecutionService, int, int]:
    cache = SQLiteCache(tmp_path / "strategy-execution.sqlite3")
    run_id = _seed_scan(cache)
    strategy = cache.strategy_lab_service.create(
        StrategySpecCreate(
            spec=StrategySpecInput(
                name="执行测试策略",
                portfolio_constraints=StrategyPortfolioConstraints(
                    stock_count=2,
                    max_stock_weight=0.5,
                    max_industry_positions=1,
                    max_industry_weight=0.6,
                    max_board_weight=0.6,
                ),
            ),
            confirmed=True,
        )
    )
    service = cache.strategy_execution_service
    return cache, service, strategy.strategy_id, run_id


def _seed_scan(cache: SQLiteCache) -> int:
    rows = [
        ("600001", "SH", "沪市样本", "银行", "20000101", 0.8),
        ("688001", "SH", "科创样本", "半导体", "20200101", 1.4),
        ("300001", "SZ", "创业样本", "医疗器械", "20150101", 2.0),
        ("920001", "BJ", "北交样本", "工业", "20220101", 2.6),
    ]
    run = cache.create_market_scan_run(
        trigger="manual",
        mode="official",
        rule_version="full-market-score-v4-test",
        as_of=SCAN_AS_OF.strftime("%Y-%m-%d %H:%M:%S"),
        data_date=SCAN_DATA_DATE.isoformat(),
        quote_date=SCAN_DATA_DATE.isoformat(),
        scope="all-a-share",
    )
    cache.start_market_scan_run(run.id)
    seeds = [
        MarketScanSeed(
            symbol=f"{code}.{market}",
            code=code,
            market=market,
            name=name,
            industry=industry,
            list_date=list_date,
            metadata_source="测试元数据",
        )
        for code, market, name, industry, list_date, _change in rows
    ]
    cache.seed_market_scan_results(run.id, seeds, excluded_count=0)
    results = []
    for code, market, name, industry, list_date, change in rows:
        quote = _quote_for(code, market, name, change_pct=change)
        klines = _daily_rows(SCAN_DATA_DATE, 80, last_close=quote.price)
        item = MarketScanResultItem(
            run_id=run.id,
            symbol=f"{code}.{market}",
            code=code,
            market=market,
            name=name,
            industry=industry,
            list_date=list_date,
            is_st=False,
            is_new=False,
            metadata_source="测试元数据",
            status="pending",
            updated_at="2026-07-17T08:30:00Z",
        )
        results.append(
            score_market_scan_item(
                item,
                quote,
                klines,
                as_of=SCAN_AS_OF,
                completed_cutoff=SCAN_DATA_DATE,
                expected_data_date=SCAN_DATA_DATE,
                min_history_rows=60,
                min_data_quality_score=0,
                mode="official",
                rule_version="full-market-score-v4-test",
            )
        )
    cache.save_market_scan_result_batch(run.id, results)
    finished = cache.finish_market_scan_run(run.id, "success", message="测试冻结扫描完成")
    assert finished.status == "success"
    return run.id
