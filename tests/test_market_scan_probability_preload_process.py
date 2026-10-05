from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time

import pytest

from app.artifacts.io import canonical_json_bytes
from app.services import market_scan_probability_fit_assessment as fits
from app.services import market_scan_probability_historical_context as historical
from app.services import market_scan_probability_outcomes as outcomes
from app.services import market_scan_probability_preload_process as worker
from app.services import market_scan_probability_preload_worker as child
from app.services import market_scan_probability_source as sources
from app.services import market_scan_probability_source_research as research


def _source_archive(directory: Path, run_id: int = 71) -> Path:
    artifact = json.loads((Path(__file__).parent / "fixtures" / "market_scan_probability_source_v2_v4.json").read_text())
    artifact["schema_version"] = sources.LEGACY_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION
    payload = artifact["payload"]
    payload["contract_version"] = sources.LEGACY_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
    payload["run"]["run_id"] = run_id
    for record in payload["records"]:
        record["source_evidence_contract_version"] = sources.MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION
    for key in ("production_score_rule_version", "production_score_spec_hash", "full_market_coverage"):
        payload["run"].pop(key)
    payload["score_semantics"].pop("production_score_spec_hash")
    for key in ("run_total_count", "run_success_count", "success_to_total_coverage", "strata_coverage", "full_market_coverage"):
        payload["quality"].pop(key)
    artifact["integrity"]["integrity_digest"] = sources.probability_source_payload_digest(payload)
    verified = sources.verify_probability_source_snapshot(artifact)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / sources.probability_source_snapshot_filename(run_id, verified)
    path.write_bytes(sources._compressed_artifact_bytes(verified))
    return path


def _archive_files(tmp_path: Path):
    source = _source_archive(tmp_path / "source")
    source_artifact = sources.load_probability_source_snapshot(source)
    frozen_outcome = json.loads((Path(__file__).parent / "fixtures" / "market_scan_probability_feature_v2_preload_outcome.json").read_text())
    assert frozen_outcome["payload"]["source"]["integrity_digest"] == source_artifact["integrity"]["integrity_digest"]
    outcome = outcomes.publish_built_probability_outcome_artifact(tmp_path / "outcomes", frozen_outcome)
    outcome_path = Path(outcome["path"])
    # This test reads frozen historical archives; old features must no longer
    # be passed through the current estimator merely to produce test input.
    assessment = json.loads((Path(__file__).parent / "fixtures" / "market_scan_probability_feature_v2_preload_fit.json").read_text())
    assert assessment["payload"]["members"][0]["source_filename"] == source.name
    assert assessment["payload"]["members"][0]["outcome_filename"] == outcome_path.name
    fit_path = Path(fits.publish_probability_fit_assessment(tmp_path / "fits", assessment)["path"])
    return [(kind, research._file_fingerprint(path)) for kind, path in (("source", source), ("outcome", outcome_path), ("fit", fit_path))]


def _store(tmp_path: Path) -> research.MarketScanProbabilitySourceResearchStore:
    return research.MarketScanProbabilitySourceResearchStore(tmp_path / "source", outcome_directory=tmp_path / "outcomes", fit_directory=tmp_path / "fits")


def _request(files) -> bytes:
    return canonical_json_bytes({"schema_version": worker._SCHEMA, "files": [worker._encode_file(*item) for item in files]})


def test_real_worker_returns_identical_compact_source_outcome_fit_and_never_changes_archives(tmp_path):
    files = _archive_files(tmp_path)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.rglob("*") if path.is_file()}
    expected = {item: child._verified_summary(*item) for item in files}

    actual = worker.isolated_probability_summaries(files)

    assert actual == expected
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.rglob("*") if path.is_file()} == before
    assert all("records" not in summary and "payload" not in summary for summary in actual.values())
    fit = actual[files[2]]
    assert fit["fit_selection_qualification"]["passed"] is False


def test_isolated_store_projection_matches_synchronous_store_and_keeps_authority_fail_closed(tmp_path):
    files = _archive_files(tmp_path)
    binding = {71: files[0][1][0].name.split("-")[-1].removesuffix(".json.gz")}
    synchronous, isolated = _store(tmp_path), _store(tmp_path)
    assert synchronous.preload(archive_bindings=binding) == 1
    assert isolated.preload_isolated(archive_bindings=binding) == 1
    assert isolated.research_projection(71) == synchronous.research_projection(71)
    assert isolated.verified_archive_digests() == synchronous.verified_archive_digests()


@pytest.mark.parametrize("kind", ["source", "outcome", "fit"])
def test_real_worker_rejects_resealed_semantic_tampering_not_only_invalid_gzip(tmp_path, kind):
    import gzip

    files = _archive_files(tmp_path)
    fingerprint = next(fingerprint for candidate, fingerprint in files if candidate == kind)
    path = fingerprint[0]
    artifact = json.loads(gzip.decompress(path.read_bytes()))
    if kind == "source":
        artifact["payload"]["quality"]["record_count"] += 1
    elif kind == "outcome":
        artifact["payload"]["quality"]["horizons"]["1"]["mature_record_count"] += 1
    else:
        artifact["payload"]["fit_selection_qualified"] = True
    artifact["integrity"]["integrity_digest"] = sources.probability_source_payload_digest(artifact["payload"])
    digest = artifact["integrity"]["integrity_digest"]
    name = path.name.rsplit("-", 1)[0] + f"-{digest}.json.gz"
    tampered = path.with_name(name)
    tampered.write_bytes(gzip.compress(canonical_json_bytes(artifact), mtime=0))

    with pytest.raises(sources.ProbabilitySourceError, match="只读校验失败"):
        worker.isolated_probability_summaries([(kind, research._file_fingerprint(tampered))])


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e309}', b'{"x":"\\ud800"}', b'\xff', b'[]', b'{'])
def test_malformed_worker_json_is_a_safe_source_error(raw):
    with pytest.raises(sources.ProbabilitySourceError, match="JSON"):
        worker._decode_worker_response(raw, b"request", [])


@pytest.mark.parametrize("mutation", ["schema", "digest", "count", "order", "fingerprint", "summary_keys", "run_id", "content_digest", "as_of"])
def test_worker_response_requires_exact_request_and_content_binding(tmp_path, mutation):
    files = _archive_files(tmp_path)
    request = _request(files)
    response = json.loads(child._worker_response(request))
    if mutation == "schema":
        response["schema_version"] = "other"
    elif mutation == "digest":
        response["request_digest"] = "0" * 64
    elif mutation == "count":
        response["results"].pop()
    elif mutation == "order":
        response["results"].reverse()
    elif mutation == "fingerprint":
        response["results"][0]["fingerprint"][4] += 1
    elif mutation == "summary_keys":
        response["results"][0]["summary"]["authority"] = True
    elif mutation == "run_id":
        response["results"][0]["summary"]["run_id"] = True
    elif mutation == "content_digest":
        response["results"][0]["summary"]["integrity_digest"] = "0" * 64
    else:
        response["results"][1]["summary"]["as_of_date"] = "2026-08-13"
    with pytest.raises(sources.ProbabilitySourceError):
        worker._decode_worker_response(canonical_json_bytes(response), request, files)


@pytest.mark.parametrize("mutation", ["relative", "traversal", "bool_fact", "directory", "duplicate", "kind", "filename", "extra"])
def test_worker_request_rejects_invalid_fingerprints_before_spawn(tmp_path, mutation, monkeypatch):
    path = _source_archive(tmp_path)
    value = json.loads(_request([("source", research._file_fingerprint(path))]))
    item = value["files"][0]
    if mutation == "relative":
        item["fingerprint"][0] = path.name
    elif mutation == "traversal":
        item["fingerprint"][0] = f"{tmp_path}/../{path.name}"
    elif mutation == "bool_fact":
        item["fingerprint"][1] = True
    elif mutation == "directory":
        item["fingerprint"][3] = 0o040755
    elif mutation == "duplicate":
        value["files"].append(deepcopy(item))
    elif mutation == "kind":
        item["kind"] = "provider"
    elif mutation == "filename":
        item["fingerprint"][0] = str(tmp_path / "secret.json.gz")
    else:
        item["database"] = "write.sqlite"
    with pytest.raises(sources.ProbabilitySourceError):
        worker._read_worker_request(canonical_json_bytes(value))


def test_worker_rejects_input_file_count_and_byte_budget_before_spawn(tmp_path, monkeypatch):
    path = _source_archive(tmp_path)
    files = [("source", research._file_fingerprint(path))]
    monkeypatch.setattr(worker, "WORKER_MAX_FILES", 0)
    with pytest.raises(sources.ProbabilitySourceError, match="数量"):
        worker.isolated_probability_summaries(files)
    monkeypatch.setattr(worker, "WORKER_MAX_FILES", 2048)
    monkeypatch.setattr(worker, "WORKER_MAX_INPUT_BYTES", 1)
    with pytest.raises(sources.ProbabilitySourceError, match="请求过大"):
        worker.isolated_probability_summaries(files)


def _capture_processes(monkeypatch):
    processes = []
    popen = subprocess.Popen

    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", start)
    return processes


@pytest.mark.parametrize("mode", ["timeout", "cancel", "oversized", "exit", "bad_json"])
def test_supervisor_bounds_live_output_cancels_and_reaps_without_leaking_stderr(tmp_path, monkeypatch, mode):
    path = _source_archive(tmp_path)
    files = [("source", research._file_fingerprint(path))]
    script = "import os, sys, time; sys.stdin.buffer.read(); sys.stderr.write('PRIVATE_TOKEN'); "
    script += {
        "timeout": "time.sleep(30)", "cancel": "time.sleep(30)",
        "oversized": "os.write(1, b'x' * 8192); time.sleep(30)",
        "exit": "sys.exit(2)", "bad_json": "print('{not-json}')",
    }[mode]
    monkeypatch.setattr(worker, "_worker_command", lambda: [sys.executable, "-c", script])
    monkeypatch.setattr(worker, "WORKER_MAX_OUTPUT_BYTES", 1024)
    processes = _capture_processes(monkeypatch)
    cancelled = Event()
    completed = Event()

    def cancel_when_started():
        while not completed.wait(0.01):
            if processes:
                cancelled.set()
                return

    canceller = Thread(target=cancel_when_started) if mode == "cancel" else None
    if canceller is not None:
        canceller.start()
    try:
        deadline = time.monotonic() + (0.2 if mode == "timeout" else 5.0)
        expected = worker.ProbabilitySourcePreloadCancelled if mode == "cancel" else sources.ProbabilitySourceError
        with pytest.raises(expected) as error:
            worker.isolated_probability_summaries(files, cancel_event=cancelled, deadline=deadline)
        assert "PRIVATE_TOKEN" not in str(error.value)
        assert len(processes) == 1 and processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
    finally:
        completed.set()
        if canceller is not None:
            canceller.join(timeout=2.0)


def test_cancelled_before_spawn_or_while_waiting_store_lock_never_publishes(tmp_path, monkeypatch):
    _source_archive(tmp_path / "source")
    store = _store(tmp_path)
    event = Event()
    event.set()
    processes = _capture_processes(monkeypatch)
    store.mark_preload_pending()
    with pytest.raises(worker.ProbabilitySourcePreloadCancelled):
        store.preload_isolated(cancel_event=event)
    assert not processes and store._snapshot is None and not store.refresh_pending()
    event.clear()
    store._refresh_lock.acquire()
    cancelled = Thread(target=lambda: (event.wait(0.05), event.set()))
    cancelled.start()
    try:
        with pytest.raises(worker.ProbabilitySourcePreloadCancelled):
            store.preload_isolated(cancel_event=event)
    finally:
        store._refresh_lock.release()
        cancelled.join(timeout=1.0)
    assert not processes and store._snapshot is None


def test_once_enabled_ordinary_preload_and_projection_incremental_refresh_stay_isolated(tmp_path, monkeypatch):
    _source_archive(tmp_path / "source")
    store = _store(tmp_path)
    processes = _capture_processes(monkeypatch)
    store.preload_isolated()
    assert len(processes) == 1

    def forbidden(*_args, **_kwargs):
        raise AssertionError("server process must not deeply load source/outcome/fit")

    for name in ("load_probability_source_snapshot", "load_probability_outcome_artifact", "load_probability_fit_assessment"):
        monkeypatch.setattr(research, name, forbidden)
    store.preload()
    store.research_projection(71)
    assert len(processes) == 1
    _source_archive(tmp_path / "source", 72)
    store.preload()
    assert len(processes) == 2 and len(store._summary_by_fingerprint) == 2
    _source_archive(tmp_path / "source", 73)
    assert store.research_projection(73)["availability"] == "source_index_verification_pending"
    assert len(processes) == 2
    store.preload()
    assert len(processes) == 3 and len(store._summary_by_fingerprint) == 3


def test_worker_checks_fingerprints_before_and_after_verification(tmp_path, monkeypatch):
    path = _source_archive(tmp_path)
    fingerprint = research._file_fingerprint(path)
    summary = research._fingerprint_summary(fingerprint, {})

    def replace(_fingerprint, _cache):
        path.write_bytes(path.read_bytes())
        return summary

    monkeypatch.setattr(research, "_fingerprint_summary", replace)
    with pytest.raises(sources.ProbabilitySourceError, match="指纹已变化"):
        child._verified_summary("source", fingerprint)
    with pytest.raises(sources.ProbabilitySourceError, match="指纹已变化"):
        child._verified_summary("source", fingerprint)


def test_worker_only_excludes_mechanically_verified_bound_legacy_semantic_drift(tmp_path, monkeypatch):
    files = _archive_files(tmp_path)
    item = files[1]
    run_id, digest, as_of = worker._file_identity(*item)

    def drift(*_args):
        raise outcomes.ProbabilityOutcomeSemanticDriftError(
            "legacy", run_id=run_id, integrity_digest=digest, as_of_date=as_of,
            source_digest="a" * 64, generated_at="2026-08-13T18:00:00+08:00",
        )

    monkeypatch.setattr(research, "_fingerprint_outcome", drift)
    assert child._verified_summary(*item)["rejection_status"] == "legacy_semantic_drift_excluded"

    def unbound(*_args):
        raise outcomes.ProbabilityOutcomeSemanticDriftError("unbound", run_id=run_id)

    monkeypatch.setattr(research, "_fingerprint_outcome", unbound)
    with pytest.raises(sources.ProbabilitySourceError, match="内容绑定"):
        child._verified_summary(*item)


def _in_process_summaries(files, **_kwargs):
    request = _request(files)
    return worker._decode_worker_response(child._worker_response(request), request, files)


def test_isolated_retry_reuses_verified_fingerprints_without_publishing_mixed_snapshot(tmp_path, monkeypatch):
    _source_archive(tmp_path / "source")
    store = _store(tmp_path)
    batches = []

    def refresh(files, **kwargs):
        batches.append(list(files))
        summaries = _in_process_summaries(files, **kwargs)
        if len(batches) == 1:
            _source_archive(tmp_path / "source", 72)
            assert store._snapshot is None and not store._summary_by_fingerprint
        return summaries

    monkeypatch.setattr(worker, "isolated_probability_summaries", refresh)
    assert store.preload_isolated() == 1
    assert [worker._file_identity(*batch[0])[0] for batch in batches] == [71, 72]
    assert len(store._summary_by_fingerprint) == 2
    assert store._snapshot == store._observed_snapshot()


def test_continually_changed_isolated_directory_never_publishes_partial_index(tmp_path, monkeypatch):
    _source_archive(tmp_path / "source")
    store = _store(tmp_path)
    count = 0

    def refresh(files, **kwargs):
        nonlocal count
        summaries = _in_process_summaries(files, **kwargs)
        count += 1
        _source_archive(tmp_path / "source", 71 + count)
        return summaries

    monkeypatch.setattr(worker, "isolated_probability_summaries", refresh)
    with pytest.raises(sources.ProbabilitySourceError, match="多次读取期间持续变化"):
        store.preload_isolated()
    assert count == research._STABLE_SNAPSHOT_READ_ATTEMPTS
    assert store._snapshot is None and store._research_by_run == {} and store._summary_by_fingerprint == {}
    assert not store.refresh_pending()


@pytest.mark.parametrize("when", ["response", "commit"])
@pytest.mark.parametrize("change", ["cancel", "bindings"])
def test_cancel_or_binding_drift_before_publication_preserves_previous_complete_index(tmp_path, monkeypatch, when, change):
    old = _source_archive(tmp_path / "source")
    old_digest = worker._file_identity("source", research._file_fingerprint(old))[1]
    store = _store(tmp_path)
    store.preload(archive_bindings={71: old_digest})
    previous = (store._snapshot, deepcopy(store._research_by_run), dict(store._summary_by_fingerprint))
    newer = _source_archive(tmp_path / "source", 72)
    new_digest = worker._file_identity("source", research._file_fingerprint(newer))[1]
    event = Event()
    changed_bindings = {73: "c" * 64}

    def mutate():
        if change == "cancel":
            event.set()
        else:
            with store._lock:
                store._archive_bindings = changed_bindings

    def refresh(files, **kwargs):
        result = _in_process_summaries(files, **kwargs)
        if when == "response":
            mutate()
        return result

    make_index = research._research_index

    def index(*args, **kwargs):
        result = make_index(*args, **kwargs)
        if when == "commit":
            mutate()
        return result

    monkeypatch.setattr(worker, "isolated_probability_summaries", refresh)
    monkeypatch.setattr(research, "_research_index", index)
    expected = worker.ProbabilitySourcePreloadCancelled if change == "cancel" else sources.ProbabilitySourceError
    with pytest.raises(expected):
        store.preload_isolated(archive_bindings={71: old_digest, 72: new_digest}, cancel_event=event)
    assert (store._snapshot, store._research_by_run, store._summary_by_fingerprint) == previous
    assert store._archive_bindings == ({71: old_digest} if change == "cancel" else changed_bindings)
    assert not store.refresh_pending()


def test_isolated_failure_preserves_cache_but_never_silently_accepts_new_invalid_artifact(tmp_path, monkeypatch):
    _source_archive(tmp_path / "source")
    store = _store(tmp_path)
    store.preload()
    previous = store._snapshot, deepcopy(store._research_by_run), dict(store._summary_by_fingerprint)
    (tmp_path / "source" / f"market-scan-probability-source-run-72-{'c' * 64}.json.gz").write_bytes(b"invalid")
    monkeypatch.setattr(worker, "isolated_probability_summaries", _in_process_summaries)
    with pytest.raises(sources.ProbabilitySourceError):
        store.preload_isolated()
    assert (store._snapshot, store._research_by_run, store._summary_by_fingerprint) == previous
    assert store.research_projection(71)["availability"] == "source_index_verification_pending"


def test_cancellation_after_atomic_commit_does_not_roll_back_only_archive_bindings(tmp_path, monkeypatch):
    source = _source_archive(tmp_path / "source")
    digest = worker._file_identity("source", research._file_fingerprint(source))[1]
    store = _store(tmp_path)
    event = Event()
    commit = store._commit_refresh

    def cancel_after_commit(*args, **kwargs):
        commit(*args, **kwargs)
        event.set()

    monkeypatch.setattr(worker, "isolated_probability_summaries", _in_process_summaries)
    monkeypatch.setattr(store, "_commit_refresh", cancel_after_commit)
    # The commit won the race. Cancellation must not restore only its old
    # bindings while leaving the new index committed under a different token.
    assert store.preload_isolated(archive_bindings={71: digest}, cancel_event=event) == 1
    assert store._archive_bindings == {71: digest}
    assert store._snapshot == store._observed_snapshot()
    assert store.verified_archive_digests() == {71: digest}


def test_isolated_selection_verifies_only_exact_bound_sources_and_latest_eligible_outcomes(tmp_path, monkeypatch):
    files = _archive_files(tmp_path)
    digest = worker._file_identity(*files[0])[1]
    (tmp_path / "source" / f"market-scan-probability-source-run-71-{'a' * 64}.json.gz").write_bytes(b"unbound replacement")
    (tmp_path / "source" / f"market-scan-probability-source-run-72-{'a' * 64}.json.gz").write_bytes(b"orphan")
    (tmp_path / "outcomes" / f"market-scan-probability-outcomes-run-72-through-2026-08-12-{'a' * 64}.json.gz").write_bytes(b"orphan")
    (tmp_path / "outcomes" / f"market-scan-probability-outcomes-run-71-through-2026-08-11-{'a' * 64}.json.gz").write_bytes(b"superseded")
    (tmp_path / "fits" / f"market-scan-probability-fit-through-run-72-{'a' * 64}.json.gz").write_bytes(b"orphan")
    calls = []

    def refresh(files, **kwargs):
        calls.extend(files)
        return _in_process_summaries(files, **kwargs)

    monkeypatch.setattr(worker, "isolated_probability_summaries", refresh)
    store = _store(tmp_path)
    assert store.preload_isolated(archive_bindings={71: digest}) == 1
    assert calls == files and store.verified_archive_digests() == {71: digest}


def test_process_cleanup_tolerates_exit_between_poll_and_terminate():
    class ExitedProcess:
        stdin = None
        stdout = None
        waits = 0

        def poll(self):
            return None

        def terminate(self):
            raise ProcessLookupError

        def wait(self, *, timeout):
            self.waits += 1
            return 0

    process = ExitedProcess()
    worker._stop_and_reap(process)
    assert process.waits == 1


def test_process_cleanup_kills_a_worker_that_does_not_exit_on_terminate():
    class StuckProcess:
        stdin = None
        stdout = None
        calls = []

        def poll(self):
            return None

        def terminate(self):
            self.calls.append("terminate")

        def wait(self, *, timeout):
            self.calls.append(("wait", timeout))
            if timeout == 1.0:
                raise subprocess.TimeoutExpired("worker", timeout)
            return -9

        def kill(self):
            self.calls.append("kill")

    process = StuckProcess()
    worker._stop_and_reap(process)
    assert process.calls == ["terminate", ("wait", 1.0), "kill", ("wait", 2.0)]


def test_spawn_failure_is_sanitized_and_expired_empty_batches_fail_closed(tmp_path, monkeypatch):
    path = _source_archive(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise OSError("PRIVATE_CONFIG")

    monkeypatch.setattr(worker.subprocess, "Popen", unavailable)
    with pytest.raises(sources.ProbabilitySourceError, match="进程不可用") as error:
        worker.isolated_probability_summaries([("source", research._file_fingerprint(path))])
    assert "PRIVATE_CONFIG" not in str(error.value)
    with pytest.raises(sources.ProbabilitySourceError, match="预热超时"):
        worker.isolated_probability_summaries([], deadline=0.0)


@pytest.mark.parametrize("kind", ["source", "isolated_source", "historical"])
@pytest.mark.parametrize("fail", [False, True])
def test_real_store_batch_lease_survives_own_preload_finally_on_success_and_error(tmp_path, monkeypatch, kind, fail):
    directory = tmp_path / "archive"
    if fail:
        directory.write_bytes(b"not a directory")
    else:
        directory.mkdir()
    store = historical.MarketScanHistoricalProbabilityContextStore(directory) if kind == "historical" else research.MarketScanProbabilitySourceResearchStore(directory)
    store.mark_preload_pending()
    store.acquire_preload_lease()
    preload = store.preload_isolated if kind == "isolated_source" else store.preload
    if fail:
        with pytest.raises(ValueError):
            preload()
    else:
        assert preload() == 0
    assert not store._preload_pending and store.refresh_pending()
    store.acquire_preload_lease()
    store.clear_preload_pending()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("interactive read stole the coordinating batch's lease")

    monkeypatch.setattr(store, "_refresh_if_changed", forbidden)
    assert (store.research_projection() if kind == "historical" else store.research_projection(71))["status"] == "not_generated"
    store.release_preload_lease()
    assert store.refresh_pending()
    store.release_preload_lease()
    assert not store.refresh_pending()
    with pytest.raises(RuntimeError, match="lease 未持有"):
        store.release_preload_lease()
    assert not store.refresh_pending()


def test_child_caps_summary_accumulation_before_reading_the_next_file(tmp_path, monkeypatch):
    first = _source_archive(tmp_path, 71)
    second = _source_archive(tmp_path, 72)
    files = [("source", research._file_fingerprint(path)) for path in (first, second)]
    called = []
    original = child._verified_summary

    def read(kind, fingerprint):
        called.append(fingerprint)
        return original(kind, fingerprint)

    monkeypatch.setattr(child, "_verified_summary", read)
    monkeypatch.setattr(child, "WORKER_MAX_OUTPUT_BYTES", 128)
    with pytest.raises(sources.ProbabilitySourceError, match="响应过大"):
        child._worker_response(_request(files))
    assert called == [files[0][1]]


def test_child_audit_denies_provider_network_sqlite_and_file_writes(tmp_path):
    script = "\n".join([
        "import os, socket, sqlite3, sys",
        "from app.services.market_scan_probability_preload_worker import _read_only_audit",
        "sys.addaudithook(_read_only_audit)",
        "actions = [lambda: open(sys.argv[1], 'w'), lambda: sqlite3.connect(sys.argv[1]), lambda: socket.socket()]",
        "for action in actions:",
        "    try: action()",
        "    except PermissionError: pass",
        "    else: raise AssertionError('write/network permitted')",
        "print('readonly')",
    ])
    target = tmp_path / "forbidden.sqlite"
    result = subprocess.run([sys.executable, "-B", "-c", script, str(target)], capture_output=True, timeout=10, check=True)
    assert result.stdout.strip() == b"readonly" and not target.exists()


def _replay_rejected_archive(tmp_path, monkeypatch):
    import gzip
    from app.services.market_scan_probability import stable_probability_hash
    from app.services.market_scan_probability_labels import probability_label_contract
    from tests import test_market_scan_probability_outcomes as support

    source_path = _source_archive(tmp_path / "source")
    source = sources.load_probability_source_snapshot(source_path)
    bars = [*support._complete_h1_rows(), support._bar("2026-08-19", 10.0, 10.1)]
    with monkeypatch.context() as patch:
        # A frozen v2 feature source is a reader fixture, not current training input.
        patch.setattr(outcomes, "_source_artifact", lambda _value: source)
        artifact = outcomes.build_probability_outcome_artifact(
            source, {"000001.SZ": bars}, generated_at="2026-08-19T18:00:00+08:00", as_of_date="2026-08-19",
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
    h5["model_limited"] = h5["daily_bar_model_limited"] = True
    digest = outcomes.probability_outcome_payload_digest(payload)
    artifact["integrity"]["integrity_digest"] = digest
    directory = tmp_path / "outcomes"
    directory.mkdir(exist_ok=True)
    path = directory / f"market-scan-probability-outcomes-run-71-through-2026-08-19-{digest}.json.gz"
    path.write_bytes(gzip.compress(canonical_json_bytes(artifact), compresslevel=9, mtime=0))
    return source_path, path


def test_real_isolated_projection_quarantines_bound_replay_and_caches_unchanged_rejection(tmp_path, monkeypatch):
    source_path, rejected_path = _replay_rejected_archive(tmp_path, monkeypatch)
    files = [(kind, research._file_fingerprint(path)) for kind, path in (("source", source_path), ("outcome", rejected_path))]
    before = {path: path.read_bytes() for path in (source_path, rejected_path)}
    summary = worker.isolated_probability_summaries([files[1]])[files[1]]
    assert summary["rejection_status"] == "replay_rejected"
    assert "旧 model_limited" in summary["rejection_reason"]
    calls = []
    real_worker = worker.isolated_probability_summaries

    def collect(batch, **kwargs):
        calls.extend(batch)
        return real_worker(batch, **kwargs)

    monkeypatch.setattr(worker, "isolated_probability_summaries", collect)
    # A fit in the contaminated cohort is neither replayed nor exposed.
    (tmp_path / "fits").mkdir()
    (tmp_path / "fits" / f"market-scan-probability-fit-through-run-71-{'c' * 64}.json.gz").write_bytes(b"untrusted fit")
    store = _store(tmp_path)
    assert store.preload_isolated() == 1
    projection = store.research_projection(71)
    assert projection["outcome_evidence_status"] == "replay_rejected"
    assert projection["fit_evidence_status"] == "cohort_quarantined"
    assert projection["outcome_progress"]["outcome_artifact_count"] == 0
    assert projection["fit_selection_qualified"] is False
    assert "run 71 replay_rejected" in store.quarantine_diagnostics()[0]
    # A directory identity change must not trigger decompression of an intact rejection.
    (tmp_path / "outcomes" / "unrelated.txt").write_text("directory changed")
    assert store.preload_isolated() == 1 and calls == files
    assert {path: path.read_bytes() for path in before} == before


def test_rejected_projection_cannot_recover_by_deletion_or_older_valid_outcome(tmp_path, monkeypatch):
    files = _archive_files(tmp_path)
    _source, rejected = _replay_rejected_archive(tmp_path, monkeypatch)
    store = _store(tmp_path)
    assert store.preload_isolated() == 1
    rejected.unlink()
    assert store.preload_isolated() == 1
    projection = store.research_projection(71)
    assert projection["fit_evidence_status"] == "cohort_quarantined"
    assert projection["outcome_progress"]["outcome_artifact_count"] == 0
    assert projection["outcome_rejection"]["as_of_date"] == "2026-08-19"
    assert files[1][1][0].exists()  # The earlier valid result never replaces the rejection.


def test_isolated_verified_chunks_resume_after_failure_without_publishing_partial_index(tmp_path, monkeypatch):
    for run_id in range(71, 77):
        _source_archive(tmp_path / "source", run_id)
    store = _store(tmp_path)
    batches = []

    def verify(batch, **kwargs):
        batches.append(list(batch))
        if len(batches) == 2:
            raise sources.ProbabilitySourceError("bounded timeout")
        return _in_process_summaries(batch, **kwargs)

    monkeypatch.setattr(worker, "isolated_probability_summaries", verify)
    with pytest.raises(sources.ProbabilitySourceError, match="bounded timeout"):
        store.preload_isolated()
    assert store._snapshot is None and store._summary_by_fingerprint == {}
    assert store._research_by_run == {} and len(store._verification_caches[0]) == 4
    assert store.preload_isolated() == 1
    assert [len(batch) for batch in batches] == [4, 2, 2]
    assert batches[1] == batches[2] and len(store._summary_by_fingerprint) == 6


@pytest.mark.parametrize("field,value", [
    ("run_id", True), ("integrity_digest", "0" * 64), ("as_of_date", "2026-08-20"),
    ("source_integrity_digest", "missing"), ("generated_at", "2026-08-19T18:00:00"),
    ("rejection_status", "accepted"), ("rejection_status", []), ("rejection_status", {}),
    ("rejection_reason", "x" * 501),
])
def test_worker_rejection_protocol_remains_bound_and_bounded(tmp_path, monkeypatch, field, value):
    _source, rejected = _replay_rejected_archive(tmp_path, monkeypatch)
    files = [("outcome", research._file_fingerprint(rejected))]
    request = _request(files)
    response = json.loads(child._worker_response(request))
    response["results"][0]["summary"][field] = value
    with pytest.raises(sources.ProbabilitySourceError, match="拒绝摘要"):
        worker._decode_worker_response(canonical_json_bytes(response), request, files)


def test_only_strictly_newer_verified_same_source_outcome_can_release_rejection(tmp_path, monkeypatch):
    source_path, _rejected = _replay_rejected_archive(tmp_path, monkeypatch)
    store = _store(tmp_path)
    assert store.preload_isolated() == 1 and store.quarantine_diagnostics()
    source = sources.load_probability_source_snapshot(source_path)
    with monkeypatch.context() as patch:
        patch.setattr(outcomes, "_source_artifact", lambda _value: source)
        replacement = outcomes.build_probability_outcome_artifact(
            source, {}, generated_at="2026-08-20T18:00:00+08:00", as_of_date="2026-08-20",
        )
    path = Path(outcomes.publish_built_probability_outcome_artifact(tmp_path / "outcomes", replacement)["path"])
    assert store.preload_isolated() == 1
    assert store.quarantine_diagnostics() == ()
    assert store.research_projection(71)["fit_evidence_status"] == "current_replay_only"
    path.unlink()
    assert store.preload_isolated() == 1 and store.quarantine_diagnostics()
    assert store.research_projection(71)["fit_evidence_status"] == "cohort_quarantined"


def test_staged_rejection_masks_old_index_until_full_snapshot_commits(tmp_path, monkeypatch):
    _archive_files(tmp_path)
    store = _store(tmp_path)
    assert store.preload_isolated() == 1
    _source, rejected = _replay_rejected_archive(tmp_path, monkeypatch)
    fingerprint = research._file_fingerprint(rejected)
    store._verification_caches[1][fingerprint] = child._verified_summary("outcome", fingerprint)
    store.acquire_preload_lease()
    try:
        masked = store.research_projection(71)
        assert masked["status"] == "not_generated"
        assert masked["availability"] == "source_index_verification_pending"
        assert masked.get("run_binding") is None
    finally:
        store.release_preload_lease()
    assert store.research_projection(71)["availability"] == "source_index_verification_pending"
    assert store.preload_isolated() == 1
    assert store.research_projection(71)["fit_evidence_status"] == "cohort_quarantined"


def test_staged_new_valid_result_cannot_release_old_index_before_atomic_commit(tmp_path, monkeypatch):
    _archive_files(tmp_path)
    store = _store(tmp_path)
    assert store.preload_isolated() == 1
    source_path, rejected = _replay_rejected_archive(tmp_path, monkeypatch)
    rejected_fingerprint = research._file_fingerprint(rejected)
    store._verification_caches[1][rejected_fingerprint] = child._verified_summary("outcome", rejected_fingerprint)
    source = sources.load_probability_source_snapshot(source_path)
    with monkeypatch.context() as patch:
        patch.setattr(outcomes, "_source_artifact", lambda _value: source)
        replacement = outcomes.build_probability_outcome_artifact(
            source, {}, generated_at="2026-08-20T18:00:00+08:00", as_of_date="2026-08-20",
        )
    path = Path(outcomes.publish_built_probability_outcome_artifact(tmp_path / "outcomes", replacement)["path"])
    fingerprint = research._file_fingerprint(path)
    store._verification_caches[1][fingerprint] = child._verified_summary("outcome", fingerprint)
    previous = store._snapshot
    assert not store.refresh_pending()
    assert store.research_projection(71)["availability"] == "source_index_verification_pending"
    assert store._snapshot == previous
