from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.paper_trading import PaperTradingAccountUpdate
from app.models.reviews import (
    AdviceEvidenceRef,
    AdviceReviewEvaluationDraft,
    AdviceReviewPlanInput,
    AdviceReviewPlanUpdate,
    structured_advice_evidence_refs,
)
from app.services.advice_review import (
    create_advice_review_plan,
    evaluate_due_advice_reviews,
    get_advice_review_detail,
    get_advice_review_summary,
    list_advice_review_details,
    list_advice_review_plans,
    list_due_advice_reviews,
    update_advice_review_plan,
)
from app.services.cache import SQLiteCache
from app.services.paper_trading import simulate_paper_portfolio
from app.services.research_replay import evaluate_advice_forward_window
from app.utils.errors import NotFoundError
from tests.test_advice_reviews import _insert_advice, _plan_input, make_kline
from tests.test_paper_trading import (
    _persist_review_plan_projection,
    _review_plan,
    _strategy_create,
)


def test_review_service_crud_lists_and_repository_pagination(tmp_path: Path) -> None:
    path = tmp_path / "review-service.db"
    cache = SQLiteCache(path)
    advice_id = _insert_advice(path)

    created = create_advice_review_plan(cache, _plan_input(advice_id))

    assert list_advice_review_plans(cache, symbol="600519", limit=1) == [created]
    assert list_advice_review_plans(cache, symbol="600519", limit=1, offset=1) == []
    assert cache.advice_review_plans(limit=0) == []
    assert get_advice_review_detail(cache, created.id).plan == created
    assert list_advice_review_details(cache, symbol="600519", limit=1)[0].plan == created
    assert list_advice_review_details(cache, symbol="600519", limit=1, offset=1) == []
    assert get_advice_review_summary(cache).total_plan_count == 1

    updated = update_advice_review_plan(
        cache,
        created.id,
        AdviceReviewPlanUpdate(
            expected_revision=created.revision,
            hypothesis="  价格站稳后   继续上行  ",
        ),
    )

    assert updated.revision == created.revision + 1
    assert updated.hypothesis == "价格站稳后 继续上行"
    assert cache.advice_review_plan(created.id) == updated
    with pytest.raises(NotFoundError, match="研究计划不存在"):
        update_advice_review_plan(
            cache,
            999_999,
            AdviceReviewPlanUpdate(expected_revision=1, hypothesis="不存在"),
        )
    with pytest.raises(NotFoundError, match="研究计划不存在"):
        get_advice_review_detail(cache, 999_999)


@pytest.mark.parametrize("limit", [0, True, 1.5])
def test_review_due_services_reject_non_positive_or_non_integer_limits(limit: object) -> None:
    datahub = SimpleNamespace()

    with pytest.raises(ValueError, match="上限必须是正整数"):
        asyncio.run(evaluate_due_advice_reviews(datahub, limit=limit))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="上限必须是正整数"):
        asyncio.run(list_due_advice_reviews(datahub, limit=limit))  # type: ignore[arg-type]


def test_review_plan_and_evidence_models_enforce_boundary_contracts() -> None:
    baseline = _plan_input(1).model_dump()
    invalid_cases = (
        ({"hypothesis": "   \t"}, "内容不能为空"),
        ({"target_price": 95, "stop_price": 95}, "目标价必须高于止损价"),
        ({"evidence_refs": ["   "]}, "证据引用不能为空"),
        ({"evidence_refs": ["x" * 241]}, "不能超过240"),
    )
    for changes, message in invalid_cases:
        with pytest.raises(ValidationError, match=message):
            AdviceReviewPlanInput.model_validate({**baseline, **changes})

    deduplicated = AdviceReviewPlanInput.model_validate(
        {**baseline, "evidence_refs": ["同一证据", "同一证据"]}
    )
    assert deduplicated.evidence_refs == ["同一证据"]

    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        AdviceEvidenceRef(
            id="score",
            value=1,
            direction="neutral",
            data_date="2026-W18-5",
            nature="observed",
            rule_version="v1",
        )
    with pytest.raises(ValidationError, match="有效值"):
        AdviceEvidenceRef(
            id="score",
            value=float("nan"),
            direction="neutral",
            data_date="2026-05-01",
            nature="observed",
            rule_version="v1",
        )


def test_structured_evidence_degrades_invalid_provenance_and_scores_safely() -> None:
    invalid = SimpleNamespace(
        anchor_date="not-a-date",
        market_time="also-invalid",
        rule_version="rule-v1",
        trend_score=70,
    )
    assert structured_advice_evidence_refs(invalid) == []

    fallback = SimpleNamespace(
        anchor_date="not-a-date",
        market_time="2026-05-10 10:00:00",
        rule_version="rule-v1",
        action="等待",
        price=100,
        trend_score=True,
        risk_level="中等",
        support=None,
        resistance=None,
        data_quality_score="not-a-score",
    )
    refs = {item.id: item for item in structured_advice_evidence_refs(fallback)}

    assert refs["trend_score"].direction == "neutral"
    assert refs["data_quality_score"].direction == "neutral"
    assert all(item.data_date == "2026-05-10" for item in refs.values())

    unsupported = SimpleNamespace(
        anchor_date="2026-05-10",
        rule_version="rule-v1",
        trend_score=object(),
    )
    with pytest.raises(ValidationError, match="value"):
        structured_advice_evidence_refs(unsupported)


def _evaluated_review_draft(path: Path) -> AdviceReviewEvaluationDraft:
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(
        _plan_input(_insert_advice(path)).model_copy(update={"horizon_days": 1})
    )
    return evaluate_advice_forward_window(
        plan,
        [
            make_kline(date="2026-05-08", close=100, high=101, low=99),
            make_kline(date="2026-05-11", close=102, high=103, low=99),
        ],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )


def test_review_evaluation_model_rejects_time_hit_and_window_contradictions(
    tmp_path: Path,
) -> None:
    draft = _evaluated_review_draft(tmp_path / "evaluation-contract.db")
    baseline = draft.model_dump()
    cases = (
        ({"snapshot_market_time": "2026-05-10"}, "必须包含日期和时间"),
        ({"as_of": "2026-05-09 16:00:00"}, "不能早于"),
        ({"as_of": "2026-05-11 16:02:00"}, "不能晚于"),
        ({"rule_version": "   "}, "rule_version 不能为空"),
        ({"target_hit": True}, "target_hit 与 target_hit_date"),
        ({"stop_hit": True}, "stop_hit 与 stop_hit_date"),
        ({"conclusion": "target_hit"}, "target_hit 结论"),
        ({"conclusion": "stop_hit"}, "stop_hit 结论"),
        ({"conclusion": "target_stop_ambiguous"}, "歧义结论"),
        (
            {
                "target_hit": True,
                "target_hit_date": "2026-05-11",
                "conclusion": "horizon_gain",
            },
            "周期结论",
        ),
        ({"conclusion": "horizon_gain", "return_pct": 0}, "价格变化方向"),
        ({"conclusion": "horizon_loss", "return_pct": 0}, "价格变化方向"),
        ({"conclusion": "horizon_flat", "return_pct": 1}, "价格变化方向"),
        ({"visible_end_date": "2026-05-11"}, "可见窗口不能晚于"),
        (
            {"forward_start_date": "2026-05-08", "forward_end_date": "2026-05-08"},
            "前向窗口必须晚于",
        ),
        ({"visible_bar_count": 0}, "空可见窗口不能包含日期"),
        (
            {
                "target_hit": True,
                "target_hit_date": "2026-05-12",
                "conclusion": "target_hit",
            },
            "target_hit_date 必须位于前向窗口",
        ),
        ({"source_session_count": 0}, "不能超过来源会话数"),
    )

    for changes, message in cases:
        with pytest.raises(ValidationError, match=message):
            AdviceReviewEvaluationDraft.model_validate({**baseline, **changes})


def test_paper_account_empty_dashboard_and_pending_strategy_lifecycle(tmp_path: Path) -> None:
    path = tmp_path / "paper-lifecycle.db"
    cache = SQLiteCache(path)

    empty = cache.paper_trading_dashboard()
    assert empty.selected_run_id is None
    assert empty.strategies == empty.trades == empty.events == empty.equity_curve == []
    assert empty.performance.total_equity == empty.account.initial_cash

    with pytest.raises(ValidationError, match="至少需要修改"):
        PaperTradingAccountUpdate()
    updated = cache.update_paper_trading_account(
        PaperTradingAccountUpdate(initial_cash=1_500_000, default_cost_profile="stress")
    )
    assert updated.initial_cash == 1_500_000
    assert updated.default_cost_profile == "stress"
    assert cache.paper_trading_account() == updated

    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    payload = _strategy_create(plan, allocation_pct=10)
    strategy = cache.create_paper_strategy(
        plan,
        payload,
        activation_market_time="2026-07-01 10:00:00",
    )
    with pytest.raises(ValueError, match="已加入模拟交易"):
        cache.create_paper_strategy(
            plan,
            payload,
            activation_market_time="2026-07-01 10:00:00",
        )
    with pytest.raises(NotFoundError, match="模拟策略不存在"):
        cache.delete_pending_paper_strategy(999_999)

    assert cache.delete_pending_paper_strategy(strategy.id) is True
    assert cache.paper_strategies() == []


def test_paper_simulation_save_detects_strategy_set_changed_after_calculation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper-cas.db"
    cache = SQLiteCache(path)
    draft = simulate_paper_portfolio(
        cache.paper_trading_account(),
        [],
        {},
        as_of=datetime(2026, 7, 3, 16),
    )
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    cache.create_paper_strategy(
        plan,
        _strategy_create(plan, allocation_pct=10),
        activation_market_time="2026-07-01 10:00:00",
    )

    with pytest.raises(RuntimeError, match="计算期间已变化"):
        cache.save_paper_simulation(draft)

    assert cache.paper_trading_runs() == []
    assert len(cache.paper_strategies()) == 1


def test_paper_strategy_creation_and_reads_fail_closed_on_ledger_corruption(
    tmp_path: Path,
) -> None:
    projection_path = tmp_path / "paper-projection-corrupt.db"
    projection_cache = SQLiteCache(projection_path)
    plan = _review_plan()
    _persist_review_plan_projection(projection_path, plan)
    with sqlite3.connect(projection_path) as conn:
        conn.execute(
            "UPDATE advice_review_plan SET evidence_refs_json = '{}' WHERE id = ?",
            (plan.id,),
        )
    with pytest.raises(ValueError, match="修订账本损坏"):
        projection_cache.create_paper_strategy(
            plan,
            _strategy_create(plan, allocation_pct=10),
            activation_market_time="2026-07-01 10:00:00",
        )
    assert projection_cache.paper_strategies() == []

    missing_path = tmp_path / "paper-ledger-missing.db"
    missing_cache = SQLiteCache(missing_path)
    _persist_review_plan_projection(missing_path, plan)
    missing_cache.create_paper_strategy(
        plan,
        _strategy_create(plan, allocation_pct=10),
        activation_market_time="2026-07-01 10:00:00",
    )
    with sqlite3.connect(missing_path) as conn:
        conn.execute(
            "DELETE FROM advice_review_plan_revision WHERE plan_id = ? AND revision = ?",
            (plan.id, plan.revision),
        )
    with pytest.raises(ValueError, match="缺少冻结计划修订账本"):
        missing_cache.paper_strategies()

    corrupt_path = tmp_path / "paper-ledger-json-corrupt.db"
    corrupt_cache = SQLiteCache(corrupt_path)
    _persist_review_plan_projection(corrupt_path, plan)
    corrupt_cache.create_paper_strategy(
        plan,
        _strategy_create(plan, allocation_pct=10),
        activation_market_time="2026-07-01 10:00:00",
    )
    with sqlite3.connect(corrupt_path) as conn:
        conn.execute(
            "UPDATE advice_review_plan_revision SET payload_json = '{bad' "
            "WHERE plan_id = ? AND revision = ?",
            (plan.id, plan.revision),
        )
    with pytest.raises(ValueError, match="冻结计划修订账本损坏"):
        corrupt_cache.paper_strategies()
