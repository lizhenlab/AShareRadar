from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast

import pytest

from app.services import market_scan_probability_historical_context as historical_context
from app.services.market_scan_probability_historical_context import (
    HISTORICAL_CONTEXT_SCHEMA_VERSION,
    HistoricalProbabilityContextError,
    MarketScanHistoricalProbabilityContextStore,
    historical_probability_context_filename,
    publish_historical_probability_context,
    verify_historical_probability_context,
)
from app.services.market_scan_probability_replay import (
    HISTORICAL_REPLAY_ARTIFACT_SCHEMA_VERSION,
    HISTORICAL_REPLAY_COHORT_MODE,
)


_SOURCE_DIGEST = "a" * 64


def test_historical_context_cli_publishes_only_non_authorizing_evidence(tmp_path, monkeypatch, capsys):
    from tools import build_market_scan_probability_historical_context as cli

    source = _source_path(tmp_path)
    source.write_bytes(b"{}")
    monkeypatch.setattr(historical_context, "verify_historical_replay_artifact", lambda _: _verified_replay())
    monkeypatch.setattr(cli.sys, "argv", ["context", "--artifact", str(source), "--output-dir", str(tmp_path)])
    assert cli.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready" and Path(report["artifact"]).is_file()
    assert report["official"] is False and report["filter_qualified"] is False
    assert report["production_ranking_effect"] == "none" and source.read_bytes() == b"{}"


@pytest.mark.parametrize("error_type", [HistoricalProbabilityContextError, ValueError])
def test_historical_context_cli_reports_failed_verification(tmp_path, monkeypatch, capsys, error_type):
    from tools import build_market_scan_probability_historical_context as cli

    def reject(*args):
        raise error_type("source verification failed")

    monkeypatch.setattr(cli, "publish_historical_probability_context", reject)
    monkeypatch.setattr(cli.sys, "argv", ["context", "--artifact", str(tmp_path / "source.json"), "--output-dir", str(tmp_path)])
    assert cli.main() == 2
    captured = capsys.readouterr()
    assert captured.out == "" and json.loads(captured.err)["status"] == "failed"
    assert list(tmp_path.iterdir()) == []


def test_publish_and_load_compact_historical_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_path(tmp_path)
    source.write_bytes(b"{}")
    monkeypatch.setattr(
        historical_context,
        "verify_historical_replay_artifact",
        lambda _: _verified_replay(),
    )

    target = publish_historical_probability_context(source, tmp_path)
    store = MarketScanHistoricalProbabilityContextStore(tmp_path)
    projection = store.research_projection()

    assert target.name == historical_probability_context_filename(
        historical_context._load_context_file(target)  # noqa: SLF001
    )
    assert store.preload() == 1
    assert projection["schema_version"] == HISTORICAL_CONTEXT_SCHEMA_VERSION
    assert projection["status"] == "ready"
    assert projection["availability"] == "historical_replay_no_verified_predictive_skill"
    assert projection["production_ranking_effect"] == "none"
    assert projection["selection_qualified"] is False
    assert projection["filter_qualified"] is False
    assert projection["sample"] == {
        "start_date": "2025-01-02",
        "end_date": "2026-01-30",
        "independent_session_count": 279,
        "record_count": 26784,
        "symbol_count": 96,
        "label_coverage": 1.0,
    }
    assert projection["horizons"]["5"]["probability"] is None  # type: ignore[index]
    assert projection["horizons"]["5"]["auc"] == pytest.approx(0.49)  # type: ignore[index]
    assert (
        projection["source_artifact"]["sha256"]
        == hashlib.sha256(  # type: ignore[index]
            b"{}"
        ).hexdigest()
    )


def test_store_rejects_changed_bound_full_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_path(tmp_path)
    source.write_bytes(b"{}")
    monkeypatch.setattr(
        historical_context,
        "verify_historical_replay_artifact",
        lambda _: _verified_replay(),
    )
    publish_historical_probability_context(source, tmp_path)
    store = MarketScanHistoricalProbabilityContextStore(tmp_path)
    assert store.research_projection()["status"] == "ready"

    source.write_bytes(b"[]")

    with pytest.raises(HistoricalProbabilityContextError, match="摘要不一致"):
        store.research_projection()


def test_store_returns_previous_projection_during_background_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    errors: list[BaseException] = []
    expected = {"status": "ready", "availability": "reference_only"}

    def delayed_projection(_directory, _snapshot):
        entered.set()
        assert release.wait(timeout=2)
        return expected

    monkeypatch.setattr(historical_context, "_load_newest_projection", delayed_projection)
    store = MarketScanHistoricalProbabilityContextStore(tmp_path)

    def preload() -> None:
        try:
            assert store.preload() == 1
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    worker = Thread(target=preload)
    worker.start()
    assert entered.wait(timeout=1)

    assert store.research_projection()["status"] == "not_generated"

    release.set()
    worker.join(timeout=2)
    assert worker.is_alive() is False
    assert errors == []
    assert store.research_projection() == expected


def test_store_scheduled_preload_keeps_request_read_nonblocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarketScanHistoricalProbabilityContextStore(tmp_path)
    store.mark_preload_pending()
    monkeypatch.setattr(
        historical_context,
        "_directory_snapshot",
        lambda _directory: pytest.fail("request must not perform a scheduled historical refresh"),
    )

    assert store.research_projection()["status"] == "not_generated"
    assert store.refresh_pending() is True
    store.clear_preload_pending()
    assert store.refresh_pending() is False


def test_context_integrity_and_authority_are_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_path(tmp_path)
    source.write_bytes(b"{}")
    monkeypatch.setattr(
        historical_context,
        "verify_historical_replay_artifact",
        lambda _: _verified_replay(),
    )
    artifact = historical_context.build_historical_probability_context(source)

    tampered = deepcopy(artifact)
    tampered["payload"]["selection_qualified"] = True  # type: ignore[index]

    with pytest.raises(HistoricalProbabilityContextError, match="摘要不一致"):
        verify_historical_probability_context(tampered)


def test_context_keeps_legitimate_underpowered_metrics_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_path(tmp_path)
    source.write_bytes(b"{}")
    replay = _verified_replay()
    fit_horizons = replay["payload"]["probability_fit"]["horizons"]
    for value in fit_horizons.values():
        value["calibration_metrics"] = None
        value["training_cutoff"] = None
        value["counts"]["available_independent_session_count"] = 0
        value["counts"]["observation_count"] = 0
    monkeypatch.setattr(
        historical_context,
        "verify_historical_replay_artifact",
        lambda _: replay,
    )

    artifact = historical_context.build_historical_probability_context(source)
    projection = artifact["payload"]

    assert projection["availability"] == "historical_replay_insufficient_evidence"
    assert projection["horizons"]["1"]["auc"] is None
    assert projection["horizons"]["1"]["brier_skill_score"] is None
    assert projection["horizons"]["1"]["training_cutoff"] is None


def test_historical_context_validation_preserves_the_research_only_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_path(tmp_path)
    source.write_bytes(b"{}")
    monkeypatch.setattr(
        historical_context,
        "verify_historical_replay_artifact",
        lambda _: _verified_replay(),
    )
    artifact = historical_context.build_historical_probability_context(source)
    payload = deepcopy(artifact["payload"])
    assert isinstance(payload, dict)
    historical_context._validate_payload(  # noqa: SLF001
        payload,
        generated_at=artifact["generated_at"],
    )

    invalid_payloads = []
    changed = deepcopy(payload)
    changed["unexpected"] = True
    invalid_payloads.append(changed)
    changed = deepcopy(payload)
    changed["schema_version"] = "old"
    invalid_payloads.append(changed)
    changed = deepcopy(payload)
    changed["status"] = "active"
    invalid_payloads.append(changed)
    changed = deepcopy(payload)
    changed["availability"] = "production_ready"
    invalid_payloads.append(changed)
    changed = deepcopy(payload)
    changed["selection_qualified"] = True
    invalid_payloads.append(changed)
    for changed in invalid_payloads:
        with pytest.raises(HistoricalProbabilityContextError):
            historical_context._validate_payload(  # noqa: SLF001
                changed,
                generated_at=artifact["generated_at"],
            )

    cohort = deepcopy(payload["cohort"])
    assert isinstance(cohort, dict)
    cohort["official"] = True
    with pytest.raises(HistoricalProbabilityContextError, match="cohort"):
        historical_context._validate_context_cohort(cohort)  # noqa: SLF001

    sample = deepcopy(payload["sample"])
    assert isinstance(sample, dict)
    sample.pop("record_count")
    with pytest.raises(HistoricalProbabilityContextError, match="样本摘要"):
        historical_context._validate_context_sample(sample)  # noqa: SLF001

    horizons = deepcopy(payload["horizons"])
    assert isinstance(horizons, dict)
    horizons.pop("20")
    with pytest.raises(HistoricalProbabilityContextError, match="周期字段"):
        historical_context._validate_context_horizons(horizons)  # noqa: SLF001

    context_source = deepcopy(payload["source_artifact"])
    assert isinstance(context_source, dict)
    context_source["full_replay_verified"] = False
    with pytest.raises(HistoricalProbabilityContextError, match="验证状态"):
        historical_context._validate_context_source(context_source)  # noqa: SLF001

    limitations = deepcopy(payload["limitations"])
    assert isinstance(limitations, list)
    with pytest.raises(HistoricalProbabilityContextError, match="边界声明"):
        historical_context._validate_context_limitations(  # noqa: SLF001
            [
                item
                for item in limitations
                if item != "historical_replay_not_live_probability_contract"
            ]
        )
    with pytest.raises(HistoricalProbabilityContextError, match="局限字段"):
        historical_context._validate_context_limitations([""])  # noqa: SLF001

    horizon = deepcopy(cast(dict[str, object], payload["horizons"])["5"])
    assert isinstance(horizon, dict)
    for key, value, message in (
        ("horizon", 1, "周期摘要"),
        ("assessment_status", "published", "评估状态"),
        ("probability", 0.6, "不能发布逐股概率"),
        ("limitations", [""], "周期局限"),
    ):
        changed_horizon = {**horizon, key: value}
        with pytest.raises(HistoricalProbabilityContextError, match=message):
            historical_context._validate_horizon(changed_horizon, 5)  # noqa: SLF001

    qualified_horizons = {
        str(item): {
            "assessment_status": "calibrated_shadow",
            "brier_skill_score": 0.01,
            "highest_bin_above_base_rate": True,
            "evaluated_fold_count": 2,
        }
        for item in historical_context.HISTORICAL_REPLAY_HORIZONS
    }
    assert (
        historical_context._historical_availability(qualified_horizons)  # noqa: SLF001
        == "historical_shadow_calibrated_reference_only"
    )


def test_historical_context_io_and_scalar_helpers_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert historical_context._directory_snapshot(tmp_path / "missing") == (None, ())  # noqa: SLF001
    assert historical_context._optional_mapping(None, "optional") == {}  # noqa: SLF001
    assert historical_context._optional_text(None, "optional") is None  # noqa: SLF001
    assert historical_context._optional_finite_number(None, "optional") is None  # noqa: SLF001
    assert historical_context._optional_probability(None, "optional") is None  # noqa: SLF001
    assert historical_context._optional_boolean(None, "optional") is None  # noqa: SLF001

    invalid_calls = (
        (historical_context._mapping, ([], "mapping")),  # noqa: SLF001
        (historical_context._sequence, ({}, "sequence")),  # noqa: SLF001
        (historical_context._required_text, ("", "text")),  # noqa: SLF001
        (historical_context._required_digest, ("x", "digest")),  # noqa: SLF001
        (historical_context._positive_int, (True, "positive")),  # noqa: SLF001
        (historical_context._positive_int, (0, "positive")),  # noqa: SLF001
        (historical_context._nonnegative_int, (-1, "nonnegative")),  # noqa: SLF001
        (historical_context._finite_number, (True, "finite")),  # noqa: SLF001
        (historical_context._finite_number, (math.inf, "finite")),  # noqa: SLF001
        (historical_context._probability, (1.1, "probability")),  # noqa: SLF001
        (historical_context._boolean, (1, "boolean")),  # noqa: SLF001
        (historical_context._aware_datetime, ("not-a-time",)),  # noqa: SLF001
        (historical_context._aware_datetime, ("2026-08-23T12:00:00",)),  # noqa: SLF001
        (historical_context._json_copy, ({"bad": math.nan},)),  # noqa: SLF001
    )
    for function, arguments in invalid_calls:
        with pytest.raises(HistoricalProbabilityContextError):
            function(*arguments)

    outside = tmp_path / "outside"
    outside.mkdir()
    replay = outside / _source_path(tmp_path).name
    replay.write_bytes(b"{}")
    with pytest.raises(HistoricalProbabilityContextError, match="同一目录"):
        publish_historical_probability_context(replay, tmp_path)

    non_object = tmp_path / "non-object.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(HistoricalProbabilityContextError, match="必须是 object"):
        historical_context._load_context_file(non_object)  # noqa: SLF001
    with pytest.raises(HistoricalProbabilityContextError, match="必须是 object"):
        historical_context.build_historical_probability_context(non_object)

    source = _source_path(tmp_path)
    source.write_bytes(b"{}")
    monkeypatch.setattr(
        historical_context,
        "verify_historical_replay_artifact",
        lambda _: _verified_replay(),
    )
    artifact = historical_context.build_historical_probability_context(source)
    source_summary = deepcopy(cast(dict[str, object], artifact["payload"])["source_artifact"])
    assert isinstance(source_summary, dict)
    historical_context._verify_bound_source(tmp_path, source_summary)  # noqa: SLF001
    with pytest.raises(HistoricalProbabilityContextError, match="大小不一致"):
        historical_context._verify_bound_source(  # noqa: SLF001
            tmp_path,
            {**source_summary, "bytes": 3},
        )
    with pytest.raises(HistoricalProbabilityContextError, match="文件摘要不一致"):
        historical_context._verify_bound_source(  # noqa: SLF001
            tmp_path,
            {**source_summary, "sha256": "b" * 64},
        )
    with pytest.raises(HistoricalProbabilityContextError, match="源不可用"):
        historical_context._verify_bound_source(  # noqa: SLF001
            tmp_path,
            {**source_summary, "filename": source.name.replace("2025", "2024")},
        )
    with pytest.raises(HistoricalProbabilityContextError, match="源文件名无效"):
        historical_context._validate_source_filename_binding(  # noqa: SLF001
            {**source_summary, "filename": f"../{source.name}"}
        )
    with pytest.raises(HistoricalProbabilityContextError, match="文件名与摘要不一致"):
        historical_context._validate_source_filename_binding(  # noqa: SLF001
            {**source_summary, "integrity_digest": "b" * 64}
        )


def _source_path(directory: Path) -> Path:
    return directory / ("market-scan-probability-historical-replay-2025-01-02-" f"2026-01-30-{_SOURCE_DIGEST}.json")


def _verified_replay() -> dict[str, Any]:
    horizons: dict[str, object] = {}
    quality_horizons: dict[str, object] = {}
    for horizon, minimum, auc, skill in (
        (1, 222, 0.499, -0.001),
        (5, 230, 0.49, -0.01),
        (20, 260, 0.45, -0.005),
    ):
        horizons[str(horizon)] = {
            "status": "insufficient_data",
            "base_rate": 0.55,
            "training_cutoff": "2025-11-13",
            "counts": {
                "available_independent_session_count": 279,
                "out_of_sample_session_count": 60,
                "evaluated_fold_count": 1,
                "observation_count": 26784,
            },
            "calibration_metrics": {
                "calibrated": {
                    "auc": auc,
                    "brier_score": 0.25,
                    "brier_skill_score": skill,
                    "ece": 0.08,
                    "bin_monotonic": False,
                    "highest_bin_above_base_rate": False,
                }
            },
            "limitations": ["shadow_only_no_production_ranking_effect"],
        }
        quality_horizons[str(horizon)] = {
            "label_coverage": 1.0,
            "minimum_required_independent_session_count": minimum,
        }
    return {
        "schema_version": HISTORICAL_REPLAY_ARTIFACT_SCHEMA_VERSION,
        "generated_at": "2026-08-11T15:58:07+00:00",
        "payload": {
            "generated_at": "2026-08-11T15:58:07+00:00",
            "cohort": {
                "mode": HISTORICAL_REPLAY_COHORT_MODE,
                "scope": "qfq_kline_daily_deterministic_market_sample",
                "rule_version": "historical-replay-common-ohlcv-v1",
                "official": False,
                "live_cohort_compatible": False,
            },
            "config": {"start_date": "2025-01-02", "end_date": "2026-01-30"},
            "quality": {
                "record_count": 26784,
                "record_independent_session_count": 279,
                "selected_symbol_count": 96,
                "horizons": quality_horizons,
            },
            "probability_fit": {"horizons": horizons},
            "limitations": ["survivorship_bias_current_qfq_cache_universe"],
        },
        "integrity": {
            "algorithm": "sha256",
            "integrity_digest": _SOURCE_DIGEST,
            "notice": "sha256_integrity_not_signature_or_official_snapshot_attestation",
        },
    }
