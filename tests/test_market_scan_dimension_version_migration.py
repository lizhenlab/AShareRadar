"""Literal pre-change artifacts must remain auditable without cross-version fitting."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.market_scan import MarketScanResultItem, MarketScanRun
from app.services import market_scan_probability as probability
from app.services import market_scan_probability_maintenance as maintenance
from app.services import market_scan_probability_outcomes as outcomes
from app.services import market_scan_probability_source as source
from app.services import market_scan_scoring as scoring
from app.services import market_scan_joint_execution_probability as joint
from app.services import market_scan_evaluation as evaluation
from app.services import market_scan_probability_research as research
from app.repositories.market_scan_mapping import encode_result_payload
from app.services.market_scan_probability_labels import ProbabilityLabelOutcome
from app.services import individual_probability_artifact as individual
from app.services.market_scan_joint_execution_source import JointExecutionSourceError, _joint_feature_schema
from app.services.market_scan_probability_research import probability_feature_vector
from app.services.market_scan_probability_source_research import MarketScanProbabilitySourceResearchStore
from tests import test_market_scan_probability_source as support
from tests.test_market_scan_probability import _signal_samples, _small_config
from tests.test_market_scan_raw_score_replay import _snapshot
from tests.test_market_scan_probability_preload_process import _source_archive


FIXTURES = Path(__file__).parent / "fixtures"
OLD_SPEC_HASH = "17c0e6b9ed6de9b39cad0d9f9d1fe14c8638749dd08796bed827869d3554e2c7"


@pytest.fixture(autouse=True)
def compact_coverage(monkeypatch):
    support._compact_source_coverage_contract.__wrapped__(monkeypatch)


def _old_source():
    return json.loads((FIXTURES / "market_scan_probability_source_dimension_v4.json").read_text())


@pytest.mark.parametrize("index,mode", [(0, "official"), (1, "preopen"), (2, "intraday")])
def test_frozen_dimension_v4_replays_exactly_and_new_risk_does_not_rewrite_rank(monkeypatch, index, mode):
    path = FIXTURES / "market_scan_dimensions_v4_snapshots.json"
    frozen = path.read_bytes()
    entry = json.loads(frozen)[index]
    item = MarketScanResultItem.model_validate(entry["item"])
    run = MarketScanRun.model_validate(entry["run"])
    details = deepcopy(item.score_details)
    scoring.verify_persisted_market_scan_result(
        item, run, expected_score_rule_version=scoring.FULL_MARKET_SCORE_RULE_VERSION,
        expected_score_spec_hash=OLD_SPEC_HASH,
    )
    assert item.score_details == details
    assert item.score_details["score_spec_hash"] == OLD_SPEC_HASH
    assert not scoring.is_current_market_scan_score_spec(details["score_spec"], OLD_SPEC_HASH)
    current, _run = _snapshot(monkeypatch, mutation="none", mode=mode)
    assert current.score_details["score_spec_hash"] != OLD_SPEC_HASH
    assert (current.raw_score, current.score, current.trend_score) == (item.raw_score, item.score, item.trend_score)
    old_dimensions = details["components"]["score_dimensions"]
    new_dimensions = current.score_details["components"]["score_dimensions"]
    assert old_dimensions["raw_features"]["downside_volatility_20d_pct"] == 0.0
    assert new_dimensions["raw_features"]["downside_volatility_20d_pct"] > 0.0
    # This small single loss remains below the existing 1% scoring threshold.
    assert new_dimensions["scores"]["risk"] == old_dimensions["scores"]["risk"]
    assert path.read_bytes() == frozen


def test_frozen_source_keeps_original_bytes_hash_and_projection(tmp_path):
    artifact = _old_source()
    assert artifact["payload"]["feature_schema"]["version"] == probability.PREVIOUS_PROBABILITY_FEATURE_VERSION
    assert artifact["payload"]["run"]["production_score_spec_hash"] == OLD_SPEC_HASH
    assert source.verify_probability_source_snapshot(artifact) == artifact
    assert scoring.stable_score_spec_hash(scoring.market_scan_score_spec_dimension_v4(min_data_quality_score=50)) == OLD_SPEC_HASH
    assert source.is_registered_production_score_contract(scoring.FULL_MARKET_SCORE_RULE_VERSION, OLD_SPEC_HASH)
    assert not source.is_current_writable_production_score_contract(scoring.FULL_MARKET_SCORE_RULE_VERSION, OLD_SPEC_HASH)
    path = tmp_path / source.probability_source_snapshot_filename(70, artifact)
    path.write_bytes(source._compressed_artifact_bytes(artifact))
    before = path.read_bytes()
    assert source.load_probability_source_snapshot(path) == artifact
    store = MarketScanProbabilitySourceResearchStore(tmp_path)
    assert store.preload() == 1
    assert store.research_projection(70)["run_binding"]["production_score_spec_hash"] == OLD_SPEC_HASH
    assert path.read_bytes() == before


@pytest.mark.parametrize("old_to_new", [True, False])
def test_resealed_feature_stamp_cannot_cross_score_semantics(old_to_new):
    artifact = _old_source() if old_to_new else support._build_current_source(support._source_projection(("600519.SH", "SH", "SH_MAIN")))
    payload = artifact["payload"]
    version = probability.PROBABILITY_FEATURE_VERSION if old_to_new else probability.PREVIOUS_PROBABILITY_FEATURE_VERSION
    payload["feature_schema"] = source._feature_schema(version)
    artifact["integrity"]["integrity_digest"] = source.probability_source_payload_digest(payload)
    with pytest.raises(source.ProbabilitySourceError, match="计算口径冲突"):
        source.verify_probability_source_snapshot(artifact)


def test_legacy_v1_cannot_relabel_old_risk_as_current_features_or_enter_fit(tmp_path):
    path = _source_archive(tmp_path)
    artifact = source.load_probability_source_snapshot(path)
    assert artifact["payload"]["contract_version"] == source.LEGACY_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
    before = deepcopy(artifact["payload"]["records"])
    artifact["payload"]["feature_schema"] = source._feature_schema(probability.PROBABILITY_FEATURE_VERSION)
    artifact["integrity"]["integrity_digest"] = source.probability_source_payload_digest(artifact["payload"])
    assert artifact["payload"]["records"] == before
    with pytest.raises(source.ProbabilitySourceError, match="缺少评分身份"):
        source.verify_probability_source_snapshot(artifact)
    # A second boundary still refuses an internally supplied, unbound payload.
    with pytest.raises(outcomes.ProbabilityOutcomeError, match="完整评分身份"):
        outcomes._joined_source_payload(
            artifact, {}, {"integrity_digest": artifact["integrity"]["integrity_digest"]},
            artifact["payload"]["cohort"], 71,
        )


@pytest.mark.parametrize("version,liquidity,expected", [
    (probability.LEGACY_PROBABILITY_FEATURE_VERSION, "mid", 1.0),
    (probability.LEGACY_PROBABILITY_FEATURE_VERSION, "medium", 0.0),
    (probability.PREVIOUS_PROBABILITY_FEATURE_VERSION, "medium", 1.0),
    (probability.PROBABILITY_FEATURE_VERSION, "medium", 1.0),
])
def test_feature_migration_preserves_historical_liquidity_encoding(version, liquidity, expected):
    vector = probability_feature_vector({}, market="SH", board="SH_MAIN", liquidity=liquidity, regime="neutral", feature_version=version)
    assert vector["liquidity_mid"] == expected


def test_old_source_cannot_enter_new_fit_or_joint_decision_source():
    artifact = _old_source()
    payload = artifact["payload"]
    with pytest.raises(outcomes.ProbabilityOutcomeError, match="仅供审计"):
        outcomes._joined_source_payload(
            artifact, {}, {"integrity_digest": artifact["integrity"]["integrity_digest"]},
            payload["cohort"], 70,
        )
    with pytest.raises(JointExecutionSourceError, match="base feature version"):
        _joint_feature_schema(payload, {record["symbol"]: record for record in payload["records"]})
    current = support._build_current_source(support._source_projection(("600519.SH", "SH", "SH_MAIN")))
    schema = _joint_feature_schema(current["payload"], {row["symbol"]: row for row in current["payload"]["records"]})
    assert schema["base_version"] == probability.PROBABILITY_FEATURE_VERSION
    assert schema["version"] == "full-market-point-in-time-features-v5-target-semideviation-all-decisions"


def test_maintenance_excludes_old_features_before_fit_readiness(monkeypatch):
    old = maintenance._SourceManifest(Path("old"), 1, "2026-08-11", "2026-08-11T16:00:00+08:00", "2026-08-11T16:01:00+08:00", ("official", "all", "same-rule"), "a" * 64, feature_version=probability.PREVIOUS_PROBABILITY_FEATURE_VERSION)
    current = replace(old, run_id=2, path=Path("current"), feature_version=probability.PROBABILITY_FEATURE_VERSION)
    labels = {1: object(), 2: object()}
    monkeypatch.setattr(maintenance, "probability_fit_corpus_ready", lambda _rows: True)
    ready = maintenance._ready_fit_cohorts([old, current], labels)
    assert len(ready) == 1 and [pair[0].run_id for pair in ready[0]] == [2]


def test_joint_deployment_cannot_extend_old_semantics_with_identical_feature_names():
    current_version = "full-market-point-in-time-features-v5-target-semideviation-all-decisions"
    corpus = SimpleNamespace(feature_version=current_version, feature_names=("risk",), label_contract_digest="a" * 64)
    evidence = {
        "feature_version": "full-market-point-in-time-features-v4-all-decisions-median-imputed",
        "label_contract_digest": "a" * 64,
        "model": {"components": {name: {"feature_names": ["risk"]} for name in joint.JOINT_EXECUTION_COMPONENTS}},
    }
    assert not joint._deployment_schema_matches(corpus, evidence)
    evidence["feature_version"] = current_version
    assert joint._deployment_schema_matches(corpus, evidence)


def test_actual_frozen_fitted_model_is_rejected_and_current_fit_replays():
    path = FIXTURES / "market_scan_probability_feature_v3_fit.json"
    encoded = path.read_bytes()
    old = json.loads(encoded)
    assert old["status"] == "calibrated_shadow" and old["model"]["converged"] is True
    assert old["evidence_digest"] == "4479ecc5d3080e1c6e8887bfad97637f66c12fe244d879f786f7c585c9aea4ce"
    with pytest.raises(probability.ProbabilityReplayError, match="契约不是已注册版本"):
        probability.verify_shadow_probability_evidence(old)
    with pytest.raises(probability.ProbabilityReplayError, match="契约不是已注册版本"):
        probability.predict_shadow_probability(old, {"trend": 1.0, "risk": -0.4})
    current = probability.fit_shadow_probability(_signal_samples(42), config=_small_config(), generated_at="2026-08-11T08:00:00Z")
    assert current["status"] == "calibrated_shadow"
    assert current["feature_version"] == probability.PROBABILITY_FEATURE_VERSION
    probability.verify_shadow_probability_evidence(current)
    assert current["evidence_digest"] != old["evidence_digest"]
    assert path.read_bytes() == encoded


def test_individual_current_estimator_rejects_old_feature_while_legacy_audit_contract_survives():
    contract = individual.individual_probability_estimator_contract()
    old = {**contract, "estimator_feature_version": probability.PREVIOUS_PROBABILITY_FEATURE_VERSION}
    with pytest.raises(individual.IndividualProbabilityArtifactError, match="estimator contract"):
        individual._validate_estimator_contract(old, legacy_source_binding=False)
    individual._validate_estimator_contract(contract, legacy_source_binding=False)
    legacy = {**old, "estimator_label_version": "market-scan-upside-label-v3-explicit-target-offset"}
    individual._validate_estimator_contract(legacy, legacy_source_binding=True)


def test_all_preexisting_literal_source_fixtures_stay_registered():
    for name in ("market_scan_probability_source_v2_v4.json", "market_scan_probability_source_v3_legacy_v5.json", "market_scan_probability_source_trend_v3.json"):
        path = FIXTURES / name
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        artifact = json.loads(path.read_bytes())
        assert source.verify_probability_source_snapshot(artifact) == artifact
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_sqlite_evaluation_preserves_old_rows_dates_and_evidence_but_refuses_training(monkeypatch):
    old = json.loads((FIXTURES / "market_scan_dimensions_v4_snapshots.json").read_text())[0]["item"]
    current, _run = _snapshot(monkeypatch, mutation="none", mode="official")
    observations = []
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        for index, details in enumerate((old["score_details"], current.score_details)):
            stored = connection.execute("SELECT ? AS metrics_json", (encode_result_payload({}, details),)).fetchone()
            contract = evaluation._probability_score_contract(stored)
            assert contract == (scoring.FULL_MARKET_SCORE_RULE_VERSION, details["score_spec_hash"])
            evidence_digest = evaluation._source_evidence_digest(stored)
            assert evidence_digest == details["components"]["score_dimensions"]["point_in_time_evidence"]["payload_digest"]
            observations.append(SimpleNamespace(
                run_id=index + 1, quote_date=f"2026-07-{16 + index}", symbol="600519.SH", market="SH", board="SH_MAIN",
                industry="白酒", liquidity_bucket="medium", regime="neutral", segment="regular", raw_score=80.0 + index,
                factor_values={"raw_score": 80.0 + index, "risk": 20.0}, mode="official", scope="full-market", rule_version="frozen-scan",
                probability_labels={1: ProbabilityLabelOutcome(1, "modelled", "verified", label=1, net_return=0.01)},
                source_evidence_digest=evidence_digest, production_score_contract=contract,
            ))
    snapshots = [SimpleNamespace(observations=(item,), eligible_dates=("2026-07-20", "2026-07-21")) for item in observations]
    rows = evaluation._probability_research_rows(snapshots)
    assert [row.session_date for row in rows] == ["2026-07-16", "2026-07-17"]
    assert [row.source_feature_contract_current for row in rows] == [False, True]
    assert [row.source_evidence_digest for row in rows] == [item.source_evidence_digest for item in observations]
    sample = research._probability_sample(rows[0], {(1, 1): 0.0}, 1, "net_excess_positive")
    assert sample.target is None and sample.executable is False
    assert sample.session_date == "2026-07-16" and sample.net_return == 0.01
    result = research.build_probability_research(rows, generated_at="2026-08-11T08:00:00Z", bootstrap_samples=100)
    assert {row["quote_date"] for row in result["records"]} == {"2026-07-16", "2026-07-17"}
    old_records = [row for row in result["records"] if row["run_id"] == 1]
    assert old_records and all("source_feature_contract_not_current_audit_only" in row["limitations"] for row in old_records)
    payload = research.probability_artifact_payload(result)
    old_artifact_records = [row for row in payload["records"] if row["run_id"] == 1]
    assert old_artifact_records and all(row["details"]["executable"] is False and row["details"]["model_target"] is None for row in old_artifact_records)
