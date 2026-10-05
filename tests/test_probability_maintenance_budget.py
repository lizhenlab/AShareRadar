from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import market_scan_probability_maintenance as maintenance


class _Clock:
    value = 0.0

    def __call__(self):
        return self.value


def _service(tmp_path, monkeypatch, count=3):
    source_dir, outcome_dir = tmp_path / "sources", tmp_path / "outcomes"
    source_dir.mkdir()
    outcome_dir.mkdir()
    service = maintenance.MarketScanProbabilityMaintenanceService(
        SimpleNamespace(path=tmp_path / "runtime.sqlite3"),
        source_directory=source_dir, outcome_directory=outcome_dir,
    )
    clock = _Clock()
    monkeypatch.setattr(maintenance.time, "monotonic", clock)
    paths, sources, loaded, processed = [], [], [], []
    for index in range(count):
        path = source_dir / f"market-scan-probability-source-run-{index}.json.gz"
        path.write_bytes(b"source")
        paths.append(path)
        sources.append(maintenance._SourceManifest(
            path=path, run_id=index, quote_date=f"2026-09-{index + 1:02}",
            as_of="2026-09-01T16:00:00+08:00", captured_at="2026-09-01T16:00:00+08:00",
            cohort=("official", "all", "rules"), digest=str(index) * 64,
        ))

    def load(path):
        loaded.append(path)
        clock.value += 1
        return sources[paths.index(path)]

    def maintain(_service, source, _latest, _as_of, _now, counts, _ledger):
        processed.append(source.run_id)
        counts["skipped"] += 1

    monkeypatch.setattr(maintenance, "_source_manifest", load)
    monkeypatch.setattr(maintenance, "_maintain_source", maintain)
    return service, clock, paths, sources, loaded, processed


def _run(service, seconds=1):
    return service.run(
        now=datetime(2026, 9, 22, tzinfo=timezone.utc),
        as_of_date="2026-09-21", time_budget_seconds=seconds,
    )


def test_catalog_budget_resumes_validated_files_without_partial_publication(tmp_path, monkeypatch):
    service, _clock, paths, _sources, loaded, processed = _service(tmp_path, monkeypatch)
    fits = []
    monkeypatch.setattr(service, "_maintain_fit", lambda *args: (fits.append(args) or (0, "threshold_pending")))

    first = _run(service)
    second = _run(service)
    third = _run(service)

    assert (first.phase, first.completed_items, first.total_items) == ("source_catalog", 1, 3)
    assert (second.phase, second.completed_items, second.total_items) == ("source_catalog", 2, 3)
    assert third.pending and third.phase == "sources"
    assert processed == fits == []
    assert loaded == paths
    final = _run(service)
    assert not final.pending and not final.degraded
    assert processed == [0, 1, 2] and len(fits) == 1
    assert loaded == paths
    assert "分段待续 source_catalog 1/3" in first.message()


def test_changed_verified_file_is_revalidated_before_partial_catalog_can_complete(tmp_path, monkeypatch):
    service, _clock, paths, _sources, loaded, processed = _service(tmp_path, monkeypatch, count=2)
    assert _run(service).pending
    paths[0].write_bytes(b"changed-source-identity")

    second = _run(service)

    assert second.pending and second.phase == "source_catalog"
    assert loaded == [paths[0], paths[0]]
    assert processed == []


def test_catalog_retains_verified_prefix_after_later_unbound_failure(tmp_path, monkeypatch):
    service, _clock, paths, sources, loaded, processed = _service(tmp_path, monkeypatch, count=2)

    def load(path):
        loaded.append(path)
        if path == paths[1]:
            raise maintenance.ProbabilityOutcomeError("unbound corruption")
        return sources[0]

    monkeypatch.setattr(maintenance, "_source_manifest", load)
    for _ in range(2):
        with pytest.raises(maintenance.ProbabilityOutcomeError, match="unbound corruption"):
            _run(service)
    assert loaded == [paths[0], paths[1], paths[1]]
    assert processed == []


def test_source_phase_resumes_and_preserves_failed_source_diagnostics(tmp_path, monkeypatch):
    service, clock, _paths, _sources, _loaded, processed = _service(tmp_path, monkeypatch)
    service._source_manifests()

    def maintain(_service, source, _latest, _as_of, _now, _counts, _ledger):
        processed.append(source.run_id)
        clock.value += 1
        if source.run_id == 0:
            raise ValueError("fixture rejected source")

    monkeypatch.setattr(maintenance, "_maintain_source", maintain)
    first, second, third = _run(service), _run(service), _run(service)

    assert first.pending and second.pending and not third.pending
    assert first.degraded and second.degraded and third.degraded
    assert third.failures == ("run 0: fixture rejected source",)
    assert processed == [0, 1, 2]
    assert not service._completed_sources


def test_catalog_changes_cannot_skip_new_or_changed_source_work(tmp_path, monkeypatch):
    service, clock, paths, sources, _loaded, processed = _service(tmp_path, monkeypatch)
    service._source_manifests()
    original = maintenance._maintain_source

    def maintain(*args):
        original(*args)
        clock.value += 1

    monkeypatch.setattr(maintenance, "_maintain_source", maintain)
    assert _run(service).pending
    sources[0] = maintenance.replace(sources[0], digest="f" * 64)
    paths[0].write_bytes(b"new-source")
    _run(service, seconds=10)
    assert processed == [0, 0, 1, 2]


def test_mutating_source_during_maintenance_defers_fit(tmp_path, monkeypatch):
    service, _clock, paths, sources, _loaded, _processed = _service(tmp_path, monkeypatch, count=1)
    fits = []

    def mutate(*_args):
        sources[0] = maintenance.replace(sources[0], digest="f" * 64)
        paths[0].write_bytes(b"new-source")

    monkeypatch.setattr(maintenance, "_maintain_source", mutate)
    monkeypatch.setattr(service, "_maintain_fit", lambda *args: (fits.append(args) or (0, "unchanged")))
    summary = _run(service, seconds=10)
    assert summary.pending and summary.phase == "source_catalog_changed"
    assert fits == []


@pytest.mark.parametrize("rejection_type", [maintenance._OutcomeReplayRejectionManifest, maintenance._OutcomeSemanticDriftManifest])
def test_partial_replay_rejection_survives_file_removal(tmp_path, monkeypatch, rejection_type):
    service, clock, _paths, _sources, _loaded, _processed = _service(tmp_path, monkeypatch, count=0)
    paths = [service.outcome_directory / f"market-scan-probability-outcomes-run-{index}.json.gz" for index in range(2)]
    for path in paths:
        path.write_bytes(b"outcome")

    def load(path):
        clock.value += 1
        return rejection_type(
            path, 1, "2026-09-21", "2026-09-21T16:00:00+08:00", "a" * 64, "b" * 64, "rejected",
        )

    monkeypatch.setattr(maintenance, "_outcome_catalog_entry", load)
    first = _run(service)
    paths[0].unlink()
    final = _run(service, seconds=10)
    assert first.pending and first.quarantined_count == 1 and first.degraded
    assert final.quarantined_count == 2 and final.degraded
    assert len(service._replay_rejections) == 2
    assert service._replay_rejections[0].archive_present is False


def test_fit_budget_checks_between_cohorts_and_keeps_published_count(tmp_path, monkeypatch):
    service, clock, _paths, _sources, _loaded, _processed = _service(tmp_path, monkeypatch, count=0)
    monkeypatch.setattr(maintenance, "_ready_fit_cohorts", lambda *_args: [("one",), ("two",)])
    built = []

    def fit(pairs):
        built.append(pairs)
        clock.value += 1
        return 1

    monkeypatch.setattr(service, "_maintain_fit_cohort", fit)
    summary = _run(service)
    assert summary.pending and summary.phase == "fit_assessment"
    assert summary.fit_assessment_count == 1 and built == [("one",)]


@pytest.mark.parametrize("budget", [0, -1, float("inf"), float("nan")])
def test_invalid_budget_rejected_before_loading(tmp_path, monkeypatch, budget):
    service, _clock, _paths, _sources, loaded, _processed = _service(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="正有限"):
        _run(service, budget)
    assert loaded == []


def test_fit_catalog_budget_keeps_only_verified_fingerprints(tmp_path, monkeypatch):
    service, clock, _paths, _sources, _loaded, _processed = _service(tmp_path, monkeypatch, count=0)
    service.fit_directory.mkdir(parents=True)
    fit_paths = [service.fit_directory / f"market-scan-probability-fit-through-run-{index}.json.gz" for index in range(2)]
    for path in fit_paths:
        path.write_bytes(b"fit")
    loaded = []

    def load(path):
        loaded.append(path)
        clock.value += 1
        return (("official", "all", "rules"), str(path))

    monkeypatch.setattr(maintenance, "_fit_corpus_identity", load)
    monkeypatch.setattr(maintenance, "_ready_fit_cohorts", lambda *_args: [("one",)])
    monkeypatch.setattr(service, "_maintain_fit_cohort", lambda _pairs: 0)
    first, second, final = _run(service), _run(service), _run(service)
    assert first.pending and first.phase == "fit_catalog"
    assert second.pending and second.phase == "fit_assessment"
    assert not final.pending
    assert loaded == fit_paths


def test_newly_published_outcome_waits_for_full_verification_before_fit(tmp_path, monkeypatch):
    service, clock, _paths, _sources, _loaded, processed = _service(tmp_path, monkeypatch, count=1)
    service._source_manifests()
    outcome = service.outcome_directory / "market-scan-probability-outcomes-run-0.json.gz"
    fits, verified = [], []

    def maintain(_service, source, _latest, _as_of, _now, counts, _ledger):
        processed.append(source.run_id)
        if not outcome.exists():
            outcome.write_bytes(b"published")
            counts["published"] += 1
            clock.value += 1

    def load(path):
        verified.append(path)
        return maintenance._OutcomeManifest(
            path, 0, "2026-09-21", "2026-09-21T16:00:00+08:00", "a" * 64,
            "0" * 64, {}, "b" * 64,
        )

    monkeypatch.setattr(maintenance, "_maintain_source", maintain)
    monkeypatch.setattr(maintenance, "_outcome_catalog_entry", load)
    monkeypatch.setattr(service, "_maintain_fit", lambda *args: (fits.append(args) or (0, "unchanged")))
    first = _run(service)
    assert first.pending and first.phase == "outcome_catalog"
    assert first.published_count == 1 and fits == verified == []
    final = _run(service)
    assert not final.pending and verified == [outcome] and len(fits) == 1


def test_cli_distinguishes_pending_and_passes_explicit_budget(tmp_path, monkeypatch, capsys):
    import sys
    from tools import maintain_market_scan_probability as cli

    captured = {}

    def maintain(_cache, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pending=True, degraded=False)

    monkeypatch.setattr(cli, "maintain_market_scan_probability", maintain)
    monkeypatch.setattr(sys, "argv", [
        "maintenance", "--database", str(tmp_path / "runtime.sqlite3"),
        "--source-dir", str(tmp_path / "source"), "--outcome-dir", str(tmp_path / "outcome"),
        "--as-of-date", "2026-09-21", "--time-budget-seconds", "1.5",
    ])
    assert cli.main() == 2
    assert captured["time_budget_seconds"] == 1.5
    assert '"pending": true' in capsys.readouterr().out


@pytest.mark.parametrize("replacement", ["older", "equal", "newer", "different_source"])
def test_semantic_drift_blocks_entire_cohort_until_later_matching_outcome(tmp_path, monkeypatch, replacement):
    service, _clock, _paths, sources, _loaded, processed = _service(tmp_path, monkeypatch)
    sources[2] = maintenance.replace(sources[2], cohort=("official", "all", "independent"))
    drift = maintenance._OutcomeSemanticDriftManifest(
        service.outcome_directory / "rejected.json.gz", 0, "2026-09-20",
        "2026-09-20T16:00:00+08:00", "e" * 64, sources[0].digest,
    )
    day = {"older": "2026-09-19", "equal": "2026-09-20"}.get(replacement, "2026-09-21")
    source_digest = "f" * 64 if replacement == "different_source" else sources[0].digest
    valid = maintenance._OutcomeManifest(
        service.outcome_directory / "valid.json.gz", 0, day, f"{day}T16:00:00+08:00",
        "a" * 64, source_digest, {}, "b" * 64,
    )

    def outcomes():
        service._semantic_drift_by_run = {0: drift}
        return (valid,)

    fitted = []
    monkeypatch.setattr(service, "_outcome_manifests", outcomes)
    monkeypatch.setattr(maintenance, "_ready_fit_cohorts", lambda selected, _outcomes: [(source,) for source in selected])
    monkeypatch.setattr(service, "_maintain_fit_cohort", lambda pair: fitted.append(pair[0].run_id) or 1)
    summary = _run(service, seconds=10)

    assert not summary.pending
    if replacement == "newer":
        assert summary.quarantined_count == 0 and fitted == [0, 1, 2]
        assert processed == [0, 1, 2]
    else:
        assert summary.quarantined_count == 1 and summary.degraded
        assert fitted == [2] and processed == [1, 2]
        assert summary.fit_status == "partial_quarantine"
