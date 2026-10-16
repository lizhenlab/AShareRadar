from __future__ import annotations

import base64
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, sha256_hex
from app.models.fuyao_dumps import FuyaoDumpManifest
from app.services import strategy_prospective_collection as collection
from app.services import workspace_backup_domains as domains
from app.services.fuyao_dumps_storage import manifest_version
from tests import test_strategy_prospective_collection as collection_tests


study = collection_tests.study


def root(path, category, *, source=None, name="archive"):
    return {"name": name, "category": category, "source_path": str(source or Path("/missing-original") / path.name),
            "archive_path": str(path)}


def seal(payload):
    content = {key: value for key, value in payload.items() if key != "digest"}
    return {**content, "digest": sha256_hex(canonical_json_bytes(content))}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def prospective(tmp_path, *, captured=False):
    directory = tmp_path / "archive-copy"
    plan = seal({"schema_version": "strategy-template-prospective-plan-v1", "plan_id": "old-study",
                 "recorded_at": "2026-07-16T16:00:00+08:00", "specification": {"signal_dates": ["2026-07-17"],
                 "historical_inference_contract": "intentionally-not-current"},
                 "calendar_sha256": sha256_hex(b"legacy-calendar"), "calendar_base64": base64.b64encode(b"legacy-calendar").decode()})
    write_json(directory / "plan.json", plan)
    payload = {}
    if captured:
        raw = canonical_json_bytes({"kind": "header"}) + b"\n" + canonical_json_bytes({"kind": "result"}) + b"\n"
        compressed = gzip.compress(raw, mtime=0)
        digest = sha256_hex(compressed)
        name = f"snapshot-2026-07-17-{digest}.json.gz"
        (directory / name).write_bytes(compressed)
        payload = {"snapshot_path": name, "snapshot_file_sha256": digest, "snapshot_byte_size": len(compressed),
                   "snapshot_raw_bytes_sha256": sha256_hex(raw), "snapshot_raw_byte_size": len(raw), "snapshot_result_count": 1}
    receipt = seal({"schema_version": "strategy-template-prospective-receipt-v1", "plan_digest": plan["digest"],
                    "previous_digest": plan["digest"], "sequence": 1, "trade_date": "2026-07-17",
                    "recorded_at": "2026-07-17T17:00:00+08:00", "status": "captured" if captured else "missing", "payload": payload})
    write_json(directory / "receipt-2026-07-17.json", receipt)
    return root(directory, "prospective", source=Path("/missing-original/old-study")), plan, receipt


def runtime(tmp_path):
    directory = tmp_path / "runtime-copy"
    files = {"code/app/example.py": b"pass\n", "venv/bin/python": b"this must never execute",
             "venv/lib/pkg/runtime.json": b"dependency resource, not a sealed receipt", "launch.py": b"raise RuntimeError('never execute')\n"}
    for name, data in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {"schema_version": "strategy-prospective-runtime-v1", "base_python": {"probe": {"stdlib": "/missing-python"}},
                "launcher_sha256": sha256_hex(files["launch.py"]),
                "code_files": {name.removeprefix("code/"): sha256_hex(data) for name, data in files.items() if name.startswith("code/")},
                "environment_files": {name.removeprefix("venv/"): sha256_hex(data) for name, data in files.items() if name.startswith("venv/")}}
    runtime_seal(directory, manifest)
    return root(directory, "runtime"), manifest


def runtime_seal(directory, manifest):
    manifest.pop("digest", None)
    manifest["digest"] = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
    write_json(directory / "runtime.json", manifest)


def ranking(tmp_path):
    directory = tmp_path / "publications"
    directory.mkdir()
    payload = {"run_id": 7, "score_rule_version": "historical-v6", "score_spec_hash": "a" * 64, "base_snapshot_digest": "b" * 64,
               "current_prediction_artifact_digest": "c" * 64, "promotion_digest": "d" * 64,
               "generated_at": "2026-07-17T17:00:00+08:00", "records": [{"symbol": "600000"}]}
    digest = sha256_hex(canonical_json_bytes({"generated_at": payload["generated_at"], "payload": payload}))
    envelope = {"schema_version": domains._RANKING_SCHEMA, "generated_at": payload["generated_at"], "payload": payload,
                "integrity": {"algorithm": "sha256", "scope": "generated_at+payload", "notice": "content_address_only_strict_replay_required", "integrity_digest": digest}}
    artifact = directory / "publication.json.gz"
    artifact.write_bytes(gzip.compress(canonical_json_bytes(envelope)))
    database = tmp_path / "snapshot.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE market_scan_probability_ranking_publication(run_id INTEGER, score_spec_hash TEXT, base_snapshot_digest TEXT, prediction_artifact_digest TEXT, promotion_digest TEXT, artifact_digest TEXT, artifact_path TEXT, record_count INTEGER, generated_at TEXT, schema_version TEXT, score_rule_version TEXT)")
        connection.execute("INSERT INTO market_scan_probability_ranking_publication VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            7, "a" * 64, "b" * 64, "c" * 64, "d" * 64, digest, "/missing-original/publications/publication.json.gz", 1, payload["generated_at"], domains._RANKING_SCHEMA, "historical-v6"))
        connection.commit()
    return root(directory, "research"), {"source_path": "/missing-original/main.sqlite", "archive_path": str(database), "role": "primary"}, envelope


def update_db(database, column, value):
    with closing(sqlite3.connect(database["archive_path"])) as connection:
        connection.execute(f"UPDATE market_scan_probability_ranking_publication SET {column}=?", (value,))
        connection.commit()


def fuyao(tmp_path):
    directory = tmp_path / "history"
    files = {name: name.encode() for name in ("daily.parquet", "actions.parquet", "source-daily.parquet", "source-actions.parquet")}
    manifest = FuyaoDumpManifest(version="0" * 64, mode="full", observed_at="2026-07-17T17:00:00+08:00",
        first_date="2026-07-17", last_date="2026-07-17", symbols=1, trading_dates=["2026-07-17"], calendar_source="fixture",
        files=[{"name": name, "sha256": sha256_hex(files[name]), "size_bytes": len(files[name]), "rows": 1}
               for name in ("daily.parquet", "actions.parquet")],
        source_sha256={name: sha256_hex(data) for name, data in files.items() if name.startswith("source-")}).model_dump(mode="json")
    manifest["version"] = manifest_version(manifest)
    version = directory / "versions" / manifest["version"]
    write_json(version / "manifest.json", manifest)
    for name, data in files.items():
        (version / name).write_bytes(data)
    write_json(directory / "current.json", {"version": manifest["version"]})
    return root(directory, "fuyao"), version


def test_real_collected_inputs_remain_verifiable_after_original_directory_is_gone(study, tmp_path, monkeypatch):
    collection.collect_strategy_prospective_inputs(study[0][0], study[1], "study")
    archived = tmp_path / "moved-root"
    shutil.copytree(study[1] / "study", archived)
    source = study[1] / "study"
    shutil.rmtree(study[1])
    monkeypatch.setattr("app.services.strategy_prospective_plan.read_strategy_prospective_plan", lambda *a, **kw: pytest.fail("business contract replayed"))
    result = domains.validate_archive_domains([root(archived, "prospective", source=source)], [])
    assert result["roots"][0]["receipt_count"] == 1
    assert result["resume_eligible"] is False
    assert result["roots"][0]["business_contract_replayed"] is False


def test_legacy_plan_contract_and_file_root_keep_original_lexical_identity(tmp_path):
    declared, _plan, _receipt = prospective(tmp_path)
    report = domains.validate_archive_domains([declared], [])
    assert report["roots"][0]["verification"] == "prospective_mechanical_chain"
    declared["archive_path"] += "/plan.json"
    declared["source_path"] += "/plan.json"
    assert domains.validate_archive_domains([declared], [])["roots"][0]["plan_count"] == 1


@pytest.mark.parametrize("field,value,error", [
    ("plan_digest", "f" * 64, "hash chain"), ("previous_digest", "f" * 64, "hash chain"),
    ("sequence", True, "sequence"), ("sequence", 2, "sequence"), ("trade_date", "2026-07-18", "sequence"),
    ("recorded_at", "2026-07-15T17:00:00+08:00", "clock"), ("status", "unknown", "status"),
    ("status", "captured", "lacks"), ("schema_version", "unknown", "schema"),
])
def test_prospective_chain_rejects_mechanical_corruption(tmp_path, field, value, error):
    declared, _plan, receipt = prospective(tmp_path)
    receipt[field] = value
    write_json(Path(declared["archive_path"]) / "receipt-2026-07-17.json", seal(receipt))
    with pytest.raises(ValueError, match=error):
        domains.validate_archive_domains([declared], [])


@pytest.mark.parametrize("mutation,error", [("digest", "digest"), ("directory", "directory"), ("calendar", "calendar"), ("signals", "schedule"), ("gap", "gaps"), ("orphan", "orphaned")])
def test_prospective_plan_and_schedule_bindings(tmp_path, mutation, error):
    declared, plan, _receipt = prospective(tmp_path)
    directory = Path(declared["archive_path"])
    if mutation == "gap":
        (directory / "receipt-2026-07-17.json").rename(directory / "receipt-2026-07-18.json")
    elif mutation == "orphan":
        (directory / "plan.json").unlink()
    else:
        if mutation == "directory":
            declared["source_path"] = "/original/wrong-name"
        elif mutation == "calendar":
            plan["calendar_sha256"] = "f" * 64
        elif mutation == "signals":
            plan["specification"]["signal_dates"] = "bad"
        plan = seal(plan)
        if mutation == "digest":
            plan["digest"] = "f" * 64
        write_json(directory / "plan.json", plan)
    with pytest.raises(ValueError, match=error):
        domains.validate_archive_domains([declared], [])


@pytest.mark.parametrize("field,value,error", [("snapshot_path", "../outside.gz", "path"),
    ("snapshot_file_sha256", "f" * 64, "compressed"), ("snapshot_byte_size", 0, "compressed"),
    ("snapshot_raw_bytes_sha256", "f" * 64, "raw"), ("snapshot_raw_byte_size", 0, "raw"),
    ("snapshot_result_count", 2, "row count"), ("snapshot_result_count", True, "row count")])
def test_snapshot_bindings_are_checked(tmp_path, field, value, error):
    declared, _plan, receipt = prospective(tmp_path, captured=True)
    receipt["payload"][field] = value
    write_json(Path(declared["archive_path"]) / "receipt-2026-07-17.json", seal(receipt))
    with pytest.raises(ValueError, match=error):
        domains.validate_archive_domains([declared], [])


def test_runtime_is_hashed_without_any_python_probe_and_nested_resource_is_not_a_receipt(tmp_path, monkeypatch):
    declared, _manifest = runtime(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("archive executed a process"))
    result = domains.validate_archive_domains([declared], [])
    assert result["roots"][0]["runtime_count"] == 1
    assert result["runtime_execution_verified"] is False
    assert result["external_dependencies"] == [{"root": "archive", "kind": "shared_base_python", "status": "not_verified"}]


@pytest.mark.parametrize("mutation,error", [("missing", "omitted"), ("extra", "unexpected"), ("changed", "digest"),
    ("receipt", "receipt digest"), ("traversal", "escapes"), ("symlink", "alias"), ("secret-excluded", "omitted")])
def test_runtime_exact_manifest_and_safe_paths(tmp_path, mutation, error):
    declared, manifest = runtime(tmp_path)
    directory = Path(declared["archive_path"])
    if mutation == "missing":
        (directory / "venv/bin/python").unlink()
    elif mutation == "extra":
        (directory / "extra.py").write_text("extra")
    elif mutation == "changed":
        (directory / "venv/bin/python").write_text("changed")
    elif mutation == "receipt":
        manifest["digest"] = "f" * 64
        write_json(directory / "runtime.json", manifest)
    elif mutation == "symlink":
        (directory / "extra.py").symlink_to(directory / "launch.py")
    else:
        manifest["code_files"]["../outside.py" if mutation == "traversal" else "app/cert.pem"] = "f" * 64
        runtime_seal(directory, manifest)
    with pytest.raises(ValueError, match=error):
        domains.validate_archive_domains([declared], [])


def test_primary_ranking_reference_maps_into_archive_and_never_opens_original(tmp_path, monkeypatch):
    declared, database, _envelope = ranking(tmp_path)
    original_stat = Path.stat
    def guarded(path, *args, **kwargs):
        assert not str(path).startswith("/missing-original"), "original source path inspected"
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", guarded)
    result = domains.validate_archive_domains([declared], [database])
    assert result["databases"][0]["known_reference_count"] == 1
    assert result["databases"][0]["relocation_status"] == "archive_requires_rebinding"


@pytest.mark.parametrize("field,value,error", [("artifact_path", "/outside.json.gz", "outside declared"),
    ("artifact_digest", "f" * 64, "digest"), ("run_id", 99, "identity"), ("record_count", 2, "record count"),
    ("schema_version", "changed-schema", "identity"), ("score_rule_version", "changed-score", "identity"),
    ("generated_at", "2026-07-18T17:00:00+08:00", "time")])
def test_primary_ranking_reference_corruption_is_rejected(tmp_path, field, value, error):
    declared, database, _envelope = ranking(tmp_path)
    update_db(database, field, value)
    with pytest.raises(ValueError, match=error):
        domains.validate_archive_domains([declared], [database])


def test_missing_referenced_publication_cannot_fall_back_to_source(tmp_path):
    declared, database, _envelope = ranking(tmp_path)
    Path(declared["archive_path"], "publication.json.gz").unlink()
    with pytest.raises(FileNotFoundError):
        domains.validate_archive_domains([declared], [database])


def test_primary_without_publications_and_auxiliary_report_honest_scope(tmp_path):
    path = tmp_path / "empty.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    declarations = [{"source_path": "/missing-main.sqlite", "archive_path": str(path), "role": role} for role in ("primary", "auxiliary")]
    report = domains.validate_archive_domains([], declarations)
    assert report["scope"] == "known_archive_domains_and_primary_ranking_references_only"
    assert [item["known_reference_count"] for item in report["databases"]] == [0, 0]
    assert report["databases"][1]["verification"] == "file_integrity_only"


def test_fuyao_verifies_only_current_archived_version_without_parsing_parquet(tmp_path):
    declared, _version = fuyao(tmp_path)
    result = domains.validate_archive_domains([declared], [])
    assert result["roots"][0]["verification"] == "fuyao_current_version_files"
    assert result["roots"][0]["current_version_count"] == 1


@pytest.mark.parametrize("mutation", ["missing", "digest", "pointer", "source-symlink"])
def test_fuyao_current_version_mechanical_corruption_fails(tmp_path, mutation):
    declared, version = fuyao(tmp_path)
    if mutation == "missing":
        (version / "actions.parquet").unlink()
    elif mutation == "digest":
        (version / "daily.parquet").write_bytes(b"wrong")
    elif mutation == "pointer":
        write_json(Path(declared["archive_path"]) / "current.json", {"version": "../escape"})
    else:
        (version / "source-daily.parquet").unlink()
        (version / "source-daily.parquet").symlink_to(version / "daily.parquet")
    with pytest.raises((ValueError, FileNotFoundError)):
        domains.validate_archive_domains([declared], [])


def test_control_attempts_are_untouched_and_unknown_research_has_no_domain_claim(tmp_path):
    attempt = tmp_path / "attempt.json"
    attempt.write_text("opaque original attempt\n")
    before = attempt.read_bytes(), attempt.stat().st_mtime_ns
    declarations = [root(attempt, "control", name="control"), root(tmp_path, "research", name="research")]
    result = domains.validate_archive_domains(declarations, [])
    assert result["roots"][0]["relocation_status"] == "archive_requires_rebinding"
    assert result["roots"][1]["verification"] == "file_integrity_only"
    assert before == (attempt.read_bytes(), attempt.stat().st_mtime_ns)


@pytest.mark.parametrize("limit", ["MAX_DOMAIN_FILES", "MAX_DOMAIN_ENTRIES", "MAX_DOMAIN_BYTES", "MAX_DOMAIN_DEPTH"])
def test_domain_budgets_fail_closed(tmp_path, monkeypatch, limit):
    declared, _manifest = runtime(tmp_path)
    monkeypatch.setattr(domains, limit, 0)
    with pytest.raises((ValueError, ArtifactIOError)):
        domains.validate_archive_domains([declared], [])


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_timeout_requires_finite_positive_value(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        domains.validate_archive_domains([root(tmp_path, "research")], [], timeout_seconds=timeout)


def test_expired_deadline_is_checked_even_for_unknown_domain(tmp_path, monkeypatch):
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(domains.time, "monotonic", lambda: next(clock))
    with pytest.raises(ValueError, match="deadline"):
        domains.validate_archive_domains([root(tmp_path, "research")], [], timeout_seconds=1)


def test_fuyao_helper_receives_deadline_control(tmp_path, monkeypatch):
    declared, _version = fuyao(tmp_path)
    clock = {"now": 0.0}
    monkeypatch.setattr(domains.time, "monotonic", lambda: clock["now"])
    def expired(root, *, verify_files, control):
        assert verify_files and root == Path(declared["archive_path"])
        clock["now"] = 2.0
        control.checkpoint()
    monkeypatch.setattr(domains, "read_dump_status", expired)
    with pytest.raises(ValueError, match="deadline"):
        domains.validate_archive_domains([declared], [], timeout_seconds=1)


@pytest.mark.parametrize("contents", [b'{"digest":1,"digest":2}', b'{"digest":NaN}', b'[]'])
def test_domain_json_rejects_duplicate_keys_nonfinite_and_nonobjects(tmp_path, contents):
    declared, _manifest = runtime(tmp_path)
    Path(declared["archive_path"], "runtime.json").write_bytes(contents)
    with pytest.raises((ArtifactIOError, ValueError)):
        domains.validate_archive_domains([declared], [])


def test_database_sqlite_query_installs_progress_deadline(tmp_path, monkeypatch):
    declared, database, _envelope = ranking(tmp_path)
    original = sqlite3.connect
    progress = []
    class Connection(sqlite3.Connection):
        def set_progress_handler(self, callback, steps):
            progress.append((callback(), steps))
            return super().set_progress_handler(callback, steps)
    monkeypatch.setattr(domains.sqlite3, "connect", lambda *args, **kwargs: original(*args, factory=Connection, **kwargs))
    domains.validate_archive_domains([declared], [database])
    assert progress == [(0, 1000)]


@pytest.mark.parametrize("limit", ["MAX_DOMAIN_SNAPSHOT_BYTES", "MAX_DOMAIN_SNAPSHOT_ROWS"])
def test_snapshot_decoding_limits_are_independent_of_valid_compressed_digest(tmp_path, monkeypatch, limit):
    declared, _plan, _receipt = prospective(tmp_path, captured=True)
    monkeypatch.setattr(domains, limit, 0)
    with pytest.raises(ValueError, match="bounds|row limit"):
        domains.validate_archive_domains([declared], [])


def test_ranking_decompression_has_independent_limit(tmp_path, monkeypatch):
    declared, database, envelope = ranking(tmp_path)
    envelope["payload"]["padding"] = "x" * 10_000
    Path(declared["archive_path"], "publication.json.gz").write_bytes(gzip.compress(canonical_json_bytes(envelope)))
    monkeypatch.setattr(domains, "MAX_DOMAIN_JSON_BYTES", 4096)
    with pytest.raises(ValueError, match="decompressed"):
        domains.validate_archive_domains([declared], [database])


@pytest.mark.parametrize("change", ["byte_change", "removed_pointer"])
def test_fuyao_detects_changes_during_helper_verification(tmp_path, monkeypatch, change):
    declared, version = fuyao(tmp_path)
    original = domains.read_dump_status
    def changed(path, **kwargs):
        value = original(path, **kwargs)
        if change == "byte_change":
            (version / "daily.parquet").write_bytes(b"changed-after-helper")
            return value
        return None
    monkeypatch.setattr(domains, "read_dump_status", changed)
    with pytest.raises(ValueError, match="changed|disappeared"):
        domains.validate_archive_domains([declared], [])


@pytest.mark.parametrize("invalid", ["empty", "too_many", "duplicate", "relative", "traversal", "role"])
def test_declarations_fail_closed(tmp_path, invalid):
    declarations = [root(tmp_path, "research")]
    databases = []
    if invalid == "empty":
        declarations = []
    elif invalid == "too_many":
        declarations *= 65
    elif invalid == "duplicate":
        declarations *= 2
    elif invalid in {"relative", "traversal"}:
        declarations[0]["source_path"] = "relative/path" if invalid == "relative" else "/original/../escape"
    else:
        databases = [{"source_path": "/original", "archive_path": str(tmp_path), "role": "unknown"}]
    with pytest.raises(ValueError):
        domains.validate_archive_domains(declarations, databases)
