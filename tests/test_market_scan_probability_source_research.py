from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import cast

import pytest

from app.services import market_scan_probability_source_research as source_research
from app.services.market_scan_manager import MarketScanManager


def test_source_research_store_rejects_empty_directory_through_ancestor_symlink(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    nested = outside / "nested"
    nested.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    store = source_research.MarketScanProbabilitySourceResearchStore(alias / "nested")
    with pytest.raises(source_research.ProbabilitySourceError, match="路径不是目录"):
        store.research_projection(71)

    loop = tmp_path / "source-loop"
    loop.symlink_to(loop, target_is_directory=True)
    with pytest.raises(source_research.ProbabilitySourceError, match="目录无法读取"):
        source_research.MarketScanProbabilitySourceResearchStore(loop / "nested").research_projection(71)


def test_source_research_reports_canonical_archived_progress_without_probability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = {
        _source_file(tmp_path, 70, "a"): _artifact(70, "2026-08-11", 10, captured_at="2026-08-11T16:05:00+08:00"),
        _source_file(tmp_path, 71, "b"): _artifact(71, "2026-08-11", 20, captured_at="2026-08-11T16:06:00+08:00"),
        _source_file(tmp_path, 72, "c"): _artifact(72, "2026-08-12", 30, captured_at="2026-08-12T16:06:00+08:00"),
    }
    calls: list[Path] = []

    def load(path: str | Path):
        resolved = Path(path).resolve()
        calls.append(resolved)
        return artifacts[resolved]

    monkeypatch.setattr(source_research, "load_probability_source_snapshot", load)
    store = source_research.MarketScanProbabilitySourceResearchStore(tmp_path)

    superseded = store.research_projection(70)
    current = store.research_projection(71)
    repeated = store.research_projection(71)

    assert superseded["status"] == "not_generated"
    assert current == repeated
    assert current["status"] == "insufficient_data"
    assert current["integrity_notice"] == "source_corpus_integrity_digest_not_probability_model_evidence"
    horizons = cast(dict[str, dict[str, dict[str, object]]], current["horizons"])
    study = horizons["5"]["net_excess_positive"]
    counts = cast(dict[str, object], study["counts"])
    contract = cast(dict[str, object], study["contract"])
    assert study["probability"] is None
    assert counts["available_independent_session_count"] == 0
    assert counts["archived_independent_session_count"] == 1
    assert counts["mature_label_session_count"] == 0
    assert counts["observation_count"] == 20
    assert counts["label_coverage"] == 0.0
    assert contract["split"] == {
        "version": "grouped-date-multifold-train-gap-calibration-gap-test-v2",
        "group": "session_date",
        "random_split_forbidden": True,
        "minimum_train_sessions": 120,
        "minimum_calibration_sessions": 40,
        "minimum_test_sessions": 60,
        "gap_sessions": 5,
        "walk_forward": "expanding_train_rolling_calibration_and_test",
    }
    assert "waiting_fixed_horizon_labels" in cast(list[str], study["limitations"])
    corpus = cast(dict[str, object], current["source_corpus"])
    assert corpus["through"] == {
        "quote_date": "2026-08-11",
        "as_of": "2026-08-11T16:06:00+08:00",
        "run_id": 71,
    }
    assert corpus["source_count"] == 1
    assert corpus["observation_count"] == 20
    assert current["generated_at"] == "2026-08-11T16:06:00+08:00"
    assert current["integrity_digest"] == corpus["integrity_digest"]
    assert len(str(corpus["integrity_digest"])) == 64

    future = store.research_projection(72)
    future_counts = cast(
        dict[str, object],
        cast(dict[str, dict[str, dict[str, object]]], future["horizons"])["5"]["net_excess_positive"]["counts"],
    )
    assert future_counts["archived_independent_session_count"] == 2
    assert future_counts["observation_count"] == 50
    assert future["integrity_digest"] != current["integrity_digest"]
    assert len(calls) == 3


def test_source_research_canonical_order_uses_scan_as_of_before_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = {
        _source_file(tmp_path, 70, "a"): _artifact(
            70,
            "2026-08-11",
            10,
            captured_at="2026-08-11T16:20:00+08:00",
            as_of="2026-08-11T16:10:00+08:00",
        ),
        _source_file(tmp_path, 71, "b"): _artifact(
            71,
            "2026-08-11",
            20,
            captured_at="2026-08-11T16:21:00+08:00",
            as_of="2026-08-11T16:00:00+08:00",
        ),
    }
    monkeypatch.setattr(
        source_research,
        "load_probability_source_snapshot",
        lambda path: artifacts[Path(path).resolve()],
    )
    store = source_research.MarketScanProbabilitySourceResearchStore(tmp_path)

    assert store.research_projection(70)["status"] == "insufficient_data"
    assert store.research_projection(71)["status"] == "not_generated"


def test_manager_uses_source_progress_only_when_model_artifact_is_not_generated() -> None:
    manager = object.__new__(MarketScanManager)
    manager._probability_store = _ProbabilityStore("not_generated")  # noqa: SLF001
    manager._probability_source_research_store = _SourceStore()  # noqa: SLF001

    research = manager._run_probability_research(71)  # noqa: SLF001
    projected, probabilities = manager._run_probability_projection(71)  # noqa: SLF001

    assert research["status"] == "insufficient_data"
    assert projected == research
    assert probabilities == {}
    assert manager._probability_source_research_store.calls == [71, 71]  # noqa: SLF001

    manager._probability_store = _ProbabilityStore("calibrated_shadow")  # noqa: SLF001
    calibrated = manager._run_probability_research(71)  # noqa: SLF001
    assert calibrated["status"] == "calibrated_shadow"
    assert manager._probability_source_research_store.calls == [71, 71]  # noqa: SLF001


def test_source_research_newest_capture_uses_aware_instant_not_iso_text_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = _artifact(
        73,
        "2026-08-13",
        10,
        captured_at="2026-08-13T17:00:00+08:00",
        as_of="2026-08-13T16:00:00+08:00",
    )
    newer = _artifact(
        73,
        "2026-08-13",
        20,
        captured_at="2026-08-13T10:00:00+00:00",
        as_of="2026-08-13T08:00:00+00:00",
    )
    artifacts = {
        _source_file(tmp_path, 73, "older"): older,
        _source_file(tmp_path, 73, "newer"): newer,
    }
    monkeypatch.setattr(
        source_research,
        "load_probability_source_snapshot",
        lambda path: artifacts[Path(path).resolve()],
    )

    research = source_research.MarketScanProbabilitySourceResearchStore(tmp_path).research_projection(73)

    study = cast(dict[str, dict[str, dict[str, object]]], research["horizons"])["5"]["net_excess_positive"]
    assert cast(dict[str, object], study["counts"])["observation_count"] == 20
    assert research["generated_at"] == "2026-08-13T10:00:00+00:00"


def test_source_research_incrementally_loads_only_new_fingerprints_for_230_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts: dict[Path, dict[str, object]] = {}
    first_date = date(2025, 1, 1)
    for offset in range(230):
        run_id = 1_000 + offset
        quote_date = (first_date + timedelta(days=offset)).isoformat()
        artifacts[_source_file(tmp_path, run_id, f"seed-{offset}")] = _artifact(
            run_id,
            quote_date,
            5_500,
            captured_at=f"{quote_date}T16:05:00+08:00",
        )
    calls: list[Path] = []

    def load(path: str | Path):
        resolved = Path(path).resolve()
        calls.append(resolved)
        return artifacts[resolved]

    monkeypatch.setattr(source_research, "load_probability_source_snapshot", load)
    store = source_research.MarketScanProbabilitySourceResearchStore(tmp_path)
    first = store.research_projection(1_229)

    assert len(calls) == 230
    assert len(store._summary_by_fingerprint) == 230  # noqa: SLF001
    assert all("payload" not in value for value in store._summary_by_fingerprint.values())  # noqa: SLF001
    assert "sources" not in cast(dict[str, object], first["source_corpus"])
    first_counts = cast(
        dict[str, object],
        cast(dict[str, dict[str, dict[str, object]]], first["horizons"])["5"]["net_excess_positive"]["counts"],
    )
    assert first_counts["archived_independent_session_count"] == 230

    run_id = 1_230
    quote_date = (first_date + timedelta(days=230)).isoformat()
    artifacts[_source_file(tmp_path, run_id, "incremental")] = _artifact(
        run_id,
        quote_date,
        5_500,
        captured_at=f"{quote_date}T16:05:00+08:00",
    )
    second = store.research_projection(run_id)

    assert len(calls) == 231
    assert len(store._summary_by_fingerprint) == 231  # noqa: SLF001
    second_counts = cast(
        dict[str, object],
        cast(dict[str, dict[str, dict[str, object]]], second["horizons"])["5"]["net_excess_positive"]["counts"],
    )
    assert second_counts["archived_independent_session_count"] == 231


def test_source_research_retries_one_concurrent_atomic_publish_without_reloading_verified_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path = _source_file(tmp_path, 80, "first")
    artifacts = {
        first_path: _artifact(80, "2026-08-11", 10, captured_at="2026-08-11T16:05:00+08:00"),
    }
    calls: list[Path] = []

    def load(path: str | Path):
        resolved = Path(path).resolve()
        calls.append(resolved)
        artifact = artifacts[resolved]
        if len(calls) == 1:
            second_path = _source_file(tmp_path, 81, "published-during-read")
            artifacts[second_path] = _artifact(
                81,
                "2026-08-12",
                20,
                captured_at="2026-08-12T16:05:00+08:00",
            )
        return artifact

    monkeypatch.setattr(source_research, "load_probability_source_snapshot", load)
    store = source_research.MarketScanProbabilitySourceResearchStore(tmp_path)

    research = store.research_projection(81)

    counts = cast(
        dict[str, object],
        cast(dict[str, dict[str, dict[str, object]]], research["horizons"])["5"]["net_excess_positive"]["counts"],
    )
    assert research["status"] == "insufficient_data"
    assert counts["archived_independent_session_count"] == 2
    assert counts["observation_count"] == 30
    assert calls == [first_path, tmp_path / "market-scan-probability-source-run-81-published-during-read.json.gz"]


def test_source_research_fails_closed_when_directory_never_stabilizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = source_research.MarketScanProbabilitySourceResearchStore(tmp_path)
    previous_snapshot = (  # noqa: SLF001
        ((1, 1, 1, 1), ()),
        (None, ()),
        "2026-08-12",
    )
    previous_research = {80: {"run_id": 80, "status": "insufficient_data"}}
    store._snapshot = previous_snapshot  # noqa: SLF001
    store._research_by_run = previous_research  # noqa: SLF001
    calls = 0

    def changing_snapshot(_directory: Path, _pattern: str):
        nonlocal calls
        calls += 1
        return ((2, calls, calls, calls), ())

    monkeypatch.setattr(source_research, "_directory_snapshot", changing_snapshot)

    with pytest.raises(source_research.ProbabilitySourceError, match="多次读取期间持续变化"):
        store.research_projection(80)

    assert calls == 6 * source_research._STABLE_SNAPSHOT_READ_ATTEMPTS  # noqa: SLF001
    assert store._snapshot is previous_snapshot  # noqa: SLF001
    assert store._research_by_run is previous_research  # noqa: SLF001


def test_source_research_does_not_fall_back_to_old_cache_when_new_archive_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path = _source_file(tmp_path, 80, "valid")
    artifacts = {
        first_path: _artifact(80, "2026-08-11", 10, captured_at="2026-08-11T16:05:00+08:00"),
    }

    def load(path: str | Path):
        resolved = Path(path).resolve()
        if resolved not in artifacts:
            raise source_research.ProbabilitySourceError("new archive is invalid")
        return artifacts[resolved]

    monkeypatch.setattr(source_research, "load_probability_source_snapshot", load)
    store = source_research.MarketScanProbabilitySourceResearchStore(tmp_path)
    assert store.research_projection(80)["status"] == "insufficient_data"
    previous_snapshot = store._snapshot  # noqa: SLF001
    _source_file(tmp_path, 81, "invalid")

    with pytest.raises(source_research.ProbabilitySourceError, match="new archive is invalid"):
        store.research_projection(81)

    assert store._snapshot == previous_snapshot  # noqa: SLF001
    assert store._research_by_run[80]["status"] == "insufficient_data"  # noqa: SLF001


def test_sampled_fit_is_visible_but_never_qualifies_selection() -> None:
    progress = {
        "available_independent_session_count": 260,
        "archived_independent_session_count": 260,
        "mature_label_session_count": 260,
        "observation_count": 1_430_000,
        "mature_observation_count": 1_430_000,
        "eligible_observation_count": 1_401_400,
        "label_coverage": 0.98,
        "outcome_artifact_count": 260,
        "next_maturity_date": None,
        "maintenance_due": False,
        "has_mature_missing_bars": False,
    }
    fit_artifact = {
        "generated_at": "2026-08-12T18:00:00+08:00",
        "payload": {
            "through_run_id": 71,
            "cohort": {"mode": "official", "scope": "全市场A股", "rule_version": "v1"},
            "members": [{"source_content_digest": "b" * 64, "outcome_content_digest": "d" * 64}],
            "input_pair_digest": "c" * 64,
            "training_cutoff": "2026-08-11",
            "fit_status": "sampled_oos_assessment",
            "fit_replay_verified": True,
            "fit_selection_qualification": {"passed": False},
            "horizons": {
                "5": {"net_excess_positive": {
                    "fit_status": "fitted_oos",
                    "deterministic_replay_verified": True,
                    "selection_qualified": True,
                    "selection_qualification": {"passed": True},
                }},
            },
        },
        "integrity": {"integrity_digest": "a" * 64},
    }
    compact_fit = source_research._compact_fit_summary(fit_artifact)  # noqa: SLF001
    fit = source_research._fit_evidence(  # noqa: SLF001
        compact_fit,
        5,
        "net_excess_positive",
    )
    assert fit is not None

    summary = source_research._source_horizon_summary(  # noqa: SLF001
        5,
        "net_excess_positive",
        progress=progress,
        fit=fit,
    )

    assert summary["pipeline_stage"] == "sampled_fit_assessed"
    assert summary["fit_status"] == "sampled_oos_assessment"
    assert summary["fit_replay_verified"] is True
    assert summary["fit_selection_qualified"] is False
    assert summary["selection_qualified"] is False
    assert summary["selection_status"] == "projection_pending"
    assert summary["probability"] is None
    assert "bounded_sample_benchmark_not_full_market_contract_selection_forbidden" in summary["limitations"]


def test_unverified_sampled_fit_remains_fail_closed() -> None:
    progress = {
        "available_independent_session_count": 260,
        "archived_independent_session_count": 260,
        "mature_label_session_count": 260,
        "observation_count": 260,
        "mature_observation_count": 260,
        "eligible_observation_count": 260,
        "label_coverage": 1.0,
        "outcome_artifact_count": 260,
        "next_maturity_date": None,
        "maintenance_due": False,
        "has_mature_missing_bars": False,
    }
    fit = {"fit_status": "sampled_oos_assessment", "deterministic_replay_verified": False}

    summary = source_research._source_horizon_summary(  # noqa: SLF001
        5,
        "net_excess_positive",
        progress=progress,
        fit=fit,
    )

    assert summary["pipeline_stage"] == "fit_insufficient"
    assert summary["fit_replay_verified"] is False
    assert summary["selection_status"] == "fail_closed_no_verified_fit_evidence"


def test_fit_binding_uses_chronological_rolling_source_outcome_pairs() -> None:
    cohort = {"mode": "official", "scope": "全市场A股", "rule_version": "v1"}
    earlier = {
        "run_id": 99,
        "quote_date": "2026-08-09",
        "as_of": "2026-08-09T16:00:00+08:00",
        "cohort": cohort,
        "integrity_digest": "a" * 64,
    }
    through = {
        "run_id": 100,
        "quote_date": "2026-08-10",
        "as_of": "2026-08-10T16:00:00+08:00",
        "cohort": cohort,
        "integrity_digest": "b" * 64,
    }
    outcomes = {
        99: {"integrity_digest": "c" * 64},
        100: {"integrity_digest": "d" * 64},
    }
    expected = source_research.stable_probability_hash([
        ("a" * 64, "c" * 64),
        ("b" * 64, "d" * 64),
    ])
    fit = {
        "cohort": cohort,
        "through_source_digest": "b" * 64,
        "through_outcome_digest": "d" * 64,
        "input_pair_digest": expected,
    }

    actual = source_research._fit_input_pair_digest(  # noqa: SLF001
        through,
        (through, earlier),
        outcomes,
    )

    assert actual == expected
    assert source_research._fit_for_source(  # noqa: SLF001
        through,
        outcomes[100],
        fit,
        canonical_sources=(through, earlier),
        outcomes=outcomes,
    ) is fit
    with pytest.raises(source_research.ProbabilitySourceError, match="rolling corpus digest"):
        source_research._fit_for_source(  # noqa: SLF001
            through,
            outcomes[100],
            {**fit, "input_pair_digest": "e" * 64},
            canonical_sources=(through, earlier),
            outcomes=outcomes,
        )


class _ProbabilityStore:
    def __init__(self, status: str) -> None:
        self.status = status

    def research_projection(self, run_id: int) -> dict[str, object]:
        return {"run_id": run_id, "status": self.status}

    def run_projection(
        self,
        run_id: int,
        *,
        symbols=None,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        del symbols
        return {"run_id": run_id, "status": self.status}, {}


class _SourceStore:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def research_projection(self, run_id: int) -> dict[str, object]:
        self.calls.append(run_id)
        return {"run_id": run_id, "status": "insufficient_data"}


def _source_file(directory: Path, run_id: int, suffix: str) -> Path:
    path = directory / f"market-scan-probability-source-run-{run_id}-{suffix}.json.gz"
    path.write_bytes(suffix.encode())
    return path.resolve()


def _artifact(
    run_id: int,
    quote_date: str,
    record_count: int,
    *,
    captured_at: str,
    as_of: str | None = None,
) -> dict[str, object]:
    return {
        "captured_at": captured_at,
        "payload": {
            "run": {
                "run_id": run_id,
                "quote_date": quote_date,
                "as_of": as_of or captured_at,
            },
            "cohort": {
                "mode": "official",
                "scope": "全市场A股",
                "rule_version": "full-market-scan-v6:test",
            },
            "quality": {"record_count": record_count},
        },
        "integrity": {"integrity_digest": f"{run_id:064x}"},
    }
