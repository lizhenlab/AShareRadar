from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from app.models.reviews import AdviceReviewPlanInput, AdviceReviewPlanUpdate
from app.repositories.advice_reviews import AdviceReviewIntegrityError, AdviceReviewRevisionConflictError
from app.services.analysis import build_analysis
from app.services.cache import SQLiteCache
from app.services.data_quality import build_data_quality
from app.services.research_replay import evaluate_advice_forward_window
from tests.factories import make_kline as make_base_kline, make_quote


def make_kline(**values):
    """Build a daily bar with replay evidence unless a test overrides it."""
    data_version = values.get("data_version", "test-daily-kline-qfq-v1")
    return make_base_kline(replay_eligible=True, **values).model_copy(
        update={"data_version": data_version}
    )


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
                'qfq', '2026-05-08', 100, 'snapshot-qfq-v1', 'daily-kline.v1'
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


def _downgrade_review_ledger_to_v3(path: Path) -> None:
    from app.db.advice_review_schema import ADVICE_REVIEW_RESULT_TABLE_SQL

    ledger_start = ADVICE_REVIEW_RESULT_TABLE_SQL.index("    attempt INTEGER")
    ledger_end = ADVICE_REVIEW_RESULT_TABLE_SQL.index(
        "    FOREIGN KEY(plan_id)",
        ledger_start,
    )
    legacy_result_sql = (
        ADVICE_REVIEW_RESULT_TABLE_SQL[:ledger_start]
        + "    UNIQUE(plan_id, plan_revision, as_of, rule_version),\n"
        + ADVICE_REVIEW_RESULT_TABLE_SQL[ledger_end:]
    )
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ALTER TABLE advice_review_result RENAME TO advice_review_result_current")
        conn.execute(legacy_result_sql)
        current_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(advice_review_result_current)")
        }
        legacy_columns = [
            str(row[1]) for row in conn.execute("PRAGMA table_info(advice_review_result)")
        ]
        shared_columns = [column for column in legacy_columns if column in current_columns]
        joined = ", ".join(shared_columns)
        conn.execute(
            f"""
            INSERT INTO advice_review_result ({joined})
            SELECT {joined} FROM advice_review_result_current
            """
        )
        conn.execute("DROP TABLE advice_review_result_current")
        conn.execute("DROP TABLE advice_review_plan_revision")
        conn.execute(
            "UPDATE advice_review_plan SET plan_payload_digest = 'legacy-unverified'"
        )
        conn.execute(
            "DELETE FROM schema_migration WHERE name = '20260813_advice_review_immutable_ledger_v4'"
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


def test_deleting_review_plan_tombstones_but_keeps_audit_ledger_and_snapshot(tmp_path: Path) -> None:
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

    assert cache.delete_advice_review_plan(plan.id, expected_revision=plan.revision) is True
    assert cache.delete_advice_review_plan(plan.id, expected_revision=plan.revision) is False

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM advice_review_plan").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM advice_review_result").fetchone()[0] == 1
        assert conn.execute("SELECT deleted_at IS NOT NULL FROM advice_review_plan").fetchone()[0] == 1
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


def test_review_plan_keeps_its_source_advice_snapshot_immutable(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    quote = make_quote(price=100, prev_close=99, timestamp="2026-05-13 10:00:00")
    rows = [make_kline(date=f"2026-05-{day:02d}", close=90 + day) for day in range(1, 14)]
    analysis = build_analysis(quote, rows, data_quality=build_data_quality(quote, rows))
    source = cache.save_advice_snapshot(analysis)
    cache.create_advice_review_plan(_plan_input(source.id))

    repeated = cache.save_advice_snapshot(analysis)

    assert repeated.id != source.id
    with sqlite3.connect(path) as conn:
        frozen = conn.execute(
            "SELECT repeat_count, market_time, price FROM advice_history WHERE id = ?",
            (source.id,),
        ).fetchone()
    assert frozen == (1, "2026-05-13 10:00:00", 100)


def test_review_evaluation_excludes_snapshot_day_and_marks_same_day_barriers_ambiguous(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    rows = [
        make_kline(date="2026-05-08", close=99, high=100, low=98),
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

    assert draft.visible_end_date == "2026-05-08"
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
        assert evaluation.snapshot_anchor_date == "2026-05-08"
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
            make_kline(date="2026-05-08", close=100, high=101, low=99),
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
                visible_start_date = '2026-05-08',
                visible_end_date = '2026-05-08',
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
    assert raw == ("evaluated", "target_hit", 11.0, 1, "2026-05-11", "2026-05-08", "2026-05-11")


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
    assert second.evaluated_at == "2026-05-11 16:01:00"
    assert len(cache.advice_review_evaluation_history(plan.id)) == 1


def test_review_evaluation_rebases_frozen_price_levels_to_current_qfq_vintage(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    rows = [
        make_kline(date="2026-05-08", close=50, high=51, low=49, data_version="rebased-qfq-v2"),
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


def test_review_excursions_ignore_suspended_session_ohlc(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    suspended = make_kline(
        date="2026-05-12",
        close=500,
        high=1_000,
        low=1,
        volume=0,
    ).model_copy(
        update={
            "session_status": "suspended",
            "open_execution_status": "unavailable",
        }
    )

    draft = evaluate_advice_forward_window(
        plan,
        [
            make_kline(date="2026-05-08", close=100, high=101, low=99),
            make_kline(date="2026-05-11", close=102, high=104, low=98),
            suspended,
            make_kline(date="2026-05-13", close=103, high=105, low=97),
        ],
        as_of=datetime(2026, 5, 13, 16),
        evaluated_at="2026-05-13 16:01:00",
    )

    assert (draft.status, draft.conclusion) == ("evaluated", "horizon_gain")
    assert draft.return_pct == 3
    assert draft.max_favorable_excursion_pct == 5
    assert draft.max_adverse_excursion_pct == -3
    assert draft.target_hit is False
    assert draft.stop_hit is False


def test_review_evaluation_ignores_conflicting_bars_after_as_of(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    rows = [
        make_kline(date="2026-05-08", close=100),
        make_kline(date="2026-05-11", close=102, high=103, low=99),
        make_kline(date="2026-05-12", close=103, high=104, low=101),
        make_kline(date="2026-05-13", close=104, high=105, low=102),
        make_kline(date="2026-05-13", close=110, high=111, low=109),
    ]

    draft = evaluate_advice_forward_window(
        plan,
        rows,
        as_of=datetime(2026, 5, 12, 16),
        evaluated_at="2026-05-12 16:01:00",
    )

    assert draft.status == "pending"
    assert draft.available_forward_days == 2
    assert draft.forward_end_date == "2026-05-12"


def test_review_evaluation_marks_mature_partial_window_insufficient(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    rows = [
        make_kline(date="2026-05-08", close=100),
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

    updated = cache.update_advice_review_plan(
        plan.id,
        AdviceReviewPlanUpdate(expected_revision=plan.revision, horizon_days=5),
    )

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


def test_plan_revisions_are_append_only_and_support_a_b_a_payloads(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))

    second = cache.update_advice_review_plan(
        plan.id,
        AdviceReviewPlanUpdate(expected_revision=1, hypothesis="第二版假设"),
    )
    assert second is not None
    third = cache.update_advice_review_plan(
        plan.id,
        AdviceReviewPlanUpdate(expected_revision=2, hypothesis=plan.hypothesis),
    )
    assert third is not None

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT revision, payload_json, payload_digest
            FROM advice_review_plan_revision WHERE plan_id = ? ORDER BY revision
            """,
            (plan.id,),
        ).fetchall()
    assert [row[0] for row in rows] == [1, 2, 3]
    assert rows[0][1] == rows[2][1]
    assert rows[0][2] == rows[2][2]
    assert rows[0][2] != rows[1][2]
    assert third.plan_payload_digest == rows[2][2]


def test_stale_plan_update_is_rejected_by_compare_and_swap(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(cache.path)))
    cache.update_advice_review_plan(
        plan.id,
        AdviceReviewPlanUpdate(expected_revision=1, hypothesis="胜出的更新"),
    )

    with pytest.raises(AdviceReviewRevisionConflictError, match="修订冲突"):
        cache.update_advice_review_plan(
            plan.id,
            AdviceReviewPlanUpdate(expected_revision=1, hypothesis="过期更新"),
        )


def test_different_evaluation_digest_appends_attempt_without_rewriting_first(tmp_path: Path) -> None:
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
    changed = draft.model_copy(
        update={
            "source_window_digest": "a" * 64,
            "evaluated_at": "2026-05-11 16:02:00",
        }
    )
    second = cache.save_advice_review_evaluation(changed)

    assert (first.attempt, second.attempt) == (1, 2)
    assert first.id != second.id
    assert first.input_digest != second.input_digest
    assert first.result_digest == second.result_digest
    history = cache.advice_review_evaluation_history(plan.id)
    assert [item.id for item in history] == [second.id, first.id]


def test_late_early_observation_cannot_replace_latest_as_of(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    rows = [make_kline(date="2026-05-11", close=102, high=103, low=99)]
    recent = evaluate_advice_forward_window(
        plan,
        rows,
        as_of=datetime(2026, 5, 12, 16),
        evaluated_at="2026-05-12 16:01:00",
    )
    cache.save_advice_review_evaluation(recent)
    older = evaluate_advice_forward_window(
        plan,
        rows,
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-13 16:01:00",
    )
    cache.save_advice_review_evaluation(older)

    detail = cache.advice_review_detail(plan.id)
    assert detail is not None and detail.latest_evaluation is not None
    assert detail.latest_evaluation.as_of == "2026-05-12 16:00:00"


def test_unregistered_or_malformed_snapshot_provenance_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    advice_id = _insert_advice(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE advice_history
            SET kline_anchor_date = 'garbage', kline_data_version = ' ',
                kline_contract_version = 'unregistered'
            WHERE id = ?
            """,
            (advice_id,),
        )
    with pytest.raises(ValueError, match="可复现"):
        cache.create_advice_review_plan(_plan_input(advice_id))


def test_corrupt_evidence_json_fails_closed_instead_of_becoming_empty(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE advice_review_plan SET evidence_refs_json = 'not-json' WHERE id = ?",
            (plan.id,),
        )
    with pytest.raises(ValueError, match="证据引用损坏"):
        cache.advice_review_plan(plan.id)


def test_ledger_migration_backfills_current_revision_and_preserves_result_ids(tmp_path: Path) -> None:
    from app.db.advice_review_schema import (
        ADVICE_REVIEW_RESULT_TABLE_SQL,
        apply_advice_review_compat_schema,
    )

    path = tmp_path / "legacy.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    draft = evaluate_advice_forward_window(
        plan,
        [make_kline(date="2026-05-11", close=102, high=103, low=99)],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )
    saved = cache.save_advice_review_evaluation(draft)

    ledger_start = ADVICE_REVIEW_RESULT_TABLE_SQL.index("    attempt INTEGER")
    ledger_end = ADVICE_REVIEW_RESULT_TABLE_SQL.index(
        "    FOREIGN KEY(plan_id)",
        ledger_start,
    )
    legacy_result_sql = (
        ADVICE_REVIEW_RESULT_TABLE_SQL[:ledger_start]
        + "    UNIQUE(plan_id, plan_revision, as_of, rule_version),\n"
        + ADVICE_REVIEW_RESULT_TABLE_SQL[ledger_end:]
    )
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ALTER TABLE advice_review_result RENAME TO advice_review_result_current")
        conn.execute(legacy_result_sql)
        current_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(advice_review_result_current)")
        }
        legacy_columns = [
            str(row[1]) for row in conn.execute("PRAGMA table_info(advice_review_result)")
        ]
        shared_columns = [column for column in legacy_columns if column in current_columns]
        joined = ", ".join(shared_columns)
        conn.execute(
            f"""
            INSERT INTO advice_review_result ({joined})
            SELECT {joined} FROM advice_review_result_current
            """
        )
        conn.execute("DROP TABLE advice_review_result_current")
        conn.execute(
            "DELETE FROM advice_review_plan_revision WHERE plan_id = ?",
            (plan.id,),
        )
        conn.execute(
            "UPDATE advice_review_plan SET plan_payload_digest = 'legacy-unverified' WHERE id = ?",
            (plan.id,),
        )
        conn.execute(
            "DELETE FROM schema_migration WHERE name = '20260813_advice_review_immutable_ledger_v4'"
        )
        conn.commit()

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        apply_advice_review_compat_schema(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        result = conn.execute(
            "SELECT id, attempt, input_digest FROM advice_review_result WHERE id = ?",
            (saved.id,),
        ).fetchone()
        revision = conn.execute(
            "SELECT payload_digest FROM advice_review_plan_revision WHERE plan_id = ? AND revision = 1",
            (plan.id,),
        ).fetchone()
    assert result is not None and result["id"] == saved.id and result["attempt"] == 1
    assert result["input_digest"] == "legacy-unverified"
    assert revision is not None and len(str(revision["payload_digest"])) == 64


@pytest.mark.parametrize(
    "fault_target",
    ["_rebuild_result_as_append_only", "_backfill_current_plan_revisions"],
)
def test_ledger_migration_fault_rolls_back_atomically_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_target: str,
) -> None:
    import app.db.advice_review_schema as review_schema

    path = tmp_path / f"legacy-{fault_target}.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    draft = evaluate_advice_forward_window(
        plan,
        [make_kline(date="2026-05-11", close=102, high=103, low=99)],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )
    saved = cache.save_advice_review_evaluation(draft)
    _downgrade_review_ledger_to_v3(path)

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        original = getattr(review_schema, fault_target)

        def fail_after_mutation(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError(f"injected {fault_target} failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(review_schema, fault_target, fail_after_mutation)
            with pytest.raises(RuntimeError, match="injected"):
                review_schema.apply_advice_review_compat_schema(conn)

        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert "attempt" not in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(advice_review_result)")
        }
        assert conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
            "AND name = 'advice_review_plan_revision'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT id FROM advice_review_result WHERE id = ?",
            (saved.id,),
        ).fetchone()[0] == saved.id
        assert conn.execute(
            "SELECT 1 FROM schema_migration WHERE name = ?",
            (review_schema.ADVICE_REVIEW_LEDGER_SCHEMA_VERSION,),
        ).fetchone() is None

        review_schema.apply_advice_review_compat_schema(conn)

        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        result = conn.execute(
            "SELECT id, attempt FROM advice_review_result WHERE id = ?",
            (saved.id,),
        ).fetchone()
        revision = conn.execute(
            """
            SELECT payload_digest FROM advice_review_plan_revision
            WHERE plan_id = ? AND revision = 1
            """,
            (plan.id,),
        ).fetchone()
    assert tuple(result) == (saved.id, 1)
    assert revision is not None and len(str(revision["payload_digest"])) == 64


def test_evaluation_contract_rejects_illegal_dates_and_contradictory_outcome(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    draft = evaluate_advice_forward_window(
        plan,
        [make_kline(date="2026-05-11", close=102, high=103, low=99)],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )

    with pytest.raises(ValueError, match="as_of"):
        draft.model_copy(update={"as_of": "not-a-date"}, deep=True).__class__.model_validate(
            {**draft.model_dump(), "as_of": "not-a-date"}
        )
    with pytest.raises(ValueError, match="status 与 conclusion"):
        draft.__class__.model_validate(
            {
                **draft.model_dump(),
                "status": "evaluated",
                "conclusion": "pending",
            }
        )
    bad_payloads = (
        {"return_pct": float("inf")},
        {"status": "pending", "conclusion": "pending", "return_pct": 1.0},
        {
            "status": "evaluated",
            "conclusion": "horizon_gain",
            "available_forward_days": 0,
            "forward_start_date": None,
            "forward_end_date": None,
            "return_pct": None,
            "max_favorable_excursion_pct": None,
            "max_adverse_excursion_pct": None,
        },
        {
            "conclusion": "horizon_gain",
            "target_hit": True,
            "target_hit_date": draft.forward_end_date,
        },
        {"forward_start_date": "2026-05-12", "forward_end_date": "2026-05-11"},
        {"target_hit_date": "2026-05-20", "target_hit": True, "conclusion": "target_hit"},
    )
    for update in bad_payloads:
        with pytest.raises(ValueError):
            draft.__class__.model_validate({**draft.model_dump(), **update})

    with pytest.raises(ValueError, match="证据合同版本未注册"):
        cache.save_advice_review_evaluation(
            draft.model_copy(update={"evidence_contract_version": "legacy-unverified"})
        )


def test_active_plan_read_rejects_an_unverified_projection_digest(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE advice_review_plan SET plan_payload_digest = 'legacy-unverified' WHERE id = ?",
            (plan.id,),
        )

    with pytest.raises(ValueError, match="缺少可验证的修订摘要"):
        cache.advice_review_plan(plan.id)


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("result_digest", "f" * 64),
        ("input_digest", "e" * 64),
        ("plan_payload_digest", "d" * 64),
    ),
)
def test_tampered_evaluation_ledgers_are_redacted_from_reads_and_summary(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    path = tmp_path / f"{column}.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(
        _plan_input(_insert_advice(path)).model_copy(update={"horizon_days": 1})
    )
    draft = evaluate_advice_forward_window(
        plan,
        [
            make_kline(date="2026-05-08", close=100, high=101, low=99),
            make_kline(date="2026-05-11", close=111, high=112, low=99),
        ],
        as_of=datetime(2026, 5, 11, 16),
        evaluated_at="2026-05-11 16:01:00",
    )
    saved = cache.save_advice_review_evaluation(draft)
    assert saved.status == "evaluated"

    with sqlite3.connect(path) as conn:
        conn.execute(
            f"UPDATE advice_review_result SET {column} = ? WHERE id = ?",
            (value, saved.id),
        )

    redacted = cache.advice_review_evaluation(saved.id)
    assert redacted is not None
    assert redacted.status == "insufficient"
    assert redacted.conclusion == "insufficient_data"
    assert redacted.return_pct is None
    summary = cache.advice_review_summary()
    assert summary.evaluated_count == 0
    assert summary.insufficient_count == 1
    assert summary.average_return_pct is None


def test_tampered_evaluation_payload_cannot_change_summary_without_matching_digest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(
        _plan_input(_insert_advice(path)).model_copy(update={"horizon_days": 1})
    )
    saved = cache.save_advice_review_evaluation(
        evaluate_advice_forward_window(
            plan,
            [
                make_kline(date="2026-05-08", close=100, high=101, low=99),
                make_kline(date="2026-05-11", close=111, high=112, low=99),
            ],
            as_of=datetime(2026, 5, 11, 16),
            evaluated_at="2026-05-11 16:01:00",
        )
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE advice_review_result
            SET conclusion = 'horizon_loss', return_pct = -99, target_hit = 0
            WHERE id = ?
            """,
            (saved.id,),
        )

    detail = cache.advice_review_detail(plan.id)
    assert detail is not None and detail.latest_evaluation is not None
    assert detail.latest_evaluation.status == "insufficient"
    assert detail.latest_evaluation.return_pct is None
    summary = cache.advice_review_summary()
    assert summary.unfavorable_count == 0
    assert summary.insufficient_count == 1


@pytest.mark.parametrize(
    ("column", "value"),
    (("attempt", 99), ("evaluated_at", "2026-05-12 16:01:00")),
)
def test_tampered_evaluation_ordering_fields_are_digest_bound(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    path = tmp_path / f"ordering-{column}.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    saved = cache.save_advice_review_evaluation(
        evaluate_advice_forward_window(
            plan,
            [make_kline(date="2026-05-11", close=102, high=103, low=99)],
            as_of=datetime(2026, 5, 11, 16),
            evaluated_at="2026-05-11 16:01:00",
        )
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"UPDATE advice_review_result SET {column} = ? WHERE id = ?",
            (value, saved.id),
        )

    observed = cache.advice_review_evaluation(saved.id)
    assert observed is not None
    assert observed.status == "insufficient"
    assert observed.conclusion == "insufficient_data"


def test_summary_rejects_a_tampered_plan_projection_or_revision_payload(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE advice_review_plan_revision SET payload_json = '{\"corrupt\":true}' WHERE plan_id = ?",
            (plan.id,),
        )

    with pytest.raises(AdviceReviewIntegrityError, match="账本"):
        cache.advice_review_summary()


def test_plan_update_preserves_automatic_evidence_and_rejects_client_structured_refs(
    tmp_path: Path,
) -> None:
    from app.models.reviews import AdviceEvidenceRef

    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    plan = cache.create_advice_review_plan(_plan_input(_insert_advice(path)))
    automatic_ids = {
        item.id for item in plan.evidence_refs if isinstance(item, AdviceEvidenceRef)
    }
    updated = cache.update_advice_review_plan(
        plan.id,
        AdviceReviewPlanUpdate(
            expected_revision=plan.revision,
            evidence_refs=["新人工引用"],
        ),
    )
    assert updated is not None
    assert automatic_ids <= {
        item.id for item in updated.evidence_refs if isinstance(item, AdviceEvidenceRef)
    }
    assert "新人工引用" in updated.evidence_refs

    client_ref = AdviceEvidenceRef(
        id="client_claim",
        value="未来消息",
        direction="positive",
        data_date="2026-05-11",
        nature="estimated",
        rule_version="client-v1",
    )
    with pytest.raises(ValueError, match="系统冻结"):
        cache.update_advice_review_plan(
            plan.id,
            AdviceReviewPlanUpdate(
                expected_revision=updated.revision,
                evidence_refs=[client_ref],
            ),
        )
