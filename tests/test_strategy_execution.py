from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.api.routes import strategy_lab
from app.artifacts.io import canonical_json_text, sha256_hex
from app.db.market_scan_integrity import market_scan_snapshot_digest
from app.models.market_scan import MarketScanResultItem, MarketScanSeed
from app.models.market_scan_executable_shadow import (
    ExecutableCandidateShadowReport,
    ExecutableShadowRunEvidence,
    executable_candidate_shadow_digest,
)
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
from app.services import market_scan_executable_shadow as executable_shadow
from app.services.market_scan_scoring import score_market_scan_item
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.strategy_execution import StrategyExecutionService
from app.services.market_scan_executable_shadow import executable_candidate_shadow_spec
from app.services.strategy_portfolio import strategy_board
from app.repositories.strategy_execution import StrategyExecutionIntegrityError
from tests.market_scan_test_support import (
    SCAN_AS_OF,
    SCAN_DATA_DATE,
    _daily_rows,
    _quote_for,
    action_pass_publication_diagnostics,
    distribution_degraded_publication_diagnostics,
)


def test_strategy_execution_rejects_preopen_review_cohort() -> None:
    with pytest.raises(ValueError):
        StrategyExecutionRequest(strategy_id=1, mode="preopen")  # type: ignore[arg-type]


def test_strategy_repository_rejects_direct_preopen_access(tmp_path) -> None:
    cache, _service, _strategy_id, _run_id = _environment(tmp_path)

    with pytest.raises(ValueError, match="不接受盘前复盘"):
        cache.strategy_execution_repo.frozen_scan(
            run_id=None,
            data_date=None,
            mode="preopen",  # type: ignore[arg-type]
        )


def test_strategy_repository_rejects_published_partial_scope(tmp_path) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - test-only cohort mutation
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_run SET scope = ? WHERE id = ?",
            ("自定义部分股票池", run_id),
        )
        _reseal_market_scan_snapshot(conn, run_id)

    with pytest.raises(ValueError, match="不是完整全市场"):
        cache.strategy_execution_repo.frozen_scan(
            run_id=run_id,
            data_date=None,
            mode="official",
        )


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
    assert draft.context.source_snapshot_digest == cache.market_scan_run(run_id).snapshot_digest
    assert draft.context.source_snapshot_seal_origin == "publication"
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
    assert {item.symbol for item in first_page.items}.isdisjoint(item.symbol for item in second_page.items)
    assert cache.strategy_execution_service is service


def test_distribution_degraded_snapshot_is_readable_but_cannot_create_execution(
    tmp_path,
) -> None:
    cache, service, strategy_id, run_id = _environment(
        tmp_path,
        action_eligible=False,
    )

    frozen = cache.strategy_execution_repo.frozen_scan(
        run_id=run_id,
        data_date=None,
        mode="official",
    )
    assert frozen.run.id == run_id
    with pytest.raises(StrategyExecutionIntegrityError, match="冻结快照校验失败"):
        service.execute(
            StrategyExecutionRequest(
                strategy_id=strategy_id,
                run_id=run_id,
                kind="historical_replay",
            )
        )
    with cache._connect() as conn:  # noqa: SLF001 - fail-closed write assertion
        assert conn.execute("SELECT COUNT(*) FROM strategy_execution").fetchone()[0] == 0


def test_strategy_execution_rows_are_append_only_and_digest_verified(tmp_path) -> None:
    cache, service, strategy_id, _run_id = _environment(tmp_path)
    draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id))

    with cache._connect() as conn:  # noqa: SLF001 - adversarial ledger mutation
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE strategy_execution SET result_digest = ? WHERE id = ?",
                ("f" * 64, draft.context.execution_id),
            )
        conn.execute("DROP TRIGGER trg_strategy_execution_no_update")
        conn.execute(
            "UPDATE strategy_execution SET result_digest = ? WHERE id = ?",
            ("f" * 64, draft.context.execution_id),
        )

    with pytest.raises(StrategyExecutionIntegrityError, match="输出摘要校验失败"):
        service.draft(draft.context.execution_id)
    with pytest.raises(StrategyExecutionIntegrityError, match="输出摘要校验失败"):
        service.executions(strategy_id=strategy_id, page=1, page_size=20)


def test_runtime_cleanup_preserves_published_source_of_immutable_execution(tmp_path) -> None:
    cache, service, strategy_id, run_id = _environment(tmp_path)
    draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    cache.maintenance_repo.settings = cache.maintenance_repo.settings.model_copy(
        update={"max_market_scan_runs": 1}
    )
    newer = cache.create_market_scan_run(
        trigger="manual",
        rule_version="cleanup-overflow-test",
        as_of="2026-07-18 16:30:00",
        data_date="2026-07-18",
        scope="test",
    )
    cache.start_market_scan_run(newer.id)
    cache.finish_market_scan_run(newer.id, "failed", message="test")

    removed = cache.cleanup_runtime_rows()

    assert removed["market_scan_run"] == 0
    assert cache.market_scan_run(run_id).id == run_id
    assert service.draft(draft.context.execution_id).context.source_snapshot_digest == (
        draft.context.source_snapshot_digest
    )


def test_strategy_execution_candidate_tamper_is_rejected_before_read(tmp_path) -> None:
    cache, service, strategy_id, _run_id = _environment(tmp_path)
    draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id))

    with cache._connect() as conn:  # noqa: SLF001 - privileged attacker regression
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM strategy_execution_candidate WHERE execution_id = ?",
                (draft.context.execution_id,),
            )
        conn.execute("DROP TRIGGER trg_strategy_execution_candidate_no_update")
        row = conn.execute(
            """
            SELECT id, candidate_json FROM strategy_execution_candidate
            WHERE execution_id = ? ORDER BY id LIMIT 1
            """,
            (draft.context.execution_id,),
        ).fetchone()
        payload = json.loads(str(row["candidate_json"]))
        payload["utility_score"] = 0.0
        conn.execute(
            "UPDATE strategy_execution_candidate SET candidate_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), int(row["id"])),
        )

    with pytest.raises(StrategyExecutionIntegrityError, match="输出摘要校验失败"):
        service.candidates(
            draft.context.execution_id,
            page=1,
            page_size=20,
            status=None,
        )


def test_intraday_full_market_snapshot_is_an_executable_research_cohort(tmp_path) -> None:
    cache, service, strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - construct an intraday frozen cohort
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_run SET mode = 'intraday' WHERE id = ?",
            (run_id,),
        )
        _reseal_market_scan_snapshot(conn, run_id)
    service._market_clock = lambda: datetime(2026, 7, 17, 10, 0)  # noqa: SLF001

    draft = service.execute(
        StrategyExecutionRequest(strategy_id=strategy_id, mode="intraday")
    )

    assert cache.market_scan_run(run_id).mode == "intraday"
    assert draft.context.market_scan_run_id == run_id
    assert draft.summary.status in {"ready", "no_trade"}


def test_strategy_execution_rejects_cross_symbol_point_in_time_evidence_swap(tmp_path) -> None:
    cache, service, strategy_id, _run_id = _environment(tmp_path)
    symbols = ("600001.SH", "688001.SH")
    with cache._connect() as conn:  # noqa: SLF001 - adversarial frozen fixture mutation
        _disable_market_scan_immutability(conn)
        payloads = {
            symbol: json.loads(
                str(
                    conn.execute(
                        "SELECT metrics_json FROM market_scan_result WHERE symbol = ?",
                        (symbol,),
                    ).fetchone()["metrics_json"]
                )
            )
            for symbol in symbols
        }
        dimensions = [
            payloads[symbol]["score_details"]["components"]["score_dimensions"]
            for symbol in symbols
        ]
        dimensions[0]["point_in_time_evidence"], dimensions[1]["point_in_time_evidence"] = (
            dimensions[1]["point_in_time_evidence"],
            dimensions[0]["point_in_time_evidence"],
        )
        for symbol in symbols:
            conn.execute(
                "UPDATE market_scan_result SET metrics_json = ? WHERE symbol = ?",
                (json.dumps(payloads[symbol], ensure_ascii=False), symbol),
            )
        _reseal_market_scan_snapshot(conn, _run_id)

    draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    swapped = [item for item in draft.candidate_preview if item.symbol in symbols]

    assert len(swapped) == 2
    assert all(item.evidence_verified is False for item in swapped)
    assert all(item.status == "rejected" for item in swapped)
    assert all(
        any("时点证据" in failure for failure in item.hard_filter_failures)
        for item in swapped
    )


def test_constraint_rejection_refills_from_lower_utility_candidates(tmp_path) -> None:
    cache, service, strategy_id, _run_id = _environment(tmp_path)
    _set_frozen_industry(cache, "688001.SH", "银行")

    draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id))

    assert [item.symbol for item in draft.selected] == ["600001.SH", "300001.SZ"]
    assert draft.summary.selected_count == 2
    assert draft.summary.replacement_attempt_count == 1
    assert draft.summary.pool_exhausted is False
    rejected = next(item for item in draft.candidate_preview if item.symbol == "688001.SH")
    assert rejected.status == "rejected"
    assert any("行业 银行 已达到" in reason for reason in rejected.reasons)


def test_constraint_refill_reports_exhausted_pool_and_is_deterministic(tmp_path) -> None:
    cache, service, strategy_id, _run_id = _environment(tmp_path)
    for symbol in ("688001.SH", "300001.SZ", "920001.BJ"):
        _set_frozen_industry(cache, symbol, "银行")

    first = service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    second = service.execute(StrategyExecutionRequest(strategy_id=strategy_id))

    assert [item.symbol for item in first.selected] == ["600001.SH"]
    assert first.summary.replacement_attempt_count == 3
    assert first.summary.pool_exhausted is True
    assert first.summary.underinvested_reason == "候选池在约束后耗尽，仅入选 1/2 只"
    assert first.result_digest == second.result_digest


def test_executable_candidate_shadow_is_read_only_sealed_and_preserves_production_rank(
    tmp_path,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    shadow = cache.domain_services.market_scan_executable_shadow
    before_bytes = hashlib.sha256(cache.path.read_bytes()).hexdigest()
    with cache._connect() as conn:  # noqa: SLF001 - immutable regression baseline
        before_ranks = conn.execute("SELECT symbol, rank, raw_score FROM market_scan_result ORDER BY symbol").fetchall()
        before_execution_count = int(conn.execute("SELECT COUNT(*) FROM strategy_execution").fetchone()[0])

    report = shadow.project(run_id, notional_cash_cny=1_000_000.0)

    with cache._connect() as conn:  # noqa: SLF001 - immutable regression assertion
        after_ranks = conn.execute("SELECT symbol, rank, raw_score FROM market_scan_result ORDER BY symbol").fetchall()
        after_execution_count = int(conn.execute("SELECT COUNT(*) FROM strategy_execution").fetchone()[0])
    assert hashlib.sha256(cache.path.read_bytes()).hexdigest() == before_bytes
    assert [tuple(row) for row in after_ranks] == [tuple(row) for row in before_ranks]
    assert before_execution_count == after_execution_count == 0
    assert report.status == "research_shadow"
    assert report.efficacy_status == "not_generated"
    assert report.production_effect == "none"
    assert report.production_ranking_mutated is False
    assert report.database_write_performed is False
    assert report.evidence.run_id == run_id
    assert report.evidence.successful_result_count == 4
    assert report.evidence.verified_point_in_time_count == 4
    assert report.evidence.production_score_rule_version == "full-market-score-v5"
    assert report.strategy_spec == executable_candidate_shadow_spec()
    assert report.gate_policy.exclude_st is True
    assert report.gate_policy.exclude_new is True
    assert report.gate_policy.adv_evidence_status == "unavailable"
    assert report.candidate_total == 4
    assert all("生产原始排名" in item.rank_change_reason for item in report.candidate_preview)

    tampered = report.model_dump(mode="json")
    tampered["limitations"][0] = "伪造为已验证收益"
    with pytest.raises(ValidationError, match="摘要校验失败"):
        ExecutableCandidateShadowReport.model_validate(tampered)

    nested_extra = report.model_dump(mode="json")
    nested_extra["candidate_preview"][0]["untrusted_probability"] = 0.99
    nested_extra["canonical_digest"] = executable_candidate_shadow_digest(nested_extra)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExecutableCandidateShadowReport.model_validate(nested_extra)

    inconsistent = report.model_dump(mode="json")
    inconsistent["summary"]["selected_count"] += 1
    inconsistent["canonical_digest"] = executable_candidate_shadow_digest(inconsistent)
    with pytest.raises(ValidationError, match="候选状态计数|入选列表数量"):
        ExecutableCandidateShadowReport.model_validate(inconsistent)


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("preview_exceeds_total", "候选总数不能小于预览数量"),
        ("evidence_total_mismatch", "冻结结果总数一致"),
        ("evaluated_total_mismatch", "组合评估数量一致"),
        ("status_count_mismatch", "候选状态计数"),
        ("adjusted_count_exceeds_selected", "约束调整数量"),
        ("duplicate_selected", "不能包含重复股票"),
        ("rejected_in_selected", "只能包含已入选或约束调整股票"),
        ("exposure_count_mismatch", "暴露审计与入选列表数量"),
        ("verified_count_mismatch", "已验证数量必须一致"),
        ("weight_mismatch", "入选权重必须一致"),
        ("turnover_mismatch", "预计换手必须一致"),
        ("cost_mismatch", "预计往返成本必须一致"),
        ("no_trade_mismatch", "no_trade 标记"),
    ),
)
def test_executable_candidate_shadow_strict_cross_field_invariants(
    tmp_path,
    case: str,
    message: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    report = cache.domain_services.market_scan_executable_shadow.project(run_id)
    payload = report.model_dump(mode="json")

    if case == "preview_exceeds_total":
        payload["candidate_total"] = len(payload["candidate_preview"]) - 1
    elif case == "evidence_total_mismatch":
        payload["evidence"]["result_count"] += 1
    elif case == "evaluated_total_mismatch":
        payload["summary"]["evaluated_count"] += 1
    elif case == "status_count_mismatch":
        payload["summary"]["rejected_count"] += 1
    elif case == "adjusted_count_exceeds_selected":
        payload["summary"]["adjusted_count"] = payload["summary"]["selected_count"] + 1
    elif case == "duplicate_selected":
        payload["selected"][1]["symbol"] = payload["selected"][0]["symbol"]
    elif case == "rejected_in_selected":
        payload["selected"][0]["status"] = "rejected"
    elif case == "exposure_count_mismatch":
        payload["exposure_audit"]["selected_count"] -= 1
    elif case == "verified_count_mismatch":
        payload["summary"]["evidence_verified_count"] -= 1
    elif case == "weight_mismatch":
        payload["summary"]["target_invested_weight"] += 0.01
    elif case == "turnover_mismatch":
        payload["summary"]["estimated_turnover"] += 0.01
    elif case == "cost_mismatch":
        payload["summary"]["estimated_round_trip_cost_cny"] += 1.0
    elif case == "no_trade_mismatch":
        payload["summary"]["no_trade"] = True
    else:
        raise AssertionError(case)
    payload["canonical_digest"] = executable_candidate_shadow_digest(payload)

    with pytest.raises(ValidationError, match=message):
        ExecutableCandidateShadowReport.model_validate(payload)


@pytest.mark.parametrize(
    ("result_count", "successful_count", "verified_count", "message"),
    (
        (1, 2, 0, "成功结果数量不能超过"),
        (2, 1, 2, "可验证时点证据数量不能超过"),
    ),
)
def test_executable_shadow_run_evidence_rejects_impossible_counts(
    result_count: int,
    successful_count: int,
    verified_count: int,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ExecutableShadowRunEvidence(
            run_id=1,
            status="success",
            mode="official",
            scope=FULL_MARKET_SCOPE,
            data_date="2026-08-11",
            quote_date="2026-08-11",
            scan_rule_version="full-market-scan-v6",
            production_score_rule_version="full-market-score-v4",
            production_score_spec_hash="a" * 64,
            result_count=result_count,
            successful_result_count=successful_count,
            verified_point_in_time_count=verified_count,
        )


def test_executable_shadow_digest_rejects_non_model_non_mapping() -> None:
    with pytest.raises(TypeError, match="必须是模型或映射"):
        executable_candidate_shadow_digest([])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("status", "running", "只接受已发布"),
        ("mode", "intraday", "只接受盘后正式"),
        ("scope", "top100-refresh", "只接受完整全市场"),
    ),
)
def test_executable_shadow_requires_published_official_full_market_run(
    tmp_path,
    field: str,
    value: str,
    message: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    run = cache.market_scan_run(run_id).model_copy(update={field: value})

    with pytest.raises(ValueError, match=message):
        executable_shadow._require_frozen_official_full_market(run)  # noqa: SLF001


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("missing_score_contract", "缺少生产评分合同或摘要"),
        ("invalid_score_rule", "缺少生产评分规则版本"),
        ("mixed_score_contract", "不一致的生产评分合同"),
    ),
)
def test_executable_shadow_fails_closed_on_unbound_production_score_contract(
    tmp_path,
    case: str,
    message: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as connection:  # noqa: SLF001 - corrupt frozen contract fixture
        _disable_market_scan_immutability(connection)
        if case == "missing_score_contract":
            connection.execute(
                """
                UPDATE market_scan_result
                SET metrics_json = json_remove(
                    metrics_json,
                    '$.score_details.score_spec_hash'
                )
                WHERE run_id = ? AND symbol = '600001.SH'
                """,
                (run_id,),
            )
        elif case == "invalid_score_rule":
            connection.execute(
                """
                UPDATE market_scan_result
                SET metrics_json = json_set(
                    metrics_json,
                    '$.score_details.score_spec.rule_version',
                    1
                )
                WHERE run_id = ? AND symbol = '600001.SH'
                """,
                (run_id,),
            )
        else:
            connection.execute(
                """
                UPDATE market_scan_result
                SET metrics_json = json_set(
                    metrics_json,
                    '$.score_details.score_spec_hash',
                    ?
                )
                WHERE run_id = ? AND symbol = '600001.SH'
                """,
                ("c" * 64, run_id),
            )
        _reseal_market_scan_snapshot(connection, run_id)

    with pytest.raises(ValueError, match=message):
        cache.domain_services.market_scan_executable_shadow.project(run_id)


def test_executable_shadow_no_trade_has_zero_exposure_and_no_weighted_scores(
    tmp_path,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)

    report = cache.domain_services.market_scan_executable_shadow.project(
        run_id,
        notional_cash_cny=10_000.0,
    )

    assert report.summary.status == "no_trade"
    assert report.selected == []
    assert report.exposure_audit.selected_weight == 0
    assert report.exposure_audit.average_risk_score is None
    assert report.exposure_audit.average_tradability_score is None


def test_executable_candidate_shadow_route_is_no_store_and_read_only(tmp_path) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    app = FastAPI()
    app.include_router(strategy_lab.router)
    app.dependency_overrides[strategy_lab.get_market_scan_executable_shadow_service] = lambda: cache.domain_services.market_scan_executable_shadow

    response = TestClient(app).get(
        "/api/strategy-lab/executable-candidate-shadow",
        params={"run_id": run_id, "notional_cash_cny": 1_000_000},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["schema_version"] == "market-scan-executable-candidate-shadow-v2"
    assert payload["production_effect"] == "none"
    assert payload["database_write_performed"] is False

    nonfinite = TestClient(app).get(
        "/api/strategy-lab/executable-candidate-shadow",
        params={"run_id": run_id, "notional_cash_cny": "nan"},
    )
    assert nonfinite.status_code == 422


def test_historical_replay_uses_exact_published_date_and_same_strategy_fingerprint(tmp_path) -> None:
    _cache, service, strategy_id, run_id = _environment(tmp_path)
    latest = service.execute(StrategyExecutionRequest(strategy_id=strategy_id, kind="latest_scan"))
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


def test_latest_scan_freshness_uses_completed_exchange_sessions_and_fails_before_write(
    tmp_path,
) -> None:
    cache, service, strategy_id, run_id = _environment(tmp_path)
    service._market_clock = lambda: datetime(2026, 8, 13, 16, 0)  # noqa: SLF001

    with pytest.raises(ValueError, match="已过期.*19 个交易日"):
        service.execute(StrategyExecutionRequest(strategy_id=strategy_id))

    with cache._connect() as conn:  # noqa: SLF001 - fail-closed write assertion
        assert conn.execute("SELECT COUNT(*) FROM strategy_execution").fetchone()[0] == 0

    replay = service.execute(
        StrategyExecutionRequest(
            strategy_id=strategy_id,
            kind="historical_replay",
            run_id=run_id,
        )
    )
    assert replay.summary.status == "ready"


def test_latest_scan_freshness_treats_weekend_as_same_completed_session(tmp_path) -> None:
    _cache, service, strategy_id, _run_id = _environment(tmp_path)
    service._market_clock = lambda: datetime(2026, 7, 19, 16, 0)  # noqa: SLF001

    draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id))

    assert draft.summary.status == "ready"


def test_intraday_latest_freshness_uses_current_quote_session(tmp_path) -> None:
    _cache, service, _strategy_id, _run_id = _environment(tmp_path)
    service._market_clock = lambda: datetime(2026, 7, 17, 10, 0)  # noqa: SLF001

    contract = service._latest_scan_freshness_contract(  # noqa: SLF001
        1,
        "2026-07-17",
        StrategyExecutionRequest(strategy_id=1, mode="intraday"),
    )

    assert contract is not None
    assert contract["reference_kind"] == "current_exchange_quote_session"
    assert contract["reference_date"] == "2026-07-17"
    assert contract["age_exchange_sessions"] == 0


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
    filtered = current.spec.model_copy(update={"hard_filters": [StrategyHardFilter(field="amount", operator="gte", value=10_000_000_000.0)]})
    updated = cache.strategy_lab_service.update(
        strategy_id,
        StrategySpecUpdate(spec=filtered, expected_revision=1, confirmed=True),
    )
    draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id, revision=updated.revision))

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
    custom_draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id, revision=custom_version.revision))
    custom_weights = {item.symbol: item.target_weight for item in custom_draft.selected}

    assert set(custom_weights) == {"600001.SH", "688001.SH"}
    assert custom_weights["600001.SH"] == pytest.approx(0.2, abs=0.001)
    assert custom_weights["688001.SH"] == pytest.approx(0.3, abs=0.001)
    assert any("自定义权重未包含" in reason for item in custom_draft.candidate_preview for reason in item.reasons)

    with cache._connect() as conn:  # noqa: SLF001 - test-only frozen snapshot mutation
        _disable_market_scan_immutability(conn)
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
        _reseal_market_scan_snapshot(conn, _run_id)
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
    risk_draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id, revision=risk_version.revision))
    tampered = {item.symbol: item for item in risk_draft.candidate_preview if item.symbol in {"600001.SH", "688001.SH"}}

    assert set(tampered) == {"600001.SH", "688001.SH"}
    assert all(item.evidence_verified is False for item in tampered.values())
    assert all(item.status == "rejected" for item in tampered.values())
    assert all(any("时点证据" in failure for failure in item.hard_filter_failures) for item in tampered.values())
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
    assert any("新买入阈值" in failure for item in draft.candidate_preview if item.symbol != "600001.SH" for failure in item.hard_filter_failures)

    frozen = service.repository.frozen_scan(run_id=None, data_date=None, mode="official")
    allowed_sources = sorted({str(source) for item in frozen.items for source in (item.quote_source, item.kline_source, item.metadata_source) if source})
    with cache._connect() as conn:  # noqa: SLF001 - test-only missing provenance case
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_result SET metadata_source = NULL WHERE symbol = '600001.SH'",
        )
        _reseal_market_scan_snapshot(conn, _run_id)
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
    source_draft = service.execute(StrategyExecutionRequest(strategy_id=strategy_id, revision=source_version.revision))
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


def _set_frozen_industry(cache: SQLiteCache, symbol: str, industry: str) -> None:
    with cache._connect() as conn:  # noqa: SLF001 - sealed fixture rewrite
        _disable_market_scan_immutability(conn)
        row = conn.execute(
            "SELECT metrics_json FROM market_scan_result WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        metrics = json.loads(str(row["metrics_json"]))
        evidence = metrics["score_details"]["components"]["score_dimensions"]["point_in_time_evidence"]
        evidence["payload"]["industry"] = industry
        evidence["payload_digest"] = sha256_hex(canonical_json_text(evidence["payload"]))
        conn.execute(
            "UPDATE market_scan_result SET industry = ?, metrics_json = ? WHERE symbol = ?",
            (industry, json.dumps(metrics, ensure_ascii=False), symbol),
        )
        run_id = int(
            conn.execute(
                "SELECT run_id FROM market_scan_result WHERE symbol = ?",
                (symbol,),
            ).fetchone()["run_id"]
        )
        _reseal_market_scan_snapshot(conn, run_id)


def _reseal_market_scan_snapshot(conn: sqlite3.Connection, run_id: int) -> None:
    """Keep downstream adversarial fixtures behind the snapshot-seal boundary."""

    conn.execute(
        "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
        (market_scan_snapshot_digest(conn, run_id), run_id),
    )


def _disable_market_scan_immutability(conn: sqlite3.Connection) -> None:
    """Let downstream tests emulate a privileged attacker who can drop DB triggers."""

    for trigger in (
        "trg_market_scan_published_run_immutable",
        "trg_market_scan_published_run_no_delete",
        "trg_market_scan_published_result_no_update",
        "trg_market_scan_published_result_no_delete",
        "trg_market_scan_published_result_no_insert",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _environment(
    tmp_path,
    *,
    action_eligible: bool = True,
) -> tuple[SQLiteCache, StrategyExecutionService, int, int]:
    cache = SQLiteCache(tmp_path / "strategy-execution.sqlite3")
    run_id = _seed_scan(cache, action_eligible=action_eligible)
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
    service._market_clock = lambda: SCAN_AS_OF  # noqa: SLF001 - deterministic latest-session fixture
    return cache, service, strategy.strategy_id, run_id


def _seed_scan(cache: SQLiteCache, *, action_eligible: bool = True) -> int:
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
        scope=FULL_MARKET_SCOPE,
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
            replace(
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
                ),
                quote_observed_at="2026-07-17T08:30:00Z",
            )
        )
    cache.save_market_scan_result_batch(run.id, results)
    finished = cache.finish_market_scan_run(
        run.id,
        "success",
        message="测试冻结扫描完成",
        publication_diagnostics=(
            action_pass_publication_diagnostics()
            if action_eligible
            else distribution_degraded_publication_diagnostics()
        ),
    )
    assert finished.status == "success"
    return run.id
