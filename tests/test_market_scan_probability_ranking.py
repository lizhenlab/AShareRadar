from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import gzip
import json
import math
from pathlib import Path
import sqlite3
from typing import cast

import pytest

import app.services.market_scan_joint_execution_probability as probability_module
import app.services.market_scan_joint_execution_source as source_module
import app.services.market_scan_probability_ranking as ranking_module
import app.services.market_scan_probability_ranking_store as ranking_store_module
import app.services.joint_execution_probability_v3 as corpus_module
from app.artifacts.io import canonical_json_bytes
from app.db.schema import initialize_schema
from app.services.market_scan_probability_ranking import (
    PROBABILITY_RANKING_SCORE_RULE_VERSION,
    ProbabilityRankingError,
    build_probability_ranking_publication_artifact,
    build_probability_ranking_shadow_artifact,
    probability_ranking_raw_score,
    probability_ranking_score_spec,
    probability_ranking_score_spec_hash,
    seal_probability_ranking_manual_control_artifact,
    verify_probability_ranking_manual_control_artifact,
    verify_probability_ranking_publication_artifact,
    verify_probability_ranking_shadow_artifact,
)
from app.services.market_scan_probability_ranking_store import (
    MarketScanProbabilityRankingStore,
)
from app.services.market_scan_joint_execution_maintenance import (
    MarketScanJointExecutionMaintenanceService,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE


def test_probability_ranking_v6_spec_is_bounded_and_keeps_v5_immutable() -> None:
    spec = probability_ranking_score_spec()

    assert spec["rule_version"] == PROBABILITY_RANKING_SCORE_RULE_VERSION
    assert cast(dict[str, object], spec["base_score_contract"])["rule_version"] == (
        "full-market-score-v5"
    )
    assert cast(dict[str, object], spec["base_score_contract"])[
        "historical_rewrite"
    ] == "forbidden"
    assert cast(dict[str, object], spec["probability_input"])[
        "missing_success_probability_policy"
    ] == "fail_entire_v6_publication"
    assert probability_ranking_raw_score(80.0, 0.2, 0.5) == (-6.0, 74.0, 74)
    assert probability_ranking_raw_score(79.0, 0.8, 0.5) == (6.0, 85.0, 85)
    assert probability_ranking_raw_score(99.0, 1.0, 0.5) == (6.0, 100.0, 100)
    assert len(probability_ranking_score_spec_hash()) == 64


def test_probability_ranking_publication_requires_pinned_human_promotion_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    study_payload = {"selection_qualified": True}
    study_digest = _digest(study_payload)
    study = probability_module.VerifiedJointExecutionProbabilityStudy(
        _encoded(study_payload),
        evidence_digest=study_digest,
        _seal=probability_module._VERIFIED_STUDY_SEAL,  # noqa: SLF001
    )
    deployment_payload = {
        "study_evidence_digest": study_digest,
    }
    deployment = probability_module.VerifiedJointExecutionDeploymentEstimator(
        _encoded(deployment_payload),
        integrity_digest="d" * 64,
        _seal=probability_module._VERIFIED_DEPLOYMENT_SEAL,  # noqa: SLF001
    )
    source_records = [
        _source_record("600519.SH", status="success", raw_score=80.0, score=80),
        _source_record("300750.SZ", status="success", raw_score=79.0, score=79),
        _source_record("920001.BJ", status="missing", raw_score=50.0, score=50),
    ]
    source = source_module.VerifiedJointExecutionSourceCorpus(
        _encoded(source_records),
        artifact_digest="a" * 64,
        run_id=101,
        signal_session="2026-08-24",
        source_snapshot_digest="b" * 64,
        decision_identity_digest="c" * 64,
        decision_membership_digest="e" * 64,
        decision_frozen_at="2026-08-24T16:00:00+08:00",
        feature_schema_digest="f" * 64,
        _seal=source_module._VERIFIED_SOURCE_SEAL,  # noqa: SLF001
    )
    prediction_records = [
        _prediction_record("600519.SH", probability=0.2),
        _prediction_record("300750.SZ", probability=0.8),
        _prediction_record("920001.BJ", probability=0.5),
    ]
    predictions = probability_module.VerifiedJointExecutionCurrentPredictionCorpus(
        _encoded(prediction_records),
        artifact_digest="1" * 64,
        deployment_artifact_digest=deployment.integrity_digest,
        decision_identity_digest=source.decision_identity_digest,
        decision_membership_digest=source.decision_membership_digest,
        generated_at="2026-08-24T16:02:00+08:00",
        run_id=source.run_id,
        signal_session=source.signal_session,
        source_artifact_digest=source.artifact_digest,
        source_snapshot_digest=source.source_snapshot_digest,
        _seal=probability_module._VERIFIED_CURRENT_PREDICTION_SEAL,  # noqa: SLF001
    )
    shadow_payload = {
        "contract_version": "probability-ranking-preregistered-shadow-v1",
        "qualified": True,
        "generated_at": "2026-08-23T16:00:00+08:00",
        "study_evidence_digest": study_digest,
        "source_run_ids": [100],
    }
    shadow = ranking_module.VerifiedProbabilityRankingShadowEvaluation(
        _encoded(shadow_payload),
        integrity_digest="2" * 64,
        qualified=True,
        _seal=ranking_module._SHADOW_SEAL,  # noqa: SLF001
    )
    control_payload = {
        "contract_version": "probability-ranking-explicit-human-control-v1",
        "action": "promote",
        "generated_at": "2026-08-23T17:00:00+08:00",
        "reviewer_id": "risk-committee-01",
        "reviewer_role": "production-risk-owner",
        "change_ticket": "ASR-RANK-V6-001",
        "effective_after_run_id": 100,
        "automatic": False,
        "reason": "all preregistered shadow gates reviewed and accepted",
        "rollback_acknowledged": True,
        "shadow_artifact_digest": shadow.integrity_digest,
        "study_evidence_digest": study_digest,
        "score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
        "score_spec_hash": probability_ranking_score_spec_hash(),
    }
    control_artifact = seal_probability_ranking_manual_control_artifact(
        control_payload,
        generated_at=cast(str, control_payload["generated_at"]),
    )
    control_digest = cast(
        str, cast(dict[str, object], control_artifact["integrity"])["integrity_digest"]
    )
    promotion = verify_probability_ranking_manual_control_artifact(
        control_artifact,
        expected_digest=control_digest,
        shadow=shadow,
    )

    assert promotion.promotion_eligible is False
    for generated_at in ("2026-08-24T16:03:00+08:00", "2026-09-11T16:03:00+08:00"):
        with pytest.raises(ProbabilityRankingError, match="current inference-qualified"):
            build_probability_ranking_publication_artifact(
                source, predictions, study, deployment, promotion,
                generated_at=generated_at,
            )
    artifact = _legacy_ranking_artifacts()["build_probability_ranking_publication_artifact"][0]
    publication = verify_probability_ranking_publication_artifact(
        artifact,
        source=source,
        predictions=predictions,
        study=study,
        deployment=deployment,
        promotion=promotion,
    )

    assert [item["symbol"] for item in publication] == ["300750.SZ", "600519.SH"]
    assert [item["rank"] for item in publication] == [1, 2]
    assert [item["base_rank"] for item in publication] == [2, 1]
    assert [item["raw_score"] for item in publication] == [85.0, 74.0]
    assert publication.payload["base_v5_mutated"] is False
    assert publication.payload["historical_ranks_mutated"] is False

    database_path = tmp_path / "ranking.sqlite3"
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        initialize_schema(conn)
        conn.execute(
            """
            INSERT INTO market_scan_run (
                id, status, trigger, mode, rule_version, as_of,
                data_date, quote_date, scope, total_count, processed_count,
                success_count, created_at, updated_at, finished_at
            ) VALUES (
                101, 'running', 'manual', 'official', 'full-market-scan-v6:test',
                '2026-08-24T16:00:00+08:00', '2026-08-24', '2026-08-24', ?,
                2, 2, 2, '2026-08-24T15:00:00+08:00',
                '2026-08-24T16:00:00+08:00', '2026-08-24T16:00:00+08:00'
            )
            """,
            (FULL_MARKET_SCOPE,),
        )
        conn.executemany(
            """
            INSERT INTO market_scan_result (
                run_id, symbol, code, market, name, status,
                rank, score, raw_score, updated_at
            ) VALUES (101, ?, ?, ?, ?, 'success', ?, ?, ?, ?)
            """,
            (
                (
                    "600519.SH",
                    "600519",
                    "SH",
                    "贵州茅台",
                    1,
                    80,
                    80.0,
                    "2026-08-24T16:00:00+08:00",
                ),
                (
                    "300750.SZ",
                    "300750",
                    "SZ",
                    "宁德时代",
                    2,
                    79,
                    79.0,
                    "2026-08-24T16:00:00+08:00",
                ),
            ),
        )
        conn.execute(
            """
            UPDATE market_scan_run
            SET status = 'success', snapshot_digest = ?,
                snapshot_seal_origin = 'publication',
                snapshot_sealed_at = '2026-08-24T16:01:00+08:00'
            WHERE id = 101
            """,
            (publication.base_snapshot_digest,),
        )
    artifact_path = (
        tmp_path
        / "joint-research"
        / "production-rankings-v6"
        / (
            f"probability-ranking-v6-run-{source.run_id}-"
            f"{publication.artifact_digest}.json.gz"
        )
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(
        gzip.compress(canonical_json_bytes(artifact), compresslevel=9, mtime=0)
    )
    store = MarketScanProbabilityRankingStore(database_path)
    assert publication.current_write_eligible is False
    with pytest.raises(ProbabilityRankingError, match="historical v6 audit token"):
        store.publish(publication, artifact_path=artifact_path)
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_scan_probability_ranking_publication").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM market_scan_probability_ranking_result").fetchone()[0] == 0
        _restore_preexisting_baseline_mirror(conn, artifact_path)
    store.publish(publication, artifact_path=artifact_path)
    store.publish(publication, artifact_path=artifact_path)
    assert store.verify_mirror(publication) is True
    with pytest.raises(ProbabilityRankingError, match="different immutable"):
        store.publish(publication, artifact_path=tmp_path / "different.json.gz")

    class _Cache:
        path = database_path

    maintenance = MarketScanJointExecutionMaintenanceService(
        cast(object, _Cache()),
        object(),
        source_directory=tmp_path / "unused-sources",
        research_directory=tmp_path / "joint-research",
        ranking_store=store,
    )
    first_replayed, first_path = maintenance._ranking_publication_for_tokens(  # noqa: SLF001
        source,
        predictions,
        study,
        deployment,
        promotion,
        generated_at="2026-08-24T16:04:00+08:00",
    )
    second_replayed, second_path = maintenance._ranking_publication_for_tokens(  # noqa: SLF001
        source,
        predictions,
        study,
        deployment,
        promotion,
        generated_at="2026-08-24T16:05:00+08:00",
    )
    assert second_replayed.artifact_digest == first_replayed.artifact_digest
    assert second_path == first_path
    assert len(
        list(
            (tmp_path / "joint-research" / "production-rankings-v6").glob(
                "*.json.gz"
            )
        )
    ) == 1

    restarted = MarketScanJointExecutionMaintenanceService(
        cast(object, _Cache()),
        object(),
        source_directory=tmp_path / "unused-sources",
        research_directory=tmp_path / "joint-research",
        ranking_store=store,
    )
    monkeypatch.setattr(
        restarted,
        "_replay_historical_ranking",
        lambda path, sources, mature_pairs: publication,
    )
    assert restarted._replay_historical_rankings([source], []) == []  # noqa: SLF001
    assert restarted.has_production_ranking(source.run_id) is True
    restarted_context, restarted_records = restarted.production_ranking_projection(
        source.run_id
    )
    assert restarted_context["status"] == "active"
    assert restarted_context["artifact_digest"] == publication.artifact_digest
    assert set(restarted_records) == {"300750.SZ", "600519.SH"}
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM market_scan_probability_ranking_result"
        ).fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                """
                UPDATE market_scan_probability_ranking_result
                SET rank = 2 WHERE run_id = 101 AND symbol = '300750.SZ'
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                """
                UPDATE market_scan_result SET rank = 2
                WHERE run_id = 101 AND symbol = '600519.SH'
                """
            )

    tampered = deepcopy(artifact)
    rows = cast(
        list[dict[str, object]], cast(dict[str, object], tampered["payload"])["records"]
    )
    rows[0]["rank"] = 2
    with pytest.raises(ProbabilityRankingError, match="verification failed"):
        verify_probability_ranking_publication_artifact(
            tampered,
            source=source,
            predictions=predictions,
            study=study,
            deployment=deployment,
            promotion=promotion,
        )

    rollback_payload = {
        "contract_version": "probability-ranking-explicit-human-control-v1",
        "action": "rollback",
        "generated_at": "2026-08-24T17:00:00+08:00",
        "reviewer_id": "risk-committee-01",
        "reviewer_role": "production-risk-owner",
        "change_ticket": "ASR-RANK-V6-ROLLBACK-001",
        "effective_after_run_id": 101,
        "automatic": False,
        "reason": "production rollback drill",
        "rollback_acknowledged": True,
        "promotion_digest": promotion.integrity_digest,
        "publication_artifact_digest": publication.artifact_digest,
    }
    rollback_artifact = seal_probability_ranking_manual_control_artifact(
        rollback_payload,
        generated_at=cast(str, rollback_payload["generated_at"]),
    )
    rollback_digest = cast(
        str, cast(dict[str, object], rollback_artifact["integrity"])["integrity_digest"]
    )
    rollback = verify_probability_ranking_manual_control_artifact(
        rollback_artifact,
        expected_digest=rollback_digest,
        shadow=None,
    )
    assert store.is_rolled_back(publication) is False
    store.record_rollback(rollback)
    store.record_rollback(rollback)
    assert store.is_rolled_back(publication) is True
    conflicting_rollback_payload = {
        **rollback.payload,
        "reason": "different reason under reused digest",
    }
    conflicting_rollback = ranking_module.VerifiedProbabilityRankingManualControl(
        _encoded(conflicting_rollback_payload),
        action="rollback",
        effective_after_run_id=rollback.effective_after_run_id,
        generated_at=rollback.generated_at,
        integrity_digest=rollback.integrity_digest,
        _seal=ranking_module._CONTROL_SEAL,  # noqa: SLF001
    )
    with pytest.raises(ProbabilityRankingError, match="different data"):
        store.record_rollback(conflicting_rollback)

    restarted_after_rollback = MarketScanJointExecutionMaintenanceService(
        cast(object, _Cache()),
        object(),
        source_directory=tmp_path / "unused-sources",
        research_directory=tmp_path / "joint-research",
        ranking_store=store,
    )
    monkeypatch.setattr(
        restarted_after_rollback,
        "_replay_historical_ranking",
        lambda path, sources, mature_pairs: publication,
    )
    assert (
        restarted_after_rollback._replay_historical_rankings([source], [])  # noqa: SLF001
        == []
    )
    assert restarted_after_rollback.has_production_ranking(source.run_id) is False
    rollback_context, rollback_records = (
        restarted_after_rollback.production_ranking_projection(source.run_id)
    )
    assert rollback_context["status"] == "inactive"
    assert rollback_records == {}
    with pytest.raises(ProbabilityRankingError, match="rollback control"):
        build_probability_ranking_publication_artifact(
            source,
            predictions,
            study,
            deployment,
            rollback,
            generated_at="2026-08-24T17:01:00+08:00",
        )


def test_probability_ranking_shadow_is_preregistered_replayable_and_fail_closed() -> None:
    source_records = [
        _shadow_source_record("600519.SH", raw_score=80.0),
        _shadow_source_record("300750.SZ", raw_score=79.0),
    ]
    source = source_module.VerifiedJointExecutionSourceCorpus(
        _encoded(source_records),
        artifact_digest="a" * 64,
        run_id=102,
        signal_session="2026-08-25",
        source_snapshot_digest="b" * 64,
        decision_identity_digest="c" * 64,
        decision_membership_digest="d" * 64,
        decision_frozen_at="2026-08-25T16:00:00+08:00",
        feature_schema_digest="e" * 64,
        _seal=source_module._VERIFIED_SOURCE_SEAL,  # noqa: SLF001
    )
    predictions = [
        {
            "sample_id": "102:600519.SH:5:net_excess_positive",
            "probability": 0.6,
            "reference_base_rate": 0.5,
        },
        {
            "sample_id": "102:300750.SZ:5:net_excess_positive",
            "probability": 0.7,
            "reference_base_rate": 0.5,
        },
    ]
    study_payload = {
        "selection_qualified": True,
        "generated_at": "2026-08-25T15:00:00+08:00",
        "predictions": predictions,
    }
    study = probability_module.VerifiedJointExecutionProbabilityStudy(
        _encoded(study_payload),
        evidence_digest=_digest(study_payload),
        _seal=probability_module._VERIFIED_STUDY_SEAL,  # noqa: SLF001
    )
    reports = [
        _shadow_report(
            "600519.SH",
            probability=0.6,
            net_excess_return=0.01,
            source_snapshot_digest=source.source_snapshot_digest,
        ),
        _shadow_report(
            "300750.SZ",
            probability=0.7,
            net_excess_return=0.02,
            source_snapshot_digest=source.source_snapshot_digest,
        ),
    ]
    corpus = corpus_module.VerifiedJointExecutionProbabilityCorpusV3(
        encoded_reports=_encoded(reports),
        integrity_digest=_digest(reports),
        _seal=corpus_module._VERIFIED_CORPUS_SEAL,  # noqa: SLF001
    )

    artifact = build_probability_ranking_shadow_artifact(
        corpus,
        [source],
        study,
        generated_at="2026-09-10T02:00:00+08:00",
    )
    verified = verify_probability_ranking_shadow_artifact(
        artifact,
        oos_corpus=corpus,
        sources=[source],
        study=study,
    )

    assert verified.qualified is False
    assert verified.payload["decision_count"] == 2
    assert "minimum_60_distinct_shadow_sessions" in cast(
        list[str], verified.payload["failed_gates"]
    )
    assert verified.payload["automatic_promotion"] is False
    assert verified.payload["inference_contract"]["promotion_eligible"] is False
    legacy_artifact = _legacy_ranking_artifacts()["build_probability_ranking_shadow_artifact"][0]
    legacy = verify_probability_ranking_shadow_artifact(
        legacy_artifact, oos_corpus=corpus, sources=[source], study=study,
    )
    assert legacy.qualified is False
    assert legacy.payload == legacy_artifact["payload"]
    with pytest.raises(ProbabilityRankingError, match="predates current inference"):
        build_probability_ranking_shadow_artifact(
            corpus, [source], study, generated_at="2026-08-25T17:00:00+08:00",
        )

    tampered = deepcopy(artifact)
    cast(dict[str, object], tampered["payload"])["qualified"] = True
    with pytest.raises(ProbabilityRankingError, match="verification failed"):
        verify_probability_ranking_shadow_artifact(
            tampered,
            oos_corpus=corpus,
            sources=[source],
            study=study,
        )


def test_probability_ranking_tokens_statistics_and_scalar_guards_fail_closed() -> None:
    with pytest.raises(TypeError, match="strict replay"):
        ranking_module.VerifiedProbabilityRankingShadowEvaluation(
            "{}",
            integrity_digest="a" * 64,
            qualified=False,
        )
    with pytest.raises(TypeError, match="pinned verification"):
        ranking_module.VerifiedProbabilityRankingManualControl(
            "{}",
            action="promote",
            effective_after_run_id=1,
            generated_at="2026-08-23T12:00:00+08:00",
            integrity_digest="a" * 64,
        )
    with pytest.raises(TypeError, match="strict replay"):
        ranking_module.VerifiedProbabilityRankingPublication(
            "{}",
            artifact_digest="a" * 64,
            base_snapshot_digest="b" * 64,
            generated_at="2026-08-23T12:00:00+08:00",
            promotion_digest="c" * 64,
            run_id=1,
            score_spec_hash="d" * 64,
        )

    publication = ranking_module.VerifiedProbabilityRankingPublication(
        _encoded({"records": [{"symbol": "600519.SH"}, {"symbol": "300750.SZ"}]}),
        artifact_digest="a" * 64,
        base_snapshot_digest="b" * 64,
        generated_at="2026-08-23T12:00:00+08:00",
        promotion_digest="c" * 64,
        run_id=1,
        score_spec_hash="d" * 64,
        _seal=ranking_module._PUBLICATION_SEAL,  # noqa: SLF001
    )
    assert publication[0]["symbol"] == "600519.SH"
    assert len(publication[:1]) == 1
    assert set(publication.record_by_symbol()) == {"600519.SH", "300750.SZ"}

    assert ranking_module.probability_ranking_adjustment(1.0, 0.0) == 6.0
    assert ranking_module.probability_ranking_adjustment(0.0, 1.0) == -6.0
    assert ranking_module._exposure([]) == {  # noqa: SLF001
        "market": 0.0,
        "liquidity": 0.0,
        "industry_bucket": 0.0,
    }
    assert ranking_module._synthetic_horizon_excess_drawdown([0.1, -0.2, 0.05]) > 0  # noqa: SLF001
    with pytest.raises(ProbabilityRankingError, match="bootstrap"):
        ranking_module._block_bootstrap_mean_ci([], [], seed="a" * 64)  # noqa: SLF001
    assert ranking_module._block_bootstrap_mean_ci(  # noqa: SLF001
        ["2026-08-23"],
        [0.01],
        seed="a" * 64,
    ) == (0.01, 0.01)
    assert ranking_module._legacy_interleaved_pair_failure_rate(  # noqa: SLF001
        [0.0] * 15,
        [0.01] * 15,
    ) is None
    pbo = ranking_module._legacy_interleaved_pair_failure_rate(  # noqa: SLF001
        [0.0] * 16,
        [0.02, -0.01] * 8,
    )
    assert pbo is not None and 0 <= pbo <= 1
    assert ranking_module._legacy_pair_case_failed(  # noqa: SLF001
        [0, 1, 2, 3],
        [[item] for item in range(8)],
        [0.0] * 8,
        [-0.01] * 8,
    ) is None
    assert ranking_module._iid_zero_benchmark_probabilistic_sharpe([0.01] * 59) is None  # noqa: SLF001
    assert ranking_module._iid_zero_benchmark_probabilistic_sharpe([0.01] * 60) is None  # noqa: SLF001
    varied = [0.01 + (index % 5) * 0.001 for index in range(60)]
    assert ranking_module._iid_zero_benchmark_probabilistic_sharpe(varied) is not None  # noqa: SLF001

    invalid_calls = (
        (ranking_module._mapping, ([], "mapping")),  # noqa: SLF001
        (ranking_module._timestamp, ("bad", "timestamp")),  # noqa: SLF001
        (ranking_module._timestamp, ("2026-08-23T12:00:00", "timestamp")),  # noqa: SLF001
        (ranking_module._probability, (1.1, "probability")),  # noqa: SLF001
        (ranking_module._score, (-1.0, "score")),  # noqa: SLF001
        (ranking_module._integer_score, (True, "integer")),  # noqa: SLF001
        (ranking_module._integer_score, (1.5, "integer")),  # noqa: SLF001
        (ranking_module._finite_float, (True, "finite")),  # noqa: SLF001
        (ranking_module._finite_float, (math.inf, "finite")),  # noqa: SLF001
        (ranking_module._positive_int, (0, "positive")),  # noqa: SLF001
        (ranking_module._nonempty_text, (" ", "text")),  # noqa: SLF001
        (ranking_module._digest_text, ("x", "digest")),  # noqa: SLF001
    )
    for function, arguments in invalid_calls:
        with pytest.raises(ProbabilityRankingError):
            function(*arguments)


def test_probability_ranking_store_rejects_unverified_tokens_and_bad_paths(
    tmp_path: Path,
) -> None:
    store = MarketScanProbabilityRankingStore(tmp_path / "empty.sqlite3")
    with pytest.raises(ProbabilityRankingError, match="verified publication"):
        store.publish(object(), artifact_path=tmp_path / "bad.json.gz")  # type: ignore[arg-type]
    with pytest.raises(ProbabilityRankingError, match="path is invalid"):
        store._publication_artifact_path(tmp_path / "bad.json")  # noqa: SLF001
    assert store.verify_mirror(object()) is False  # type: ignore[arg-type]
    with pytest.raises(ProbabilityRankingError, match="rollback token"):
        store.record_rollback(object())  # type: ignore[arg-type]
    with pytest.raises(ProbabilityRankingError, match="publication token"):
        store.is_rolled_back(object())  # type: ignore[arg-type]
    assert ranking_module.probability_ranking_raw_score(0.0, 0.0, 1.0) == (
        -6.0,
        0.0,
        0,
    )
    assert ranking_store_module._same_float(1.0, 1.0 + 1e-10) is True
    assert ranking_store_module._same_float(True, 1.0) is False
    assert ranking_store_module._same_float("1", 1.0) is False


def test_probability_ranking_authority_and_envelope_guards_reject_substitution() -> None:
    source = source_module.VerifiedJointExecutionSourceCorpus(
        _encoded([_source_record("600519.SH", status="success", raw_score=80.0, score=80)]),
        artifact_digest="a" * 64,
        run_id=2,
        signal_session="2026-08-23",
        source_snapshot_digest="b" * 64,
        decision_identity_digest="c" * 64,
        decision_membership_digest="d" * 64,
        decision_frozen_at="2026-08-23T16:00:00+08:00",
        feature_schema_digest="e" * 64,
        _seal=source_module._VERIFIED_SOURCE_SEAL,  # noqa: SLF001
    )
    study_payload = {
        "selection_qualified": True,
        "generated_at": "2026-08-23T16:00:00+08:00",
        "predictions": [],
    }
    study = probability_module.VerifiedJointExecutionProbabilityStudy(
        _encoded(study_payload),
        evidence_digest="f" * 64,
        _seal=probability_module._VERIFIED_STUDY_SEAL,  # noqa: SLF001
    )
    deployment = probability_module.VerifiedJointExecutionDeploymentEstimator(
        _encoded({"study_evidence_digest": study.evidence_digest}),
        integrity_digest="1" * 64,
        _seal=probability_module._VERIFIED_DEPLOYMENT_SEAL,  # noqa: SLF001
    )
    predictions = probability_module.VerifiedJointExecutionCurrentPredictionCorpus(
        _encoded([_prediction_record("600519.SH", probability=0.6)]),
        artifact_digest="2" * 64,
        deployment_artifact_digest=deployment.integrity_digest,
        decision_identity_digest=source.decision_identity_digest,
        decision_membership_digest=source.decision_membership_digest,
        generated_at="2026-08-23T16:02:00+08:00",
        run_id=source.run_id,
        signal_session=source.signal_session,
        source_artifact_digest=source.artifact_digest,
        source_snapshot_digest=source.source_snapshot_digest,
        _seal=probability_module._VERIFIED_CURRENT_PREDICTION_SEAL,  # noqa: SLF001
    )
    promotion = ranking_module.VerifiedProbabilityRankingManualControl(
        _encoded(
            {
                "study_evidence_digest": study.evidence_digest,
                "score_spec_hash": probability_ranking_score_spec_hash(),
            }
        ),
        action="promote",
        effective_after_run_id=1,
        generated_at="2026-08-23T16:01:00+08:00",
        integrity_digest="3" * 64,
        _seal=ranking_module._CONTROL_SEAL,  # noqa: SLF001
    )
    oos = corpus_module.VerifiedJointExecutionProbabilityCorpusV3(
        encoded_reports="[]",
        integrity_digest="4" * 64,
        _seal=corpus_module._VERIFIED_CORPUS_SEAL,  # noqa: SLF001
    )

    with pytest.raises(ProbabilityRankingError, match="OOS"):
        ranking_module._require_shadow_tokens(object(), [source], study)  # noqa: SLF001
    with pytest.raises(ProbabilityRankingError, match="selected study"):
        ranking_module._require_shadow_tokens(oos, [source], object())  # noqa: SLF001
    with pytest.raises(ProbabilityRankingError, match="source tokens"):
        ranking_module._require_shadow_tokens(oos, [], study)  # noqa: SLF001
    unselected = probability_module.VerifiedJointExecutionProbabilityStudy(
        _encoded({"selection_qualified": False}),
        evidence_digest="5" * 64,
        _seal=probability_module._VERIFIED_STUDY_SEAL,  # noqa: SLF001
    )
    with pytest.raises(ProbabilityRankingError, match="selected probability"):
        ranking_module._require_shadow_tokens(oos, [source], unselected)  # noqa: SLF001

    invalid_publication_tokens = (
        (object(), predictions, study, deployment, promotion),
        (source, object(), study, deployment, promotion),
        (source, predictions, object(), deployment, promotion),
        (source, predictions, study, object(), promotion),
        (source, predictions, study, deployment, object()),
    )
    for tokens in invalid_publication_tokens:
        with pytest.raises(ProbabilityRankingError):
            ranking_module._require_publication_tokens(*tokens)  # noqa: SLF001
    rollback = ranking_module.VerifiedProbabilityRankingManualControl(
        "{}",
        action="rollback",
        effective_after_run_id=1,
        generated_at="2026-08-23T16:01:00+08:00",
        integrity_digest="6" * 64,
        _seal=ranking_module._CONTROL_SEAL,  # noqa: SLF001
    )
    with pytest.raises(ProbabilityRankingError, match="rollback"):
        ranking_module._require_publication_tokens(  # noqa: SLF001
            source,
            predictions,
            study,
            deployment,
            rollback,
        )
    wrong_predictions = probability_module.VerifiedJointExecutionCurrentPredictionCorpus(
        _encoded([_prediction_record("600519.SH", probability=0.6)]),
        artifact_digest="2" * 64,
        deployment_artifact_digest="9" * 64,
        decision_identity_digest=source.decision_identity_digest,
        decision_membership_digest=source.decision_membership_digest,
        generated_at="2026-08-23T16:02:00+08:00",
        run_id=source.run_id,
        signal_session=source.signal_session,
        source_artifact_digest=source.artifact_digest,
        source_snapshot_digest=source.source_snapshot_digest,
        _seal=probability_module._VERIFIED_CURRENT_PREDICTION_SEAL,  # noqa: SLF001
    )
    with pytest.raises(ProbabilityRankingError, match="bindings differ"):
        ranking_module._require_publication_tokens(  # noqa: SLF001
            source,
            wrong_predictions,
            study,
            deployment,
            promotion,
        )
    with pytest.raises(ProbabilityRankingError, match="duplicated"):
        ranking_module._unique_sources([source, source])  # noqa: SLF001
    with pytest.raises(ProbabilityRankingError, match="after promotion"):
        ranking_module._validate_publication_timing(  # noqa: SLF001
            source,
            predictions,
            promotion,
            datetime.fromisoformat("2026-08-23T16:00:00+08:00"),
        )

    with pytest.raises(ProbabilityRankingError, match="decision sets differ"):
        ranking_module._publication_candidates(  # noqa: SLF001
            source,
            predictions.__class__(
                "[]",
                artifact_digest=predictions.artifact_digest,
                deployment_artifact_digest=predictions.deployment_artifact_digest,
                decision_identity_digest=predictions.decision_identity_digest,
                decision_membership_digest=predictions.decision_membership_digest,
                generated_at=predictions.generated_at,
                run_id=predictions.run_id,
                signal_session=predictions.signal_session,
                source_artifact_digest=predictions.source_artifact_digest,
                source_snapshot_digest=predictions.source_snapshot_digest,
                _seal=probability_module._VERIFIED_CURRENT_PREDICTION_SEAL,  # noqa: SLF001
            ),
        )

    with pytest.raises(ProbabilityRankingError, match="one-hot"):
        ranking_module._ranking_categories({})  # noqa: SLF001
    with pytest.raises(ProbabilityRankingError, match="action is invalid"):
        ranking_module._manual_control_action({"action": "automatic"})  # noqa: SLF001
    with pytest.raises(ProbabilityRankingError, match="schema mismatch"):
        ranking_module._validate_manual_control_schema({}, "promote")  # noqa: SLF001
    with pytest.raises(ProbabilityRankingError, match="safety contract"):
        ranking_module._validate_manual_control_common({}, {})  # noqa: SLF001
    with pytest.raises(ProbabilityRankingError, match="qualified shadow"):
        ranking_module._validate_manual_promotion(  # noqa: SLF001
            {},
            None,
            1,
            datetime.fromisoformat("2026-08-23T16:00:00+08:00"),
        )
    with pytest.raises(ProbabilityRankingError, match="sha256"):
        ranking_module._validate_manual_rollback({"promotion_digest": "bad"})  # noqa: SLF001

    generated_at = "2026-08-23T16:00:00+08:00"
    with pytest.raises(ProbabilityRankingError, match="timestamp differs"):
        ranking_module._seal_envelope(  # noqa: SLF001
            "test-v1",
            {"generated_at": "2026-08-23T15:00:00+08:00"},
            generated_at=generated_at,
        )
    envelope = ranking_module._seal_envelope(  # noqa: SLF001
        "test-v1",
        {"generated_at": generated_at},
        generated_at=generated_at,
    )
    payload, digest = ranking_module._verify_envelope(  # noqa: SLF001
        envelope,
        schema_version="test-v1",
    )
    assert payload["generated_at"] == generated_at
    assert len(digest) == 64
    for changed in (
        {**envelope, "extra": True},
        {**envelope, "schema_version": "wrong"},
        {**envelope, "integrity": {}},
        {**envelope, "generated_at": "bad"},
    ):
        with pytest.raises(ProbabilityRankingError):
            ranking_module._verify_envelope(changed, schema_version="test-v1")  # noqa: SLF001

def _source_record(
    symbol: str,
    *,
    status: str,
    raw_score: float,
    score: int,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "result_status": status,
        "features": {
            "final_score_score": float(score),
            "raw_score": raw_score,
        },
        "record_digest": _digest([symbol, status, raw_score, score]),
    }


def _prediction_record(symbol: str, *, probability: float) -> dict[str, object]:
    return {
        "symbol": symbol,
        "probability": probability,
        "reference_base_rate": 0.5,
        "record_digest": _digest([symbol, probability]),
    }


def _shadow_source_record(symbol: str, *, raw_score: float) -> dict[str, object]:
    record = _source_record(
        symbol,
        status="success",
        raw_score=raw_score,
        score=round(raw_score),
    )
    features = cast(dict[str, float], record["features"])
    features.update(
        {
            "market_sh": 1.0,
            "market_sz": 0.0,
            "liquidity_medium": 1.0,
            "liquidity_high": 0.0,
            "industry_bucket_01": 1.0,
            "industry_bucket_02": 0.0,
        }
    )
    return record


def _shadow_report(
    symbol: str,
    *,
    probability: float,
    net_excess_return: float,
    source_snapshot_digest: str,
) -> dict[str, object]:
    return {
        "sample_id": f"102:{symbol}:5:net_excess_positive",
        "symbol": symbol,
        "signal_session": "2026-08-25",
        "decision_set": {
            "source_run_id": 102,
            "source_snapshot_digest": source_snapshot_digest,
        },
        "probabilities": {"action_probability": probability},
        "observed_outcome": {
            "entry_fill": True,
            "exit_executable": True,
            "net_excess_return": net_excess_return,
        },
        "entry_state": {"execution_state": "executable"},
    }


def _encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    from app.artifacts.io import canonical_json_bytes, sha256_hex

    return sha256_hex(canonical_json_bytes(value))


def _legacy_ranking_artifacts() -> dict[str, list[dict[str, object]]]:
    return json.loads(
        (Path(__file__).parent / "fixtures" / "probability_ranking_legacy_v1_artifacts.json").read_text()
    )


def _restore_preexisting_baseline_mirror(conn: sqlite3.Connection, artifact_path: Path) -> None:
    mirror = json.loads(
        (Path(__file__).parent / "fixtures" / "probability_ranking_legacy_v1_mirror.json").read_text()
    )
    publication = mirror["publication"]
    publication["artifact_path"] = str(artifact_path)
    for table, rows in (("market_scan_probability_ranking_publication", [publication]),
                        ("market_scan_probability_ranking_result", mirror["results"])):
        for row in rows:
            names = list(row)
            conn.execute(
                f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                tuple(row[name] for name in names),
            )
