"""Frozen old labels replay under their own semantics but cannot train v4."""

from copy import deepcopy
import gzip

import pytest

from app.services import market_scan_probability as probability
from app.services import market_scan_probability_outcomes as outcomes
from app.services.market_scan_probability_labels import (
    PREVIOUS_PROBABILITY_LABEL_VERSION,
    PROBABILITY_LABEL_VERSION,
    SUPERSEDED_PROBABILITY_LABEL_VERSIONS,
    ProbabilityLabelConfig,
    probability_label_contract,
)
from tests.test_market_scan_probability_outcomes import GENERATED_AT, _complete_h1_rows, _source


def _artifact(monkeypatch, *, opening_lock=True):
    source = _source()
    monkeypatch.setattr(outcomes, "_source_artifact", lambda _value: source)
    rows = _complete_h1_rows()
    if opening_lock:
        rows[-1] = rows[-1].model_copy(update={"session_status": "trading", "open_execution_status": "locked_limit_down"})
    return outcomes.build_probability_outcome_artifact(
        source, {"600001.SH": rows}, generated_at=GENERATED_AT, as_of_date="2026-08-13",
    )


def _seal(artifact):
    payload = artifact["payload"]
    payload["label_contract_digest"] = probability.stable_probability_hash(payload["label_contract"])
    payload["quality"] = outcomes._quality(payload["records"], payload["source"], (1, 5, 20))
    artifact["integrity"]["integrity_digest"] = outcomes.probability_outcome_payload_digest(payload)
    return artifact


def _legacy(artifact, version, *, opening_lock=True):
    result = deepcopy(artifact)
    payload = result["payload"]
    payload["label_contract"] = probability_label_contract(label_version=version)
    if opening_lock:
        # Exact old result captured before the fix; do not calculate it with the
        # new implementation when testing the historical replay boundary.
        payload["records"][0]["horizons"]["1"]["outcome"] = {
            "horizon": 1, "status": "unfilled", "reason": "locked_limit_down", "label": None,
            "gross_return": None, "net_return": None, "cost_drag": None,
            "entry_date": "2026-08-12", "exit_date": "2026-08-13", "entry_price": None, "exit_price": None,
            "model_limited": True, "rule_profile_verified": True, "daily_bar_model_limited": True,
        }
    return _seal(result)


@pytest.mark.parametrize("version", SUPERSEDED_PROBABILITY_LABEL_VERSIONS)
@pytest.mark.parametrize("opening_lock", [False, True])
def test_frozen_old_labels_validate_without_rewrite_then_require_regeneration(tmp_path, monkeypatch, version, opening_lock):
    current = _artifact(monkeypatch, opening_lock=opening_lock)
    legacy = _legacy(current, version, opening_lock=opening_lock)
    assert current["payload"]["label_contract"]["label_version"] == PROBABILITY_LABEL_VERSION
    assert current["integrity"]["integrity_digest"] != legacy["integrity"]["integrity_digest"]
    assert current["payload"]["records"][0]["horizons"]["1"]["outcome"]["status"] == "modelled"
    digest = legacy["integrity"]["integrity_digest"]
    path = tmp_path / f"market-scan-probability-outcomes-run-71-through-2026-08-13-{digest}.json.gz"
    frozen = gzip.compress(outcomes._canonical_json(legacy).encode(), compresslevel=9, mtime=0)
    path.write_bytes(frozen)
    with pytest.raises(outcomes.ProbabilityOutcomeSemanticDriftError, match="旧执行时点契约") as drift:
        outcomes.load_probability_outcome_artifact(path)
    assert drift.value.integrity_digest == digest and drift.value.run_id == 71
    assert path.read_bytes() == frozen
    with pytest.raises(outcomes.ProbabilityOutcomeSemanticDriftError):
        outcomes.probability_research_rows_from_outcome_artifacts([_source()], [legacy])
    with pytest.raises(ValueError, match="label_version"):
        probability._bound_label_contract(probability.ProbabilityConfig(label_contract=legacy["payload"]["label_contract"]))


@pytest.mark.parametrize("version", SUPERSEDED_PROBABILITY_LABEL_VERSIONS)
def test_resigning_old_outcome_as_new_version_cannot_upgrade_old_execution_semantics(monkeypatch, version):
    current = _artifact(monkeypatch)
    forged = _legacy(current, version)
    forged["payload"]["label_contract"] = current["payload"]["label_contract"]
    _seal(forged)
    with pytest.raises(outcomes.ProbabilityOutcomeError, match="不能由固定会话K线重放") as error:
        outcomes.verify_probability_outcome_artifact(forged)
    assert not isinstance(error.value, outcomes.ProbabilityOutcomeSemanticDriftError)


@pytest.mark.parametrize("version", SUPERSEDED_PROBABILITY_LABEL_VERSIONS)
def test_old_contract_still_checks_all_siblings_before_classifying_semantic_drift(monkeypatch, version):
    legacy = _legacy(_artifact(monkeypatch), version)
    sibling = deepcopy(legacy["payload"]["records"][0])
    sibling["symbol"] = "600002.SH"
    sibling["horizons"]["1"]["outcome"]["label"] = 1
    legacy["payload"]["records"].append(sibling)
    _seal(legacy)
    with pytest.raises(outcomes.ProbabilityOutcomeError, match="不能由固定会话K线重放") as error:
        outcomes.verify_probability_outcome_artifact(legacy)
    assert not isinstance(error.value, outcomes.ProbabilityOutcomeSemanticDriftError)


def test_legacy_contract_digest_damage_is_integrity_error_before_replay(monkeypatch):
    legacy = _legacy(_artifact(monkeypatch), SUPERSEDED_PROBABILITY_LABEL_VERSIONS[-1])
    legacy["payload"]["label_contract_digest"] = "b" * 64
    legacy["integrity"]["integrity_digest"] = outcomes.probability_outcome_payload_digest(legacy["payload"])
    with pytest.raises(outcomes.ProbabilityOutcomeError, match="label_contract_digest") as error:
        outcomes.verify_probability_outcome_artifact(legacy)
    assert not isinstance(error.value, outcomes.ProbabilityOutcomeSemanticDriftError)


def test_previous_label_contract_keeps_its_pre_fix_digest():
    previous = probability_label_contract(ProbabilityLabelConfig(horizons=(1,)), label_version=PREVIOUS_PROBABILITY_LABEL_VERSION)
    assert probability.stable_probability_hash(previous) == "a8adec9282391169ff6f9630d2756952b2882967af0db1ac7a26713efb6021cb"
