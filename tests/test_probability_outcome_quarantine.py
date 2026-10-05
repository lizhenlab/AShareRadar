"""Rejected immutable labels stay rejected without blocking unrelated maintenance."""

from copy import deepcopy
from dataclasses import replace
import gzip
from pathlib import Path

import pytest

from app.services import market_scan_probability_maintenance as maintenance
from app.services import market_scan_probability_outcomes as outcomes
from app.services.market_scan_probability import stable_probability_hash
from app.services.market_scan_probability_labels import probability_label_contract
from tests import test_market_scan_probability_maintenance as support
from tests import test_market_scan_probability_outcomes as outcome_support


def _legacy_missing_previous_bar(monkeypatch):
    source = outcome_support._source()
    monkeypatch.setattr(outcomes, "_source_artifact", lambda _value: source)
    rows = [*outcome_support._complete_h1_rows(), outcome_support._bar("2026-08-19", 10.0, 10.1)]
    artifact = outcomes.build_probability_outcome_artifact(
        source, {"600001.SH": rows}, generated_at="2026-08-19T18:00:00+08:00", as_of_date="2026-08-19",
    )
    payload = artifact["payload"]
    payload["label_contract"] = probability_label_contract(label_version="market-scan-upside-label-v3-explicit-target-offset")
    payload["label_contract_digest"] = stable_probability_hash(payload["label_contract"])
    record = payload["records"][0]
    evidence = record["bar_evidence"]
    evidence["version"] = "qfq-daily-fixed-session-bar-evidence-v1"
    for bar in evidence["bars"]:
        bar.pop("session_status")
        bar.pop("open_execution_status")
    evidence["bar_set_digest"] = stable_probability_hash(evidence["bars"])
    h5 = record["horizons"]["5"]["outcome"]
    assert h5["reason"] == "fixed_exit_previous_session_bar_missing"
    assert h5["net_return"] is None
    # Literal flags from the historical 000635.SZ / run 135 failure. The data
    # unavailability is unchanged; the old execution-limitation flags are not.
    h5["model_limited"] = h5["daily_bar_model_limited"] = True
    artifact["integrity"]["integrity_digest"] = outcomes.probability_outcome_payload_digest(payload)
    return artifact


def _write_archive(directory, artifact):
    payload = artifact["payload"]
    digest = artifact["integrity"]["integrity_digest"]
    path = directory / f"market-scan-probability-outcomes-run-{payload['source']['run_id']}-through-{payload['as_of_date']}-{digest}.json.gz"
    path.write_bytes(gzip.compress(outcomes._canonical_json(artifact).encode(), compresslevel=9, mtime=0))
    return path


def test_old_missing_previous_bar_flags_remain_rejected_with_bound_diagnostic(monkeypatch, tmp_path):
    artifact = _legacy_missing_previous_bar(monkeypatch)
    original = deepcopy(artifact)
    path = _write_archive(tmp_path, artifact)
    frozen = path.read_bytes()
    with pytest.raises(outcomes.ProbabilityOutcomeReplayError, match="旧 model_limited") as error:
        outcomes.load_probability_outcome_artifact(path)
    assert not isinstance(error.value, outcomes.ProbabilityOutcomeSemanticDriftError)
    assert (error.value.run_id, error.value.as_of_date) == (71, "2026-08-19")
    assert error.value.source_digest == artifact["payload"]["source"]["integrity_digest"]
    assert error.value.integrity_digest == artifact["integrity"]["integrity_digest"]
    with pytest.raises(outcomes.ProbabilityOutcomeReplayError) as unbound:
        outcomes.verify_probability_outcome_artifact(artifact)
    assert unbound.value.run_id is None
    assert path.read_bytes() == frozen and artifact == original


@pytest.mark.parametrize("corruption", ["filename", "gzip", "digest"])
def test_replay_rejection_cannot_bypass_mechanical_seal(monkeypatch, tmp_path, corruption):
    artifact = _legacy_missing_previous_bar(monkeypatch)
    if corruption == "digest":
        artifact["payload"]["records"][0]["instrument"]["quote_amount"] += 1
    path = _write_archive(tmp_path, artifact)
    if corruption == "filename":
        wrong = path.with_name(path.name.replace("run-71-", "run-72-"))
        path.rename(wrong)
        path = wrong
    elif corruption == "gzip":
        path.write_bytes(gzip.compress(outcomes._canonical_json(artifact).encode(), mtime=1))
    with pytest.raises(outcomes.ProbabilityOutcomeError) as error:
        maintenance._outcome_catalog_entry(path)
    assert not isinstance(error.value, outcomes.ProbabilityOutcomeReplayError)


def test_catalog_caches_rejection_and_revalidates_changed_bytes(monkeypatch, tmp_path):
    artifact = _legacy_missing_previous_bar(monkeypatch)
    path = _write_archive(tmp_path, artifact)
    service = maintenance.MarketScanProbabilityMaintenanceService(
        support._Cache(tmp_path / "runtime.sqlite3"), outcome_directory=tmp_path,
    )
    loads = []
    real_load = maintenance.load_probability_outcome_artifact

    def load(candidate):
        loads.append(candidate)
        return real_load(candidate)

    monkeypatch.setattr(maintenance, "load_probability_outcome_artifact", load)
    assert service._outcome_manifests() == service._outcome_manifests() == ()
    assert loads == [path]
    assert len(service._replay_rejections) == 1
    path.write_bytes(b"changed corrupt bytes")
    with pytest.raises(outcomes.ProbabilityOutcomeError):
        service._outcome_manifests()
    assert loads == [path, path]


@pytest.mark.parametrize("remove_directory", [False, True])
def test_disappearing_archive_cannot_silently_release_process_quarantine(monkeypatch, tmp_path, remove_directory):
    artifact = _legacy_missing_previous_bar(monkeypatch)
    directory = tmp_path / "outcomes"
    directory.mkdir()
    path = _write_archive(directory, artifact)
    cache = support._Cache(tmp_path / "runtime.sqlite3")
    service = maintenance.MarketScanProbabilityMaintenanceService(cache, outcome_directory=directory)
    source = maintenance._SourceManifest(
        tmp_path / "source.json.gz", 71, "2026-08-11", "2026-08-11T16:00:00+08:00",
        "2026-08-11T16:05:00+08:00", ("official", "全市场A股", "v1"), "a" * 64,
    )
    monkeypatch.setattr(service, "_source_manifests", lambda: (source,))
    assert service._outcome_manifests() == ()
    path.unlink()
    if remove_directory:
        directory.rmdir()
    for _ in range(2):
        summary = service.run(now=support._at("2026-08-20"), as_of_date="2026-08-20")
        assert summary.quarantined_count == 1 and summary.degraded
        assert summary.fit_status == "quarantined" and summary.due_count == 0
        assert "档案当前缺失，保留本进程隔离" in summary.failures[0]
    assert cache.calls == []
    directory.mkdir(exist_ok=True)
    current = outcomes.build_probability_outcome_artifact(
        outcome_support._source(), {"600001.SH": outcome_support._complete_h1_rows()},
        generated_at="2026-08-20T18:00:00+08:00", as_of_date="2026-08-20",
    )
    outcomes.publish_built_probability_outcome_artifact(directory, current)
    restored = service.run(now=support._at("2026-08-20"), as_of_date="2026-08-20")
    assert restored.quarantined_count == 0 and not restored.degraded
    assert restored.fit_status == "threshold_pending" and cache.calls == []
    assert len(service._replay_rejections) == 1  # evidence memory survives recovery too


def _rejected(source, *, day="2026-08-19"):
    return maintenance._OutcomeReplayRejectionManifest(
        Path("rejected.json.gz"), source.run_id, day, f"{day}T18:00:00+08:00",
        "e" * 64, source.digest, "000635.SZ 旧 model_limited/daily_bar_model_limited 标记冲突",
    )


def test_quarantined_source_skips_rebuild_but_unrelated_labels_continue(monkeypatch, tmp_path):
    service, cache, published = support._service(
        tmp_path, monkeypatch, [support._source(71, "2026-08-11"), support._source(72, "2026-08-12")],
    )
    sources = service._source_manifests()
    service._replay_rejections = (_rejected(sources[0]),)
    monkeypatch.setattr(service, "_outcome_manifests", lambda: ())
    monkeypatch.setattr(maintenance, "probability_outcome_required_dates", lambda *_args, **_kwargs: ("2026-08-19",))
    summaries = [service.run(now=support._at("2026-08-19"), as_of_date="2026-08-19") for _ in range(2)]
    assert all(item.quarantined_count == 1 and item.degraded for item in summaries)
    assert all(item.fit_status == "quarantined" and item.failed_count == 0 for item in summaries)
    assert all("000635.SZ" in item.failures[0] and "旧 model_limited" in item.failures[0] for item in summaries)
    assert all("重放拒绝隔离档案 1" in item.message() for item in summaries)
    assert {item["source"]["run_id"] for item in published} == {72}
    assert len(cache.calls) == 2  # no read or rebuild for rejected run 71


def test_quarantine_blocks_entire_cohort_instead_of_dropping_bad_sample(monkeypatch, tmp_path):
    service, _cache, _published = support._service(
        tmp_path, monkeypatch, [support._source(71, "2026-08-11"), support._source(72, "2026-08-12")],
    )
    sources = service._source_manifests()
    independent = replace(sources[1], run_id=73, cohort=("official", "全市场A股", "other-rule"))
    seen = []
    monkeypatch.setattr(maintenance, "_ready_fit_cohorts", lambda selected, _outcomes: seen.extend(selected) or [])
    count, status = service._maintain_fit((*sources, independent), {}, [], frozenset({sources[0].cohort}))
    assert (count, status) == (0, "quarantined")
    assert seen == [independent]


@pytest.mark.parametrize("replacement", ["older", "equal", "different_source", "newer"])
def test_quarantine_recovers_only_with_later_verified_matching_source(monkeypatch, tmp_path, replacement):
    service, _cache, _published = support._service(tmp_path, monkeypatch, [support._source(71, "2026-08-11")])
    source = service._source_manifests()[0]
    day = {"older": "2026-08-18", "equal": "2026-08-19"}.get(replacement, "2026-08-20")
    valid = support._outcome_manifest_fixture(tmp_path / "valid.json.gz", as_of_date=day, generated_at=f"{day}T18:00:00+08:00")
    if replacement == "different_source":
        valid = replace(valid, source_digest="b" * 64)
    quarantine = maintenance._replay_quarantine((source,), {71: valid}, (_rejected(source),))
    assert bool(quarantine.rejected) is (replacement != "newer")
    assert bool(quarantine.cohorts) is (replacement != "newer")


def test_unbound_rejection_cannot_be_cached(monkeypatch, tmp_path):
    def reject(_path):
        raise outcomes.ProbabilityOutcomeReplayError("unbound")

    monkeypatch.setattr(maintenance, "load_probability_outcome_artifact", reject)
    with pytest.raises(outcomes.ProbabilityOutcomeError, match="缺少机械封存身份"):
        maintenance._outcome_catalog_entry(tmp_path / "unbound.json.gz")
