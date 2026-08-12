from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from app.models.market import Kline
from app.services.market_scan_probability import stable_probability_hash
import app.services.market_scan_probability_outcomes as outcomes_module
from app.services.market_scan_probability_outcomes import (
    ProbabilityOutcomeError,
    build_probability_outcome_artifact,
    list_probability_outcome_artifacts,
    load_probability_outcome_artifact,
    load_probability_outcome_artifact_for_run,
    probability_outcome_corpus_progress,
    probability_outcome_payload_digest,
    probability_outcome_required_dates,
    probability_research_rows_from_outcome_artifacts,
    probability_samples_from_outcome_artifacts,
    publish_built_probability_outcome_artifact,
    publish_probability_outcome_artifact,
    verify_probability_outcome_artifact,
)


GENERATED_AT = "2026-08-13T16:30:00+08:00"


def test_outcome_artifact_is_compact_content_addressed_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)
    rows = {"600001.SH": _complete_h1_rows()}

    first = publish_probability_outcome_artifact(
        tmp_path,
        source,
        rows,
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )
    repeated = publish_probability_outcome_artifact(
        tmp_path,
        source,
        rows,
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )

    assert first == repeated
    assert list_probability_outcome_artifacts(tmp_path) == [first]
    loaded = load_probability_outcome_artifact(cast(str, first["path"]))
    assert load_probability_outcome_artifact_for_run(tmp_path, 71) == loaded
    record = cast(list[dict[str, object]], cast(dict[str, object], loaded["payload"])["records"])[0]
    assert "features" not in record and "dimensions" not in record
    assert record["feature_vector_digest"] == stable_probability_hash(_features())
    h1 = cast(dict[str, object], cast(dict[str, object], record["horizons"])["1"])
    assert h1["maturity"] == "mature"
    assert cast(dict[str, object], h1["outcome"])["status"] == "modelled"

    built = build_probability_outcome_artifact(
        source,
        rows,
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )
    assert publish_built_probability_outcome_artifact(tmp_path, built) == first


def test_outcome_required_dates_are_exact_and_horizon_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)

    assert probability_outcome_required_dates(source, as_of_date="2026-08-12") == ()
    assert probability_outcome_required_dates(source, as_of_date="2026-08-13") == (
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
    )
    assert probability_outcome_required_dates(source, as_of_date="2026-08-19") == (
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
        "2026-08-18",
        "2026-08-19",
    )


def test_outcome_uses_fixed_sessions_and_never_shifts_a_missing_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)
    rows = [*_complete_h1_rows()[:-1], _bar("2026-08-14", 12.0, 12.2)]

    artifact = build_probability_outcome_artifact(
        source,
        {"600001.SH": rows},
        generated_at="2026-08-14T16:30:00+08:00",
        as_of_date="2026-08-14",
    )

    payload = cast(dict[str, object], artifact["payload"])
    record = cast(list[dict[str, object]], payload["records"])[0]
    evidence = cast(dict[str, object], record["bar_evidence"])
    assert evidence["requested_dates"] == ["2026-08-11", "2026-08-12", "2026-08-13"]
    assert evidence["missing_dates"] == ["2026-08-13"]
    assert "2026-08-14" not in cast(list[str], evidence["observed_dates"])
    h1 = cast(dict[str, object], cast(dict[str, object], record["horizons"])["1"])
    outcome = cast(dict[str, object], h1["outcome"])
    assert h1["target_session_date"] == "2026-08-13"
    assert outcome["status"] == "data_unavailable"
    assert outcome["reason"] == "fixed_exit_session_bar_missing"
    assert outcome["exit_date"] == "2026-08-13"
    quality = cast(dict[str, object], cast(dict[str, object], payload["quality"])["horizons"])
    assert cast(dict[str, object], quality["1"])["label_coverage"] == 0.0


def test_not_mature_horizons_do_not_request_bars_or_fabricate_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)

    artifact = build_probability_outcome_artifact(
        source,
        {},
        generated_at="2026-08-12T11:30:00+08:00",
        as_of_date="2026-08-12",
    )

    payload = cast(dict[str, object], artifact["payload"])
    record = cast(list[dict[str, object]], payload["records"])[0]
    assert cast(dict[str, object], record["bar_evidence"])["requested_dates"] == []
    for state in cast(dict[str, dict[str, object]], record["horizons"]).values():
        assert state["maturity"] == "not_mature"
        assert state["outcome"] is None
    progress = probability_outcome_corpus_progress([artifact])
    assert all(cast(dict[str, object], value)["mature_label_session_count"] == 0 for value in progress.values())


def test_outcome_verification_replays_labels_and_join_restores_source_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)
    artifact = build_probability_outcome_artifact(
        source,
        {"600001.SH": _complete_h1_rows()},
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )

    research_rows = probability_research_rows_from_outcome_artifacts([source], [artifact])
    assert len(research_rows) == 1
    assert research_rows[0].features == _features()
    assert research_rows[0].mature_horizons == frozenset({1})
    samples = probability_samples_from_outcome_artifacts(
        [source],
        [artifact],
        horizon=1,
        target="absolute_net_positive",
    )
    assert len(samples) == 1 and samples[0].executable is True and samples[0].target == 1

    tampered = deepcopy(artifact)
    payload = cast(dict[str, object], tampered["payload"])
    record = cast(list[dict[str, object]], payload["records"])[0]
    outcome = cast(dict[str, object], cast(dict[str, object], cast(dict[str, object], record["horizons"])["1"])["outcome"])
    outcome["net_return"] = -0.25
    cast(dict[str, object], tampered["integrity"])["integrity_digest"] = probability_outcome_payload_digest(payload)
    with pytest.raises(ProbabilityOutcomeError, match="不能由固定会话K线重放"):
        verify_probability_outcome_artifact(tampered)


def _source() -> dict[str, object]:
    features = _features()
    record = {
        "symbol": "600001.SH",
        "features": features,
        "feature_vector_digest": stable_probability_hash(features),
        "dimensions": {
            "mode": "official",
            "scope": "test-full-market",
            "rule_version": "full-market-scan-v6:test",
            "market": "SH",
            "board": "SH_MAIN",
            "industry": "test",
            "liquidity": "medium",
            "regime": "neutral",
            "segment": "regular",
        },
        "source_evidence_digest": "b" * 64,
        "instrument": {
            "market": "SH",
            "list_date": "2020-01-02",
            "is_st": False,
            "quote_amount": 200_000_000.0,
            "adjustment_mode": "qfq",
        },
    }
    return {
        "schema_version": "market-scan-probability-source-artifact-v1",
        "captured_at": "2026-08-11T16:30:00+08:00",
        "payload": {
            "contract_version": "market-scan-probability-source-snapshot-v1",
            "run": {
                "run_id": 71,
                "quote_date": "2026-08-11",
                "data_date": "2026-08-11",
                "as_of": "2026-08-11T16:00:00+08:00",
            },
            "cohort": {
                "mode": "official",
                "scope": "test-full-market",
                "rule_version": "full-market-scan-v6:test",
            },
            "records": [record],
            "quality": {"record_count": 1},
        },
        "integrity": {"integrity_digest": "a" * 64},
    }


def _features() -> dict[str, float]:
    return {"trend_score": 80.0, "production_score": 82.0}


def _complete_h1_rows() -> list[Kline]:
    return [
        _bar("2026-08-11", 10.0, 10.0),
        _bar("2026-08-12", 10.0, 10.5),
        _bar("2026-08-13", 10.5, 11.0),
    ]


def _bar(row_date: str, open_price: float, close_price: float) -> Kline:
    return Kline(
        date=row_date,
        open=open_price,
        close=close_price,
        high=max(open_price, close_price) + 0.2,
        low=min(open_price, close_price) - 0.1,
        volume=1_000,
        adjustment_mode="qfq",
        data_version="probability-outcome-test-v1",
        contract_version="daily-kline.v1",
        source="test",
    )
