from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools import strategy_prospective_runtime as runtime


@pytest.fixture(scope="module")
def prepared_runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("prospective-runtime")
    source = root / "source"
    source.mkdir()
    (source / "app/resources").mkdir(parents=True)
    (source / "tools").mkdir()
    (source / "app/__init__.py").write_text("")
    (source / "app/config_settings.py").write_text("_SHELL_ENV_VALUES = None\n")
    (source / "app/calculation.py").write_text("VALUE = 'frozen-source'\n")
    shutil.copy2(runtime.__file__, source / "tools/strategy_prospective_runtime.py")
    (source / "app/resources/trading_calendar.json").write_text('{"trade_dates": []}')
    (source / "app/resources/.env").write_text("DO_NOT_COPY=private-resource\n")
    (source / ".env").write_text("DO_NOT_COPY=private-project\n")
    for filename in runtime.LOCK_FILES:
        (source / filename).write_text("3.12\n" if filename == ".python-version" else "# fixed dependency declaration\n")
    (source / "requirements-lock.txt").write_text("independent-package==1.0\n")
    script = """import json, os, sys
from app import calculation, config_settings
import independent_package
from tools.strategy_prospective_runtime import inspect_running_runtime
inspect_running_runtime()
print(json.dumps({'source': calculation.VALUE, 'dependency': independent_package.VALUE,
 'dependency_file': independent_package.__file__, 'app_file': calculation.__file__,
 'prefix': sys.prefix, 'paths': sys.path, 'shell_fallback': config_settings._SHELL_ENV_VALUES,
 'secret_inherited': 'ASHARE_RADAR_LLM_API_KEY' in os.environ,
 'pythonpath_inherited': 'PYTHONPATH' in os.environ,
 'minimum': os.environ['ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE'],
 'runtime_directory': os.environ['ASHARE_RADAR_PROSPECTIVE_RUNTIME_DIR'],
 'runtime_digest': os.environ['ASHARE_RADAR_PROSPECTIVE_RUNTIME_DIGEST'],
 'argv': sys.argv[1:]}))
"""
    for entrypoint in runtime.ENTRYPOINTS:
        (source / "tools" / entrypoint).write_text(script)
    original_environment = root / "original-venv"
    subprocess.run([sys.executable, "-I", "-B", "-m", "venv", "--without-pip", str(original_environment)], check=True)
    site = runtime._package_directory(original_environment)
    (site / "independent_package.py").write_text("VALUE = 'frozen-dependency'\n")
    metadata = site / "independent_package-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: independent-package\nVersion: 1.0\n")
    (site / "hostile.pth").write_text("import sys; raise RuntimeError('site hooks must never execute')\n")
    (site / ".env").write_text("DO_NOT_COPY=private-package\n")
    destination = root / "sealed"
    manifest = runtime.prepare_runtime(source, destination, original_environment,
                                       configuration={"ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE": "65"})
    return root, source, original_environment, destination, manifest


@pytest.fixture
def sealed(prepared_runtime, tmp_path):
    root = tmp_path / "sealed-copy"
    shutil.copytree(prepared_runtime[3], root)
    return root


def _status_arguments(tmp_path):
    return ["--root", str(tmp_path / "plans"), "--plan-id", "future-study", "status"]


def test_runtime_is_actually_independent_of_source_packages_environment_and_site_hooks(prepared_runtime, sealed, tmp_path, monkeypatch):
    _root, source, environment, _sealed, manifest = prepared_runtime
    (source / "app/calculation.py").write_text("VALUE = 'changed-development-source'\n")
    (runtime._package_directory(environment) / "independent_package.py").write_text("VALUE = 'changed-development-dependency'\n")
    monkeypatch.setenv("PYTHONPATH", str(source))
    monkeypatch.setenv("ASHARE_RADAR_LLM_API_KEY", "do-not-inherit-this-value")
    monkeypatch.setenv("ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE", "1")
    result = runtime.run_runtime(sealed, _status_arguments(tmp_path), capture_output=True)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["source"] == "frozen-source" and output["dependency"] == "frozen-dependency"
    assert Path(output["app_file"]).is_relative_to(sealed / "code")
    assert Path(output["dependency_file"]).is_relative_to(sealed / "venv")
    assert output["prefix"] == str(sealed / "venv")
    assert not output["secret_inherited"] and not output["pythonpath_inherited"]
    assert output["shell_fallback"] == {} and output["minimum"] == "65"
    assert output["runtime_directory"] == str(sealed) and output["runtime_digest"] == manifest["digest"]
    assert not (sealed / "venv/bin/python").is_symlink()
    assert not any(path.is_symlink() for path in (sealed / "venv").rglob("*"))
    assert all(not Path(value).is_relative_to(source) for value in output["paths"])
    assert all(not Path(value).is_relative_to(environment) for value in output["paths"])
    assert not list(sealed.rglob(".env")) and not list(sealed.rglob("*.pyc"))
    assert manifest["installed_packages"] == [{"name": "independent-package", "version": "1.0"}]
    assert manifest["dependency_lock_validation"]["required_package_count"] == 1
    assert runtime.inspect_runtime(sealed)["digest"] == manifest["digest"]


@pytest.mark.parametrize("filename", [
    "code/app/calculation.py", "code/app/resources/trading_calendar.json", "code/requirements-lock.txt",
    "venv/lib/python3.12/site-packages/independent_package.py",
    "venv/lib/python3.12/site-packages/independent_package-1.0.dist-info/METADATA", "venv/pyvenv.cfg", "launch.py",
])
def test_changed_runtime_input_is_rejected_before_subprocess_launch(sealed, tmp_path, filename, monkeypatch):
    path = sealed / filename
    path.write_text(path.read_text() + "\n# altered\n")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: pytest.fail("changed runtime executed"))
    with pytest.raises(ValueError, match="drift"):
        runtime.run_runtime(sealed, _status_arguments(tmp_path), capture_output=True)


@pytest.mark.parametrize("mutation", ["add", "remove", "symlink"])
def test_package_file_set_and_symlinks_are_checked(sealed, mutation):
    site = runtime._package_directory(sealed / "venv")
    if mutation == "add":
        (site / "injected.py").write_text("raise RuntimeError('injected')\n")
    elif mutation == "remove":
        (site / "independent_package.py").unlink()
    else:
        (site / "injected.py").symlink_to(site / "independent_package.py")
    with pytest.raises(ValueError, match="drift|symbolic"):
        runtime.inspect_runtime(sealed)


def test_configuration_receipt_cannot_be_silently_changed(sealed):
    path = sealed / "runtime.json"
    manifest = json.loads(path.read_bytes())
    manifest["configuration"]["ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE"] = "1"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="digest mismatch"):
        runtime.inspect_runtime(sealed)


def test_watch_rechecks_runtime_and_original_launch_digest(sealed, monkeypatch):
    original = runtime.inspect_runtime(sealed)
    for key, value in runtime._environment(original["configuration"]).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(runtime.RUNTIME_DIRECTORY_ENV, str(sealed))
    monkeypatch.setenv(runtime.RUNTIME_DIGEST_ENV, original["digest"])
    assert runtime.inspect_running_runtime()["digest"] == original["digest"]
    monkeypatch.setenv("ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE", "2")
    with pytest.raises(ValueError, match="environment configuration drift"):
        runtime.inspect_running_runtime()
    monkeypatch.setenv("ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE", "65")
    path = sealed / "runtime.json"
    replacement = json.loads(json.dumps(original))
    replacement.pop("digest")
    replacement["configuration"]["ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE"] = "1"
    replacement["digest"] = hashlib.sha256(runtime._canonical(replacement)).hexdigest()
    path.write_bytes(runtime._canonical(replacement))
    with pytest.raises(ValueError, match="admitted digest"):
        runtime.inspect_running_runtime()
    path.write_bytes(runtime._canonical(original))
    (sealed / "code/app/resources/trading_calendar.json").write_text("{}")
    with pytest.raises(ValueError, match="content drift"):
        runtime.inspect_running_runtime()


def test_watch_requires_verified_startup_marker(monkeypatch):
    monkeypatch.delenv(runtime.RUNTIME_DIRECTORY_ENV, raising=False)
    monkeypatch.delenv(runtime.RUNTIME_DIGEST_ENV, raising=False)
    with pytest.raises(ValueError, match="lacks its verified"):
        runtime.inspect_running_runtime()


def test_expected_digest_is_checked_before_probing_any_runtime_python(sealed, monkeypatch):
    monkeypatch.setattr(runtime, "_probe", lambda _python: pytest.fail("unadmitted executable was probed"))
    with pytest.raises(ValueError, match="admitted digest"):
        runtime.inspect_runtime(sealed, expected_digest="f" * 64)
    monkeypatch.setenv(runtime.RUNTIME_DIRECTORY_ENV, str(sealed))
    monkeypatch.setenv(runtime.RUNTIME_DIGEST_ENV, "f" * 64)
    with pytest.raises(ValueError, match="admitted digest"):
        runtime.inspect_running_runtime()


@pytest.mark.parametrize("payload", [b'[]', b'{"digest": "first", "digest": "last"}'])
def test_runtime_manifest_rejects_non_object_and_duplicate_keys(sealed, payload):
    (sealed / "runtime.json").write_bytes(payload)
    with pytest.raises(ValueError):
        runtime.inspect_runtime(sealed)


def test_runtime_manifest_is_bounded_before_json_decoding(sealed, monkeypatch):
    monkeypatch.setattr(runtime, "MAX_RUNTIME_RECEIPT_BYTES", 64)
    with pytest.raises(ValueError, match="bounded regular"):
        runtime.inspect_runtime(sealed)


def test_runtime_manifest_fifo_is_rejected_without_waiting_for_a_writer(sealed):
    path = sealed / "runtime.json"
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match="bounded regular"):
        runtime.inspect_runtime(sealed)


def test_runtime_manifest_replacement_during_read_is_unknown_not_verified(sealed, tmp_path, monkeypatch):
    original = runtime.os.fstat
    other = tmp_path / "other.json"
    other.write_bytes(b"{}")
    changed = other.stat()
    calls = []
    def observed(descriptor):
        calls.append(descriptor)
        return original(descriptor) if len(calls) == 1 else changed
    monkeypatch.setattr(runtime.os, "fstat", observed)
    with pytest.raises(OSError, match="changed during inspection"):
        runtime._read_runtime_manifest(sealed / "runtime.json")


def test_runtime_cli_inspect_outputs_only_verified_identity_or_sanitized_failure(sealed, capsys):
    assert runtime.main(["inspect", str(sealed)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
    (sealed / "runtime.json").write_text('{"private":"do not expose"}')
    assert runtime.main(["inspect", str(sealed)]) == 2
    output = capsys.readouterr()
    assert output.out == "" and "do not expose" not in output.err
    assert json.loads(output.err)["status"] == "failed"


def test_runtime_cli_prepare_and_run_forward_explicit_arguments(tmp_path, monkeypatch, capsys):
    calls = []
    def prepared(source, destination, environment, *, configuration):
        calls.append((source, destination, environment, configuration))
        return {"digest": "a" * 64, "schema_version": runtime.SCHEMA}
    monkeypatch.setattr(runtime, "prepare_runtime", prepared)
    assert runtime.main(["prepare", "--source-root", str(tmp_path / "source"), "--destination", str(tmp_path / "sealed"),
        "--environment-root", str(tmp_path / "environment"), "--min-data-quality-score", "65"]) == 0
    assert calls[0][-1] == {"ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE": "65"}
    def launched(directory, arguments, *, entrypoint):
        calls.append((directory, arguments, entrypoint))
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(runtime, "run_runtime", launched)
    assert runtime.main(["run", "--directory", str(tmp_path / "sealed"), "--", "--root", str(tmp_path / "plans"),
        "--plan-id", "study", "status"]) == 7
    assert calls[-1][1] == ["--root", str(tmp_path / "plans"), "--plan-id", "study", "status"]
    capsys.readouterr()


def test_dependency_lock_rejects_installed_version_mismatch_before_copy(prepared_runtime, tmp_path):
    _root, source, environment, _destination, _manifest = prepared_runtime
    changed_source = tmp_path / "source"
    shutil.copytree(source, changed_source)
    (changed_source / "requirements-lock.txt").write_text("independent-package==2.0\n")
    destination = tmp_path / "rejected-runtime"
    with pytest.raises(ValueError, match="disagree with runtime dependency lock"):
        runtime.prepare_runtime(changed_source, destination, environment)
    assert not destination.exists()
    assert not list(tmp_path.glob(".rejected-runtime-*"))


def test_shared_base_standard_library_drift_is_rejected(sealed, monkeypatch):
    original = runtime._base_identity
    def changed(probe):
        value = original(probe)
        value["stdlib_files"]["changed.py"] = "0" * 64
        return value
    monkeypatch.setattr(runtime, "_base_identity", changed)
    with pytest.raises(ValueError, match="base Python drift"):
        runtime.inspect_runtime(sealed)


@pytest.mark.parametrize("arguments", [
    ["--plan-id", "study", "status"],
    ["--root", "relative-plans", "--plan-id", "study", "status"],
    ["--root", "/tmp/plans", "--plan-id", "study", "capture"],
    ["--root", "/tmp/plans", "--plan-id", "study", "readiness"],
    ["--root", "/tmp/plans", "--plan-id", "study", "evidence"],
    ["--root", "/tmp/plans", "--root", "/tmp/other", "--plan-id", "study", "status"],
    ["--root", "/tmp/plans", "--plan-id", "study", "capture", "--database=relative.sqlite3"],
])
def test_implicit_or_relative_data_paths_rejected_before_runtime_access(tmp_path, arguments):
    with pytest.raises(ValueError):
        runtime.run_runtime(tmp_path / "does-not-exist", arguments)


def test_runner_requires_all_explicit_paths_and_preserves_arguments(sealed, tmp_path):
    arguments = ["--root", str(tmp_path / "plans"), "--plan-id", "study", "--database", str(tmp_path / "market.sqlite3"),
                 "--control-root", str(tmp_path / "control"), "--expected-plan-digest", "f" * 64, "--watch"]
    result = runtime.run_runtime(sealed, arguments, entrypoint="run_strategy_prospective.py", capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["argv"] == arguments
    with pytest.raises(ValueError, match="requires explicit"):
        runtime.run_runtime(sealed, arguments[:6], entrypoint="run_strategy_prospective.py")
    with pytest.raises(ValueError, match="unsupported"):
        runtime.run_runtime(sealed, arguments, entrypoint="../unapproved.py")


def test_prepare_rejects_existing_runtime_and_sensitive_configuration(prepared_runtime, tmp_path):
    _root, source, environment, destination, _manifest = prepared_runtime
    with pytest.raises(ValueError, match="must be new"):
        runtime.prepare_runtime(source, destination, environment)
    with pytest.raises(ValueError, match="non-sensitive"):
        runtime.prepare_runtime(source, tmp_path / "rejected", environment, configuration={"ASHARE_RADAR_LLM_API_KEY": "secret"})
    assert not (tmp_path / "rejected").exists()


def test_runtime_receipt_is_canonical_and_seals_full_content(sealed):
    manifest = runtime.inspect_runtime(sealed)
    digest = manifest.pop("digest")
    assert digest == hashlib.sha256(runtime._canonical(manifest)).hexdigest()
    assert "app/calculation.py" in manifest["code_files"]
    assert "app/resources/trading_calendar.json" in manifest["code_files"]
    assert "requirements-lock.txt" in manifest["code_files"]
    assert "lib/python3.12/site-packages/independent_package.py" in manifest["environment_files"]
