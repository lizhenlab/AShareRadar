from __future__ import annotations

from app.models.strategy_evidence import StrategyEvidenceRefreshRequest
from app.models.strategy_execution import StrategyExecutionRequest
from tests.test_strategy_execution import _environment


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
    assert evidence.coverage[0].scope == "全市场"
    assert evidence.coverage[0].total_count == 4
    assert {item.scope for item in evidence.coverage} >= {"上海主板", "科创板", "创业板", "北交所"}
    assert {item.dimension for item in evidence.dimensions} == {
        "alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"
    }
    assert evidence.top_n[0].top_n == 20
    assert evidence.top_n[0].net_return == 0.008
    assert evidence.rank_evidence[0].rank_ic == 0.12
    assert evidence.shadow_candidates[0].point_in_time_integrity_verified is True
    assert evidence.promotion.automatic_promotion is False
    assert evidence.promotion.pbo_ready is False
    assert evidence.baseline_generated_at == "2026-07-30T13:16:29Z"
    assert len(evidence.baseline_report_digest or "") == 64
    assert cache.strategy_evidence_service.latest(
        strategy_id, revision=1, mode="official"
    ) == evidence


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
                "method": "preregistered-ablation-plus-PBO-before-promotion",
                "ready": False,
            },
            "blocking_reasons": ["独立交易日样本不足", "PBO 尚未就绪"],
            "conclusion": "样本不足，不晋级生产。",
        },
    }
