"""Fixed-session artifacts must retain the execution restrictions used by labels."""

from copy import deepcopy
from dataclasses import asdict
import gzip
import json
from pathlib import Path

import pytest

from app.services import market_scan_probability_outcomes as outcomes
from app.services import market_scan_probability_maintenance as maintenance
from app.services.market_scan_probability import stable_probability_hash
from app.services.market_scan_probability_labels import ProbabilityLabelConfig, build_probability_label_outcomes
from tests.test_market_scan_probability_outcomes import GENERATED_AT, _complete_h1_rows, _source


def _build(monkeypatch, rows=None):
    source = _source()
    monkeypatch.setattr(outcomes, "_source_artifact", lambda _: source)
    return outcomes.build_probability_outcome_artifact(
        source, {"600001.SH": rows if rows is not None else _complete_h1_rows()},
        generated_at=GENERATED_AT, as_of_date="2026-08-13",
    )


@pytest.mark.parametrize("position,field,value", [
    (1, "session_status", "suspended"), (2, "session_status", "suspended"),
    (1, "open_execution_status", "locked_limit_up"),
    (2, "open_execution_status", "locked_limit_down"),
    (1, "open_execution_status", "unavailable"), (2, "open_execution_status", "unavailable"),
    (1, "open_execution_status", "locked_limit_down"), (2, "open_execution_status", "locked_limit_up"),
])
def test_artifact_labels_and_replay_preserve_every_execution_restriction(monkeypatch, position, field, value) -> None:
    rows = _complete_h1_rows()
    rows[position] = rows[position].model_copy(update={field: value})
    expected = build_probability_label_outcomes(
        symbol="600001.SH", market="SH", list_date="2020-01-02", is_st=False,
        quote_date="2026-08-11", amount=200_000_000, rows=rows,
        eligible_dates=("2026-08-12", "2026-08-13"), config=ProbabilityLabelConfig(horizons=(1,)),
    )[1]
    artifact = _build(monkeypatch, rows)
    record = artifact["payload"]["records"][0]

    assert record["horizons"]["1"]["outcome"] == asdict(expected)
    assert record["bar_evidence"]["bars"][position][field] == value
    assert outcomes.verify_probability_outcome_artifact(artifact) == artifact


@pytest.mark.parametrize("reverse", [False, True])
def test_same_day_execution_evidence_cannot_disappear_during_artifact_normalization(monkeypatch, reverse) -> None:
    rows = _complete_h1_rows()
    restricted = rows[1].model_copy(update={"session_status": "suspended"})
    rows = [restricted, *rows] if reverse else [*rows, restricted]
    with pytest.raises(outcomes.ProbabilityOutcomeError, match="冲突K线"):
        _build(monkeypatch, rows)


def _legacy_v1():
    return json.loads((Path(__file__).parent / "fixtures" / "probability_outcome_bar_evidence_v1.json").read_text())


def test_intact_legacy_bar_evidence_is_not_silently_upgraded_or_used_for_labels(monkeypatch, tmp_path) -> None:
    artifact = _legacy_v1()
    digest = artifact["integrity"]["integrity_digest"]
    path = tmp_path / f"market-scan-probability-outcomes-run-71-through-2026-08-13-{digest}.json.gz"
    path.write_bytes(gzip.compress(outcomes._canonical_json(artifact).encode(), compresslevel=9, mtime=0))

    with pytest.raises(outcomes.ProbabilityOutcomeSemanticDriftError, match="执行状态") as error:
        outcomes.load_probability_outcome_artifact(path)
    assert error.value.run_id == 71 and error.value.integrity_digest == digest
    manifest = maintenance._outcome_catalog_entry(path)
    assert isinstance(manifest, maintenance._OutcomeSemanticDriftManifest)
    assert manifest.run_id == 71 and manifest.source_digest == "a" * 64
    with pytest.raises(outcomes.ProbabilityOutcomeSemanticDriftError):
        outcomes.verify_probability_outcome_artifact(artifact)


@pytest.mark.parametrize("reseal", [False, True])
def test_corrupt_legacy_bar_evidence_is_not_misclassified_as_safe_semantic_drift(monkeypatch, reseal) -> None:
    artifact = _legacy_v1()
    artifact["payload"]["records"][0]["bar_evidence"]["bars"][1]["high"] = 1.0
    if reseal:
        evidence = artifact["payload"]["records"][0]["bar_evidence"]
        evidence["bar_set_digest"] = stable_probability_hash(evidence["bars"])
        artifact["integrity"]["integrity_digest"] = outcomes.probability_outcome_payload_digest(artifact["payload"])
    with pytest.raises(outcomes.ProbabilityOutcomeError) as error:
        outcomes.verify_probability_outcome_artifact(artifact)
    assert not isinstance(error.value, outcomes.ProbabilityOutcomeSemanticDriftError)


def test_current_evidence_cannot_omit_execution_fields_or_reseal_a_different_label(monkeypatch) -> None:
    artifact = _build(monkeypatch)
    missing = deepcopy(artifact)
    missing["payload"]["records"][0]["bar_evidence"]["bars"][1].pop("session_status", None)
    missing["integrity"]["integrity_digest"] = outcomes.probability_outcome_payload_digest(missing["payload"])
    with pytest.raises(outcomes.ProbabilityOutcomeError):
        outcomes.verify_probability_outcome_artifact(missing)

    changed = deepcopy(artifact)
    evidence = changed["payload"]["records"][0]["bar_evidence"]
    evidence["bars"][1]["session_status"] = "suspended"
    evidence["bar_set_digest"] = stable_probability_hash(evidence["bars"])
    changed["integrity"]["integrity_digest"] = outcomes.probability_outcome_payload_digest(changed["payload"])
    with pytest.raises(outcomes.ProbabilityOutcomeError, match="不能由固定会话K线重放"):
        outcomes.verify_probability_outcome_artifact(changed)
