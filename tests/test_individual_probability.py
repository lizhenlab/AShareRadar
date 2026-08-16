from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

import app.models.individual_probability as individual_model_module
from app.models.individual_probability import (
    IndividualProbabilityCounts,
    IndividualProbabilityEvidence,
    IndividualProbabilityInterval,
    IndividualProbabilityMetrics,
    IndividualProbabilityTargetContract,
    IndividualUpsideHorizon,
    IndividualUpsideProbabilityReport,
    REGISTERED_FEATURE_VERSION,
    REGISTERED_MODEL_VERSION,
)
from app.artifacts.io import ArtifactIOError, canonical_json_bytes, sha256_hex
from app.services import individual_probability as service_module
from app.services import individual_probability_artifact as artifact_module
from app.services import market_scan_probability_source as source_module
from app.services.individual_probability import (
    IndividualProbabilityStore,
    project_individual_upside_probability,
)
from app.services.individual_probability_artifact import (
    INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION,
    INDIVIDUAL_PROBABILITY_HISTORY_DATABASE_MAX_BYTES,
    IndividualProbabilityArtifactError,
    LEGACY_INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION,
    individual_probability_estimator_contract,
    individual_probability_target_contract,
    load_individual_probability_assessment,
    verify_individual_probability_assessment,
    write_individual_probability_assessment,
)
from app.services.paper_trading_costs import resolve_cost_profile
from app.services.market_scan_scoring import (
    FULL_MARKET_SCORE_RULE_VERSION,
    market_scan_score_spec,
    stable_score_spec_hash,
)
from tools import evaluate_individual_probability as evaluation_cli
from tests import test_market_scan_probability_source as probability_source_test_support


ASSESSMENT = Path(
    "docs/research/artifacts/" "individual-upside-probability-assessment-" "517691b101dcb2142693a74f6e5ac9ef10f386c545572b6bacfe161f186ba677.json"
)
VALID_REPORT_TIME = "2026-08-12T18:00:00+08:00"
PRODUCTION_SCORE_SPEC_HASH = stable_score_spec_hash(market_scan_score_spec(min_data_quality_score=50))


@pytest.fixture(autouse=True)
def _compact_official_source_coverage_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        source_module,
        "PROBABILITY_SOURCE_MINIMUM_POPULATION",
        {"ALL": 1, "SH": 1, "SZ": 1, "BJ": 1},
    )
    monkeypatch.setattr(
        source_module,
        "PROBABILITY_SOURCE_MINIMUM_COVERAGE",
        {scope: 0.0 for scope in ("ALL", "SH", "SZ", "BJ")},
    )
    monkeypatch.setattr(
        source_module,
        "PROBABILITY_SOURCE_MINIMUM_ELIGIBLE_RATIO",
        {scope: 0.0 for scope in ("ALL", "SH", "SZ", "BJ")},
    )


def test_real_assessment_projects_three_independent_null_horizons() -> None:
    assessment = load_individual_probability_assessment(ASSESSMENT)
    report = project_individual_upside_probability("600519", assessment)

    assert assessment["schema_version"] == LEGACY_INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION
    assert report.symbol == "600519.SH"
    assert report.signal_date is None
    assert report.generated_at == assessment["generated_at"]
    assert report.status == "insufficient_data"
    assert [(item.display_day, item.holding_sessions) for item in report.horizons] == [
        (2, 1),
        (3, 2),
        (4, 3),
    ]
    assert all(item.probability is None and item.confidence_interval is None for item in report.horizons)
    assert [item.counts.independent_session_count for item in report.horizons] == [279, 279, 279]
    assert [item.counts.out_of_sample_session_count for item in report.horizons] == [60, 60, 60]
    assert report.evidence.official_pit_session_count == 0
    assert report.evidence.required_official_pit_session_count == 288
    assert report.evidence.historical_replay_official is False
    assert report.evidence.selection_qualified is False
    assert "legacy_official_pit_sources_audit_only_not_current_evidence" in report.limitations
    assert "compact_horizon_metrics_not_independently_replayable" in report.limitations
    assert report.production_effect == "none"


def test_historical_metrics_are_diagnostics_not_current_probability() -> None:
    report = project_individual_upside_probability("600519.SH", load_individual_probability_assessment(ASSESSMENT))
    horizon = report.horizons[0]

    assert horizon.calibration_metrics is not None
    assert horizon.calibration_metrics.brier_score == pytest.approx(0.2488552947734205)
    assert horizon.calibration_metrics.actual_positive_rate_ci_95 is not None
    assert horizon.calibration_metrics.actual_positive_rate_ci_95.level == 0.95
    assert horizon.probability is None
    assert horizon.confidence_interval is None
    assert "historical_replay_not_official_point_in_time" in horizon.gate_reasons


def test_missing_assessment_is_typed_not_generated() -> None:
    report = project_individual_upside_probability("000001", None, generated_at=VALID_REPORT_TIME)

    assert report.status == "not_generated"
    assert report.signal_date is None
    assert report.generated_at == VALID_REPORT_TIME
    assert all(item.status == "not_generated" and item.probability is None for item in report.horizons)
    assert report.evidence.required_official_pit_session_count == 288


def test_assessment_v2_binds_h1_purged_estimator_and_split_contract() -> None:
    assessment = load_individual_probability_assessment(ASSESSMENT)
    payload = assessment["payload"]
    assert isinstance(payload, dict)
    contract = payload["estimator_contract"]

    assert contract == individual_probability_estimator_contract()
    assert contract["split_version"] == "grouped-date-multifold-target-offset-purge-v3"
    assert contract["model_version"] == ("shadow-up-probability-logit-l2-v2-convergence-required")
    assert contract["estimator_feature_version"] == ("full-market-point-in-time-features-v3-liquidity-medium")
    assert contract["required_official_pit_sessions"] == 288
    longest = contract["horizon_split_contracts"]["3"]
    assert longest["entry_session_offset"] == 1
    assert longest["target_session_offset"] == 4
    assert longest["gap_sessions"] == 4
    assert longest["minimum_selection_independent_sessions"] == 288


def test_superseded_assessment_v1_is_explicitly_replay_only_not_runtime() -> None:
    legacy = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    legacy["schema_version"] = "individual-upside-probability-assessment-v1"
    _reseal(legacy)

    with pytest.raises(IndividualProbabilityArtifactError, match="仅供历史审计"):
        verify_individual_probability_assessment(legacy)

    report = project_individual_upside_probability("600519", legacy, generated_at=VALID_REPORT_TIME)
    assert report.status == "not_generated"
    assert report.evidence.required_official_pit_session_count == 288
    assert report.evidence.assessment_digest is None
    assert report.limitations[0] == "assessment_invalid_or_unreadable"


def test_superseded_assessment_v3_is_audit_only_not_current_runtime() -> None:
    superseded = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    superseded["schema_version"] = "individual-upside-probability-assessment-v3-source-contract-bound"
    _reseal(superseded)

    with pytest.raises(IndividualProbabilityArtifactError, match="仅供历史审计"):
        verify_individual_probability_assessment(superseded)


def test_store_rejects_superseded_v1_candidate_instead_of_serving_it(
    tmp_path: Path,
) -> None:
    legacy = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    legacy["schema_version"] = "individual-upside-probability-assessment-v1"
    _reseal(legacy)
    digest = legacy["integrity"]["integrity_digest"]
    candidate = tmp_path / f"individual-upside-probability-assessment-{digest}.json"
    candidate.write_text(json.dumps(legacy), encoding="utf-8")

    with pytest.raises(IndividualProbabilityArtifactError, match="损坏证据"):
        IndividualProbabilityStore(tmp_path).latest()


def test_interval_contract_is_object_and_rejects_invalid_values() -> None:
    assert IndividualProbabilityInterval(lower=0.2, upper=0.8).model_dump(mode="json") == {
        "lower": 0.2,
        "upper": 0.8,
        "level": 0.95,
    }
    with pytest.raises(ValidationError):
        IndividualProbabilityInterval(lower=0.8, upper=0.2)
    with pytest.raises(ValidationError):
        IndividualProbabilityInterval(lower=0.2, upper=0.8, level=0.9)
    with pytest.raises(ValidationError):
        IndividualProbabilityInterval(lower=float("nan"), upper=0.8)


def test_historical_rate_interval_is_same_json_shape_and_covers_rate() -> None:
    metrics = IndividualProbabilityMetrics(
        actual_positive_rate=0.5,
        actual_positive_rate_ci_95={"lower": 0.4, "upper": 0.6, "level": 0.95},
    )
    assert metrics.model_dump(mode="json")["actual_positive_rate_ci_95"] == {
        "lower": 0.4,
        "upper": 0.6,
        "level": 0.95,
    }
    with pytest.raises(ValidationError):
        IndividualProbabilityMetrics(
            actual_positive_rate=0.7,
            actual_positive_rate_ci_95={"lower": 0.4, "upper": 0.6, "level": 0.95},
        )


def test_report_allows_mixed_independently_gated_horizon_states() -> None:
    counts = _calibrated_counts(2)
    calibrated = _calibrated_horizon(2)
    report = IndividualUpsideProbabilityReport(
        symbol="600519.SH",
        signal_date="2026-08-12",
        generated_at="2026-08-12T15:15:00+08:00",
        status="calibrated_shadow",
        target_contract=IndividualProbabilityTargetContract.model_validate(individual_probability_target_contract()),
        horizons=[
            calibrated,
            _empty_horizon(3, 2, counts),
            _empty_horizon(4, 3, counts),
        ],
        evidence=_calibrated_report_evidence(),
    )
    assert report.horizons[0].probability == 0.55
    assert report.horizons[1].probability is None


def test_report_rejects_child_probability_that_bypasses_report_gate() -> None:
    counts = IndividualProbabilityCounts()
    with pytest.raises(ValidationError):
        IndividualUpsideProbabilityReport(
            symbol="600519.SH",
            generated_at=VALID_REPORT_TIME,
            status="insufficient_data",
            target_contract=IndividualProbabilityTargetContract.model_validate(individual_probability_target_contract()),
            horizons=[
                IndividualUpsideHorizon(
                    display_day=2,
                    holding_sessions=1,
                    status="calibrated_shadow",
                    probability=0.55,
                    confidence_interval={"lower": 0.5, "upper": 0.6, "level": 0.95},
                    counts=counts,
                    feature_version=REGISTERED_FEATURE_VERSION,
                ),
                _empty_horizon(3, 2, counts),
                _empty_horizon(4, 3, counts),
            ],
            evidence=IndividualProbabilityEvidence(required_official_pit_session_count=288),
        )


@pytest.mark.parametrize(
    "counts",
    [
        {"observation_count": 1, "eligible_observation_count": 2},
        {
            "observation_count": 2,
            "eligible_observation_count": 1,
            "out_of_sample_observation_count": 2,
        },
        {
            "independent_session_count": 1,
            "out_of_sample_session_count": 2,
        },
    ],
)
def test_count_contract_rejects_impossible_cohort_order(counts: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        IndividualProbabilityCounts.model_validate(counts)


@pytest.mark.parametrize(
    ("counts", "message"),
    [
        (
            {"eligible_observation_count": 2, "independent_session_count": 3},
            "independent sessions",
        ),
        (
            {
                "eligible_observation_count": 2,
                "independent_session_count": 2,
                "out_of_sample_observation_count": 1,
                "out_of_sample_session_count": 2,
            },
            "OOS sessions 不能超过 OOS observations",
        ),
    ],
)
def test_count_contract_rejects_session_counts_without_observations(
    counts: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        IndividualProbabilityCounts.model_validate({"observation_count": 2, **counts})


def test_target_contract_rejects_shifted_display_exit() -> None:
    contract = individual_probability_target_contract()
    contract["exits"] = {
        "D+2": "D_plus_3_close_holding_session_2",
        "D+3": "D_plus_3_close_holding_session_2",
        "D+4": "D_plus_4_close_holding_session_3",
    }

    with pytest.raises(ValidationError, match=r"D\+2/D\+3/D\+4"):
        IndividualProbabilityTargetContract.model_validate(contract)


def test_target_contract_rejects_changed_execution_notional() -> None:
    contract = individual_probability_target_contract()
    contract["execution_notional"] = 99_999.0

    with pytest.raises(ValidationError, match="名义本金"):
        IndividualProbabilityTargetContract.model_validate(contract)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not-a-time", "有效时间"),
        ("2026-08-12T15:15:00", "含时区"),
        ("2099-01-01T00:00:00+08:00", "不能晚于当前时间"),
    ],
)
def test_public_report_time_parser_fails_closed(
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        individual_model_module._aware_report_time(value)  # noqa: SLF001


@pytest.mark.parametrize(
    ("signal_date", "message"),
    [
        ("20260812", "ISO 日期"),
        ("2026-05-01", "可信交易所交易日"),
    ],
)
def test_public_signal_date_requires_registered_exchange_session(
    signal_date: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        individual_model_module._validate_signal_maturity(  # noqa: SLF001
            signal_date,
            datetime(2026, 8, 12, 16, tzinfo=timezone(timedelta(hours=8))),
        )


@pytest.mark.parametrize("training_cutoff", ["garbage", "2026-05-01", "2026-08-12"])
def test_public_calibrated_training_cutoff_is_prior_exchange_session(
    training_cutoff: str,
) -> None:
    horizon = _calibrated_horizon(2).model_copy(update={"training_cutoff": training_cutoff})
    report = SimpleNamespace(horizons=[horizon])
    with pytest.raises(ValueError, match="training_cutoff"):
        individual_model_module._validate_training_cutoffs(  # noqa: SLF001
            report,
            "2026-08-12",
        )


def test_public_brier_skill_must_be_replayable_from_declared_scores() -> None:
    with pytest.raises(ValidationError, match="Brier skill"):
        IndividualProbabilityMetrics(
            brier_score=0.9,
            reference_brier_score=0.1,
            brier_skill_score=0.2,
        )


def test_child_probability_cannot_bypass_an_uncalibrated_report() -> None:
    counts = _calibrated_counts(2)
    with pytest.raises(ValidationError, match="子周期不能绕过"):
        IndividualUpsideProbabilityReport(
            symbol="600519.SH",
            generated_at=VALID_REPORT_TIME,
            status="insufficient_data",
            target_contract=IndividualProbabilityTargetContract.model_validate(individual_probability_target_contract()),
            horizons=[
                _calibrated_horizon(2),
                _empty_horizon(3, 2, counts),
                _empty_horizon(4, 3, counts),
            ],
            evidence=IndividualProbabilityEvidence(required_official_pit_session_count=288),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"display_day": 3}, "展示日"),
        ({"status": "insufficient_data", "probability": 0.5}, "必须为 null"),
        ({"status": "calibrated_shadow"}, "同时提供"),
        (
            {
                "status": "calibrated_shadow",
                "probability": 0.5,
                "confidence_interval": {"lower": 0.4, "upper": 0.6},
                "gate_reasons": ["selection_gate_failed:brier_skill"],
            },
            "阻断或限制原因",
        ),
        (
            {
                "status": "calibrated_shadow",
                "probability": 0.8,
                "confidence_interval": {"lower": 0.4, "upper": 0.6},
            },
            "必须覆盖 probability",
        ),
    ],
)
def test_horizon_contract_fails_closed_for_inconsistent_probability_state(
    overrides: dict[str, object],
    message: str,
) -> None:
    value: dict[str, object] = {
        "display_day": 2,
        "holding_sessions": 1,
        "status": "insufficient_data",
        "counts": {},
        "feature_version": REGISTERED_FEATURE_VERSION,
    }
    value.update(overrides)
    if value.get("status") == "calibrated_shadow" and value.get("probability") is not None:
        value.update(
            counts=_calibrated_counts(2).model_dump(),
            base_rate=0.5,
            calibration_metrics=_calibrated_metrics().model_dump(),
            training_cutoff="2026-08-11",
            model_version=REGISTERED_MODEL_VERSION,
            evidence_digest="a" * 64,
        )

    with pytest.raises(ValidationError, match=message):
        IndividualUpsideHorizon.model_validate(value)


def test_report_contract_requires_exact_horizons_and_gate_binding() -> None:
    contract = IndividualProbabilityTargetContract.model_validate(individual_probability_target_contract())
    counts = IndividualProbabilityCounts()
    base = {
        "symbol": "600519.SH",
        "generated_at": VALID_REPORT_TIME,
        "status": "insufficient_data",
        "target_contract": contract,
        "horizons": [
            _empty_horizon(2, 1, counts),
            _empty_horizon(3, 2, counts),
            _empty_horizon(4, 3, counts),
        ],
        "evidence": IndividualProbabilityEvidence(required_official_pit_session_count=288),
    }

    wrong_horizons = dict(base)
    wrong_horizons["horizons"] = [
        _empty_horizon(2, 1, counts),
        _empty_horizon(4, 3, counts),
        _empty_horizon(3, 2, counts),
    ]
    with pytest.raises(ValidationError, match="各返回一次"):
        IndividualUpsideProbabilityReport.model_validate(wrong_horizons)

    no_report_gate = dict(base, status="calibrated_shadow")
    with pytest.raises(ValidationError, match="selection 门禁"):
        IndividualUpsideProbabilityReport.model_validate(no_report_gate)

    no_calibrated_child = dict(base, status="calibrated_shadow")
    no_calibrated_child["signal_date"] = "2026-08-12"
    no_calibrated_child["generated_at"] = "2026-08-12T15:15:00+08:00"
    no_calibrated_child["evidence"] = _calibrated_report_evidence()
    with pytest.raises(ValidationError, match="至少一个独立周期"):
        IndividualUpsideProbabilityReport.model_validate(no_calibrated_child)

    false_status_with_gate = dict(base, status="insufficient_data")
    false_status_with_gate["signal_date"] = "2026-08-12"
    false_status_with_gate["generated_at"] = "2026-08-12T15:15:00+08:00"
    false_status_with_gate["evidence"] = _calibrated_report_evidence()
    with pytest.raises(ValidationError, match="不能声明 selection_qualified"):
        IndividualUpsideProbabilityReport.model_validate(false_status_with_gate)


def test_calibrated_report_cannot_bypass_official_or_oos_authorization() -> None:
    counts = _calibrated_counts(2)
    calibrated = _calibrated_horizon(2)
    base = {
        "symbol": "600519.SH",
        "signal_date": "2026-08-12",
        "generated_at": "2026-08-12T15:15:00+08:00",
        "status": "calibrated_shadow",
        "target_contract": IndividualProbabilityTargetContract.model_validate(individual_probability_target_contract()),
        "horizons": [calibrated, _empty_horizon(3, 2, counts), _empty_horizon(4, 3, counts)],
        "evidence": _calibrated_report_evidence(),
    }

    for updates in (
        {"signal_date": None},
        {"generated_at": "2026-08-12T15:14:59+08:00"},
        {"evidence": base["evidence"].model_copy(update={"official_pit_session_count": 287})},
    ):
        with pytest.raises(ValidationError):
            IndividualUpsideProbabilityReport.model_validate({**base, **updates})

    with pytest.raises(ValidationError, match="folds"):
        IndividualUpsideHorizon.model_validate(
            {
                **calibrated.model_dump(),
                "counts": {**counts.model_dump(), "evaluated_fold_count": 0},
            }
        )


def test_store_fails_closed_when_any_candidate_is_corrupt(tmp_path: Path) -> None:
    good = tmp_path / ASSESSMENT.name
    good.write_bytes(ASSESSMENT.read_bytes())
    corrupt = tmp_path / ("individual-upside-probability-assessment-" + "f" * 64 + ".json")
    corrupt.write_text("{}", encoding="utf-8")

    with pytest.raises(IndividualProbabilityArtifactError, match="损坏证据"):
        IndividualProbabilityStore(tmp_path).latest()


def test_store_rejects_matching_candidate_symlink_without_reading_target(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-individual-probability.json"
    outside.write_text("secret", encoding="utf-8")
    candidate = tmp_path / ("individual-upside-probability-assessment-" + "e" * 64 + ".json")
    candidate.symlink_to(outside)

    with pytest.raises(IndividualProbabilityArtifactError, match="非普通候选文件"):
        IndividualProbabilityStore(tmp_path).latest()
    assert outside.read_text(encoding="utf-8") == "secret"


def test_store_rejects_symlink_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(IndividualProbabilityArtifactError, match="符号链接"):
        IndividualProbabilityStore(link).latest()


def test_store_rejects_broken_symlink_directory_without_fallback(tmp_path: Path) -> None:
    link = tmp_path / "broken"
    link.symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(IndividualProbabilityArtifactError, match="符号链接"):
        IndividualProbabilityStore(link, fallback_directory=ASSESSMENT.parent).latest()


def test_store_selects_semantic_generated_at_not_mutable_mtime(tmp_path: Path) -> None:
    original = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    older_path = tmp_path / ASSESSMENT.name
    older_path.write_bytes(ASSESSMENT.read_bytes())
    # A single valid artifact still loads regardless of materialization metadata.
    store = IndividualProbabilityStore(tmp_path)
    assert store.latest() == original


def test_store_uses_tracked_baseline_only_when_primary_has_no_candidate(tmp_path: Path) -> None:
    store = IndividualProbabilityStore(
        tmp_path / "primary",
        fallback_directory=ASSESSMENT.parent,
    )

    assert store.latest() == json.loads(ASSESSMENT.read_text(encoding="utf-8"))


def test_store_empty_primary_without_fallback_is_cached_as_none(tmp_path: Path) -> None:
    store = IndividualProbabilityStore(tmp_path / "missing")

    assert store.latest() is None
    assert store.latest() is None


def test_store_reuses_verified_candidate_until_fingerprint_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / ASSESSMENT.name
    target.write_bytes(ASSESSMENT.read_bytes())
    calls = 0
    original = service_module.load_individual_probability_assessment

    def counted_load(path: str | Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(service_module, "load_individual_probability_assessment", counted_load)
    store = IndividualProbabilityStore(tmp_path)

    first = store.latest()
    second = store.latest()

    assert first == second
    assert first is not second
    assert calls == 1


def test_store_return_value_cannot_poison_nested_verified_cache(tmp_path: Path) -> None:
    target = tmp_path / ASSESSMENT.name
    target.write_bytes(ASSESSMENT.read_bytes())
    store = IndividualProbabilityStore(tmp_path)

    first = store.latest()
    assert first is not None
    payload = first["payload"]
    assert isinstance(payload, dict)
    limitations = payload["limitations"]
    assert isinstance(limitations, list)
    limitations.append("caller_injected_limitation")
    horizons = payload["horizons"]
    assert isinstance(horizons, dict)
    horizon = horizons["1"]
    assert isinstance(horizon, dict)
    metrics = horizon["calibration_metrics"]
    assert isinstance(metrics, dict)
    metrics["brier_score"] = 0.0

    second = store.latest()
    assert second is not None
    second_payload = second["payload"]
    assert isinstance(second_payload, dict)
    assert "caller_injected_limitation" not in second_payload["limitations"]
    second_horizons = second_payload["horizons"]
    assert isinstance(second_horizons, dict)
    second_horizon = second_horizons["1"]
    assert isinstance(second_horizon, dict)
    second_metrics = second_horizon["calibration_metrics"]
    assert isinstance(second_metrics, dict)
    assert second_metrics["brier_score"] == pytest.approx(0.2488552947734205)


def test_store_detects_same_size_same_mtime_candidate_rewrite(tmp_path: Path) -> None:
    target = tmp_path / ASSESSMENT.name
    original = ASSESSMENT.read_bytes()
    target.write_bytes(original)
    store = IndividualProbabilityStore(tmp_path)
    assert store.latest() is not None
    facts = target.stat()

    target.write_bytes(b"X" * len(original))
    os.utime(target, ns=(facts.st_atime_ns, facts.st_mtime_ns))

    with pytest.raises(IndividualProbabilityArtifactError, match="损坏证据"):
        store.latest()


def test_store_rejects_content_address_mismatch(tmp_path: Path) -> None:
    wrong_name = tmp_path / ("individual-upside-probability-assessment-" + "0" * 64 + ".json")
    wrong_name.write_bytes(ASSESSMENT.read_bytes())

    with pytest.raises(IndividualProbabilityArtifactError, match="内容地址不一致"):
        IndividualProbabilityStore(tmp_path).latest()


def test_store_defensively_rejects_unparseable_loaded_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / ("individual-upside-probability-assessment-" + "0" * 64 + ".json")
    candidate.write_text("{}", encoding="utf-8")
    artifact = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    artifact["generated_at"] = "not-a-time"
    artifact["integrity"]["integrity_digest"] = "0" * 64
    monkeypatch.setattr(
        service_module,
        "load_individual_probability_assessment",
        lambda _path: artifact,
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="generated_at 无效"):
        IndividualProbabilityStore(tmp_path).latest()


def test_store_factory_binds_primary_and_tracked_fallback(tmp_path: Path) -> None:
    store = service_module.individual_probability_store_for_cache_path(tmp_path / "cache.sqlite3")

    assert store.directory == tmp_path / "research" / "individual_probability"
    assert store.fallback_directory is None


def test_projection_guard_withholds_probability_even_after_future_gates_pass() -> None:
    evidence = deepcopy(json.loads(ASSESSMENT.read_text(encoding="utf-8"))["payload"]["horizons"]["1"])
    evidence["selection_qualified"] = True
    evidence["gate_reasons"] = []

    projected = service_module._horizon_projection(evidence, all_official=True)

    assert projected.status == "insufficient_data"
    assert projected.probability is None
    assert "current_stock_replayable_predictor_not_persisted" in projected.gate_reasons


def test_compact_nonofficial_fit_can_never_self_authorize_current_selection() -> None:
    core = {
        "status": "calibrated_shadow",
        "selection_qualified": True,
        "selection_qualification": {"gates": {"all_registered_gates": True}},
        "counts": {
            "observation_count": 300,
            "eligible_observation_count": 300,
            "available_independent_session_count": 300,
            "out_of_sample_observation_count": 120,
            "out_of_sample_session_count": 120,
            "evaluated_fold_count": 2,
        },
        "calibration_metrics": None,
        "training_cutoff": "2026-01-01",
        "base_rate": 0.5,
        "model_version": artifact_module.PROBABILITY_MODEL_VERSION,
        "feature_version": artifact_module.PROBABILITY_FEATURE_VERSION,
        "evidence_digest": "a" * 64,
        "limitations": [],
    }

    compact = artifact_module._compact_horizon_evidence(
        core,
        holding=1,
        official_session_count=288,
    )

    assert compact["fit_status"] == "calibrated_shadow"
    assert compact["selection_qualified"] is False
    assert "historical_replay_not_official_point_in_time" in compact["gate_reasons"]


def test_projection_handles_absent_interval_and_non_list_gate_reasons() -> None:
    evidence = deepcopy(json.loads(ASSESSMENT.read_text(encoding="utf-8"))["payload"]["horizons"]["1"])
    evidence["gate_reasons"] = "untrusted-string"
    evidence["calibration_metrics"]["actual_positive_rate_ci_95"] = None

    projected = service_module._horizon_projection(evidence, all_official=False)

    assert projected.calibration_metrics is not None
    assert projected.calibration_metrics.actual_positive_rate_ci_95 is None
    assert projected.gate_reasons == ["official_pit_and_replay_gate_not_satisfied"]


def test_projection_invalid_nested_shape_degrades_to_typed_not_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    artifact["payload"] = "not-an-object"
    monkeypatch.setattr(
        service_module,
        "load_or_validate_assessment",
        lambda _value: artifact,
    )

    report = project_individual_upside_probability("600519", artifact, generated_at=VALID_REPORT_TIME)

    assert report.status == "not_generated"
    assert report.limitations[0] == "assessment_invalid_or_unreadable"


def test_store_path_must_be_real_directory(tmp_path: Path) -> None:
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("x", encoding="utf-8")

    with pytest.raises(IndividualProbabilityArtifactError, match="必须是目录"):
        service_module._require_real_directory(regular_file)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("generated_at",), "not-a-time"),
        (("integrity", "notice"), "not-the-registered-notice"),
        (("payload", "source", "history_manifest_digest"), "g" * 64),
        (("payload", "source", "historical_replay_start_date"), "not-a-date"),
        (("payload", "source", "historical_replay_end_date"), "2020-01-01"),
        (("payload", "estimator_contract", "required_official_pit_sessions"), 286),
        (("payload", "estimator_contract", "horizon_split_contracts", "3", "gap_sessions"), 3),
        (("payload", "estimator_contract", "split_version"), "legacy-split"),
        (("payload", "horizons", "1", "model_version"), "legacy-model"),
        (("payload", "horizons", "1", "base_rate"), 2.0),
        (("payload", "horizons", "1", "training_cutoff"), "not-a-date"),
        (("payload", "horizons", "1", "evidence_digest"), "x" * 64),
        (("payload", "horizons", "1", "calibration_metrics", "brier_score"), 2.0),
        (
            ("payload", "horizons", "1", "calibration_metrics", "actual_positive_rate_ci_95"),
            [0.8, 0.2],
        ),
        (("payload", "horizons", "1", "counts", "eligible_observation_count"), 26785),
        (("payload", "horizons", "1", "counts", "out_of_sample_observation_count"), 26785),
        (("payload", "horizons", "1", "counts", "out_of_sample_session_count"), 280),
        (("payload", "horizons", "1", "counts", "evaluated_fold_count"), 0),
        (("payload", "horizons", "1", "counts", "observation_count"), 26783),
        (("payload", "horizons", "2", "counts", "eligible_observation_count"), 26783),
        (("payload", "horizons", "1", "training_cutoff"), "2020-01-01"),
        (("payload", "official_pit", "session_dates", "1"), "2027-08-12"),
        (("payload", "official_pit", "sources", "0", "run_id"), 0),
        (("payload", "official_pit", "sources", "0", "integrity_digest"), "x" * 64),
        (("payload", "official_pit", "sources", "1", "data_date"), "2026-08-11"),
        (("payload", "official_pit", "sources", "1", "data_date"), "2026-08-13"),
        (("payload", "horizons", "2", "selection_qualified"), True),
        (("schema_version",), "unsupported-assessment-v99"),
        (("generated_at",), ""),
        (("payload", "target_contract", "version"), "shifted-target"),
        (("payload", "source", "historical_replay_official"), True),
        (("payload", "source", "record_count"), 1),
        (("payload", "source", "history_database_file"), "../outside.sqlite3"),
        (("payload", "official_pit", "session_dates", "1"), "2026-08-11"),
        (("payload", "official_pit", "session_count"), 3),
        (("payload", "official_pit", "ready"), True),
        (("payload", "official_pit", "sources"), "not-an-array"),
        (("payload", "horizons", "1", "display_day"), 3),
        (("payload", "horizons", "1", "fit_status"), "trained"),
        (("payload", "horizons", "1", "counts"), {"observation_count": 26784}),
        (("payload", "horizons", "1", "gate_reasons"), []),
        (("payload", "horizons", "1", "selection_qualified"), "yes"),
        (("payload", "horizons", "1", "base_rate"), None),
        (("payload", "horizons", "1", "calibration_metrics"), {"brier_score": 0.2}),
        (("payload", "horizons", "1", "calibration_metrics", "bin_monotonic"), "yes"),
        (("payload", "horizons", "1", "calibration_metrics", "brier_score"), "bad"),
        (("payload", "horizons", "1", "calibration_metrics", "actual_positive_rate_ci_95"), "bad"),
        (("payload", "horizons", "1", "calibration_metrics", "actual_positive_rate"), 0.99),
        (("payload", "limitations"), []),
    ],
)
def test_resealed_semantic_tampering_is_rejected(
    path: tuple[str, ...],
    value: object,
) -> None:
    artifact = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    _set_nested_value(artifact, path, value)
    _reseal(artifact)

    with pytest.raises(IndividualProbabilityArtifactError):
        verify_individual_probability_assessment(artifact)


@pytest.mark.parametrize(
    "path",
    [
        ("payload",),
        ("integrity", "notice"),
        ("payload", "production_effect"),
        ("payload", "source", "symbol_count"),
        ("payload", "official_pit", "ready"),
        ("payload", "official_pit", "sources", "0", "run_id"),
        ("payload", "horizons", "3"),
        ("payload", "horizons", "1", "model_version"),
        ("payload", "horizons", "1", "counts", "observation_count"),
        ("payload", "horizons", "1", "calibration_metrics", "auc"),
    ],
)
def test_resealed_missing_contract_fields_are_rejected(path: tuple[str, ...]) -> None:
    artifact = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    _delete_nested_value(artifact, path)
    _reseal(artifact)

    with pytest.raises(IndividualProbabilityArtifactError):
        verify_individual_probability_assessment(artifact)


def test_non_finite_assessment_is_rejected_before_digest_verification() -> None:
    artifact = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    artifact["payload"]["source"]["symbol_count"] = float("nan")

    with pytest.raises(IndividualProbabilityArtifactError, match="有限 JSON"):
        verify_individual_probability_assessment(artifact)


def test_store_rejects_conflicting_artifacts_at_equal_generated_instant(
    tmp_path: Path,
) -> None:
    first = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    second = deepcopy(first)
    second["generated_at"] = "2026-08-12T20:00:00+08:00"
    _reseal(second)
    first_digest = first["integrity"]["integrity_digest"]
    second_digest = second["integrity"]["integrity_digest"]
    (tmp_path / f"individual-upside-probability-assessment-{first_digest}.json").write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
    (tmp_path / f"individual-upside-probability-assessment-{second_digest}.json").write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(IndividualProbabilityArtifactError, match="同 generated_at"):
        IndividualProbabilityStore(tmp_path).latest()


def test_future_generated_at_is_rejected_and_cannot_win_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    future["generated_at"] = "2099-01-01T00:00:00+00:00"
    _reseal(future)
    monkeypatch.setattr(
        artifact_module,
        "utc_now",
        lambda: datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="不能晚于当前时间"):
        verify_individual_probability_assessment(future)

    good = tmp_path / ASSESSMENT.name
    good.write_bytes(ASSESSMENT.read_bytes())
    digest = future["integrity"]["integrity_digest"]
    (tmp_path / f"individual-upside-probability-assessment-{digest}.json").write_text(
        json.dumps(future, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="损坏证据"):
        IndividualProbabilityStore(tmp_path).latest()


def test_same_day_official_pit_is_not_mature_before_market_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    artifact["generated_at"] = "2026-08-13T09:00:00+08:00"
    artifact["payload"]["official_pit"]["session_dates"][-1] = "2026-08-13"
    artifact["payload"]["official_pit"]["sources"][-1]["data_date"] = "2026-08-13"
    _reseal(artifact)
    monkeypatch.setattr(
        artifact_module,
        "utc_now",
        lambda: datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="已完成交易日"):
        verify_individual_probability_assessment(artifact)


@pytest.mark.parametrize("generated_time", ["15:00:00", "15:14:59"])
def test_same_day_official_pit_is_not_mature_before_daily_bar_publish(
    monkeypatch: pytest.MonkeyPatch,
    generated_time: str,
) -> None:
    artifact = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    artifact["generated_at"] = f"2026-08-13T{generated_time}+08:00"
    artifact["payload"]["official_pit"]["session_dates"][-1] = "2026-08-13"
    artifact["payload"]["official_pit"]["sources"][-1]["data_date"] = "2026-08-13"
    _reseal(artifact)
    monkeypatch.setattr(
        artifact_module,
        "utc_now",
        lambda: datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="已完成交易日"):
        verify_individual_probability_assessment(artifact)


def test_same_day_official_pit_is_mature_at_daily_bar_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    artifact["generated_at"] = "2026-08-13T15:15:00+08:00"
    artifact["payload"]["official_pit"]["session_dates"][-1] = "2026-08-13"
    artifact["payload"]["official_pit"]["sources"][-1]["data_date"] = "2026-08-13"
    _reseal(artifact)
    monkeypatch.setattr(
        artifact_module,
        "utc_now",
        lambda: datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc),
    )

    verified = verify_individual_probability_assessment(artifact)
    assert verified["generated_at"] == "2026-08-13T15:15:00+08:00"


def test_projection_never_uses_nonofficial_history_end_as_signal_date() -> None:
    artifact = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    official = artifact["payload"]["official_pit"]
    official.update(
        session_dates=[],
        session_count=0,
        ready=False,
        sources=[],
    )
    _reseal(artifact)

    report = project_individual_upside_probability("600519", artifact)

    assert report.signal_date is None
    assert report.evidence.historical_replay_session_count == 279
    assert all(item.probability is None for item in report.horizons)


def test_resealed_future_official_source_and_date_are_rejected() -> None:
    artifact = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    artifact["payload"]["official_pit"]["session_dates"][1] = "2027-08-12"
    artifact["payload"]["official_pit"]["sources"][1]["data_date"] = "2027-08-12"
    _reseal(artifact)

    with pytest.raises(IndividualProbabilityArtifactError, match="晚于"):
        verify_individual_probability_assessment(artifact)


def test_probability_samples_bind_fixed_d2_d3_d4_exit_indices() -> None:
    bars = _synthetic_history_bars()
    bars[62] = replace(bars[62], close=110.0, high=111.0)
    bars[63] = replace(bars[63], close=90.0, low=89.0)
    bars[64] = replace(bars[64], close=120.0, high=121.0)

    signal_dates, samples = artifact_module._probability_samples({"600000.SH": bars})

    assert signal_dates[0] == bars[60].date
    assert [samples[holding][0].target for holding in (1, 2, 3)] == [1, 0, 1]
    profile = resolve_cost_profile("base")
    for holding, exit_index in ((1, 62), (2, 63), (3, 64)):
        expected = artifact_module._net_label(bars[61], bars[exit_index], profile)
        observed = samples[holding][0]
        assert (observed.target, observed.net_return, observed.executable) == expected


def test_probability_samples_mark_non_executable_without_shifting_exit() -> None:
    bars = _synthetic_history_bars()
    bars[62] = replace(bars[62], volume=0.0)
    bars[63] = replace(bars[63], contract_version="other-contract")
    bars[64] = replace(bars[64], close=120.0, high=121.0)

    _, samples = artifact_module._probability_samples({"600000.SH": bars})

    assert (samples[1][0].target, samples[1][0].net_return, samples[1][0].executable) == (
        None,
        None,
        False,
    )
    assert (samples[2][0].target, samples[2][0].net_return, samples[2][0].executable) == (
        None,
        None,
        False,
    )
    assert samples[3][0].target == 1 and samples[3][0].executable is True

    suspended_entry = _synthetic_history_bars()
    suspended_entry[61] = replace(suspended_entry[61], volume=0.0)
    _, entry_samples = artifact_module._probability_samples({"600000.SH": suspended_entry})
    assert all(entry_samples[holding][0].executable is False for holding in (1, 2, 3))


def test_probability_samples_bind_signal_entry_exit_contract_and_valid_ohlcv() -> None:
    changed_contract = _synthetic_history_bars()
    changed_contract[61] = replace(
        changed_contract[61],
        data_version="future-vintage",
        contract_version="future-contract",
    )
    changed_contract[62] = replace(
        changed_contract[62],
        data_version="future-vintage",
        contract_version="future-contract",
    )

    _, changed_samples = artifact_module._probability_samples({"600000.SH": changed_contract})
    assert changed_samples[1][0].target is None
    assert changed_samples[1][0].net_return is None
    assert changed_samples[1][0].executable is False

    invalid_exit = _synthetic_history_bars()
    invalid_exit[62] = replace(invalid_exit[62], close=-1.0)
    _, invalid_samples = artifact_module._probability_samples({"600000.SH": invalid_exit})
    assert invalid_samples[1][0].target is None
    assert invalid_samples[1][0].net_return is None
    assert invalid_samples[1][0].executable is False


def test_history_database_snapshot_rejects_mutation_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "history.sqlite3"
    database.write_bytes(b"verified")
    original_read = artifact_module.read_regular_file

    def mutate_after_read(path: str | Path, *, max_bytes: int) -> bytes:
        encoded = original_read(path, max_bytes=max_bytes)
        database.write_bytes(b"modified")
        return encoded

    monkeypatch.setattr(artifact_module, "read_regular_file", mutate_after_read)
    with pytest.raises(IndividualProbabilityArtifactError, match="读取期间"):
        artifact_module._verified_history_database_bytes(database, sha256_hex(b"verified"), len(b"verified"))


def test_history_database_snapshot_rejects_symlink_sidecar_and_size_cap(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    database.write_bytes(b"verified")
    sidecar_target = tmp_path / "outside"
    sidecar_target.write_bytes(b"do-not-read")
    Path(f"{database}-wal").symlink_to(sidecar_target)
    with pytest.raises(IndividualProbabilityArtifactError, match="sidecar"):
        artifact_module._verified_history_database_bytes(database, sha256_hex(b"verified"), len(b"verified"))
    Path(f"{database}-wal").unlink()
    with pytest.raises(IndividualProbabilityArtifactError, match="大小上限"):
        artifact_module._verified_history_database_bytes(
            database,
            sha256_hex(b"verified"),
            INDIVIDUAL_PROBABILITY_HISTORY_DATABASE_MAX_BYTES + 1,
        )


def test_minimal_attested_sqlite_builds_current_v2_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "attested-history.sqlite3"
    _write_synthetic_history_database(database)
    encoded = database.read_bytes()
    manifest = tmp_path / "history.manifest.json"

    def load_manifest(path: str | Path) -> dict[str, object]:
        assert Path(path) == manifest
        return {
            "payload": {
                "database": {
                    "path": str(database),
                    "sha256": sha256_hex(encoded),
                    "size_bytes": len(encoded),
                }
            },
            "integrity": {"integrity_digest": "a" * 64},
        }

    fitted_horizons: list[int] = []

    def fit_zero_fold(
        samples: list[object] | tuple[object, ...],
        *,
        config: object,
        generated_at: str,
    ) -> dict[str, object]:
        assert len(samples) == 1
        assert generated_at == "2026-08-13T01:02:03+00:00"
        holding = int(getattr(config, "horizon"))
        fitted_horizons.append(holding)
        return {
            "status": "insufficient_data",
            "selection_qualified": False,
            "selection_qualification": {"gates": {"multiple_complete_oos_folds": False}},
            "counts": {
                "observation_count": 1,
                "eligible_observation_count": 1,
                "available_independent_session_count": 1,
                "out_of_sample_observation_count": 0,
                "out_of_sample_session_count": 0,
                "evaluated_fold_count": 0,
            },
            "calibration_metrics": None,
            "training_cutoff": None,
            "base_rate": None,
            "model_version": artifact_module.PROBABILITY_MODEL_VERSION,
            "feature_version": artifact_module.PROBABILITY_FEATURE_VERSION,
            "evidence_digest": sha256_hex(f"holding-{holding}"),
            "limitations": ["minimum_independent_sessions_not_met"],
        }

    monkeypatch.setattr(artifact_module, "load_market_scan_probability_history_manifest", load_manifest)
    monkeypatch.setattr(artifact_module, "fit_shadow_probability", fit_zero_fold)
    monkeypatch.setattr(
        artifact_module,
        "utc_now",
        lambda: datetime(2026, 8, 13, 1, 2, 3, tzinfo=timezone.utc),
    )

    assessment = artifact_module.build_individual_probability_assessment(manifest)

    assert assessment["schema_version"] == INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION
    assert assessment["generated_at"] == "2026-08-13T01:02:03+00:00"
    assert fitted_horizons == [1, 2, 3]
    payload = assessment["payload"]
    assert payload["source"]["historical_replay_session_count"] == 1
    assert payload["source"]["record_count"] == 3
    assert payload["official_pit"]["ready"] is False
    assert payload["estimator_contract"] == individual_probability_estimator_contract()
    assert all(
        payload["horizons"][str(holding)]["gate_reasons"]
        == [
            "historical_replay_not_official_point_in_time",
            "official_pit_sessions_below_registered_minimum",
            "selection_gate_failed:multiple_complete_oos_folds",
            "minimum_independent_sessions_not_met",
        ]
        for holding in (1, 2, 3)
    )


def test_assessment_write_load_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    artifact = load_individual_probability_assessment(ASSESSMENT)

    first = write_individual_probability_assessment(tmp_path, artifact)
    second = write_individual_probability_assessment(tmp_path, artifact)

    assert first == second
    assert first.name == ASSESSMENT.name
    assert load_individual_probability_assessment(first) == artifact


def test_assessment_io_failures_are_wrapped_as_domain_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    array_file = tmp_path / "array.json"
    array_file.write_text("[]", encoding="utf-8")
    with pytest.raises(IndividualProbabilityArtifactError, match="顶层必须是 object"):
        load_individual_probability_assessment(array_file)
    with pytest.raises(IndividualProbabilityArtifactError, match="无法安全读取"):
        load_individual_probability_assessment(tmp_path / "missing.json")

    monkeypatch.setattr(
        artifact_module,
        "exclusive_atomic_publish",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ArtifactIOError("write failed")),
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="无法发布"):
        write_individual_probability_assessment(
            tmp_path,
            load_individual_probability_assessment(ASSESSMENT),
        )


def test_official_pit_sources_filter_deduplicate_sort_and_reject_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = {
        "shadow": _official_source_snapshot("2026-08-09", 7, mode="shadow"),
        "late": _official_source_snapshot("2026-08-12", 12),
        "early": _official_source_snapshot("2026-08-11", 11),
        "early-copy": _official_source_snapshot("2026-08-11", 11),
    }
    monkeypatch.setattr(
        artifact_module,
        "load_probability_source_snapshot",
        lambda path: snapshots[str(path)],
    )

    sources = artifact_module._official_pit_sources(["shadow", "late", "early", "early-copy"])

    assert [(source.data_date, source.run_id) for source in sources] == [
        ("2026-08-11", 11),
        ("2026-08-12", 12),
    ]
    assert all(
        source.source_schema_version == artifact_module.PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION
        and source.source_contract_version == artifact_module.PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
        and source.feature_version == artifact_module.PROBABILITY_FEATURE_VERSION
        and source.source_evidence_contract_version == artifact_module.MARKET_SCAN_EVIDENCE_CONTRACT_VERSION
        for source in sources
    )

    snapshots["conflict"] = _official_source_snapshot("2026-08-11", 99)
    with pytest.raises(IndividualProbabilityArtifactError, match="冲突 source"):
        artifact_module._official_pit_sources(["early", "conflict"])


def test_real_legacy_run_71_77_sources_are_audit_only_not_current_pit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = Path("data/research/market_scan_probability_source")
    paths = [next(directory.glob(f"market-scan-probability-source-run-{run_id}-*.json.gz")) for run_id in (71, 77)]
    snapshots = {path: artifact_module.load_probability_source_snapshot(path) for path in paths}

    assert all(snapshot["schema_version"] != artifact_module.PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION for snapshot in snapshots.values())
    monkeypatch.setattr(
        artifact_module,
        "load_probability_source_snapshot",
        lambda path: snapshots[Path(path)],
    )
    assert artifact_module._official_pit_sources(paths) == ()


def test_official_pit_source_rejects_invalid_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_date = _official_source_snapshot("not-a-date", 1)
    invalid_digest = _official_source_snapshot("2026-08-11", 1)
    invalid_digest["integrity"]["integrity_digest"] = "bad"
    snapshots = {"date": invalid_date, "digest": invalid_digest}
    monkeypatch.setattr(
        artifact_module,
        "load_probability_source_snapshot",
        lambda path: snapshots[str(path)],
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="ISO 日期"):
        artifact_module._official_pit_sources(["date"])
    with pytest.raises(IndividualProbabilityArtifactError, match="digest 无效"):
        artifact_module._official_pit_sources(["digest"])


def test_official_pit_source_must_exist_by_assessment_generated_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _official_source_snapshot("2026-08-13", 13)
    snapshot["captured_at"] = "2026-08-13T15:16:00+08:00"
    monkeypatch.setattr(
        artifact_module,
        "load_probability_source_snapshot",
        lambda _path: snapshot,
    )
    monkeypatch.setattr(
        artifact_module,
        "utc_now",
        lambda: datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="不能晚于"):
        artifact_module._official_pit_sources(
            ["source"],
            generated_at="2026-08-13T15:15:00+08:00",
        )

    snapshot["captured_at"] = "2026-08-13T15:15:00+08:00"
    sources = artifact_module._official_pit_sources(
        ["source"],
        generated_at="2026-08-13T15:15:00+08:00",
    )
    assert [(source.data_date, source.run_id) for source in sources] == [
        ("2026-08-13", 13),
    ]


def test_run87_weekend_source_roundtrips_current_assessment_store(
    tmp_path: Path,
) -> None:
    data_date = "2026-08-14"
    as_of = "2026-08-15T00:38:35+08:00"
    identities = [
        *((f"{600000 + index:06d}.SH", "SH", "SH_MAIN") for index in range(34)),
        *((f"{index + 1:06d}.SZ", "SZ", "SZ_MAIN") for index in range(33)),
        *((f"{430001 + index:06d}.BJ", "BJ", "BSE") for index in range(33)),
    ]
    items = [
        probability_source_test_support._result_item(  # noqa: SLF001
            symbol,
            market,
            board,
            quote_date=data_date,
            run_as_of=as_of,
            quote_timestamp="2026-08-14T16:14:15+08:00",
            quote_observed_at=as_of,
        )
        for symbol, market, board in identities
    ]
    for item in items:
        item["run_id"] = 87
    run_projection = probability_source_test_support._run(  # noqa: SLF001
        success_count=len(items),
        market_success_counts={"SH": 34, "SZ": 33, "BJ": 33},
        quote_date=data_date,
        as_of=as_of,
    )
    run_projection["run_id"] = 87
    projection = source_module.project_probability_source_capture(
        run_projection,
        items,
        canonical_published=True,
    )
    snapshot = source_module.build_probability_source_snapshot(
        run=cast(dict[str, object], projection["run"]),
        records=cast(list[dict[str, object]], projection["records"]),
        captured_at="2026-08-15T00:49:29+08:00",
        projection_receipt=projection,
    )
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    source = artifact_module._official_pit_source(snapshot, payload, run)  # noqa: SLF001
    assert source is not None

    assessment_payload = deepcopy(
        cast(dict[str, object], json.loads(ASSESSMENT.read_text(encoding="utf-8"))["payload"])
    )
    horizons = cast(dict[str, dict[str, object]], assessment_payload["horizons"])
    for horizon in horizons.values():
        metrics = cast(dict[str, object], horizon["calibration_metrics"])
        metrics.update(
            selection_gate_version=None,
            calibration_bin_count=None,
            minimum_calibration_bin_session_count=None,
            all_folds_positive_brier_skill=None,
        )
    assessment_payload["limitations"] = list(artifact_module._LIMITATIONS)  # noqa: SLF001
    assessment_payload["official_pit"] = {
        "session_dates": [source.data_date],
        "session_count": 1,
        "required_session_count": artifact_module.required_official_pit_sessions(),
        "ready": False,
        "sources": [artifact_module._official_source_identity(source)],  # noqa: SLF001
    }
    assessment = artifact_module._seal_assessment(  # noqa: SLF001
        assessment_payload,
        "2026-08-15T00:50:00+08:00",
    )
    target = write_individual_probability_assessment(tmp_path, assessment)

    loaded = load_individual_probability_assessment(target)
    stored = IndividualProbabilityStore(tmp_path).latest()
    loaded_source = cast(
        dict[str, object],
        cast(dict[str, object], cast(dict[str, object], loaded["payload"])["official_pit"])["sources"][0],
    )
    assert stored == loaded
    assert loaded_source["run_id"] == 87
    assert loaded_source["data_date"] == "2026-08-14"
    assert loaded_source["as_of"] == "2026-08-15T00:38:35+08:00"
    assert loaded_source["captured_at"] == "2026-08-15T00:49:29+08:00"


@pytest.mark.parametrize(
    ("as_of", "captured_at"),
    (
        ("2026-08-11T15:14:59+08:00", "2026-08-11T16:00:00+08:00"),
        ("2026-08-11T15:14:59+08:00", "2026-08-11T15:14:59+08:00"),
    ),
)
def test_current_official_source_requires_1515_maturity(
    monkeypatch: pytest.MonkeyPatch,
    as_of: str,
    captured_at: str,
) -> None:
    snapshot = _official_source_snapshot("2026-08-11", 11)
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    run["as_of"] = as_of
    snapshot["captured_at"] = captured_at
    monkeypatch.setattr(
        artifact_module,
        "load_probability_source_snapshot",
        lambda _path: snapshot,
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="15:15"):
        artifact_module._official_pit_sources(["source"])


def test_current_official_source_rejects_low_success_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _official_source_snapshot("2026-08-11", 11)
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    quality = cast(dict[str, object], payload["quality"])
    run["total_count"] = 5_000
    quality["run_total_count"] = 5_000
    quality["success_to_total_coverage"] = 3 / 5_000
    monkeypatch.setattr(
        artifact_module,
        "load_probability_source_snapshot",
        lambda _path: snapshot,
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="低于预注册门槛"):
        artifact_module._official_pit_sources(["source"])


def test_current_official_source_rejects_unregistered_production_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _official_source_snapshot("2026-08-11", 11)
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    run["production_score_spec_hash"] = "b" * 64
    monkeypatch.setattr(
        artifact_module,
        "load_probability_source_snapshot",
        lambda _path: snapshot,
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="未注册"):
        artifact_module._official_pit_sources(["source"])


def test_current_official_sources_require_one_cross_date_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _official_source_snapshot("2026-08-11", 11)
    second = _official_source_snapshot("2026-08-12", 12)
    second_payload = cast(dict[str, object], second["payload"])
    second_run = cast(dict[str, object], second_payload["run"])
    second_run["rule_version"] = "full-market-scan-v6:other-contract"
    snapshots = {"first": first, "second": second}
    monkeypatch.setattr(
        artifact_module,
        "load_probability_source_snapshot",
        lambda path: snapshots[str(path)],
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="跨日.*不唯一"):
        artifact_module._official_pit_sources(["first", "second"])


def test_current_official_sources_reject_same_run_across_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _official_source_snapshot("2026-08-11", 11)
    second = _official_source_snapshot("2026-08-12", 11)
    snapshots = {"first": first, "second": second}
    monkeypatch.setattr(
        artifact_module,
        "load_probability_source_snapshot",
        lambda path: snapshots[str(path)],
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="run_id.*跨日期"):
        artifact_module._official_pit_sources(["first", "second"])


def test_current_official_source_date_is_bound_in_shanghai_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _official_source_snapshot("2026-08-11", 11)
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    run["as_of"] = "2026-08-11T23:00:00-07:00"
    snapshot["captured_at"] = "2026-08-11T23:01:00-07:00"
    monkeypatch.setattr(
        artifact_module,
        "load_probability_source_snapshot",
        lambda _path: snapshot,
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="15:15"):
        artifact_module._official_pit_sources(["source"])


def test_previous_source_rollover_keeps_frozen_same_day_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _official_source_snapshot("2026-08-14", 82)
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    source = artifact_module._official_pit_source(snapshot, payload, run)  # noqa: SLF001
    assert source is not None
    identity = artifact_module._official_source_identity(source)  # noqa: SLF001
    identity.update(
        source_schema_version="market-scan-probability-source-artifact-v2",
        source_contract_version="market-scan-probability-source-snapshot-v2",
        production_score_rule_version="full-market-score-v4",
        production_score_spec_hash=("30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"),
        as_of="2026-08-15T00:38:35+08:00",
        captured_at="2026-08-15T00:49:29+08:00",
    )
    coverage = cast(dict[str, object], identity["full_market_coverage"])
    coverage["contract_version"] = "market-scan-probability-source-full-market-coverage-v1"
    for scope in cast(dict[str, dict[str, object]], coverage["scopes"]).values():
        scope.pop("missing_count")
        scope.pop("skipped_count")

    def unexpected(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("previous source must not consult current calendar")

    monkeypatch.setattr(
        artifact_module,
        "current_official_source_temporal_contract_matches",
        unexpected,
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="15:15"):
        artifact_module._validated_official_source(  # noqa: SLF001
            identity,
            legacy_source_binding=False,
        )


def test_current_assessment_rejects_embedded_source_captured_after_generated_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _official_source_snapshot("2026-08-13", 13)
    snapshot["captured_at"] = "2026-08-13T15:16:00+08:00"
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    source = artifact_module._official_pit_source(snapshot, payload, run)
    assert source is not None
    official = artifact_module._ValidatedOfficialPit(
        (date(2026, 8, 13),),
        (source,),
        1,
        artifact_module.required_official_pit_sessions(),
        False,
    )
    monkeypatch.setattr(
        artifact_module,
        "latest_expected_daily_kline_date",
        lambda _value: date(2026, 8, 13),
    )
    monkeypatch.setattr(artifact_module, "is_trading_day", lambda _value: True)
    monkeypatch.setattr(
        artifact_module,
        "utc_now",
        lambda: datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="captured_at"):
        artifact_module._validate_official_pit_timing(
            official,
            "2026-08-13T15:15:00+08:00",
        )


def test_v4_metrics_bind_selection_summary_while_v2_keeps_legacy_shape() -> None:
    legacy_metrics = {
        "brier_score": 0.2,
        "reference_brier_score": 0.25,
        "brier_skill_score": 0.2,
        "ece": 0.05,
        "auc": 0.6,
        "actual_positive_rate": 0.5,
        "actual_positive_rate_ci_95": None,
        "bin_monotonic": True,
        "highest_bin_above_base_rate": True,
    }
    current = artifact_module._public_metrics(
        {
            **legacy_metrics,
            "calibration_bins": [
                {"independent_session_count": 31},
                {"independent_session_count": 37},
            ],
        },
        fold_stability={"all_folds_positive_brier_skill": True},
        selection={"version": "market-scan-probability-selection-gates-v1"},
    )
    assert current is not None
    assert current["calibration_bin_count"] == 2
    assert current["minimum_calibration_bin_session_count"] == 31
    assert current["all_folds_positive_brier_skill"] is True
    artifact_module._validate_metrics(current, legacy_source_binding=False)
    artifact_module._validate_metrics(legacy_metrics, legacy_source_binding=True)

    with pytest.raises(IndividualProbabilityArtifactError, match="metrics 字段"):
        artifact_module._validate_metrics(legacy_metrics, legacy_source_binding=False)


def test_v4_source_identity_exactly_binds_full_market_coverage() -> None:
    snapshot = _official_source_snapshot("2026-08-11", 11)
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    source = artifact_module._official_pit_source(snapshot, payload, run)
    assert source is not None
    identity = {
        name: getattr(source, name)
        for name in (
            "data_date",
            "run_id",
            "integrity_digest",
            "source_schema_version",
            "source_contract_version",
            "feature_version",
            "source_evidence_contract_version",
            "as_of",
            "captured_at",
            "run_rule_version",
            "production_score_rule_version",
            "production_score_spec_hash",
            "total_count",
            "success_count",
            "record_count",
            "success_to_total_coverage",
            "full_market_coverage",
        )
    }
    assert (
        artifact_module._validated_official_source(
            identity,
            legacy_source_binding=False,
        )
        == source
    )

    tampered = deepcopy(identity)
    coverage = cast(dict[str, object], tampered["full_market_coverage"])
    scopes = cast(dict[str, dict[str, object]], coverage["scopes"])
    scopes["SH"]["success_count"] = 0
    with pytest.raises(IndividualProbabilityArtifactError, match="coverage 无效"):
        artifact_module._validated_official_source(
            tampered,
            legacy_source_binding=False,
        )


def test_individual_source_identity_reads_previous_v2_v4_but_rejects_v4_as_v3() -> None:
    snapshot = _official_source_snapshot("2026-08-11", 11)
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    source = artifact_module._official_pit_source(snapshot, payload, run)
    assert source is not None
    identity = {
        name: getattr(source, name)
        for name in (
            "data_date", "run_id", "integrity_digest", "source_schema_version",
            "source_contract_version", "feature_version",
            "source_evidence_contract_version", "as_of", "captured_at",
            "run_rule_version", "production_score_rule_version",
            "production_score_spec_hash", "total_count", "success_count",
            "record_count", "success_to_total_coverage", "full_market_coverage",
        )
    }
    v4_hash = "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
    current_v4 = deepcopy(identity)
    current_v4.update(
        production_score_rule_version="full-market-score-v4",
        production_score_spec_hash=v4_hash,
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="未注册"):
        artifact_module._validated_official_source(
            current_v4,
            legacy_source_binding=False,
        )

    previous = deepcopy(current_v4)
    previous["source_schema_version"] = "market-scan-probability-source-artifact-v2"
    previous["source_contract_version"] = "market-scan-probability-source-snapshot-v2"
    coverage = cast(dict[str, object], previous["full_market_coverage"])
    coverage["contract_version"] = "market-scan-probability-source-full-market-coverage-v1"
    for scope in cast(dict[str, dict[str, object]], coverage["scopes"]).values():
        scope.pop("missing_count")
        scope.pop("skipped_count")

    verified = artifact_module._validated_official_source(
        previous,
        legacy_source_binding=False,
    )

    assert verified.source_schema_version == "market-scan-probability-source-artifact-v2"
    assert verified.production_score_rule_version == "full-market-score-v4"


def test_official_intake_rejects_quality_run_coverage_divergence() -> None:
    snapshot = _official_source_snapshot("2026-08-11", 11)
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    quality = cast(dict[str, object], payload["quality"])
    quality["full_market_coverage"] = {}

    with pytest.raises(IndividualProbabilityArtifactError, match="coverage 与 run"):
        artifact_module._official_pit_source(snapshot, payload, run)


def test_history_series_rejects_empty_and_cross_symbol_date_mismatch() -> None:
    empty = _RowsConnection([])
    with pytest.raises(IndividualProbabilityArtifactError, match="没有 qfq"):
        artifact_module._history_series(empty)

    mismatch = _RowsConnection(
        [
            _history_row("600000.SH", "2026-08-11"),
            _history_row("000001.SZ", "2026-08-12"),
        ]
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="日期合同不一致"):
        artifact_module._history_series(mismatch)


def test_history_and_label_boundaries_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(IndividualProbabilityArtifactError, match="固定 replay 窗口"):
        artifact_module._probability_samples({"600000.SH": _synthetic_history_bars()[:81]})

    profile = resolve_cost_profile("base")
    bars = _synthetic_history_bars()
    expensive_entry = replace(bars[61], open=2_000.0)
    assert artifact_module._net_label(expensive_entry, bars[62], profile) == (
        None,
        None,
        False,
    )

    database = tmp_path / "history.sqlite3"
    database.write_bytes(b"verified")
    with pytest.raises(IndividualProbabilityArtifactError, match="SHA-256 无效"):
        artifact_module._verified_history_database_bytes(database, "bad", len(b"verified"))
    with pytest.raises(IndividualProbabilityArtifactError, match="大小或摘要冲突"):
        artifact_module._verified_history_database_bytes(
            database,
            sha256_hex(b"different"),
            len(b"verified"),
        )
    with pytest.raises(IndividualProbabilityArtifactError, match="普通文件"):
        artifact_module._regular_file_identity(tmp_path)
    with pytest.raises(IndividualProbabilityArtifactError, match="无法只读评估"):
        with artifact_module._readonly_database(b"not-sqlite") as connection:
            connection.execute("SELECT name FROM sqlite_master").fetchall()


def test_current_source_intake_rejects_resealed_contract_and_count_attacks() -> None:
    cases = (
        ("payload-contract", "payload contract"),
        ("feature-contract", "feature contract"),
        ("empty-records", "records 无效"),
        ("evidence-contract", "evidence contract"),
        ("quote-date", "quote_date/data_date"),
        ("record-count", "records/counts"),
        ("quality-count", "quality/counts"),
        ("coverage-replay", "coverage 无法重放"),
    )
    for attack, message in cases:
        snapshot = _official_source_snapshot("2026-08-11", 11)
        payload = cast(dict[str, object], snapshot["payload"])
        run = cast(dict[str, object], payload["run"])
        quality = cast(dict[str, object], payload["quality"])
        records = cast(list[dict[str, object]], payload["records"])
        if attack == "payload-contract":
            payload["contract_version"] = "superseded-contract"
        elif attack == "feature-contract":
            cast(dict[str, object], payload["feature_schema"])["version"] = "future-feature-contract"
        elif attack == "empty-records":
            payload["records"] = []
        elif attack == "evidence-contract":
            records[0]["source_evidence_contract_version"] = "unregistered-evidence"
        elif attack == "quote-date":
            run["quote_date"] = "2026-08-10"
        elif attack == "record-count":
            run["total_count"] = 2
            run["success_count"] = 2
        elif attack == "quality-count":
            quality["run_total_count"] = 2
        else:
            quality["record_coverage"] = 0.5

        with pytest.raises(IndividualProbabilityArtifactError, match=message):
            artifact_module._official_pit_source(snapshot, payload, run)


def test_v4_embedded_source_is_exactly_round_trip_bound_and_rejects_tampering() -> None:
    snapshot = _official_source_snapshot("2026-08-11", 11)
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    source = artifact_module._official_pit_source(snapshot, payload, run)
    assert source is not None
    identity = artifact_module._official_source_identity(source)
    assert (
        artifact_module._validated_official_source(
            identity,
            legacy_source_binding=False,
        )
        == source
    )

    attacks: tuple[tuple[str, object, str], ...] = (
        ("source_schema_version", "legacy-source-v1", "当前合同"),
        ("production_score_spec_hash", "b" * 64, "未注册"),
        ("success_count", 2, "counts/coverage"),
        ("run_rule_version", "", "run rule"),
        ("success_to_total_coverage", float("nan"), "coverage 无效"),
    )
    for field, replacement, message in attacks:
        tampered = deepcopy(identity)
        tampered[field] = replacement
        with pytest.raises(IndividualProbabilityArtifactError, match=message):
            artifact_module._validated_official_source(
                tampered,
                legacy_source_binding=False,
            )


def test_v4_and_legacy_v2_source_shapes_cannot_be_reinterpreted() -> None:
    legacy_identity = {
        "data_date": "2026-08-11",
        "run_id": 11,
        "integrity_digest": "a" * 64,
    }
    snapshot = _official_source_snapshot("2026-08-11", 11)
    payload = cast(dict[str, object], snapshot["payload"])
    run = cast(dict[str, object], payload["run"])
    current = artifact_module._official_source_identity(
        cast(
            artifact_module._OfficialPitSource,
            artifact_module._official_pit_source(snapshot, payload, run),
        )
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="字段无效"):
        artifact_module._validated_official_source(
            legacy_identity,
            legacy_source_binding=False,
        )
    with pytest.raises(IndividualProbabilityArtifactError, match="字段无效"):
        artifact_module._validated_official_source(
            current,
            legacy_source_binding=True,
        )

    unchanged = load_individual_probability_assessment(ASSESSMENT)
    report = project_individual_upside_probability("600519", unchanged)
    assert unchanged["schema_version"] == LEGACY_INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION
    assert report.signal_date is None
    assert report.evidence.official_pit_session_count == 0
    assert report.evidence.required_official_pit_session_count == 288
    assert all(item.probability is None for item in report.horizons)


def test_source_timestamps_and_generated_time_fail_closed_on_ambiguous_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        artifact_module,
        "utc_now",
        lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
    )
    for captured_at in (None, "not-a-time"):
        with pytest.raises(IndividualProbabilityArtifactError, match="captured_at 无效"):
            artifact_module._validate_official_source_available_at(
                {"captured_at": captured_at},
                "2026-08-13T16:00:00+08:00",
            )

    for value in (7, "2026-08-13T15:15:00", "2026-02-30T15:15:00+08:00"):
        with pytest.raises(IndividualProbabilityArtifactError, match="source time 无效"):
            artifact_module._source_timestamp(value, "source time")

    with pytest.raises(IndividualProbabilityArtifactError, match="含时区"):
        artifact_module._validated_assessment_generated_at("2026-08-13T15:15:00")


def test_official_timing_rejects_calendar_outage_and_non_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    official = artifact_module._ValidatedOfficialPit(
        (date(2026, 8, 13),),
        (),
        1,
        artifact_module.required_official_pit_sessions(),
        False,
    )
    monkeypatch.setattr(
        artifact_module,
        "utc_now",
        lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        artifact_module,
        "latest_expected_daily_kline_date",
        lambda _value: (_ for _ in ()).throw(artifact_module.TradingCalendarCoverageError("calendar unavailable")),
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="交易日历"):
        artifact_module._validate_official_pit_timing(
            official,
            "2026-08-13T16:00:00+08:00",
        )

    monkeypatch.setattr(
        artifact_module,
        "latest_expected_daily_kline_date",
        lambda _value: date(2026, 8, 13),
    )
    monkeypatch.setattr(artifact_module, "is_trading_day", lambda _value: False)
    with pytest.raises(IndividualProbabilityArtifactError, match="可信交易日"):
        artifact_module._validate_official_pit_timing(
            official,
            "2026-08-13T16:00:00+08:00",
        )


def test_horizon_set_rejects_source_count_and_authorization_bypasses() -> None:
    counts = {
        "observation_count": 1,
        "eligible_observation_count": 1,
        "independent_session_count": 1,
        "out_of_sample_observation_count": 0,
        "out_of_sample_session_count": 0,
        "evaluated_fold_count": 0,
    }
    source = artifact_module._ValidatedSource(
        date(2026, 8, 11),
        date(2026, 8, 11),
        1,
        1,
        3,
        False,
    )
    official = artifact_module._ValidatedOfficialPit(
        (),
        (),
        0,
        artifact_module.required_official_pit_sessions(),
        False,
    )
    mismatched = artifact_module._ValidatedHorizon(
        {**counts, "observation_count": 2},
        False,
        (
            "historical_replay_not_official_point_in_time",
            "official_pit_sessions_below_registered_minimum",
        ),
        None,
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="observation count"):
        artifact_module._validate_horizon_set(
            (mismatched, mismatched, mismatched),
            source,
            official,
        )

    no_replay_gate = artifact_module._ValidatedHorizon(
        counts,
        False,
        ("official_pit_sessions_below_registered_minimum",),
        None,
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="historical replay"):
        artifact_module._validate_gate_binding(no_replay_gate, source, official)

    no_official_gate = artifact_module._ValidatedHorizon(
        counts,
        False,
        ("historical_replay_not_official_point_in_time",),
        None,
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="official PIT gate"):
        artifact_module._validate_gate_binding(no_official_gate, source, official)

    bypass = artifact_module._ValidatedHorizon(
        counts,
        True,
        (
            "historical_replay_not_official_point_in_time",
            "official_pit_sessions_below_registered_minimum",
        ),
        None,
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="不能绕过"):
        artifact_module._validate_gate_binding(bypass, source, official)


def test_compact_metrics_and_validation_reject_malformed_selection_evidence() -> None:
    evidence = {
        "status": "calibrated_shadow",
        "selection_qualified": False,
        "selection_qualification": {"version": "gate-v1", "gates": {}},
        "counts": {
            "observation_count": 10,
            "eligible_observation_count": 10,
            "available_independent_session_count": 10,
            "out_of_sample_observation_count": 5,
            "out_of_sample_session_count": 5,
            "evaluated_fold_count": 1,
        },
        "calibration_metrics": {
            "calibrated": {
                "brier_score": 0.2,
                "calibration_bins": None,
            },
            "fold_stability": {"all_folds_positive_brier_skill": True},
        },
        "evidence_digest": "a" * 64,
        "limitations": None,
    }
    compact = artifact_module._compact_horizon_evidence(
        evidence,
        holding=1,
        official_session_count=0,
    )
    metrics = cast(dict[str, object], compact["calibration_metrics"])
    assert metrics["selection_gate_version"] is None
    assert metrics["calibration_bin_count"] is None
    assert metrics["all_folds_positive_brier_skill"] is True
    artifact_module._validate_metrics(metrics, legacy_source_binding=False)

    assert (
        artifact_module._validate_horizon_selection(
            "calibrated_shadow",
            True,
            ("no_blocker",),
        )
        is None
    )

    malformed_metrics = {
        "brier_score": 0.2,
        "reference_brier_score": 0.25,
        "brier_skill_score": 0.2,
        "ece": 0.05,
        "auc": 0.6,
        "actual_positive_rate": 0.5,
        "actual_positive_rate_ci_95": None,
        "bin_monotonic": True,
        "highest_bin_above_base_rate": True,
        "selection_gate_version": "unregistered-gate",
        "calibration_bin_count": 2,
        "minimum_calibration_bin_session_count": 20,
        "all_folds_positive_brier_skill": True,
    }
    with pytest.raises(IndividualProbabilityArtifactError, match="selection gate version"):
        artifact_module._validate_metrics(
            malformed_metrics,
            legacy_source_binding=False,
        )
    malformed_metrics["selection_gate_version"] = None
    malformed_metrics["actual_positive_rate_ci_95"] = ["0.4", 0.6]
    with pytest.raises(IndividualProbabilityArtifactError, match="CI 无效"):
        artifact_module._validate_metrics(
            malformed_metrics,
            legacy_source_binding=False,
        )


def test_database_fail_closed_paths_cover_read_sidecar_and_toctou_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "history.sqlite3"
    database.write_bytes(b"verified")
    monkeypatch.setattr(
        artifact_module,
        "read_regular_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ArtifactIOError("denied")),
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="无法安全读取"):
        artifact_module._verified_history_database_bytes(
            database,
            sha256_hex(b"verified"),
            len(b"verified"),
        )

    source = artifact_module._AssessmentSource(
        "a" * 64,
        sha256_hex(b"verified"),
        database,
        b"verified",
        (1, 2, 3, 4),
    )
    monkeypatch.setattr(
        artifact_module,
        "_verified_history_database_bytes",
        lambda *_args: artifact_module._DatabaseSnapshot(b"changed", (1, 2, 3, 4)),
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="评估期间"):
        artifact_module._assert_history_database_unchanged(source)

    original_is_symlink = Path.is_symlink

    def broken_sidecar_check(path: Path) -> bool:
        if str(path).endswith("-wal"):
            raise OSError("permission denied")
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", broken_sidecar_check)
    with pytest.raises(IndividualProbabilityArtifactError, match="sidecar 无法检查"):
        artifact_module._reject_history_database_sidecars(database)


def test_low_level_artifact_validators_reject_non_regular_and_non_json_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "history.sqlite3"
    database.write_bytes(b"verified")
    monkeypatch.setattr(
        artifact_module,
        "path_has_only_trusted_aliases",
        lambda _path: (_ for _ in ()).throw(RuntimeError("alias loop")),
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="不可安全读取"):
        artifact_module._regular_file_identity(database)

    with pytest.raises(IndividualProbabilityArtifactError, match="必须是 object"):
        artifact_module._mapping([], "attack")
    with pytest.raises(IndividualProbabilityArtifactError, match="非负整数"):
        artifact_module._nonnegative_int(-1, "attack")
    with pytest.raises(IndividualProbabilityArtifactError, match="ISO 日期"):
        artifact_module._iso_date(20260813, "attack")

    monkeypatch.setattr(
        artifact_module.sqlite3,
        "connect",
        lambda *_args: (_ for _ in ()).throw(sqlite3.Error("unavailable")),
    )
    with pytest.raises(IndividualProbabilityArtifactError, match="无法只读评估"):
        with artifact_module._readonly_database(b"verified"):
            pytest.fail("unreachable")


def test_invalid_history_date_and_unaffordable_lot_are_not_labelled() -> None:
    bars = _synthetic_history_bars()
    invalid_date = replace(bars[61], date="not-a-date")
    assert artifact_module._valid_label_bar(invalid_date) is False

    profile = resolve_cost_profile("base")
    unaffordable = replace(bars[61], open=1_000_000.0, high=1_000_001.0)
    assert artifact_module._net_label(unaffordable, bars[62], profile) == (
        None,
        None,
        False,
    )


def test_cli_main_builds_writes_and_summarizes_all_horizons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "assessment.json"
    target.write_text("sealed", encoding="utf-8")
    artifact = {
        "integrity": {"integrity_digest": "d" * 64},
        "payload": {
            "official_pit": {"session_count": 2},
            "horizons": {
                "1": _cli_horizon(2, metrics=True),
                "2": _cli_horizon(3, metrics=False),
                "3": _cli_horizon(4, metrics=True),
            },
        },
    }
    observed: dict[str, object] = {}

    def build(
        manifest: Path,
        *,
        official_source_paths: tuple[Path, ...],
        generated_at: str | None,
    ) -> dict[str, object]:
        observed.update(
            manifest=manifest,
            sources=official_source_paths,
            generated_at=generated_at,
        )
        return artifact

    def write(directory: Path, value: object) -> Path:
        observed.update(directory=directory, artifact=value)
        return target

    monkeypatch.setattr(evaluation_cli, "build_individual_probability_assessment", build)
    monkeypatch.setattr(evaluation_cli, "write_individual_probability_assessment", write)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_individual_probability.py",
            "--history-manifest",
            "history.json",
            "--official-source",
            "source-b.json",
            "--official-source",
            "source-a.json",
            "--output-directory",
            str(tmp_path),
            "--generated-at",
            "2026-08-13T00:00:00Z",
        ],
    )

    assert evaluation_cli.main() == 0

    assert observed == {
        "manifest": Path("history.json"),
        "sources": (Path("source-b.json"), Path("source-a.json")),
        "generated_at": "2026-08-13T00:00:00Z",
        "directory": tmp_path,
        "artifact": artifact,
    }
    output = capsys.readouterr().out
    assert f"path={target}" in output
    assert "official_pit_sessions=2" in output
    assert "holding=1 display_day=2" in output
    assert "holding=2 display_day=3" in output
    assert "brier=None" in output


def test_cli_projection_rejects_non_mapping() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        evaluation_cli._mapping([])


def _write_synthetic_history_database(path: Path) -> None:
    bars = _synthetic_history_bars()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE kline_daily ("
            "symbol TEXT NOT NULL,date TEXT NOT NULL,open REAL NOT NULL,"
            "close REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,"
            "volume REAL NOT NULL,adjustment_mode TEXT NOT NULL,"
            "data_version TEXT NOT NULL,contract_version TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO kline_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    bar.symbol,
                    bar.date,
                    bar.open,
                    bar.close,
                    bar.high,
                    bar.low,
                    bar.volume,
                    bar.adjustment_mode,
                    bar.data_version,
                    bar.contract_version,
                )
                for bar in bars
            ],
        )


def _official_source_snapshot(
    data_date: str,
    run_id: int,
    *,
    mode: str = "official",
) -> dict[str, object]:
    market_progress = [
        {
            "market": market,
            "total_count": 1,
            "processed_count": 1,
            "success_count": 1,
            "missing_count": 0,
            "skipped_count": 0,
        }
        for market in ("SH", "SZ", "BJ")
    ]
    full_market_coverage = source_module._project_full_market_coverage(  # noqa: SLF001
        market_progress,
        total_count=3,
        success_count=3,
        skipped_count=0,
    )
    return {
        "schema_version": artifact_module.PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION,
        "captured_at": f"{data_date}T16:00:00+08:00",
        "payload": {
            "contract_version": artifact_module.PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION,
            "run": {
                "mode": mode,
                "canonical_published": True,
                "data_date": data_date,
                "quote_date": data_date,
                "run_id": run_id,
                "as_of": f"{data_date}T15:15:00+08:00",
                "rule_version": "full-market-scan-v6:test-contract",
                "production_score_rule_version": FULL_MARKET_SCORE_RULE_VERSION,
                "production_score_spec_hash": PRODUCTION_SCORE_SPEC_HASH,
                "total_count": 3,
                "success_count": 3,
                "skipped_count": 0,
                "full_market_coverage": full_market_coverage,
            },
            "feature_schema": {
                "version": artifact_module.PROBABILITY_FEATURE_VERSION,
            },
            "records": [
                {
                    "source_evidence_contract_version": (artifact_module.MARKET_SCAN_EVIDENCE_CONTRACT_VERSION),
                },
                {
                    "source_evidence_contract_version": (artifact_module.MARKET_SCAN_EVIDENCE_CONTRACT_VERSION),
                },
                {
                    "source_evidence_contract_version": (artifact_module.MARKET_SCAN_EVIDENCE_CONTRACT_VERSION),
                },
            ],
            "quality": {
                "run_total_count": 3,
                "run_success_count": 3,
                "record_count": 3,
                "expected_record_count": 3,
                "record_coverage": 1.0,
                "success_to_total_coverage": 1.0,
                "full_market_coverage": full_market_coverage,
            },
        },
        "integrity": {
            "integrity_digest": sha256_hex(f"{data_date}:{run_id}"),
        },
    }


class _RowsConnection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def execute(self, _sql: str) -> list[dict[str, object]]:
        return self.rows


def _history_row(symbol: str, data_date: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "date": data_date,
        "open": 10.0,
        "close": 10.1,
        "high": 10.2,
        "low": 9.9,
        "volume": 1_000.0,
        "adjustment_mode": "qfq",
        "data_version": "daily-v1",
        "contract_version": "contract-v1",
    }


def _cli_horizon(display_day: int, *, metrics: bool) -> dict[str, object]:
    return {
        "display_day": display_day,
        "fit_status": "insufficient_data",
        "selection_qualified": False,
        "calibration_metrics": (
            {
                "brier_score": 0.2,
                "brier_skill_score": 0.1,
                "auc": 0.6,
            }
            if metrics
            else None
        ),
        "counts": {
            "independent_session_count": 279,
            "out_of_sample_session_count": 60,
        },
    }


def _synthetic_history_bars() -> list[artifact_module._HistoryBar]:
    return [
        artifact_module._HistoryBar(
            symbol="600000.SH",
            date=(date(2025, 1, 1) + timedelta(days=index)).isoformat(),
            open=100.0,
            close=100.0,
            high=101.0,
            low=99.0,
            volume=1_000.0,
            adjustment_mode="qfq",
            data_version="daily-v1",
            contract_version="contract-v1",
        )
        for index in range(82)
    ]


def _empty_horizon(
    display_day: int,
    holding_sessions: int,
    counts: IndividualProbabilityCounts,
) -> IndividualUpsideHorizon:
    return IndividualUpsideHorizon(
        display_day=display_day,
        holding_sessions=holding_sessions,
        status="insufficient_data",
        counts=counts,
        feature_version=REGISTERED_FEATURE_VERSION,
    )


def _calibrated_counts(display_day: int) -> IndividualProbabilityCounts:
    minimum = {2: 284, 3: 286, 4: 288}[display_day]
    return IndividualProbabilityCounts(
        observation_count=1_000,
        eligible_observation_count=1_000,
        independent_session_count=minimum,
        out_of_sample_observation_count=120,
        out_of_sample_session_count=120,
        evaluated_fold_count=2,
    )


def _calibrated_metrics() -> IndividualProbabilityMetrics:
    return IndividualProbabilityMetrics(
        brier_score=0.2,
        reference_brier_score=0.25,
        brier_skill_score=0.2,
        ece=0.03,
        auc=0.65,
        actual_positive_rate=0.5,
        actual_positive_rate_ci_95={"lower": 0.45, "upper": 0.55, "level": 0.95},
        bin_monotonic=True,
        highest_bin_above_base_rate=True,
        selection_gate_version="market-scan-probability-selection-gates-v1",
        calibration_bin_count=5,
        minimum_calibration_bin_session_count=20,
        all_folds_positive_brier_skill=True,
    )


def _calibrated_horizon(display_day: int) -> IndividualUpsideHorizon:
    return IndividualUpsideHorizon(
        display_day=display_day,
        holding_sessions=display_day - 1,
        status="calibrated_shadow",
        probability=0.55,
        confidence_interval={"lower": 0.5, "upper": 0.6, "level": 0.95},
        base_rate=0.5,
        counts=_calibrated_counts(display_day),
        calibration_metrics=_calibrated_metrics(),
        training_cutoff="2026-08-11",
        model_version=REGISTERED_MODEL_VERSION,
        feature_version=REGISTERED_FEATURE_VERSION,
        evidence_digest="a" * 64,
    )


def _calibrated_report_evidence() -> IndividualProbabilityEvidence:
    return IndividualProbabilityEvidence(
        assessment_digest="b" * 64,
        history_manifest_digest="c" * 64,
        history_database_sha256="d" * 64,
        official_pit_session_count=288,
        required_official_pit_session_count=288,
        historical_replay_session_count=288,
        historical_replay_official=True,
        selection_qualified=True,
    )


def _reseal(artifact: dict) -> None:
    unsigned = {key: value for key, value in artifact.items() if key != "integrity"}
    artifact["integrity"]["integrity_digest"] = sha256_hex(canonical_json_bytes(unsigned))


def _set_nested_value(container: object, path: tuple[str, ...], value: object) -> None:
    cursor = container
    for key in path[:-1]:
        if isinstance(cursor, dict):
            cursor = cursor[key]
        elif isinstance(cursor, list):
            cursor = cursor[int(key)]
        else:
            raise AssertionError("tamper path does not resolve")
    if isinstance(cursor, dict):
        cursor[path[-1]] = value
    elif isinstance(cursor, list):
        cursor[int(path[-1])] = value
    else:
        raise AssertionError("tamper path does not resolve")


def _delete_nested_value(container: object, path: tuple[str, ...]) -> None:
    cursor = container
    for key in path[:-1]:
        if isinstance(cursor, dict):
            cursor = cursor[key]
        elif isinstance(cursor, list):
            cursor = cursor[int(key)]
        else:
            raise AssertionError("tamper path does not resolve")
    if isinstance(cursor, dict):
        del cursor[path[-1]]
    elif isinstance(cursor, list):
        del cursor[int(path[-1])]
    else:
        raise AssertionError("tamper path does not resolve")
