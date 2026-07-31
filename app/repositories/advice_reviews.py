from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import math
import sqlite3

from app.db.advice_review_schema import (
    ADVICE_REVIEW_INDEX_SQL,
    ADVICE_REVIEW_PLAN_TABLE_SQL,
    ADVICE_REVIEW_RESULT_TABLE_SQL,
    ADVICE_REVIEW_SCHEMA_SQL,
    ADVICE_REVIEW_SCHEMA_VERSION,
)
from app.db.connection import SQLITE_AUDIT_EPOCH_FUNCTION
from app.models.reviews import (
    AdviceEvidenceRef,
    AdviceReviewDetail,
    AdviceReviewEvaluation,
    AdviceReviewEvaluationDraft,
    AdviceReviewPlan,
    AdviceReviewPlanInput,
    AdviceReviewPlanUpdate,
    AdviceReviewSummary,
    AdviceSnapshotRef,
    structured_advice_evidence_refs,
)
from app.repositories.base import SQLiteRepository
from app.utils.audit_time import audit_now_text as now_text
from app.utils.errors import NotFoundError
from app.utils.market_time import normalize_market_datetime
from app.utils.symbols import standard_symbol


_PLAN_SELECT_COLUMNS = """
    id,
    advice_id,
    symbol,
    snapshot_market_time,
    snapshot_price,
    snapshot_adjustment_mode,
    snapshot_anchor_date,
    snapshot_anchor_close,
    snapshot_data_version,
    snapshot_contract_version,
    hypothesis,
    trigger_condition,
    invalidation_condition,
    trigger_basis,
    invalidation_basis,
    target_price,
    stop_price,
    horizon_days,
    evidence_refs_json,
    revision,
    created_at,
    updated_at
"""

_RESULT_SELECT_COLUMNS = """
    id,
    plan_id,
    plan_revision,
    advice_id,
    symbol,
    snapshot_market_time,
    as_of,
    evaluated_at,
    status,
    conclusion,
    rule_version,
    trigger_basis,
    invalidation_basis,
    snapshot_adjustment_mode,
    snapshot_anchor_date,
    snapshot_anchor_close,
    snapshot_data_version,
    snapshot_contract_version,
    evaluation_adjustment_mode,
    evaluation_data_version,
    evaluation_contract_version,
    anchor_evaluation_close,
    price_scale_factor,
    normalized_entry_price,
    normalized_target_price,
    normalized_stop_price,
    entry_price,
    target_price,
    stop_price,
    horizon_days,
    visible_bar_count,
    visible_start_date,
    visible_end_date,
    available_forward_days,
    forward_start_date,
    forward_end_date,
    return_pct,
    max_favorable_excursion_pct,
    max_adverse_excursion_pct,
    target_hit,
    target_hit_date,
    stop_hit,
    stop_hit_date
"""

_PLAN_MUTABLE_FIELDS = (
    "hypothesis",
    "trigger_condition",
    "invalidation_condition",
    "target_price",
    "stop_price",
    "horizon_days",
    "evidence_refs",
)

_INVALID_PROVENANCE_VERSIONS = {"", "unknown", "legacy"}


@dataclass(frozen=True)
class _EvaluationResultValues:
    status: str
    conclusion: str
    anchor_evaluation_close: float | None
    price_scale_factor: float | None
    normalized_entry_price: float | None
    normalized_target_price: float | None
    normalized_stop_price: float | None
    visible_bar_count: int
    visible_start_date: str | None
    visible_end_date: str | None
    available_forward_days: int
    forward_start_date: str | None
    forward_end_date: str | None
    return_pct: float | None
    max_favorable_excursion_pct: float | None
    max_adverse_excursion_pct: float | None
    target_hit: bool
    target_hit_date: str | None
    stop_hit: bool
    stop_hit_date: str | None


_RESULT_INSERT_FIELDS = (
    "plan_id",
    "plan_revision",
    "advice_id",
    "symbol",
    "snapshot_market_time",
    "as_of",
    "evaluated_at",
    "status",
    "conclusion",
    "rule_version",
    "trigger_basis",
    "invalidation_basis",
    "snapshot_adjustment_mode",
    "snapshot_anchor_date",
    "snapshot_anchor_close",
    "snapshot_data_version",
    "snapshot_contract_version",
    "evaluation_adjustment_mode",
    "evaluation_data_version",
    "evaluation_contract_version",
    "anchor_evaluation_close",
    "price_scale_factor",
    "normalized_entry_price",
    "normalized_target_price",
    "normalized_stop_price",
    "entry_price",
    "target_price",
    "stop_price",
    "horizon_days",
    "visible_bar_count",
    "visible_start_date",
    "visible_end_date",
    "available_forward_days",
    "forward_start_date",
    "forward_end_date",
    "return_pct",
    "max_favorable_excursion_pct",
    "max_adverse_excursion_pct",
    "target_hit",
    "target_hit_date",
    "stop_hit",
    "stop_hit_date",
)

_PLAN_INSERT_SQL = """
    INSERT INTO advice_review_plan (
        advice_id,
        symbol,
        snapshot_market_time,
        snapshot_price,
        snapshot_adjustment_mode,
        snapshot_anchor_date,
        snapshot_anchor_close,
        snapshot_data_version,
        snapshot_contract_version,
        hypothesis,
        trigger_condition,
        invalidation_condition,
        trigger_basis,
        invalidation_basis,
        target_price,
        stop_price,
        horizon_days,
        evidence_refs_json,
        revision,
        created_at,
        updated_at
    ) VALUES (
        :advice_id,
        :symbol,
        :snapshot_market_time,
        :snapshot_price,
        :snapshot_adjustment_mode,
        :snapshot_anchor_date,
        :snapshot_anchor_close,
        :snapshot_data_version,
        :snapshot_contract_version,
        :hypothesis,
        :trigger_condition,
        :invalidation_condition,
        :trigger_basis,
        :invalidation_basis,
        :target_price,
        :stop_price,
        :horizon_days,
        :evidence_refs_json,
        1,
        :created_at,
        :updated_at
    )
"""

_EVALUATION_CANDIDATE_PLAN_SQL = f"""
    SELECT {_PLAN_SELECT_COLUMNS}
    FROM advice_review_plan
    WHERE id IN (
        WITH ranked_results AS (
            SELECT
                result.*,
                ROW_NUMBER() OVER (
                    PARTITION BY result.plan_id, result.plan_revision
                    ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(result.evaluated_at) DESC,
                             result.id DESC
                ) AS result_rank
            FROM advice_review_result result
        )
        SELECT plan.id
        FROM advice_review_plan plan
        LEFT JOIN ranked_results result
          ON result.plan_id = plan.id
         AND result.plan_revision = plan.revision
         AND result.result_rank = 1
        WHERE (result.id IS NULL OR result.status != 'evaluated')
          AND date(
                plan.snapshot_market_time,
                '+' || plan.horizon_days || ' days'
              ) <= date(?)
        ORDER BY date(
                     plan.snapshot_market_time,
                     '+' || plan.horizon_days || ' days'
                 ) ASC,
                 plan.snapshot_market_time ASC,
                 plan.id ASC
        LIMIT ?
    )
    ORDER BY date(
                 snapshot_market_time,
                 '+' || horizon_days || ' days'
             ) ASC,
             snapshot_market_time ASC,
             id ASC
"""


class AdviceReviewRepository(SQLiteRepository):
    def create_plan(self, payload: AdviceReviewPlanInput) -> AdviceReviewPlan:
        normalized_symbol = standard_symbol(payload.symbol)
        timestamp = now_text()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            snapshot = _required_advice_snapshot(conn, payload.advice_id)
            _validate_snapshot_binding(snapshot, normalized_symbol, payload)
            cursor = _insert_plan(conn, _plan_insert_params(snapshot, payload, timestamp))
            row = _plan_row(conn, int(cursor.lastrowid))
        return _required_plan_from_row(row, "研究计划创建失败")

    def plan(self, plan_id: int) -> AdviceReviewPlan | None:
        with self._lock, self._connect() as conn:
            row = _plan_row(conn, plan_id)
        return _plan_from_row(row) if row else None

    def plan_by_advice(self, advice_id: int) -> AdviceReviewPlan | None:
        with self._lock, self._connect() as conn:
            row = _plan_row_by_advice(conn, advice_id)
        return _plan_from_row(row) if row else None

    def plans(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AdviceReviewPlan]:
        if limit <= 0:
            return []
        offset = max(0, offset)
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(standard_symbol(symbol))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_PLAN_SELECT_COLUMNS}
                FROM advice_review_plan
                {where_sql}
                ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(updated_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [_plan_from_row(row) for row in rows]

    def details(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AdviceReviewDetail]:
        if limit <= 0:
            return []
        offset = max(0, offset)
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(standard_symbol(symbol))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as conn:
            plan_rows = conn.execute(
                f"""
                SELECT {_PLAN_SELECT_COLUMNS}
                FROM advice_review_plan
                {where_sql}
                ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(updated_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            plans = [_plan_from_row(row) for row in plan_rows]
            result_rows = _latest_result_rows(conn, plans)
        return _review_details(plans, result_rows)

    def evaluation_candidates(
        self,
        *,
        as_of_date: str,
        limit: int,
    ) -> list[AdviceReviewDetail]:
        if limit <= 0:
            return []
        cutoff = _strict_iso_date(as_of_date)
        with self._lock, self._connect() as conn:
            plan_rows = conn.execute(_EVALUATION_CANDIDATE_PLAN_SQL, (cutoff, limit)).fetchall()
            plans = [_plan_from_row(row) for row in plan_rows]
            result_rows = _latest_result_rows(conn, plans)
        return _review_details(plans, result_rows)

    def update_plan(self, plan_id: int, payload: AdviceReviewPlanUpdate) -> AdviceReviewPlan | None:
        requested = {field for field in payload.model_fields_set if field in _PLAN_MUTABLE_FIELDS}
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = _plan_row(conn, plan_id)
            if row is None:
                return None
            current = _plan_from_row(row)
            updates = _normalized_plan_updates(current, payload, requested)
            if not updates:
                return current
            merged = current.model_copy(update=updates)
            _validate_plan_prices(merged.target_price, current.snapshot_price, merged.stop_price)
            assignments: list[str] = []
            params: list[object] = []
            for field, value in updates.items():
                column = "evidence_refs_json" if field == "evidence_refs" else field
                assignments.append(f"{column} = ?")
                params.append(_evidence_refs_json(value) if field == "evidence_refs" else value)
            assignments.extend(("revision = revision + 1", "updated_at = ?"))
            params.extend((now_text(), plan_id))
            conn.execute(
                f"UPDATE advice_review_plan SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            updated_row = _plan_row(conn, plan_id)
        return _required_plan_from_row(updated_row, "研究计划更新失败")

    def delete_plan(self, plan_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM advice_review_plan WHERE id = ?", (plan_id,))
            return cursor.rowcount > 0

    def detail(self, plan_id: int) -> AdviceReviewDetail | None:
        with self._lock, self._connect() as conn:
            plan_row = _plan_row(conn, plan_id)
            if plan_row is None:
                return None
            plan = _plan_from_row(plan_row)
            result_row = _latest_result_row(conn, plan.id, plan.revision)
        return AdviceReviewDetail(
            plan=plan,
            latest_evaluation=_evaluation_from_row(result_row) if result_row else None,
        )

    def evaluation(self, evaluation_id: int) -> AdviceReviewEvaluation | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT {_RESULT_SELECT_COLUMNS} FROM advice_review_result WHERE id = ?",
                (evaluation_id,),
            ).fetchone()
        return _evaluation_from_row(row) if row else None

    def evaluation_history(self, plan_id: int, limit: int = 100) -> list[AdviceReviewEvaluation]:
        if limit <= 0:
            return []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_RESULT_SELECT_COLUMNS}
                FROM advice_review_result
                WHERE plan_id = ?
                ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(evaluated_at) DESC, id DESC
                LIMIT ?
                """,
                (plan_id, limit),
            ).fetchall()
        return [_evaluation_from_row(row) for row in rows]

    def save_evaluation(self, evaluation: AdviceReviewEvaluationDraft) -> AdviceReviewEvaluation:
        params = _evaluation_insert_values(evaluation)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            plan_row = _plan_row(conn, evaluation.plan_id)
            if plan_row is None:
                raise NotFoundError("研究计划不存在")
            plan = _plan_from_row(plan_row)
            _validate_evaluation_binding(plan, evaluation)
            conn.execute(_evaluation_upsert_sql(), params)
            row = conn.execute(
                f"""
                SELECT {_RESULT_SELECT_COLUMNS}
                FROM advice_review_result
                WHERE plan_id = ? AND plan_revision = ? AND as_of = ? AND rule_version = ?
                """,
                (
                    evaluation.plan_id,
                    evaluation.plan_revision,
                    evaluation.as_of,
                    evaluation.rule_version,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("建议复盘结果保存失败")
        return _evaluation_from_row(row)

    def summary(self) -> AdviceReviewSummary:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                WITH ranked_results AS (
                    SELECT
                        result.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY result.plan_id, result.plan_revision
                            ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(result.evaluated_at) DESC, result.id DESC
                        ) AS result_rank
                    FROM advice_review_result result
                    JOIN advice_review_plan plan
                      ON plan.id = result.plan_id
                     AND plan.revision = result.plan_revision
                )
                SELECT
                    plan.id AS plan_id,
                    result.id AS result_id,
                    result.status,
                    result.conclusion,
                    result.return_pct,
                    result.max_favorable_excursion_pct,
                    result.max_adverse_excursion_pct,
                    result.target_hit,
                    result.stop_hit,
                    result.snapshot_adjustment_mode,
                    result.snapshot_anchor_date,
                    result.snapshot_anchor_close,
                    result.snapshot_data_version,
                    result.snapshot_contract_version
                FROM advice_review_plan plan
                LEFT JOIN ranked_results result
                  ON result.plan_id = plan.id
                 AND result.plan_revision = plan.revision
                 AND result.result_rank = 1
                ORDER BY plan.id ASC
                """
            ).fetchall()
        return _review_summary_from_rows(rows)


def _validate_snapshot_binding(
    snapshot: AdviceSnapshotRef,
    normalized_symbol: str,
    payload: AdviceReviewPlanInput,
) -> None:
    if snapshot.symbol != normalized_symbol:
        raise ValueError("研究计划 symbol 必须与 advice snapshot 一致")
    if not _has_verifiable_snapshot_provenance(
        adjustment_mode=snapshot.adjustment_mode,
        anchor_date=snapshot.anchor_date,
        anchor_close=snapshot.anchor_close,
        data_version=snapshot.data_version,
        contract_version=snapshot.contract_version,
    ):
        raise ValueError("advice snapshot 缺少可复现的 qfq 价格基准，不能建立复盘计划")
    if snapshot.advice_contract_version in _INVALID_PROVENANCE_VERSIONS:
        raise ValueError("advice snapshot 合同版本无效，不能建立复盘计划")
    if snapshot.rule_version in _INVALID_PROVENANCE_VERSIONS:
        raise ValueError("advice snapshot 规则版本无效，不能建立复盘计划")
    _validate_plan_prices(payload.target_price, snapshot.price, payload.stop_price)


def _plan_insert_params(
    snapshot: AdviceSnapshotRef,
    payload: AdviceReviewPlanInput,
    timestamp: str,
) -> dict[str, object]:
    evidence_refs = _merged_evidence_refs(
        structured_advice_evidence_refs(snapshot),
        payload.evidence_refs,
    )
    return {
        "advice_id": snapshot.advice_id,
        "symbol": snapshot.symbol,
        "snapshot_market_time": snapshot.market_time,
        "snapshot_price": snapshot.price,
        "snapshot_adjustment_mode": snapshot.adjustment_mode,
        "snapshot_anchor_date": snapshot.anchor_date,
        "snapshot_anchor_close": snapshot.anchor_close,
        "snapshot_data_version": snapshot.data_version,
        "snapshot_contract_version": snapshot.contract_version,
        "hypothesis": payload.hypothesis,
        "trigger_condition": payload.trigger_condition,
        "invalidation_condition": payload.invalidation_condition,
        "trigger_basis": payload.trigger_basis,
        "invalidation_basis": payload.invalidation_basis,
        "target_price": float(payload.target_price),
        "stop_price": float(payload.stop_price),
        "horizon_days": payload.horizon_days,
        "evidence_refs_json": _evidence_refs_json(evidence_refs),
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _insert_plan(conn: sqlite3.Connection, params: dict[str, object]) -> sqlite3.Cursor:
    try:
        return conn.execute(_PLAN_INSERT_SQL, params)
    except sqlite3.IntegrityError as exc:
        advice_id = int(params["advice_id"])
        if _plan_row_by_advice(conn, advice_id) is not None:
            raise ValueError("该 advice snapshot 已存在研究计划") from exc
        raise


def _required_advice_snapshot(conn: sqlite3.Connection, advice_id: int) -> AdviceSnapshotRef:
    row = _required_advice_snapshot_row(conn, advice_id)
    market_time = normalize_market_datetime(row["market_time"])
    if market_time is None:
        raise ValueError("advice snapshot 缺少有效 market_time，不能建立无前视复盘")
    return _advice_snapshot_from_row(
        row,
        symbol=_snapshot_symbol(row["symbol"]),
        market_time=market_time,
        price=_positive_finite_float(row["price"], "advice snapshot 价格无效"),
    )


def _required_advice_snapshot_row(conn: sqlite3.Connection, advice_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, symbol, market_time, price,
               action, trend_score, risk_level, support, resistance, data_quality_score,
               snapshot_contract_version, rule_version,
               kline_adjustment_mode, kline_anchor_date, kline_anchor_close,
               kline_data_version, kline_contract_version
        FROM advice_history
        WHERE id = ?
        """,
        (advice_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("advice snapshot 不存在")
    return row


def _snapshot_symbol(value: object) -> str:
    try:
        return standard_symbol(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("advice snapshot 的 symbol 无效") from exc


def _advice_snapshot_from_row(
    row: sqlite3.Row,
    *,
    symbol: str,
    market_time: str,
    price: float,
) -> AdviceSnapshotRef:
    return AdviceSnapshotRef(
        advice_id=int(row["id"]),
        symbol=symbol,
        market_time=market_time,
        price=price,
        adjustment_mode=_snapshot_text(row["kline_adjustment_mode"], "unknown"),
        anchor_date=row["kline_anchor_date"],
        anchor_close=_optional_float(row["kline_anchor_close"]),
        data_version=_snapshot_text(row["kline_data_version"], "unknown"),
        contract_version=_snapshot_text(row["kline_contract_version"], "unknown"),
        advice_contract_version=_snapshot_text(row["snapshot_contract_version"], "legacy"),
        rule_version=_snapshot_text(row["rule_version"], "unknown"),
        action=_snapshot_optional_text(row["action"]),
        trend_score=_optional_int(row["trend_score"]),
        risk_level=_snapshot_optional_text(row["risk_level"]),
        support=_optional_float(row["support"]),
        resistance=_optional_float(row["resistance"]),
        data_quality_score=_optional_int(row["data_quality_score"]),
    )


def _snapshot_text(value: object, fallback: str) -> str:
    return str(value or fallback)


def _snapshot_optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _plan_row(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_PLAN_SELECT_COLUMNS} FROM advice_review_plan WHERE id = ?",
        (plan_id,),
    ).fetchone()


def _plan_row_by_advice(conn: sqlite3.Connection, advice_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_PLAN_SELECT_COLUMNS} FROM advice_review_plan WHERE advice_id = ?",
        (advice_id,),
    ).fetchone()


def _latest_result_row(conn: sqlite3.Connection, plan_id: int, revision: int) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT {_RESULT_SELECT_COLUMNS}
        FROM advice_review_result
        WHERE plan_id = ? AND plan_revision = ?
        ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(evaluated_at) DESC, id DESC
        LIMIT 1
        """,
        (plan_id, revision),
    ).fetchone()


def _latest_result_rows(
    conn: sqlite3.Connection,
    plans: list[AdviceReviewPlan],
) -> list[sqlite3.Row]:
    if not plans:
        return []
    placeholders = ", ".join("?" for _ in plans)
    plan_ids = [plan.id for plan in plans]
    rows = conn.execute(
        f"""
        SELECT {_RESULT_SELECT_COLUMNS}
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY plan_id, plan_revision
                       ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(evaluated_at) DESC, id DESC
                   ) AS result_rank
            FROM advice_review_result
            WHERE plan_id IN ({placeholders})
        )
        WHERE result_rank = 1
        """,
        plan_ids,
    ).fetchall()
    current_revisions = {(plan.id, plan.revision) for plan in plans}
    return [row for row in rows if (int(row["plan_id"]), int(row["plan_revision"])) in current_revisions]


def _review_details(
    plans: list[AdviceReviewPlan],
    result_rows: list[sqlite3.Row],
) -> list[AdviceReviewDetail]:
    results = {(int(row["plan_id"]), int(row["plan_revision"])): _evaluation_from_row(row) for row in result_rows}
    return [
        AdviceReviewDetail(
            plan=plan,
            latest_evaluation=results.get((plan.id, plan.revision)),
        )
        for plan in plans
    ]


def _plan_from_row(row: sqlite3.Row) -> AdviceReviewPlan:
    return AdviceReviewPlan(
        id=int(row["id"]),
        advice_id=int(row["advice_id"]),
        symbol=str(row["symbol"]),
        snapshot_market_time=str(row["snapshot_market_time"]),
        snapshot_price=float(row["snapshot_price"]),
        snapshot_adjustment_mode=str(row["snapshot_adjustment_mode"] or "unknown"),
        snapshot_anchor_date=row["snapshot_anchor_date"],
        snapshot_anchor_close=_optional_float(row["snapshot_anchor_close"]),
        snapshot_data_version=str(row["snapshot_data_version"] or "unknown"),
        snapshot_contract_version=str(row["snapshot_contract_version"] or "unknown"),
        hypothesis=str(row["hypothesis"]),
        trigger_condition=str(row["trigger_condition"]),
        invalidation_condition=str(row["invalidation_condition"]),
        trigger_basis=str(row["trigger_basis"]),
        invalidation_basis=str(row["invalidation_basis"]),
        target_price=float(row["target_price"]),
        stop_price=float(row["stop_price"]),
        horizon_days=int(row["horizon_days"]),
        evidence_refs=_evidence_refs_from_json(row["evidence_refs_json"]),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _required_plan_from_row(row: sqlite3.Row | None, message: str) -> AdviceReviewPlan:
    if row is None:
        raise RuntimeError(message)
    return _plan_from_row(row)


def _evaluation_from_row(row: sqlite3.Row) -> AdviceReviewEvaluation:
    snapshot_adjustment_mode = str(row["snapshot_adjustment_mode"] or "unknown")
    snapshot_anchor_date = row["snapshot_anchor_date"]
    snapshot_anchor_close = _optional_float(row["snapshot_anchor_close"])
    snapshot_data_version = str(row["snapshot_data_version"] or "unknown")
    snapshot_contract_version = str(row["snapshot_contract_version"] or "unknown")
    result_values = _evaluation_result_values(row)
    return AdviceReviewEvaluation(
        id=int(row["id"]),
        plan_id=int(row["plan_id"]),
        plan_revision=int(row["plan_revision"]),
        advice_id=int(row["advice_id"]),
        symbol=str(row["symbol"]),
        snapshot_market_time=str(row["snapshot_market_time"]),
        as_of=str(row["as_of"]),
        evaluated_at=str(row["evaluated_at"]),
        status=result_values.status,
        conclusion=result_values.conclusion,
        rule_version=str(row["rule_version"]),
        trigger_basis=str(row["trigger_basis"]),
        invalidation_basis=str(row["invalidation_basis"]),
        snapshot_adjustment_mode=snapshot_adjustment_mode,
        snapshot_anchor_date=snapshot_anchor_date,
        snapshot_anchor_close=snapshot_anchor_close,
        snapshot_data_version=snapshot_data_version,
        snapshot_contract_version=snapshot_contract_version,
        evaluation_adjustment_mode=str(row["evaluation_adjustment_mode"] or "unknown"),
        evaluation_data_version=str(row["evaluation_data_version"] or "unknown"),
        evaluation_contract_version=str(row["evaluation_contract_version"] or "unknown"),
        anchor_evaluation_close=result_values.anchor_evaluation_close,
        price_scale_factor=result_values.price_scale_factor,
        normalized_entry_price=result_values.normalized_entry_price,
        normalized_target_price=result_values.normalized_target_price,
        normalized_stop_price=result_values.normalized_stop_price,
        entry_price=float(row["entry_price"]),
        target_price=float(row["target_price"]),
        stop_price=float(row["stop_price"]),
        horizon_days=int(row["horizon_days"]),
        visible_bar_count=result_values.visible_bar_count,
        visible_start_date=result_values.visible_start_date,
        visible_end_date=result_values.visible_end_date,
        available_forward_days=result_values.available_forward_days,
        forward_start_date=result_values.forward_start_date,
        forward_end_date=result_values.forward_end_date,
        return_pct=result_values.return_pct,
        max_favorable_excursion_pct=result_values.max_favorable_excursion_pct,
        max_adverse_excursion_pct=result_values.max_adverse_excursion_pct,
        target_hit=result_values.target_hit,
        target_hit_date=result_values.target_hit_date,
        stop_hit=result_values.stop_hit,
        stop_hit_date=result_values.stop_hit_date,
    )


def _evaluation_result_values(row: sqlite3.Row) -> _EvaluationResultValues:
    if _evaluation_snapshot_is_verifiable(row):
        return _stored_evaluation_result_values(row)
    return _legacy_sanitized_result_values()


def _stored_evaluation_result_values(row: sqlite3.Row) -> _EvaluationResultValues:
    return _EvaluationResultValues(
        status=str(row["status"]),
        conclusion=str(row["conclusion"]),
        anchor_evaluation_close=_optional_float(row["anchor_evaluation_close"]),
        price_scale_factor=_optional_float(row["price_scale_factor"]),
        normalized_entry_price=_optional_float(row["normalized_entry_price"]),
        normalized_target_price=_optional_float(row["normalized_target_price"]),
        normalized_stop_price=_optional_float(row["normalized_stop_price"]),
        visible_bar_count=int(row["visible_bar_count"]),
        visible_start_date=row["visible_start_date"],
        visible_end_date=row["visible_end_date"],
        available_forward_days=int(row["available_forward_days"]),
        forward_start_date=row["forward_start_date"],
        forward_end_date=row["forward_end_date"],
        return_pct=_optional_float(row["return_pct"]),
        max_favorable_excursion_pct=_optional_float(row["max_favorable_excursion_pct"]),
        max_adverse_excursion_pct=_optional_float(row["max_adverse_excursion_pct"]),
        target_hit=bool(row["target_hit"]),
        target_hit_date=row["target_hit_date"],
        stop_hit=bool(row["stop_hit"]),
        stop_hit_date=row["stop_hit_date"],
    )


def _legacy_sanitized_result_values() -> _EvaluationResultValues:
    return _EvaluationResultValues(
        status="insufficient",
        conclusion="insufficient_data",
        anchor_evaluation_close=None,
        price_scale_factor=None,
        normalized_entry_price=None,
        normalized_target_price=None,
        normalized_stop_price=None,
        visible_bar_count=0,
        visible_start_date=None,
        visible_end_date=None,
        available_forward_days=0,
        forward_start_date=None,
        forward_end_date=None,
        return_pct=None,
        max_favorable_excursion_pct=None,
        max_adverse_excursion_pct=None,
        target_hit=False,
        target_hit_date=None,
        stop_hit=False,
        stop_hit_date=None,
    )


def _evaluation_snapshot_is_verifiable(row: sqlite3.Row) -> bool:
    return _has_verifiable_snapshot_provenance(
        adjustment_mode=str(row["snapshot_adjustment_mode"] or "unknown"),
        anchor_date=row["snapshot_anchor_date"],
        anchor_close=_optional_float(row["snapshot_anchor_close"]),
        data_version=str(row["snapshot_data_version"] or "unknown"),
        contract_version=str(row["snapshot_contract_version"] or "unknown"),
    )


def _has_verifiable_snapshot_provenance(
    *,
    adjustment_mode: str,
    anchor_date: object,
    anchor_close: float | None,
    data_version: str,
    contract_version: str,
) -> bool:
    return (
        adjustment_mode == "qfq"
        and bool(anchor_date)
        and anchor_close is not None
        and math.isfinite(anchor_close)
        and anchor_close > 0
        and data_version not in _INVALID_PROVENANCE_VERSIONS
        and contract_version not in _INVALID_PROVENANCE_VERSIONS
    )


def _normalized_plan_updates(
    current: AdviceReviewPlan,
    payload: AdviceReviewPlanUpdate,
    requested: set[str],
) -> dict[str, object]:
    updates: dict[str, object] = {}
    for field in requested:
        value = getattr(payload, field)
        if value is None:
            continue
        normalized: object = list(value) if field == "evidence_refs" else value
        if normalized != getattr(current, field):
            updates[field] = normalized
    return updates


def _validate_plan_prices(target_price: float, snapshot_price: float, stop_price: float) -> None:
    target = _positive_finite_float(target_price, "目标价必须是大于0的有效数字")
    entry = _positive_finite_float(snapshot_price, "advice snapshot 价格无效")
    stop = _positive_finite_float(stop_price, "止损价必须是大于0的有效数字")
    if not target > entry > stop:
        raise ValueError("价格关系必须满足 target_price > advice snapshot price > stop_price")


def _validate_evaluation_binding(plan: AdviceReviewPlan, evaluation: AdviceReviewEvaluationDraft) -> None:
    if plan.revision != evaluation.plan_revision:
        raise ValueError("研究计划已更新，请基于最新 revision 重新评估")
    expected = (
        plan.advice_id,
        plan.symbol,
        plan.snapshot_market_time,
        plan.snapshot_price,
        plan.snapshot_adjustment_mode,
        plan.snapshot_anchor_date,
        plan.snapshot_anchor_close,
        plan.snapshot_data_version,
        plan.snapshot_contract_version,
        plan.trigger_basis,
        plan.invalidation_basis,
        plan.target_price,
        plan.stop_price,
        plan.horizon_days,
    )
    observed = (
        evaluation.advice_id,
        evaluation.symbol,
        evaluation.snapshot_market_time,
        evaluation.entry_price,
        evaluation.snapshot_adjustment_mode,
        evaluation.snapshot_anchor_date,
        evaluation.snapshot_anchor_close,
        evaluation.snapshot_data_version,
        evaluation.snapshot_contract_version,
        evaluation.trigger_basis,
        evaluation.invalidation_basis,
        evaluation.target_price,
        evaluation.stop_price,
        evaluation.horizon_days,
    )
    if observed != expected:
        raise ValueError("复盘结果与研究计划绑定信息不一致")


def _evaluation_insert_values(evaluation: AdviceReviewEvaluationDraft) -> dict[str, object | None]:
    values = evaluation.model_dump()
    values["target_hit"] = int(evaluation.target_hit)
    values["stop_hit"] = int(evaluation.stop_hit)
    return {field: values[field] for field in _RESULT_INSERT_FIELDS}


def _evaluation_upsert_sql() -> str:
    columns = ", ".join(_RESULT_INSERT_FIELDS)
    placeholders = ", ".join(f":{field}" for field in _RESULT_INSERT_FIELDS)
    update_fields = tuple(field for field in _RESULT_INSERT_FIELDS if field not in {"plan_id", "plan_revision", "as_of", "rule_version"})
    assignments = ", ".join(f"{field} = excluded.{field}" for field in update_fields)
    return f"""
        INSERT INTO advice_review_result ({columns})
        VALUES ({placeholders})
        ON CONFLICT(plan_id, plan_revision, as_of, rule_version)
        DO UPDATE SET {assignments}
    """


def _positive_finite_float(value: object, message: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(message)
    return parsed


def _strict_iso_date(value: object) -> str:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("as_of_date 必须是 YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError("as_of_date 必须是 YYYY-MM-DD")
    return text


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed


def _evidence_refs_json(values: object) -> str:
    serialized = [item.model_dump(mode="json") if isinstance(item, AdviceEvidenceRef) else item for item in list(values)]
    return json.dumps(serialized, ensure_ascii=False, separators=(",", ":"))


def _merged_evidence_refs(
    automatic: list[AdviceEvidenceRef],
    custom: list[AdviceEvidenceRef | str],
) -> list[AdviceEvidenceRef | str]:
    merged: list[AdviceEvidenceRef | str] = []
    seen: set[tuple[str, str]] = set()
    for item in [*automatic, *custom]:
        key = ("structured", item.id) if isinstance(item, AdviceEvidenceRef) else ("text", item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _evidence_refs_from_json(value: object) -> list[AdviceEvidenceRef | str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    refs: list[AdviceEvidenceRef | str] = []
    for item in parsed:
        if isinstance(item, str):
            refs.append(item)
            continue
        if not isinstance(item, dict):
            continue
        try:
            refs.append(AdviceEvidenceRef.model_validate(item))
        except ValueError:
            continue
    return refs


def _review_summary_from_rows(rows: list[sqlite3.Row]) -> AdviceReviewSummary:
    conclusion_counts: dict[str, int] = {}
    pending_count = 0
    insufficient_count = 0
    evaluated_count = 0
    return_values: list[float] = []
    mfe_values: list[float] = []
    mae_values: list[float] = []
    for row in rows:
        has_result = row["result_id"] is not None
        verifiable = has_result and _evaluation_snapshot_is_verifiable(row)
        status = str(row["status"] or "pending") if verifiable else ("insufficient" if has_result else "pending")
        conclusion = str(row["conclusion"] or "pending") if verifiable else ("insufficient_data" if has_result else "pending")
        conclusion_counts[conclusion] = conclusion_counts.get(conclusion, 0) + 1
        pending_count += int(status == "pending")
        insufficient_count += int(status == "insufficient")
        evaluated_count += int(status == "evaluated")
        if status == "evaluated":
            _append_finite(return_values, row["return_pct"])
            _append_finite(mfe_values, row["max_favorable_excursion_pct"])
            _append_finite(mae_values, row["max_adverse_excursion_pct"])
    favorable_count = sum(conclusion_counts.get(item, 0) for item in ("target_hit", "horizon_gain"))
    unfavorable_count = sum(conclusion_counts.get(item, 0) for item in ("stop_hit", "horizon_loss"))
    decided_count = favorable_count + unfavorable_count
    return AdviceReviewSummary(
        generated_at=now_text(),
        total_plan_count=len(rows),
        pending_count=pending_count,
        insufficient_count=insufficient_count,
        evaluated_count=evaluated_count,
        favorable_count=favorable_count,
        unfavorable_count=unfavorable_count,
        ambiguous_count=conclusion_counts.get("target_stop_ambiguous", 0),
        target_hit_count=conclusion_counts.get("target_hit", 0),
        stop_hit_count=conclusion_counts.get("stop_hit", 0),
        favorable_rate_pct=round(favorable_count / decided_count * 100, 2) if decided_count else None,
        average_return_pct=_average_or_none(return_values),
        average_mfe_pct=_average_or_none(mfe_values),
        average_mae_pct=_average_or_none(mae_values),
        conclusion_counts=dict(sorted(conclusion_counts.items())),
    )


def _append_finite(values: list[float], value: object) -> None:
    parsed = _optional_float(value)
    if parsed is not None:
        values.append(parsed)


def _average_or_none(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


__all__ = [
    "ADVICE_REVIEW_INDEX_SQL",
    "ADVICE_REVIEW_PLAN_TABLE_SQL",
    "ADVICE_REVIEW_RESULT_TABLE_SQL",
    "ADVICE_REVIEW_SCHEMA_SQL",
    "ADVICE_REVIEW_SCHEMA_VERSION",
    "AdviceReviewRepository",
]
