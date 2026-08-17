from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest
from pydantic import ValidationError

from app.db.market_scan_integrity import market_scan_snapshot_digest
from app.models.market_scan import MarketScanProductionScoreContract
from app.models.strategy_evidence import (
    StrategyEvidenceCenter,
    StrategyEvidenceExecution,
    StrategyEvidenceRefreshRequest,
    StrategyEvidenceResearchBoundary,
)
from app.models.strategy_execution import StrategyExecutionRequest
from app.repositories.strategy_evidence import (
    StrategyEvidenceIntegrityError,
    strategy_evidence_digest,
)
from app.services.strategy_evidence import (
    _OFFLINE_REPORT,
    _load_offline_evaluation_report,
    _promotion,
    _execution_contract_compatibility,
    _shadow,
)
from tests.test_strategy_execution import _disable_market_scan_immutability, _environment
from tests.market_scan_test_support import distribution_degraded_publication_diagnostics


ROOT = Path(__file__).resolve().parents[1]


def test_retained_v55_artifact_is_compact_and_projects_real_shadow_evidence() -> None:
    report = _load_offline_evaluation_report()
    projection = report.get("artifact_projection")
    assert isinstance(projection, dict)
    assert projection["schema_version"] == "market-scan-shadow-comparison-compact-v1"
    assert _OFFLINE_REPORT.stat().st_size < 10_000_000
    assert "probability_research" not in report["production"]
    assert all("probability_research" not in candidate for candidate in report["candidates"].values())

    candidates = _shadow(report, mode="official")
    assert {item.candidate_id for item in candidates} >= {"v5_5_bounded_nonlinear_stability"}
    assert all(len(item.spec_hash or "") == 64 for item in candidates)
    assert all([row.top_n for row in item.top_n] == [20, 50, 100] for item in candidates)
    assert all(item.rank_delta_vs_production.compared_item_count == 5_499 for item in candidates)
    promotion = _promotion(report)
    assert promotion.multiple_testing_method == "benjamini-hochberg-fdr"
    assert promotion.pbo_ready is False
    assert promotion.pbo_status == "not_computed"
    assert promotion.deflated_sharpe_status == "not_computed"


def test_evidence_center_reuses_offline_report_and_verifies_execution_digest(
    tmp_path,
    monkeypatch,
) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    monkeypatch.setattr(
        "app.services.strategy_evidence._load_offline_evaluation_report",
        lambda: _evaluation_report("official"),
    )

    evidence = cache.strategy_evidence_service.refresh(
        strategy_id,
        revision=1,
        mode=StrategyEvidenceRefreshRequest().mode,
    )

    assert evidence.status == "insufficient_data"
    assert evidence.strategy_fingerprint == draft.context.strategy_fingerprint
    assert evidence.execution.execution_id == draft.context.execution_id
    assert evidence.execution.evidence_digest_verified is True
    assert evidence.execution.production_score_rule_version == "full-market-score-v5"
    assert evidence.research_boundary.execution_contract_compatibility == "incompatible"
    assert any("v4 基线不兼容" in blocker for blocker in evidence.promotion.blockers)
    assert evidence.coverage[0].scope == "全市场"
    assert evidence.coverage[0].total_count == 4
    assert {item.scope for item in evidence.coverage} >= {"上海主板", "科创板", "创业板", "北交所"}
    assert {item.dimension for item in evidence.dimensions} == {"alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"}
    assert evidence.top_n[0].top_n == 20
    assert evidence.top_n[0].net_return == 0.008
    assert evidence.rank_evidence[0].rank_ic == 0.12
    assert evidence.shadow_candidates[0].point_in_time_integrity_verified is True
    assert evidence.research_boundary.statement == "影子研究，不改变生产排名"
    assert evidence.research_boundary.production_ranking_mutated is False
    assert evidence.shadow_candidates[0].coverage.item_coverage_ratio is None
    assert evidence.shadow_candidates[0].coverage.status == "unavailable"
    assert evidence.shadow_candidates[0].rank_delta_vs_production.mean_rank_delta is None
    assert {item.status for item in evidence.shadow_candidates[0].top_n} == {"unavailable"}
    assert evidence.promotion.automatic_promotion is False
    assert evidence.promotion.multiple_testing_ready is False
    assert evidence.promotion.pbo_ready is False
    assert evidence.promotion.pbo_status == "not_computed"
    assert evidence.promotion.deflated_sharpe_status == "not_computed"
    assert evidence.baseline_generated_at == "2026-07-30T13:16:29Z"
    assert evidence.baseline_projection_schema_version is None
    assert len(evidence.baseline_report_digest or "") == 64
    assert cache.strategy_evidence_service.latest(strategy_id, revision=1, mode="official") == evidence


@pytest.mark.parametrize("mutation", ("tampered", "legacy_backfill"))
def test_evidence_refresh_rejects_untrusted_execution_source_snapshot(
    tmp_path,
    monkeypatch,
    mutation: str,
) -> None:
    cache, execution_service, strategy_id, run_id = _environment(tmp_path)
    execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    monkeypatch.setattr(
        "app.services.strategy_evidence._load_offline_evaluation_report",
        lambda: _evaluation_report("official"),
    )
    with cache._connect() as conn:  # noqa: SLF001 - privileged source attack
        _disable_market_scan_immutability(conn)
        if mutation == "tampered":
            conn.execute(
                "UPDATE market_scan_result SET amount = amount + 1 WHERE run_id = ?",
                (run_id,),
            )
        else:
            conn.execute(
                """
                UPDATE market_scan_run
                SET snapshot_digest = NULL, snapshot_seal_origin = 'legacy_backfill'
                WHERE id = ?
                """,
                (run_id,),
            )
            conn.execute(
                "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
                (market_scan_snapshot_digest(conn, run_id), run_id),
            )

    with pytest.raises(StrategyEvidenceIntegrityError, match="来源榜单"):
        cache.strategy_evidence_service.refresh(
            strategy_id,
            revision=1,
            mode="official",
        )


def test_evidence_refresh_rejects_distribution_degraded_execution_source(
    tmp_path,
    monkeypatch,
) -> None:
    cache, execution_service, strategy_id, run_id = _environment(tmp_path)
    execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    monkeypatch.setattr(
        "app.services.strategy_evidence._load_offline_evaluation_report",
        lambda: _evaluation_report("official"),
    )
    with cache._connect() as conn:  # noqa: SLF001 - privileged source mutation
        _disable_market_scan_immutability(conn)
        conn.execute(
            """
            UPDATE market_scan_run
            SET snapshot_digest = NULL,
                publication_diagnostics_json = ?
            WHERE id = ?
            """,
            (
                distribution_degraded_publication_diagnostics().model_dump_json(),
                run_id,
            ),
        )
        digest = market_scan_snapshot_digest(conn, run_id)
        conn.execute(
            "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
            (digest, run_id),
        )
        conn.execute("DROP TRIGGER IF EXISTS trg_strategy_execution_no_update")
        conn.execute(
            """
            UPDATE strategy_execution
            SET source_snapshot_digest = ?
            WHERE market_scan_run_id = ?
            """,
            (digest, run_id),
        )

    with pytest.raises(StrategyEvidenceIntegrityError, match="来源榜单"):
        cache.strategy_evidence_service.refresh(
            strategy_id,
            revision=1,
            mode="official",
        )
    with cache._connect() as conn:  # noqa: SLF001 - fail-closed write assertion
        assert conn.execute("SELECT COUNT(*) FROM strategy_evidence_snapshot").fetchone()[0] == 0


def test_evidence_center_does_not_claim_custom_strategy_effectiveness_without_execution(
    tmp_path,
    monkeypatch,
) -> None:
    cache, _execution_service, strategy_id, _run_id = _environment(tmp_path)
    monkeypatch.setattr(
        "app.services.strategy_evidence._load_offline_evaluation_report",
        lambda: _evaluation_report("official"),
    )

    evidence = cache.strategy_evidence_service.refresh(
        strategy_id,
        revision=1,
        mode="official",
    )

    assert evidence.execution.execution_id is None
    assert evidence.execution.evidence_digest_verified is False
    assert any("不等同于当前自定义" in item for item in evidence.limitations)
    assert any("尚无独立执行证据" in item for item in evidence.limitations)


def test_promotion_is_rebuilt_from_candidate_gates_not_aggregate_claims() -> None:
    report = deepcopy(_load_offline_evaluation_report())
    promotion = report["promotion"]
    promotion["eligible_for_human_review"] = True
    promotion["observed_independent_session_count"] = promotion["required_independent_session_count"]
    promotion["point_in_time_input_integrity_verified"] = True
    promotion["multiple_testing_control"]["ready"] = True
    promotion["blocking_reasons"] = []

    projected = _promotion(report)

    assert projected.eligible_for_manual_review is False
    assert any("聚合标志" in blocker for blocker in projected.blockers)


def test_offline_report_loader_rejects_modified_compact_baseline(
    tmp_path,
    monkeypatch,
) -> None:
    report = deepcopy(_load_offline_evaluation_report())
    report["status"] = "ok"
    tampered = tmp_path / "shadow-report.json"
    tampered.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("app.services.strategy_evidence._OFFLINE_REPORT", tampered)

    with pytest.raises(RuntimeError, match="内容摘要"):
        _load_offline_evaluation_report()


def test_latest_evidence_rejects_payload_tamper_without_falling_back(
    tmp_path,
    monkeypatch,
) -> None:
    cache, _execution_service, strategy_id, _run_id = _environment(tmp_path)
    monkeypatch.setattr(
        "app.services.strategy_evidence._load_offline_evaluation_report",
        lambda: _evaluation_report("official"),
    )
    cache.strategy_evidence_service.refresh(strategy_id, revision=1, mode="official")
    cache.strategy_evidence_service.refresh(strategy_id, revision=1, mode="official")
    with cache._connect() as conn:  # noqa: SLF001 - integrity boundary mutation
        row = conn.execute("SELECT id, evidence_json FROM strategy_evidence_snapshot ORDER BY id DESC LIMIT 1").fetchone()
        payload = json.loads(str(row["evidence_json"]))
        payload["promotion"]["eligible_for_manual_review"] = True
        conn.execute(
            "UPDATE strategy_evidence_snapshot SET evidence_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), int(row["id"])),
        )

    with pytest.raises(StrategyEvidenceIntegrityError, match="摘要不一致"):
        cache.strategy_evidence_service.latest(strategy_id, revision=1, mode="official")


def test_latest_evidence_rejects_resealed_row_identity_mismatch(
    tmp_path,
    monkeypatch,
) -> None:
    cache, _execution_service, strategy_id, _run_id = _environment(tmp_path)
    monkeypatch.setattr(
        "app.services.strategy_evidence._load_offline_evaluation_report",
        lambda: _evaluation_report("official"),
    )
    cache.strategy_evidence_service.refresh(strategy_id, revision=1, mode="official")
    with cache._connect() as conn:  # noqa: SLF001 - integrity boundary mutation
        row = conn.execute("SELECT id, evidence_json FROM strategy_evidence_snapshot ORDER BY id DESC LIMIT 1").fetchone()
        payload = json.loads(str(row["evidence_json"]))
        payload["mode"] = "intraday"
        conn.execute(
            """
            UPDATE strategy_evidence_snapshot
            SET mode = 'official', evidence_json = ?, evidence_digest = ?
            WHERE id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False),
                strategy_evidence_digest(payload),
                int(row["id"]),
            ),
        )

    with pytest.raises(StrategyEvidenceIntegrityError, match="数据库身份"):
        cache.strategy_evidence_service.latest(strategy_id, revision=1, mode="official")


def test_refresh_rejects_tampered_execution_before_saving_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    cache, execution_service, strategy_id, _run_id = _environment(tmp_path)
    draft = execution_service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    monkeypatch.setattr(
        "app.services.strategy_evidence._load_offline_evaluation_report",
        lambda: _evaluation_report("official"),
    )
    with cache._connect() as conn:  # noqa: SLF001 - integrity boundary mutation
        row = conn.execute(
            """
            SELECT id, candidate_json FROM strategy_execution_candidate
            WHERE execution_id = ? ORDER BY id LIMIT 1
            """,
            (draft.context.execution_id,),
        ).fetchone()
        candidate = json.loads(str(row["candidate_json"]))
        candidate["name"] = "被篡改的候选"
        conn.execute("DROP TRIGGER trg_strategy_execution_candidate_no_update")
        conn.execute(
            "UPDATE strategy_execution_candidate SET candidate_json = ? WHERE id = ?",
            (json.dumps(candidate, ensure_ascii=False), int(row["id"])),
        )

    with pytest.raises(StrategyEvidenceIntegrityError, match="执行结果摘要"):
        cache.strategy_evidence_service.refresh(strategy_id, revision=1, mode="official")
    with cache._connect() as conn:  # noqa: SLF001 - zero-write assertion
        assert conn.execute("SELECT COUNT(*) FROM strategy_evidence_snapshot").fetchone()[0] == 0


def test_evidence_center_projects_typed_shadow_candidate_artifact_without_mutating_v4(
    tmp_path,
    monkeypatch,
) -> None:
    cache, _execution_service, strategy_id, _run_id = _environment(tmp_path)
    report = _evaluation_report("official")
    report["artifact_projection"] = {"schema_version": "market-scan-shadow-comparison-compact-v1"}
    candidate = report["candidates"]["v5_full"]
    candidate.update(
        {
            "evaluation_quality": {"item_coverage_ratio": 0.97},
            "cohorts": [
                {
                    "dimensions": {
                        "mode": "official",
                        "scope": "all-a-share",
                        "rule_version": "full-market-shadow-score-v5.5",
                        "industry": "测试行业",
                    },
                    "top_n": 20,
                    "horizon_trading_days": 5,
                    "status": "ok",
                    "sample_size": 999,
                    "independent_session_count": 999,
                    "session_average_return": 0.99,
                },
                {
                    "dimensions": {
                        "mode": "official",
                        "scope": "TOP100快速更新评分",
                        "rule_version": "full-market-shadow-score-v5.5",
                    },
                    "top_n": 20,
                    "horizon_trading_days": 5,
                    "status": "ok",
                    "sample_size": 999,
                    "independent_session_count": 999,
                    "session_average_return": 0.88,
                },
                *[
                    {
                        "dimensions": {
                            "mode": "official",
                            "scope": "all-a-share",
                            "rule_version": "full-market-shadow-score-v5.5",
                        },
                        "top_n": top_n,
                        "horizon_trading_days": 5,
                        "status": "insufficient_data",
                        "sample_size": top_n * 3,
                        "independent_session_count": 3,
                        "session_average_return": 0.01,
                        "execution": {
                            "average_net_return": None,
                            "average_cost_drag": None,
                        },
                        "insufficient_reasons": ["minimum_session_count"],
                    }
                    for top_n in (20, 50, 100)
                ],
            ],
            "stability": [{"mode": "official", "top_n": top_n, "turnover_rate": 0.25} for top_n in (20, 50, 100)],
            "rank_delta_vs_production": {
                "status": "available",
                "candidate_ranking_count": 130,
                "production_ranking_count": 125,
                "common_symbol_count": 120,
                "missing_candidate_count": 5,
                "missing_production_count": 10,
                "mean_rank_delta": 0.0,
                "median_rank_delta": -2.0,
                "mean_absolute": 14.5,
                "max_absolute": 120.0,
                "top20_overlap": 0.0,
                "top50_overlap": 0.6,
                "top100_overlap": 0.7,
            },
            "exposure_audit": [
                {
                    "board": [{"share_difference": -0.2}],
                    "industry": [{"share_difference": 0.3}],
                    "liquidity": [],
                }
            ],
        }
    )
    report["promotion"]["candidate_gates"] = {
        "v5_full": {
            "gate_version": "shadow-promotion-gates-v2",
            "decision": "remain-shadow",
            "passed": False,
            "failed_criteria": ["independent_sessions", "board_industry_liquidity_exposure"],
            "criteria": {
                "hysteresis_turnover_top100": {
                    "observed": 0.25,
                    "threshold": {"maximum": 0.35},
                    "passed": True,
                },
                "board_industry_liquidity_exposure": {
                    "observed": 0.3,
                    "threshold": {"maximum_absolute_share_difference": 0.2},
                    "passed": False,
                },
            },
        }
    }
    monkeypatch.setattr(
        "app.services.strategy_evidence._load_offline_evaluation_report",
        lambda: report,
    )

    evidence = cache.strategy_evidence_service.refresh(
        strategy_id,
        revision=1,
        mode="official",
    )

    shadow = evidence.shadow_candidates[0]
    assert (
        evidence.research_boundary.baseline_production_score_rule_version
        == "full-market-score-v4"
    )
    assert evidence.baseline_projection_schema_version == "market-scan-shadow-comparison-compact-v1"
    assert shadow.coverage.item_coverage_ratio == 0.97
    assert [item.top_n for item in shadow.top_n] == [20, 50, 100]
    assert shadow.top_n[0].independent_session_count == 3
    assert shadow.top_n[0].gross_return == 0.01
    assert shadow.top_n[0].net_return is None
    assert shadow.rank_delta_vs_production.mean_rank_delta == 0.0
    assert shadow.rank_delta_vs_production.compared_item_count == 120
    assert shadow.rank_delta_vs_production.candidate_ranking_count == 130
    assert shadow.rank_delta_vs_production.top20_overlap_ratio == 0.0
    assert shadow.constraints.hysteresis_turnover_rate == 0.25
    assert shadow.exposure.maximum_absolute_share_difference == 0.3
    assert shadow.exposure.passed is False
    assert shadow.promotion_gate.failed_criteria == [
        "independent_sessions",
        "board_industry_liquidity_exposure",
    ]

    tampered = evidence.model_dump(mode="json")
    tampered["research_boundary"]["production_ranking_mutated"] = True
    with pytest.raises(ValidationError):
        StrategyEvidenceCenter.model_validate(tampered)


def test_strategy_evidence_contract_compatibility_is_exact_not_version_alias() -> None:
    frozen_hash = "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
    v4 = MarketScanProductionScoreContract("full-market-score-v4", frozen_hash, 1)
    wrong_v4 = MarketScanProductionScoreContract("full-market-score-v4", "a" * 64, 1)
    v5 = MarketScanProductionScoreContract("full-market-score-v5", "b" * 64, 1)
    execution_marker = object()

    assert _execution_contract_compatibility(execution_marker, v4) == "compatible"  # type: ignore[arg-type]
    assert _execution_contract_compatibility(execution_marker, wrong_v4) == "incompatible"  # type: ignore[arg-type]
    assert _execution_contract_compatibility(execution_marker, v5) == "incompatible"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("compatibility", "rule_version", "spec_hash"),
    (
        (
            "compatible",
            "full-market-score-v4",
            "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a",
        ),
        ("incompatible", "full-market-score-v5", "b" * 64),
    ),
)
def test_python_strategy_evidence_boundary_model_dump_is_accepted_by_javascript(
    compatibility: str,
    rule_version: str,
    spec_hash: str,
) -> None:
    boundary_json = StrategyEvidenceResearchBoundary(
        execution_contract_compatibility=compatibility,
    ).model_dump_json()
    execution_json = StrategyEvidenceExecution(
        execution_id=1,
        production_score_rule_version=rule_version,
        production_score_spec_hash=spec_hash,
        evidence_digest_verified=True,
    ).model_dump_json()
    script = f"""
      import {{ validateEvidence }} from "./static/js/strategy-lab-contracts.js";
      const evidence = {{
        strategy_fingerprint: {json.dumps("c" * 64)},
        research_boundary: JSON.parse({json.dumps(boundary_json)}),
        execution: JSON.parse({json.dumps(execution_json)}),
        coverage: [], top_n: [], shadow_candidates: [],
        promotion: {{
          pbo_status: "not_computed", deflated_sharpe_status: "not_computed",
          pbo_ready: false,
        }},
      }};
      validateEvidence(evidence);
      const tampered = structuredClone(evidence);
      tampered.research_boundary.execution_contract_compatibility =
        tampered.research_boundary.execution_contract_compatibility === "compatible"
          ? "incompatible"
          : "compatible";
      let rejected = false;
      try {{ validateEvidence(tampered); }} catch (error) {{
        rejected = error.message.includes("兼容性不一致");
      }}
      if (!rejected) throw new Error("tampered execution compatibility was accepted");
    """
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_shadow_evidence_contract_and_view_preserve_unavailable_nulls() -> None:
    script = r"""
      import { validateEvidence } from "./static/js/strategy-lab-contracts.js";
      import { renderEvidence } from "./static/js/strategy-lab-view.js";

      const evidence = payload();
      validateEvidence(evidence);
      const elements = { strategyEvidenceContent: { innerHTML: "" } };
      renderEvidence(elements, evidence);
      const html = elements.strategyEvidenceContent.innerHTML;

      assert(html.includes("影子研究，不改变生产排名"), "research boundary was not rendered");
      assert(html.includes("full-market-score-v4") && html.includes("生产排名写入：否"), "v4 mutation boundary was lost");
      assert(html.includes("v5_5&lt;candidate&gt;") && !html.includes("<candidate>"), "candidate id was not escaped");
      assert(html.includes("a".repeat(64)), "candidate spec hash was not rendered");
      assert(html.includes("Top20") && html.includes("Top50") && html.includes("Top100"), "Top N evidence rows are incomplete");
      assert(html.includes("离线 artifact 未持久化逐股排名差"), "typed rank-delta unavailability was hidden");
      assert(!html.includes("Top N 净收益</span><strong>0.00%"), "missing production net return was rendered as zero");
      assert(html.includes("<td>--</td>"), "missing Shadow metrics were not kept empty");

      const tampered = structuredClone(evidence);
      tampered.research_boundary.production_ranking_mutated = true;
      let rejected = false;
      try { validateEvidence(tampered); } catch (error) { rejected = error.message.includes("研究边界无效"); }
      assert(rejected, "mutated production boundary was accepted");

      function payload() {
        const unavailableTopN = (top_n) => ({
          top_n, horizon_trading_days: 5, status: "unavailable", sample_size: null,
          independent_session_count: null, gross_return: null, net_return: null,
          cost_drag: null, turnover_rate: null, insufficient_reasons: ["artifact_missing"],
        });
        return {
          strategy_fingerprint: "b".repeat(64), status: "insufficient_data",
          research_boundary: {
            status: "shadow_only", baseline_kind: "offline_evaluation_baseline",
            baseline_production_score_rule_version: "full-market-score-v4",
            baseline_production_score_spec_hash: "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a",
            execution_contract_compatibility: "not_available",
            production_ranking_mutated: false, statement: "影子研究，不改变生产排名",
          },
          promotion: {
            observed_independent_session_count: 3, required_independent_session_count: 20,
            multiple_testing_ready: false, pbo_ready: false,
            pbo_status: "not_computed", deflated_sharpe_status: "not_computed",
            blockers: ["样本不足"], conclusion: "保持 Shadow。",
          },
          coverage: [], top_n: [], rank_evidence: [], execution: {
            execution_id: null, production_score_rule_version: null,
            production_score_spec_hash: null, evidence_digest_verified: false,
          },
          baseline_generated_at: null, baseline_report_digest: null,
          data_sources: [], freshness_notes: [], limitations: [],
          shadow_candidates: [{
            candidate_id: "v5_5<candidate>", status: "insufficient_data",
            evidence_status: "insufficient_data", spec_hash: "a".repeat(64),
            point_in_time_integrity_verified: false, independent_session_count: 3,
            coverage: {
              status: "unavailable", independent_session_count: 3, scored_run_count: null,
              scored_item_count: null, item_coverage_ratio: null,
              reasons: ["离线 artifact 未记录候选评分行覆盖率"],
            },
            top_n: [20, 50, 100].map(unavailableTopN),
            rank_delta_vs_production: {
              status: "unavailable", compared_run_count: null, compared_item_count: null,
              candidate_ranking_count: null, production_ranking_count: null,
              common_symbol_count: null, missing_candidate_count: null,
              missing_production_count: null,
              mean_rank_delta: null, median_rank_delta: null, mean_absolute_rank_delta: null,
              maximum_absolute_rank_delta: null, top20_overlap_ratio: null,
              top50_overlap_ratio: null, top100_overlap_ratio: null,
              reasons: ["离线 artifact 未持久化逐股排名差"],
            },
            constraints: {
              status: "unavailable", passed: null, hysteresis_turnover_rate: null,
              failed_constraints: [], reasons: ["约束证据不可用"],
            },
            exposure: {
              status: "unavailable", passed: null, record_count: null,
              maximum_absolute_share_difference: null, threshold: null,
              reasons: ["暴露证据不可用"],
            },
            promotion_gate: {
              status: "unavailable", gate_version: null, decision: null, passed: null,
              failed_criteria: [], reasons: ["门禁证据不可用"],
            },
          }],
        };
      }
      function assert(condition, message) { if (!condition) throw new Error(message); }
    """
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_strategy_evidence_loading_stays_out_of_initial_activation() -> None:
    controller = (ROOT / "static/js/strategy-lab-controller.js").read_text(encoding="utf-8")

    activate_body = controller.split("async function activate()", 1)[1].split("async function loadStrategies", 1)[0]
    assert "loadEvidence" not in activate_body
    assert "loadEvidence(false)" in controller
    assert "/evidence/refresh" in controller


def _evaluation_report(mode: str) -> dict[str, object]:
    contract = {"mode": mode, "scope": "all-a-share", "rule_version": "full-market-score-v4"}
    return {
        "schema_version": "market-scan-shadow-comparison-v1",
        "generated_at": "2026-07-30T13:16:29Z",
        "status": "insufficient_data",
        "production": {
            "status": "insufficient_data",
            "source": {
                "ranking_source": "persisted_market_scan_result",
                "forward_price_source": "persisted_qfq_kline_daily",
                "independent_session_count": 3,
            },
            "cohorts": [
                {
                    "dimensions": contract,
                    "top_n": 20,
                    "horizon_trading_days": 5,
                    "status": "insufficient_data",
                    "sample_size": 40,
                    "independent_session_count": 3,
                    "session_average_return": 0.01,
                    "session_return_confidence_interval_95": [-0.02, 0.03],
                    "session_maximum_drawdown": -0.04,
                    "maximum_adverse_excursion": -0.08,
                    "insufficient_reasons": ["独立交易日样本不足"],
                    "execution": {"average_net_return": 0.008, "average_cost_drag": 0.002},
                }
            ],
            "stability": [{"mode": mode, "top_n": 20, "turnover_rate": 0.35}],
            "rank_ic": [
                {
                    "mode": mode,
                    "horizon_trading_days": 5,
                    "status": "insufficient_data",
                    "independent_session_count": 3,
                    "mean_rank_ic": 0.12,
                    "icir": 0.4,
                    "confidence_interval_95": [-0.1, 0.3],
                }
            ],
            "deciles": [{"mode": mode, "horizon_trading_days": 5, "monotonic": None}],
            "exposure_audit": [{"run_id": 7, "top_n": 20, "board": []}],
            "limitations": ["只读取时点可见数据。"],
        },
        "candidates": {
            "v5_full": {
                "status": "insufficient_data",
                "source": {"independent_session_count": 3},
                "shadow": {
                    "spec_hash": "a" * 64,
                    "input_integrity": {"eligible_for_promotion_evidence": True},
                },
            }
        },
        "promotion": {
            "automatic_promotion": False,
            "eligible_for_human_review": False,
            "observed_independent_session_count": 3,
            "required_independent_session_count": 20,
            "point_in_time_input_integrity_verified": True,
            "multiple_testing_control": {
                "method": "benjamini-hochberg-fdr",
                "ready": False,
                "pbo": {"status": "not_computed", "value": None},
                "deflated_sharpe_ratio": {"status": "not_computed", "value": None},
            },
            "blocking_reasons": ["独立交易日样本不足", "BH-FDR 尚未就绪"],
            "conclusion": "样本不足，不晋级生产。",
        },
    }
