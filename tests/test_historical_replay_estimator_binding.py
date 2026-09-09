"""Historical OHLCV fits explicitly bind estimator provenance across upgrades."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services import market_scan_probability_replay as replay
from app.services.experimental_probability_model import build_experimental_model
from app.services.market_scan_probability import (
    PROBABILITY_LABEL_VERSION, ProbabilityConfig, build_probability_contract,
    stable_probability_hash,
)
from app.services.market_scan_probability_historical_context import (
    HistoricalProbabilityContextError, build_historical_probability_context,
    verify_historical_probability_context,
)
from tests.test_market_scan_probability_replay import (
    _daily_catalog, _initialize, _patch_calendar, _seed_bars,
)


LEGACY_PATH = Path(__file__).parent / "fixtures/historical_replay_v1_shared_label_v3.json"


def _legacy():
    return json.loads(LEGACY_PATH.read_text())


def _new_report(tmp_path, monkeypatch):
    catalog = _daily_catalog(90)
    database = tmp_path / "historical-fit.sqlite3"
    _initialize(database)
    _seed_bars(database, ("600001.SH",), catalog)
    _patch_calendar(monkeypatch, catalog)
    signal = catalog[61].isoformat()
    return replay.evaluate_market_scan_probability_replay(
        database, config=replay.HistoricalReplayConfig(
            signal, signal, symbol_limit=1, symbols=("600001.SH",),
        ), generated_at="2026-08-11T12:00:00Z",
    )


def test_genuine_old_current_split_archive_requires_regeneration_without_refitting(monkeypatch):
    before = LEGACY_PATH.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported fit must not be silently recomputed")

    monkeypatch.setattr(replay, "fit_shadow_probability", forbidden)
    with pytest.raises(replay.HistoricalReplayFitContractSupersededError) as error:
        replay.verify_historical_replay_artifact(_legacy())
    assert error.value.code == "superseded-fit-contract"
    assert "重新生成" in str(error.value)
    with pytest.raises(replay.HistoricalReplayFitContractSupersededError):
        replay.load_historical_replay_artifact(LEGACY_PATH)
    assert LEGACY_PATH.read_bytes() == before


def test_damaged_old_envelope_is_not_misclassified_as_a_superseded_fit():
    artifact = _legacy()
    artifact["integrity"]["integrity_digest"] = "f" * 64
    with pytest.raises(replay.HistoricalReplayError) as error:
        replay.verify_historical_replay_artifact(artifact)
    assert not isinstance(error.value, replay.HistoricalReplayFitContractSupersededError)


def test_current_artifact_binds_private_label_and_registered_estimator(tmp_path, monkeypatch):
    report = _new_report(tmp_path, monkeypatch)
    binding = report["probability_fit"]["estimator_binding"]
    assert binding["label_source"] == "historical-replay-fixed-session-cost-label-v1"
    assert binding["label_execution_evidence"] == "fixed-session-cost-only-not-phase-tradeability"
    assert binding["shared_estimator_label_version"] == PROBABILITY_LABEL_VERSION
    assert binding["registered_config_digests"] == {
        str(horizon): stable_probability_hash(build_probability_contract(
            ProbabilityConfig(horizon=horizon, target="net_return_positive"),
        )) for horizon in (1, 5, 20)
    }
    artifact = replay.build_historical_replay_artifact(report)
    assert artifact["schema_version"] == "market-scan-probability-historical-replay-artifact-v2"
    assert replay.verify_historical_replay_artifact(artifact) == artifact
    target = tmp_path / replay.historical_replay_artifact_filename(artifact)
    target.write_text(replay.canonical_historical_replay_json(artifact))
    assert replay.load_historical_replay_artifact(target) == artifact


@pytest.mark.parametrize("mutation", ["missing_binding", "old_metadata", "changed_digest", "old_split"])
def test_current_builder_cannot_reseal_unbound_or_changed_fit(current_report, mutation):
    report = deepcopy(current_report)
    fit = report["probability_fit"]
    error_type = replay.HistoricalReplayFitContractSupersededError
    if mutation == "missing_binding":
        fit.pop("estimator_binding")
    elif mutation == "old_metadata":
        fit["estimator_binding"]["shared_estimator_label_version"] = "market-scan-upside-label-v3-explicit-target-offset"
    elif mutation == "changed_digest":
        fit["horizons"]["5"]["evidence_digest"] = "f" * 64
        error_type = replay.HistoricalReplayError
    else:
        report["quality"]["registered_probability_split_defaults"] = replay._superseded_registered_split_defaults()  # noqa: SLF001
    with pytest.raises(error_type):
        replay.build_historical_replay_artifact(report)


@pytest.fixture
def current_report(tmp_path, monkeypatch):
    return _new_report(tmp_path, monkeypatch)


def test_context_and_experimental_consumers_keep_the_superseded_diagnosis(tmp_path):
    before = LEGACY_PATH.read_bytes()
    with pytest.raises(HistoricalProbabilityContextError, match="superseded-fit-contract"):
        build_historical_probability_context(LEGACY_PATH)
    with pytest.raises(replay.HistoricalReplayFitContractSupersededError):
        build_experimental_model(LEGACY_PATH, tmp_path)
    assert not list(tmp_path.iterdir())
    assert LEGACY_PATH.read_bytes() == before


def test_compact_context_reports_a_superseded_full_replay_source(tmp_path, current_report):
    artifact = replay.build_historical_replay_artifact(current_report)
    path = tmp_path / replay.historical_replay_artifact_filename(artifact)
    path.write_text(replay.canonical_historical_replay_json(artifact))
    context = build_historical_probability_context(path)
    context["payload"]["source_artifact"]["schema_version"] = replay.HISTORICAL_REPLAY_SUPERSEDED_ARTIFACT_SCHEMA_VERSION
    context["integrity"]["integrity_digest"] = sha256_hex(canonical_json_bytes({
        key: value for key, value in context.items() if key != "integrity"
    }))
    with pytest.raises(HistoricalProbabilityContextError, match="superseded-fit-contract"):
        verify_historical_probability_context(context)
