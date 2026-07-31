from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_datahub
from app.api.routes import reviews
from app.config import Settings
from app.models.advice_change import CONCLUSION_BASIS, MODEL_VERSION, SNAPSHOT_CONTRACT_VERSION
from app.models.analysis import DataQuality, KlineQuality
from app.models.market import DAILY_KLINE_CONTRACT_VERSION
from app.models.reviews import (
    AdviceEvidenceRef,
    AdviceReviewDetail,
    AdviceReviewPlan,
    AdviceReviewPlanInput,
    AdviceReviewSummary,
)
from app.models.rule_versions import RULE_VERSION
from app.services import advice_review
from app.services.advice_review import (
    build_advice_evidence_refs,
    evaluate_due_advice_reviews,
    list_due_advice_reviews,
)
from app.services.analysis import build_analysis
from app.services.cache import SQLiteCache
from app.services.research_replay import evaluate_advice_forward_window
from app.services.scheduler_contracts import _TASK_DEFINITIONS
from app.workflows import individual
from app.workflows.individual import refresh_active_research_queue
from tests.factories import make_kline, make_quote


AFTER_CLOSE = datetime(2026, 7, 17, 16, 0, 0)


def test_active_research_refresh_is_bounded_excludes_symbols_and_isolates_failures(monkeypatch) -> None:
    cache = _ResearchQueueCache(
        active_symbols=("600001.SH", "600002.SH", "600003.SH"),
        excluded_symbols=("600099.SH",),
    )
    hub = SimpleNamespace(cache=cache)
    analyses = {
        "600001.SH": _valid_analysis("600001"),
        "600002.SH": RuntimeError("provider failed"),
        "600003.SH": _valid_analysis("600003"),
    }
    calls: list[str] = []

    async def analyze(_datahub, symbol: str, *, persist_history: bool):
        assert persist_history is False
        calls.append(symbol)
        result = analyses[symbol]
        if isinstance(result, Exception):
            raise result
        return result

    async def llm_must_not_run(*_args, **_kwargs):
        raise AssertionError("active research refresh must not call an LLM")

    monkeypatch.setattr(individual, "analyze_individual_stock", analyze)
    monkeypatch.setattr(individual, "enhance_stock_answer", llm_must_not_run)
    monkeypatch.setattr(individual, "is_trading_day", lambda _value: True)

    summary = asyncio.run(refresh_active_research_queue(hub, now=AFTER_CLOSE, limit=2))

    assert calls == ["600001.SH", "600002.SH"]
    assert cache.saved_symbols == ["600001.SH"]
    assert summary.selected_count == 2
    assert summary.saved_count == 1
    assert summary.failed_count == 1
    assert summary.skipped_count == 0
    assert [item.status for item in summary.items] == ["saved", "failed"]
    assert "600099.SH" not in calls
    assert "600003.SH" not in calls


def test_active_research_refresh_rejects_stale_low_quality_and_invalid_contract(monkeypatch) -> None:
    cache = _ResearchQueueCache(active_symbols=("600001.SH", "600002.SH", "600003.SH"))
    hub = SimpleNamespace(cache=cache)
    stale = _valid_analysis("600001").model_copy(
        update={
            "quote": _valid_analysis("600001").quote.model_copy(update={"timestamp": "2026-07-16 15:00:00"}),
        }
    )
    low_quality = _valid_analysis("600002")
    low_quality = low_quality.model_copy(update={"data_quality": low_quality.data_quality.model_copy(update={"score": 49})})
    invalid_contract = _valid_analysis("600003")
    invalid_contract = invalid_contract.model_copy(
        update={
            "klines": [
                invalid_contract.klines[-1].model_copy(update={"contract_version": "unknown"}),
            ]
        }
    )
    analyses = {
        "600001.SH": stale,
        "600002.SH": low_quality,
        "600003.SH": invalid_contract,
    }

    async def analyze(_datahub, symbol: str, *, persist_history: bool):
        assert persist_history is False
        return analyses[symbol]

    monkeypatch.setattr(individual, "analyze_individual_stock", analyze)
    monkeypatch.setattr(individual, "is_trading_day", lambda _value: True)

    summary = asyncio.run(refresh_active_research_queue(hub, now=AFTER_CLOSE, limit=3))

    assert cache.saved_symbols == []
    assert summary.saved_count == 0
    assert summary.skipped_count == 3
    assert [item.reason_code for item in summary.items] == [
        "stale_data_date",
        "low_data_quality",
        "invalid_rule_contract",
    ]


def test_active_research_refresh_runs_only_after_daily_bar_publication(monkeypatch) -> None:
    cache = _ResearchQueueCache(active_symbols=("600001.SH",))
    hub = SimpleNamespace(cache=cache)
    calls = 0

    async def analyze(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _valid_analysis("600001")

    monkeypatch.setattr(individual, "analyze_individual_stock", analyze)
    monkeypatch.setattr(individual, "is_trading_day", lambda _value: True)

    summary = asyncio.run(
        refresh_active_research_queue(
            hub,
            now=datetime(2026, 7, 17, 15, 10),
            limit=5,
        )
    )

    assert calls == 0
    assert summary.selected_count == 0
    assert summary.deferred is True
    assert summary.reason_code == "not_after_close"


def test_active_research_refresh_rotates_candidates_across_two_bounded_rounds(monkeypatch) -> None:
    cache = _ResearchQueueCache(
        active_symbols=("600001.SH", "600002.SH", "600003.SH", "600004.SH"),
        excluded_symbols=("600099.SH",),
    )
    hub = SimpleNamespace(cache=cache)
    calls: list[str] = []

    async def analyze(_datahub, symbol: str, *, persist_history: bool):
        assert persist_history is False
        calls.append(symbol)
        if symbol == "600001.SH":
            raise RuntimeError("isolated symbol failure")
        return _valid_analysis(symbol[:6])

    monkeypatch.setattr(individual, "analyze_individual_stock", analyze)
    monkeypatch.setattr(individual, "is_trading_day", lambda _value: True)

    first = asyncio.run(refresh_active_research_queue(hub, now=AFTER_CLOSE, limit=2))
    second = asyncio.run(refresh_active_research_queue(hub, now=AFTER_CLOSE, limit=2))

    assert calls == ["600001.SH", "600002.SH", "600003.SH", "600004.SH"]
    assert cache.saved_symbols == ["600002.SH", "600003.SH", "600004.SH"]
    assert first.selected_count == second.selected_count == 2
    assert first.failed_count == 1
    assert first.saved_count == 1
    assert second.saved_count == 2
    assert "600099.SH" not in calls


def test_active_research_same_day_revision_is_not_mistaken_for_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = SQLiteCache(
        settings=Settings(
            cache_path=tmp_path / "cache.sqlite3",
            advice_history_dedupe_seconds=3600,
        )
    )
    original = _valid_analysis("600519")
    cache.save_watchlist_item(original.quote)
    same_conclusion_revision = original.model_copy(
        update={
            "klines": [
                original.klines[-1].model_copy(update={"data_version": "test-daily-kline-qfq-v2"})
            ]
        }
    )
    changed_conclusion = same_conclusion_revision.model_copy(
        update={
            "action_advice": same_conclusion_revision.action_advice.model_copy(
                update={"confidence": same_conclusion_revision.action_advice.confidence + 1}
            )
        }
    )
    analyses = iter((original, original, same_conclusion_revision, changed_conclusion))

    async def analyze(_datahub, _symbol: str, *, persist_history: bool):
        assert persist_history is False
        return next(analyses)

    monkeypatch.setattr(individual, "analyze_individual_stock", analyze)
    monkeypatch.setattr(individual, "is_trading_day", lambda _value: True)
    hub = SimpleNamespace(cache=cache)

    results = [
        asyncio.run(refresh_active_research_queue(hub, now=AFTER_CLOSE, limit=1))
        for _ in range(4)
    ]

    assert [result.items[0].status for result in results] == [
        "saved",
        "unchanged",
        "saved",
        "saved",
    ]
    timeline = cache.advice_history("600519.SH", limit=10)
    assert len(timeline) == 3
    assert timeline[0].kline_data_version == "test-daily-kline-qfq-v2"
    watchlist = cache.watchlist()[0]
    assert watchlist.unread_change_count == 1


def test_plan_created_from_advice_snapshot_has_structured_evidence_refs(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    settings = Settings(cache_path=path, advice_history_dedupe_seconds=0)
    cache = SQLiteCache(settings=settings)
    advice = cache.save_advice_snapshot(_valid_analysis("600519"))

    plan = cache.create_advice_review_plan(
        AdviceReviewPlanInput(
            advice_id=advice.id,
            symbol="600519.SH",
            hypothesis="trend continues",
            trigger_condition="price reaches 105",
            invalidation_condition="price falls to 95",
            target_price=105,
            stop_price=95,
            horizon_days=3,
        )
    )

    assert plan.evidence_refs
    assert all(isinstance(item, AdviceEvidenceRef) for item in plan.evidence_refs)
    assert {item.id for item in plan.evidence_refs} >= {
        "action",
        "price",
        "trend_score",
        "risk_level",
        "support",
        "resistance",
        "data_quality_score",
    }
    assert all(item.data_date == "2026-07-17" for item in plan.evidence_refs)
    assert all(item.rule_version == "rules.v2" for item in plan.evidence_refs)
    assert all(
        set(item.model_dump()) == {"id", "value", "direction", "data_date", "nature", "rule_version"}
        for item in plan.evidence_refs
    )

    raw_refs = build_advice_evidence_refs(cache.advice_timeline("600519.SH", limit=1)[0])
    assert raw_refs == plan.evidence_refs


def test_plan_custom_evidence_refs_are_merged_with_snapshot_evidence(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    analysis = _valid_analysis("600519")
    advice = cache.save_advice_snapshot(analysis)

    plan = cache.create_advice_review_plan(
        _plan_input(advice.id, "600519.SH").model_copy(
            update={"evidence_refs": ["manual: reviewed earnings call"]}
        )
    )

    structured = [item for item in plan.evidence_refs if isinstance(item, AdviceEvidenceRef)]
    assert {item.id for item in structured} >= {"action", "price"}
    assert all(item.rule_version == RULE_VERSION for item in structured)
    assert next(item for item in structured if item.id == "action").value == analysis.action_advice.action
    assert plan.evidence_refs[-1] == "manual: reviewed earnings call"


@pytest.mark.parametrize(
    ("risk_level", "expected_direction"),
    [
        ("低风险", "positive"),
        ("中等风险", "neutral"),
        ("高风险", "negative"),
    ],
)
def test_structured_risk_evidence_direction(
    risk_level: str,
    expected_direction: str,
) -> None:
    snapshot = SimpleNamespace(
        rule_version=RULE_VERSION,
        anchor_date="2026-07-17",
        action="持有",
        price=100,
        trend_score=50,
        risk_level=risk_level,
        support=95,
        resistance=105,
        data_quality_score=90,
    )

    risk = next(item for item in build_advice_evidence_refs(snapshot) if item.id == "risk_level")

    assert risk.direction == expected_direction


def test_forward_evaluation_exposes_trigger_and_invalidation_evidence_with_excursions() -> None:
    plan = _review_plan(horizon_days=3)
    rows = [
        make_kline(date="2026-07-16", close=100, high=101, low=99),
        make_kline(date="2026-07-17", close=104, high=106, low=98),
    ]

    draft = evaluate_advice_forward_window(
        plan,
        rows,
        as_of=AFTER_CLOSE,
        evaluated_at="2026-07-17T08:01:00.000000Z",
    )

    assert draft.trigger_evidence.met is True
    assert draft.trigger_evidence.price == pytest.approx(105)
    assert draft.trigger_evidence.data_date == "2026-07-17"
    assert draft.trigger_evidence.rule_version == draft.rule_version
    assert draft.trigger_evidence.basis == "daily_high_gte_target_price"
    assert draft.invalidation_evidence.met is False
    assert draft.invalidation_evidence.price == pytest.approx(95)
    assert draft.invalidation_evidence.basis == "daily_low_lte_stop_price"
    assert draft.max_favorable_excursion_pct == pytest.approx(6)
    assert draft.max_adverse_excursion_pct == pytest.approx(-2)


def test_review_price_evidence_does_not_claim_mismatched_free_text_condition() -> None:
    plan = _review_plan(horizon_days=1).model_copy(
        update={
            "trigger_condition": "董事会正式批准回购方案",
            "invalidation_condition": "监管机构撤销业务许可",
        }
    )

    draft = evaluate_advice_forward_window(
        plan,
        [
            make_kline(date="2026-07-16", close=100, high=101, low=99),
            make_kline(date="2026-07-17", close=104, high=106, low=98),
        ],
        as_of=AFTER_CLOSE,
        evaluated_at="2026-07-17T08:01:00.000000Z",
    )

    assert plan.trigger_basis == "daily_high_gte_target_price"
    assert plan.invalidation_basis == "daily_low_lte_stop_price"
    assert draft.trigger_evidence.met is True
    assert draft.trigger_evidence.basis == plan.trigger_basis
    assert draft.invalidation_evidence.basis == plan.invalidation_basis
    assert "董事会" not in str(draft.trigger_evidence.model_dump())
    assert "监管" not in str(draft.invalidation_evidence.model_dump())


def test_due_review_batch_is_bounded_and_isolates_plan_failures(monkeypatch) -> None:
    details = [
        AdviceReviewDetail(plan=_review_plan(plan_id=1, advice_id=11, symbol="600001.SH", horizon_days=1)),
        AdviceReviewDetail(plan=_review_plan(plan_id=2, advice_id=12, symbol="600002.SH", horizon_days=1)),
        AdviceReviewDetail(plan=_review_plan(plan_id=3, advice_id=13, symbol="600003.SH", horizon_days=1)),
    ]
    cache = _DueReviewCache(details)
    hub = SimpleNamespace(cache=cache)
    calls: list[int] = []

    async def evaluate(_datahub, plan_id: int, *, as_of, now=None):
        calls.append(plan_id)
        if plan_id == 2:
            raise RuntimeError("one plan failed")
        return SimpleNamespace(id=101, status="evaluated", conclusion="horizon_gain")

    monkeypatch.setattr(advice_review, "evaluate_advice_review_plan", evaluate)
    monkeypatch.setattr(advice_review, "is_trading_day", lambda _value: True)

    summary = asyncio.run(evaluate_due_advice_reviews(hub, as_of=AFTER_CLOSE, limit=2))

    assert calls == [1, 2]
    assert summary.attempted_count == 2
    assert summary.evaluated_count == 1
    assert summary.failed_count == 1
    assert [item.plan_id for item in summary.items] == [1, 2]


def test_due_review_list_exposes_due_date_and_overdue_trading_days() -> None:
    detail = AdviceReviewDetail(
        plan=_review_plan(plan_id=1, advice_id=11, symbol="600001.SH", horizon_days=1)
    )
    cache = _DueReviewCache([detail], expected_as_of_date="2026-07-21")

    items = asyncio.run(
        list_due_advice_reviews(
            SimpleNamespace(cache=cache),
            as_of=datetime(2026, 7, 21, 16),
            limit=10,
        )
    )

    assert len(items) == 1
    assert items[0].plan.id == 1
    assert items[0].due_date == "2026-07-17"
    assert items[0].overdue_trading_days == 2


def test_due_review_repository_prioritizes_old_unfinished_plan_over_many_recent_not_due(
    tmp_path: Path,
) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    finished_analysis = _valid_analysis_on_date("600000", "2026-06-30")
    finished_advice = cache.save_advice_snapshot(
        finished_analysis,
        snapshot_market_time="2026-06-30 15:15:00",
    )
    finished_plan = cache.create_advice_review_plan(
        _plan_input(finished_advice.id, "600000.SH").model_copy(update={"horizon_days": 1})
    )
    cache.save_advice_review_evaluation(
        evaluate_advice_forward_window(
            finished_plan,
            [
                make_kline(date="2026-06-30", close=100, high=101, low=99),
                make_kline(date="2026-07-01", close=104, high=106, low=98),
            ],
            as_of=datetime(2026, 7, 1, 16),
            evaluated_at="2026-07-01T08:01:00.000000Z",
        )
    )
    old_analysis = _valid_analysis_on_date("600001", "2026-07-01")
    old_advice = cache.save_advice_snapshot(
        old_analysis,
        snapshot_market_time="2026-07-01 15:15:00",
    )
    old_plan = cache.create_advice_review_plan(
        _plan_input(old_advice.id, "600001.SH").model_copy(update={"horizon_days": 1})
    )
    for offset in range(30):
        code = f"60{100 + offset:04d}"
        recent = _valid_analysis(code)
        advice = cache.save_advice_snapshot(
            recent,
            snapshot_market_time="2026-07-17 15:15:00",
        )
        cache.create_advice_review_plan(
            _plan_input(advice.id, f"{code}.SH").model_copy(update={"horizon_days": 60})
        )

    candidates = cache.advice_review_evaluation_candidates(
        as_of_date="2026-07-20",
        limit=1,
    )

    assert [detail.plan.id for detail in candidates] == [old_plan.id]
    assert finished_plan.id not in {detail.plan.id for detail in candidates}


def test_due_review_candidates_order_by_conservative_due_key_before_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    old_template = _valid_analysis_on_date("601000", "2026-05-21")
    for offset in range(500):
        code = f"{601000 + offset:06d}"
        old_analysis = old_template.model_copy(
            update={
                "quote": old_template.quote.model_copy(
                    update={"code": code, "name": f"Test {code}"}
                )
            }
        )
        advice = cache.save_advice_snapshot(
            old_analysis,
            snapshot_market_time="2026-05-21 15:15:00",
        )
        cache.create_advice_review_plan(
            _plan_input(advice.id, f"{code}.SH").model_copy(update={"horizon_days": 60})
        )

    short_analysis = _valid_analysis_on_date("603999", "2026-07-17")
    short_advice = cache.save_advice_snapshot(
        short_analysis,
        snapshot_market_time="2026-07-17 15:15:00",
    )
    short_plan = cache.create_advice_review_plan(
        _plan_input(short_advice.id, "603999.SH").model_copy(update={"horizon_days": 1})
    )
    calls: list[int] = []

    async def evaluate(_datahub, plan_id: int, *, as_of, now=None):
        calls.append(plan_id)
        return SimpleNamespace(id=plan_id + 10_000, status="evaluated", conclusion="horizon_gain")

    monkeypatch.setattr(advice_review, "evaluate_advice_review_plan", evaluate)
    monkeypatch.setattr(advice_review, "is_trading_day", lambda value: value.weekday() < 5)

    summary = asyncio.run(
        evaluate_due_advice_reviews(
            SimpleNamespace(cache=cache),
            as_of=datetime(2026, 7, 20, 16),
            limit=50,
        )
    )

    assert calls == [short_plan.id]
    assert summary.candidate_count == 1
    assert summary.attempted_count == 1


def test_background_refresh_and_review_errors_redact_configured_secrets(monkeypatch) -> None:
    secret = "provider-token-value-123456"
    provider_url = "https://private-provider.example/internal"
    settings = SimpleNamespace(
        tushare_token=secret,
        llm_api_key=secret,
        llm_base_url=provider_url,
    )
    error = RuntimeError(f"request failed: {provider_url}?token={secret}; credential={secret}")

    research_cache = _ResearchQueueCache(active_symbols=("600001.SH",))
    research_hub = SimpleNamespace(cache=research_cache, settings=settings)

    async def fail_analysis(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(individual, "analyze_individual_stock", fail_analysis)
    monkeypatch.setattr(individual, "is_trading_day", lambda _value: True)
    research = asyncio.run(refresh_active_research_queue(research_hub, now=AFTER_CLOSE, limit=1))

    review_cache = _DueReviewCache(
        [AdviceReviewDetail(plan=_review_plan(plan_id=9, advice_id=19, horizon_days=1))]
    )
    review_hub = SimpleNamespace(cache=review_cache, settings=settings)

    async def fail_review(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(advice_review, "evaluate_advice_review_plan", fail_review)
    monkeypatch.setattr(advice_review, "is_trading_day", lambda _value: True)
    review = asyncio.run(evaluate_due_advice_reviews(review_hub, as_of=AFTER_CLOSE, limit=1))

    messages = [research.items[0].message, review.items[0].message]
    assert all(message for message in messages)
    assert all(secret not in message for message in messages)
    assert all(provider_url not in message for message in messages)


def test_review_summary_api_exposes_global_local_contract() -> None:
    summary = AdviceReviewSummary(
        generated_at="2026-07-17T08:00:00.000000Z",
        total_plan_count=7,
        pending_count=2,
        insufficient_count=1,
        evaluated_count=4,
        favorable_count=2,
        unfavorable_count=1,
        ambiguous_count=1,
        target_hit_count=1,
        stop_hit_count=1,
        favorable_rate_pct=66.67,
        average_return_pct=2.5,
        average_mfe_pct=5.0,
        average_mae_pct=-2.0,
        conclusion_counts={"target_hit": 1, "horizon_gain": 1, "stop_hit": 1, "target_stop_ambiguous": 1},
    )
    cache = SimpleNamespace(advice_review_summary=lambda: summary)
    app = FastAPI()
    app.include_router(reviews.router)
    app.dependency_overrides[get_datahub] = lambda: SimpleNamespace(cache=cache)

    response = TestClient(app).get("/api/reviews/summary")

    assert response.status_code == 200
    assert response.json() == summary.model_dump()
    assert "user_id" not in response.json()


def test_review_summary_uses_persisted_current_revision_results(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    first_advice = cache.save_advice_snapshot(_valid_analysis("600519"))
    second_advice = cache.save_advice_snapshot(_valid_analysis("600002"))
    first_plan = cache.create_advice_review_plan(_plan_input(first_advice.id, "600519.SH"))
    cache.create_advice_review_plan(_plan_input(second_advice.id, "600002.SH"))
    draft = evaluate_advice_forward_window(
        first_plan,
        [
            make_kline(date="2026-07-17", close=100, high=101, low=98),
            make_kline(date="2026-07-20", close=104, high=106, low=98),
        ],
        as_of=datetime(2026, 7, 20, 16),
        evaluated_at="2026-07-20T08:01:00.000000Z",
    )
    saved = cache.save_advice_review_evaluation(draft)

    fetched = cache.advice_review_evaluation(saved.id)
    summary = cache.advice_review_summary()

    assert fetched is not None
    assert fetched.trigger_evidence.met is True
    assert fetched.trigger_evidence.price == pytest.approx(105)
    assert fetched.trigger_basis == "daily_high_gte_target_price"
    assert fetched.invalidation_basis == "daily_low_lte_stop_price"
    assert fetched.trigger_evidence.basis == fetched.trigger_basis
    assert summary.total_plan_count == 2
    assert summary.pending_count == 1
    assert summary.evaluated_count == 1
    assert summary.favorable_count == 1
    assert summary.target_hit_count == 1
    assert summary.average_mfe_pct == pytest.approx(6)


def test_opened_timeline_watermark_keeps_a_later_refresh_unread(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(settings=Settings(cache_path=path, advice_history_dedupe_seconds=0))
    analysis = _valid_analysis("600519")
    cache.save_watchlist_item(analysis.quote)
    cache.save_advice_snapshot(analysis)
    first_change = cache.save_advice_snapshot(
        analysis.model_copy(
            update={
                "action_advice": analysis.action_advice.model_copy(
                    update={"confidence": analysis.action_advice.confidence + 1}
                )
            }
        )
    )
    displayed_watermark = cache.advice_timeline("600519.SH", limit=1)[0].id
    assert displayed_watermark == first_change.id

    cache.save_advice_snapshot(
        analysis.model_copy(
            update={
                "action_advice": analysis.action_advice.model_copy(
                    update={"confidence": analysis.action_advice.confidence + 2}
                )
            }
        )
    )
    marked = cache.mark_watchlist_viewed(
        "600519.SH",
        viewed_through_advice_id=displayed_watermark,
    )

    assert marked is not None
    assert marked.unread_change_count == 1


def test_scheduler_registers_bounded_research_refresh_and_due_review_tasks() -> None:
    definitions = {item.name: item for item in _TASK_DEFINITIONS}

    assert definitions["refresh_research_queue"].handler_name == "_refresh_research_queue"
    assert definitions["evaluate_due_reviews"].handler_name == "_evaluate_due_reviews"
    assert definitions["refresh_research_queue"].settings_interval_attr == "scheduler_kline_interval_seconds"
    assert definitions["evaluate_due_reviews"].settings_interval_attr == "scheduler_kline_interval_seconds"


def _valid_analysis(code: str):
    quote = make_quote(
        price=100,
        prev_close=99,
        high=101,
        low=98,
        timestamp="2026-07-17 16:00:00",
    ).model_copy(update={"code": code, "name": f"Test {code}"})
    rows = [make_kline(date="2026-07-17", close=100, high=101, low=98)]
    quality = DataQuality(
        level="good",
        source="test",
        quote_time=quote.timestamp,
        kline_count=len(rows),
        score=90,
        kline_quality=KlineQuality(
            level="fresh",
            last_date="2026-07-17",
            latest_expected_date="2026-07-17",
            latest_allowed_date="2026-07-17",
            days_behind_expected=0,
        ),
    )
    return build_analysis(quote, rows, data_quality=quality)


def _valid_analysis_on_date(code: str, data_date: str):
    analysis = _valid_analysis(code)
    quote = analysis.quote.model_copy(update={"timestamp": f"{data_date} 16:00:00"})
    rows = [analysis.klines[-1].model_copy(update={"date": data_date})]
    quality = analysis.data_quality.model_copy(
        update={
            "quote_time": quote.timestamp,
            "kline_quality": analysis.data_quality.kline_quality.model_copy(
                update={
                    "last_date": data_date,
                    "latest_expected_date": data_date,
                    "latest_allowed_date": data_date,
                }
            ),
        }
    )
    return analysis.model_copy(update={"quote": quote, "klines": rows, "data_quality": quality})


def _review_plan(
    *,
    plan_id: int = 1,
    advice_id: int = 1,
    symbol: str = "600519.SH",
    horizon_days: int = 3,
) -> AdviceReviewPlan:
    return AdviceReviewPlan(
        id=plan_id,
        advice_id=advice_id,
        symbol=symbol,
        snapshot_market_time="2026-07-16 15:00:00",
        snapshot_price=100,
        snapshot_adjustment_mode="qfq",
        snapshot_anchor_date="2026-07-16",
        snapshot_anchor_close=100,
        snapshot_data_version="test-daily-kline-qfq-v1",
        snapshot_contract_version="daily-kline.v1",
        hypothesis="trend continues",
        trigger_condition="price reaches 105",
        invalidation_condition="price falls to 95",
        target_price=105,
        stop_price=95,
        horizon_days=horizon_days,
        evidence_refs=[],
        revision=1,
        created_at="2026-07-16T07:01:00.000000Z",
        updated_at="2026-07-16T07:01:00.000000Z",
    )


def _plan_input(advice_id: int, symbol: str) -> AdviceReviewPlanInput:
    return AdviceReviewPlanInput(
        advice_id=advice_id,
        symbol=symbol,
        hypothesis="trend continues",
        trigger_condition="price reaches 105",
        invalidation_condition="price falls to 95",
        target_price=105,
        stop_price=95,
        horizon_days=1,
    )


class _ResearchQueueCache:
    def __init__(
        self,
        *,
        active_symbols: tuple[str, ...],
        excluded_symbols: tuple[str, ...] = (),
    ) -> None:
        self.selection = SimpleNamespace(
            active_symbols=active_symbols,
            excluded_symbols=excluded_symbols,
            has_entries=True,
        )
        self.saved_symbols: list[str] = []
        self._next_id = 1
        self._latest_by_symbol: dict[str, SimpleNamespace] = {}

    def watchlist_symbol_selection(self):
        return self.selection

    def advice_timeline(self, symbol: str, limit: int = 1):
        assert limit == 1
        latest = self._latest_by_symbol.get(symbol)
        return [latest] if latest is not None else []

    def latest_advice_timeline_by_symbols(self, symbols):
        return {symbol: self._latest_by_symbol[symbol] for symbol in symbols if symbol in self._latest_by_symbol}

    def save_advice_snapshot(self, analysis, *, snapshot_market_time: str | None = None):
        assert snapshot_market_time == "2026-07-17 15:15:00"
        symbol = f"{analysis.quote.code}.{analysis.quote.market}"
        self.saved_symbols.append(symbol)
        anchor = analysis.klines[-1]
        item = SimpleNamespace(
            id=self._next_id,
            symbol=symbol,
            market_time=snapshot_market_time,
            created_at="2026-07-17T08:15:00.000000Z",
            updated_at="2026-07-17T08:15:00.000000Z",
            action=analysis.action_advice.action,
            confidence=analysis.action_advice.confidence,
            trend_score=analysis.trend_score,
            trend_label=analysis.trend_label,
            risk_level=analysis.risk_level,
            support=analysis.support,
            resistance=analysis.resistance,
            data_quality_score=analysis.data_quality.score,
            data_quality_level=analysis.data_quality.level,
            data_quality_source=analysis.data_quality.source,
            snapshot_contract_version=SNAPSHOT_CONTRACT_VERSION,
            conclusion_basis=CONCLUSION_BASIS,
            rule_version=RULE_VERSION,
            model_version=MODEL_VERSION,
            kline_adjustment_mode=anchor.adjustment_mode,
            kline_anchor_date=anchor.date,
            kline_anchor_close=anchor.close,
            kline_data_version=anchor.data_version,
            kline_contract_version=DAILY_KLINE_CONTRACT_VERSION,
        )
        self._next_id += 1
        self._latest_by_symbol[symbol] = item
        return item


class _DueReviewCache:
    def __init__(
        self,
        details: list[AdviceReviewDetail],
        *,
        expected_as_of_date: str = "2026-07-17",
    ) -> None:
        self.details = details
        self.expected_as_of_date = expected_as_of_date

    def advice_review_evaluation_candidates(self, *, as_of_date: str, limit: int):
        assert as_of_date == self.expected_as_of_date
        return self.details[:limit]
