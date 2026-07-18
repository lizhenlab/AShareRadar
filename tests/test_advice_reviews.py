from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from app.models.reviews import AdviceReviewPlanInput, AdviceReviewPlanUpdate
from app.services.analysis import build_analysis
from app.services.cache import SQLiteCache
from app.services.data_quality import build_data_quality
from app.services.research_replay import evaluate_advice_forward_window
from tests.factories import make_kline, make_quote


def _insert_advice(path: Path, *, market_time: str | None = "2026-05-10 10:00:00") -> int:
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO advice_history (
                symbol, code, market, name, action, confidence, trend_score,
                trend_label, risk_level, price, change_pct, support, resistance,
                data_quality_score, data_quality_level, reason, summary, created_at,
                updated_at, repeat_count, snapshot_contract_version, conclusion_basis,
                rule_version, model_version, market_time, data_quality_source,
                kline_adjustment_mode, kline_anchor_date, kline_anchor_close,
                kline_data_version, kline_contract_version
            ) VALUES (
                '600519.SH', '600519', 'SH', '贵州茅台', '等待信号', 60, 55,
                '中性观察', '可控风险', 100, 0, 95, 110,
                90, '优秀', '测试理由', '测试摘要', '2026-05-10 10:00:01',
                '2026-05-10 10:00:01', 1, 'v1', 'rule', 'rule-v1', 'model-v1', ?, 'test',
                'qfq', '2026-05-09', 100, 'snapshot-qfq-v1', 'daily-kline.v1'
            )
            """,
            (market_time,),
        )
        return int(cursor.lastrowid)


def _plan_input(advice_id: int) -> AdviceReviewPlanInput:
    return AdviceReviewPlanInput(
        advice_id=advice_id,
        symbol="600519.SH",
        hypothesis="价格站稳后趋势延续",
        trigger_condition="收盘站上 101",
        invalidation_condition="跌破 95",
        target_price=110,
        stop_price=95,
        horizon_days=3,
        evidence_refs=["建议快照", "日K结构"],
    )


def test_review_schema_and_plan_are_initialized_with_the_cache(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    advice_id = _insert_advice(path)

    plan = cache.create_advice_review_plan(_plan_input(advice_id))

    assert plan.advice_id == advice_id
    assert plan.snapshot_market_time == "2026-05-10 10:00:00"
    assert plan.revision == 1
    assert cache.advice_review_plan_by_advice(advice_id) == plan
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migration WHERE name = '20260716_advice_review_v1'").fetchone()[0] == 1


def test_deleting_review_plan_cascades_evaluations_but_keeps_advice_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    advice_id = _insert_advice(path)
    plan = cache.create_advice_review_plan(_plan_input(advice_id))
    draft = evaluate_advice_forward_window(
        plan,
        [make_kline(date="2026-05-11", close=102, high=103, low=99)],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )
    cache.save_advice_review_evaluation(draft)

    assert cache.delete_advice_review_plan(plan.id) is True
    assert cache.delete_advice_review_plan(plan.id) is False

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM advice_review_plan").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM advice_review_result").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM advice_history WHERE id = ?", (advice_id,)).fetchone()[0] == 1


def test_review_plan_rejects_a_snapshot_without_market_time(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    advice_id = _insert_advice(path, market_time=None)

    with pytest.raises(ValueError, match="market_time"):
        cache.create_advice_review_plan(_plan_input(advice_id))


def test_saved_advice_freezes_latest_completed_qfq_anchor_for_review(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    quote = make_quote(price=100, prev_close=99, high=101, low=98, timestamp="2026-05-13 10:00:00")
    rows = [make_kline(date=f"2026-05-{day:02d}", close=90 + day) for day in range(1, 14)]
    analysis = build_analysis(quote, rows, data_quality=build_data_quality(quote, rows))

    advice = cache.save_advice_snapshot(analysis)
    plan = cache.create_advice_review_plan(_plan_input(advice.id))

    assert plan.snapshot_adjustment_mode == "qfq"
    assert plan.snapshot_anchor_date == "2026-05-12"
    assert plan.snapshot_anchor_close == 102
    assert plan.snapshot_data_version == "test-daily-kline-qfq-v1"
    assert plan.snapshot_contract_version == "daily-kline.v1"


def test_review_evaluation_excludes_snapshot_day_and_marks_same_day_barriers_ambiguous(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    rows = [
        make_kline(date="2026-05-09", close=99, high=100, low=98),
        make_kline(date="2026-05-10", close=111, high=112, low=94),
        make_kline(date="2026-05-11", close=102, high=111, low=94),
        make_kline(date="2026-05-12", close=103, high=104, low=100),
    ]

    draft = evaluate_advice_forward_window(
        plan,
        rows,
        as_of=datetime(2026, 5, 12, 16),
        evaluated_at="2026-05-12 16:01:00",
    )

    assert draft.visible_end_date == "2026-05-09"
    assert draft.forward_start_date == "2026-05-11"
    assert draft.available_forward_days == 1
    assert draft.status == "evaluated"
    assert draft.conclusion == "target_stop_ambiguous"
    assert draft.target_hit_date == "2026-05-11"
    assert draft.stop_hit_date == "2026-05-11"


def test_review_evaluation_distinguishes_pending_from_missing_data(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))

    pending = evaluate_advice_forward_window(
        plan,
        [],
        as_of=datetime(2026, 5, 10, 16),
        evaluated_at="2026-05-10 16:01:00",
    )
    insufficient = evaluate_advice_forward_window(
        plan,
        [],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )

    assert (pending.status, pending.conclusion) == ("pending", "pending")
    assert (insufficient.status, insufficient.conclusion) == ("insufficient", "insufficient_data")


def test_repository_preserves_new_pending_with_complete_snapshot_provenance(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    draft = evaluate_advice_forward_window(
        plan,
        [],
        as_of=datetime(2026, 5, 10, 16),
        evaluated_at="2026-05-10 16:01:00",
    )

    saved = cache.save_advice_review_evaluation(draft)
    fetched = cache.advice_review_evaluation(saved.id)
    detail = cache.advice_review_detail(plan.id)

    assert fetched is not None
    assert detail is not None
    assert detail.latest_evaluation is not None
    for evaluation in (saved, fetched, detail.latest_evaluation):
        assert (evaluation.status, evaluation.conclusion) == ("pending", "pending")
        assert evaluation.snapshot_adjustment_mode == "qfq"
        assert evaluation.snapshot_anchor_date == "2026-05-09"
        assert evaluation.snapshot_anchor_close == 100
        assert evaluation.snapshot_data_version == "snapshot-qfq-v1"
        assert evaluation.snapshot_contract_version == "daily-kline.v1"
        assert evaluation.evaluation_adjustment_mode == "unknown"
        assert evaluation.evaluation_data_version == "unknown"
        assert evaluation.evaluation_contract_version == "unknown"


def test_repository_redacts_unverifiable_legacy_evaluation_without_mutating_audit_row(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    draft = evaluate_advice_forward_window(
        plan,
        [
            make_kline(date="2026-05-09", close=100, high=101, low=99),
            make_kline(date="2026-05-11", close=111, high=112, low=99),
        ],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )
    saved = cache.save_advice_review_evaluation(draft)

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE advice_review_result
            SET snapshot_adjustment_mode = 'unknown',
                snapshot_anchor_date = NULL,
                snapshot_anchor_close = NULL,
                snapshot_data_version = 'unknown',
                snapshot_contract_version = 'unknown',
                status = 'evaluated',
                conclusion = 'target_hit',
                anchor_evaluation_close = 100,
                price_scale_factor = 1,
                normalized_entry_price = 100,
                normalized_target_price = 110,
                normalized_stop_price = 95,
                visible_bar_count = 1,
                visible_start_date = '2026-05-09',
                visible_end_date = '2026-05-09',
                available_forward_days = 1,
                forward_start_date = '2026-05-11',
                forward_end_date = '2026-05-11',
                return_pct = 11,
                max_favorable_excursion_pct = 12,
                max_adverse_excursion_pct = -1,
                target_hit = 1,
                target_hit_date = '2026-05-11'
            WHERE id = ?
            """,
            (saved.id,),
        )

    detail = cache.advice_review_detail(plan.id)
    details = cache.advice_review_details(symbol=plan.symbol, limit=10)
    fetched = cache.advice_review_evaluation(saved.id)
    history = cache.advice_review_evaluation_history(plan.id)

    assert detail is not None
    assert detail.latest_evaluation is not None
    assert details[0].latest_evaluation is not None
    assert fetched is not None
    for evaluation in (detail.latest_evaluation, details[0].latest_evaluation, fetched, history[0]):
        assert (evaluation.status, evaluation.conclusion) == ("insufficient", "insufficient_data")
        assert evaluation.anchor_evaluation_close is None
        assert evaluation.price_scale_factor is None
        assert evaluation.normalized_entry_price is None
        assert evaluation.normalized_target_price is None
        assert evaluation.normalized_stop_price is None
        assert evaluation.visible_bar_count == 0
        assert evaluation.visible_start_date is None
        assert evaluation.visible_end_date is None
        assert evaluation.available_forward_days == 0
        assert evaluation.forward_start_date is None
        assert evaluation.forward_end_date is None
        assert evaluation.return_pct is None
        assert evaluation.max_favorable_excursion_pct is None
        assert evaluation.max_adverse_excursion_pct is None
        assert evaluation.target_hit is False
        assert evaluation.target_hit_date is None
        assert evaluation.stop_hit is False
        assert evaluation.stop_hit_date is None

    with sqlite3.connect(path) as conn:
        raw = conn.execute(
            """
            SELECT status, conclusion, return_pct, target_hit, target_hit_date,
                   visible_end_date, forward_end_date
            FROM advice_review_result
            WHERE id = ?
            """,
            (saved.id,),
        ).fetchone()
    assert raw == ("evaluated", "target_hit", 11.0, 1, "2026-05-11", "2026-05-09", "2026-05-11")


def test_review_evaluation_upsert_is_idempotent_for_one_revision_and_as_of(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    draft = evaluate_advice_forward_window(
        plan,
        [make_kline(date="2026-05-11", close=102, high=103, low=99)],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )

    first = cache.save_advice_review_evaluation(draft)
    second = cache.save_advice_review_evaluation(draft.model_copy(update={"evaluated_at": "2026-05-11 16:02:00"}))

    assert second.id == first.id
    assert second.evaluated_at == "2026-05-11 16:02:00"
    assert len(cache.advice_review_evaluation_history(plan.id)) == 1


def test_review_evaluation_rebases_frozen_price_levels_to_current_qfq_vintage(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    rows = [
        make_kline(date="2026-05-09", close=50, high=51, low=49, data_version="rebased-qfq-v2"),
        make_kline(date="2026-05-11", close=54, high=56, low=48, data_version="rebased-qfq-v2"),
    ]

    draft = evaluate_advice_forward_window(
        plan,
        rows,
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )

    assert draft.conclusion == "target_hit"
    assert draft.price_scale_factor == pytest.approx(0.5)
    assert draft.normalized_entry_price == pytest.approx(50)
    assert draft.normalized_target_price == pytest.approx(55)
    assert draft.normalized_stop_price == pytest.approx(47.5)
    assert draft.snapshot_data_version == "snapshot-qfq-v1"
    assert draft.evaluation_data_version == "rebased-qfq-v2"


def test_review_evaluation_marks_mature_partial_window_insufficient(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    rows = [
        make_kline(date="2026-05-09", close=100),
        make_kline(date="2026-05-11", close=102, high=103, low=99),
    ]

    draft = evaluate_advice_forward_window(
        plan,
        rows,
        as_of=datetime(2026, 5, 20, 16),
        evaluated_at="2026-05-20 16:01:00",
    )

    assert draft.available_forward_days == 1
    assert (draft.status, draft.conclusion) == ("insufficient", "insufficient_data")


def test_review_details_join_only_the_latest_result_for_current_revision(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    revision_one = evaluate_advice_forward_window(
        plan,
        [make_kline(date="2026-05-11", close=102, high=103, low=99)],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )
    cache.save_advice_review_evaluation(revision_one)

    updated = cache.update_advice_review_plan(plan.id, AdviceReviewPlanUpdate(horizon_days=5))

    assert updated is not None
    assert updated.revision == 2
    detail = cache.advice_review_details(symbol="600519", limit=10)[0]
    assert detail.plan.revision == 2
    assert detail.latest_evaluation is None

    revision_two = evaluate_advice_forward_window(
        updated,
        [make_kline(date="2026-05-11", close=102, high=103, low=99)],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:02:00",
    )
    saved = cache.save_advice_review_evaluation(revision_two)

    detail = cache.advice_review_details(symbol="600519", limit=10)[0]
    assert detail.latest_evaluation is not None
    assert detail.latest_evaluation.id == saved.id
    assert detail.latest_evaluation.plan_revision == 2
