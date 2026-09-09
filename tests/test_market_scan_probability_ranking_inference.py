from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from itertools import combinations
import json
from pathlib import Path
from statistics import mean
from types import SimpleNamespace
from typing import cast

import pytest

import app.services.market_scan_probability_ranking as ranking
import app.services.market_scan_joint_execution_maintenance as maintenance
from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.market_scan_probability_ranking_inference import (
    RANKING_INFERENCE_REGISTERED_AT,
    RANKING_SHADOW_CONTRACT_VERSION,
    ranking_inference_contract,
)
from tests.test_market_scan_joint_execution_maintenance_contract import _service, _state


def test_regime_aliasing_cannot_qualify_new_ranking_shadow() -> None:
    decisions = _regime_decisions()
    old = ranking._shadow_analysis(decisions, legacy=True)  # noqa: SLF001
    current = ranking._shadow_analysis(decisions)  # noqa: SLF001
    assert old["qualified"] is True
    assert old["failed_gates"] == []
    assert old["metrics"]["probability_of_backtest_overfitting"] == 0.0
    assert old["metrics"]["deflated_sharpe_probability"] > 0.999999
    assert old["metrics"]["costed_net_excess_delta_ci95"][0] > 0
    assert _contiguous_pair_failure_diagnostic() == pytest.approx(17 / 53)
    assert current["qualified"] is False
    assert set(current["failed_gates"]) == {
        "pbo_at_most_20pct", "deflated_sharpe_probability_at_least_95pct",
        "maximum_drawdown_not_materially_worse",
    }
    assert current["session_summaries"] == old["session_summaries"]
    assert current["session_summary_digest"] == old["session_summary_digest"]
    metrics = current["metrics"]
    assert metrics["distinct_session_count"] == 240
    assert "independent_session_count" not in metrics
    assert metrics["decision_count"] == 28_800
    for name in ("probability_of_backtest_overfitting", "deflated_sharpe_probability",
                 "v5_maximum_drawdown", "v6_maximum_drawdown"):
        assert metrics[name] is None
    assert metrics["iid_zero_benchmark_probabilistic_sharpe_diagnostic"] == old["metrics"]["deflated_sharpe_probability"]
    diagnostic = metrics["synthetic_horizon_excess_return_chain_drawdown_diagnostic"]
    assert diagnostic["v5"] == 0
    assert diagnostic["v6"] == pytest.approx(0.00239717018883423)
    assert diagnostic["promotion_eligible"] is False


def test_zero_benchmark_iid_psr_is_explicitly_not_promotion_evidence() -> None:
    repeated = [value for value in [-0.01, 0.016] * 6 for _ in range(5)]
    psr = ranking._iid_zero_benchmark_probabilistic_sharpe(repeated)  # noqa: SLF001
    assert psr == pytest.approx(0.9618501805124022)
    policy = ranking_inference_contract()
    assert policy["iid_zero_benchmark_probabilistic_sharpe"]["promotion_eligible"] is False
    assert policy["deflated_sharpe_probability"]["status"] == "unavailable"
    assert policy["probability_of_backtest_overfitting"]["status"] == "unavailable"
    assert policy["portfolio_drawdown"]["status"] == "unavailable"
    assert policy["promotion_eligible"] is False
    assert policy["registered_at"] == RANKING_INFERENCE_REGISTERED_AT
    assert policy["registered_at"] != ranking.PROBABILITY_RANKING_PREREGISTERED_AT
    policy["promotion_eligible"] = True
    assert ranking_inference_contract()["promotion_eligible"] is False


@pytest.mark.parametrize("restamp", ["future_v1", "current_v2"])
def test_old_qualified_shadow_cannot_be_restamped_as_current_promotion(restamp: str) -> None:
    artifact = _legacy_artifacts()["seal_probability_ranking_manual_control_artifact"][0]
    payload = deepcopy(artifact["payload"])
    shadow = ranking.VerifiedProbabilityRankingShadowEvaluation(
        json.dumps({
            "contract_version": "probability-ranking-preregistered-shadow-v1",
            "qualified": True, "generated_at": "2026-08-23T16:00:00+08:00",
            "study_evidence_digest": payload["study_evidence_digest"], "source_run_ids": [100],
        }), integrity_digest=payload["shadow_artifact_digest"], qualified=False,
        _seal=ranking._SHADOW_SEAL,  # noqa: SLF001
    )
    legacy = ranking.verify_probability_ranking_manual_control_artifact(
        artifact, expected_digest=artifact["integrity"]["integrity_digest"], shadow=shadow,
    )
    assert legacy.action == "promote" and legacy.promotion_eligible is False
    payload["generated_at"] = "2026-09-11T16:00:00+08:00"
    if restamp == "current_v2":
        payload["contract_version"] = "probability-ranking-explicit-human-control-v2"
        payload["inference_contract_digest"] = sha256_hex(canonical_json_bytes(ranking_inference_contract()))
    restamped = ranking.seal_probability_ranking_manual_control_artifact(
        payload, generated_at=payload["generated_at"],
    )
    with pytest.raises(ranking.ProbabilityRankingError, match="historical shadow|current inference"):
        ranking.verify_probability_ranking_manual_control_artifact(
            restamped, expected_digest=restamped["integrity"]["integrity_digest"], shadow=shadow,
        )


def test_pinned_rollback_remains_available_when_current_shadow_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    artifact = _legacy_artifacts()["seal_probability_ranking_manual_control_artifact"][1]
    service.ranking_control_digest = artifact["integrity"]["integrity_digest"]
    monkeypatch.setattr(maintenance, "_load_pinned_authorization", lambda *_args: artifact)
    monkeypatch.setattr(service, "_publish_envelope", lambda *_args, **_kwargs: tmp_path / "rollback.json.gz")
    monkeypatch.setattr(service, "_ranking_shadow_for_oos", lambda *_args, **_kwargs: pytest.fail("rollback must not require shadow"))
    state = _state(ranking_control=True)
    result = service._ranking_maintenance(state, cast(maintenance._AuthorizedMaintenance, object()))  # noqa: SLF001
    assert result.status == "rollback_active"
    assert result.control.action == "rollback"
    assert result.control.promotion_eligible is False
    assert state.failures == []
    assert state.blockers == ["probability_ranking_manual_rollback_active"]
    assert service.ranking_store.rollbacks == [result.control]


def test_current_shadow_cache_skips_same_corpus_legacy_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    oos, study = SimpleNamespace(integrity_digest="a" * 64), SimpleNamespace(evidence_digest="b" * 64)
    old = {"generated_at": "2026-08-23T16:00:00+08:00", "payload": {
        "contract_version": "probability-ranking-preregistered-shadow-v1",
        "oos_corpus_digest": oos.integrity_digest, "study_evidence_digest": study.evidence_digest,
    }}
    built = {"payload": {"contract_version": RANKING_SHADOW_CONTRACT_VERSION}}
    token = SimpleNamespace(integrity_digest="c" * 64, qualified=False)
    monkeypatch.setattr(maintenance, "_managed_artifact_paths", lambda *_args: (tmp_path / "old.json.gz",))
    monkeypatch.setattr(maintenance, "_read_gzip_json", lambda *_args: old)
    monkeypatch.setattr(maintenance, "build_probability_ranking_shadow_artifact", lambda *_args, **_kwargs: built)
    def verify(artifact: object, **_kwargs: object) -> object:
        assert artifact is built
        return token
    monkeypatch.setattr(maintenance, "verify_probability_ranking_shadow_artifact", verify)
    monkeypatch.setattr(service, "_publish_envelope", lambda *_args, **_kwargs: tmp_path / "new.json.gz")
    assert service._ranking_shadow_for_oos(  # noqa: SLF001
        oos, [], study, generated_at="2026-09-11T16:00:00+08:00",
    ) is token


def _legacy_artifacts() -> dict[str, list[dict[str, object]]]:
    return json.loads((Path(__file__).parent / "fixtures" / "probability_ranking_legacy_v1_artifacts.json").read_text())


def _regime_decisions() -> list[ranking._ShadowDecision]:
    regimes = [0.0001, -0.00004, -0.00004, 0.0001, 0.0001, -0.00004, -0.00004, 0.0001]
    deltas = [value for value in regimes for _ in range(30)]
    session = date(2026, 9, 11)
    output = []
    for index, delta in enumerate(deltas):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        for stock in range(120):
            base = 80.0 if stock < 80 else (72.0 if stock < 100 else 70.0)
            probability = 0.5 if stock < 100 else 0.8
            adjustment, score, _ = ranking.probability_ranking_raw_score(base, probability, 0.5)
            output.append(ranking._ShadowDecision(  # noqa: SLF001
                sample_id=f"{index}-{stock}", run_id=index+1, session=session.isoformat(),
                symbol=f"{stock:06d}.SH", base_raw_score=base,
                probability=probability, reference_base_rate=0.5,
                adjustment=adjustment, v6_raw_score=score,
                net_excess_return=0.0 if stock < 100 else 5 * delta,
                unresolved_exit=False, capacity_exceeded=False,
                categories={group: f"{group}_{stock % count}" for group, count in
                            [("market", 2), ("liquidity", 4), ("industry_bucket", 4)]},
            ))
        session += timedelta(days=1)
    return output


def _contiguous_pair_failure_diagnostic() -> float:
    # Comparator only: preserving long states is not sufficient to establish full PBO.
    values = [0.0001, -0.00004, -0.00004, 0.0001, 0.0001, -0.00004, -0.00004, 0.0001]
    cases = []
    for selected in combinations(range(8), 4):
        if mean(values[index] for index in selected) > 0:
            cases.append(mean(value for index, value in enumerate(values) if index not in selected) <= 0)
    return sum(cases) / len(cases)
