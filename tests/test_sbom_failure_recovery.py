from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

import pytest

from tools import generate_sbom as sbom


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["write", "replace", "interrupt"])
def test_failed_sbom_publication_preserves_target_and_removes_staging(tmp_path, monkeypatch, existing, failure):
    target = tmp_path / "python.cdx.json"
    if existing:
        target.write_bytes(b"previous inventory")
    error = KeyboardInterrupt("interrupted write") if failure == "interrupt" else OSError("synthetic disk failure")
    original = sbom.tempfile.NamedTemporaryFile

    def failing_temporary(*args, **kwargs):
        handle = original(*args, **kwargs)
        write = handle.write

        def partial_write(content):
            write(content[:4])
            raise error

        handle.write = partial_write
        return handle

    if failure == "replace":
        def failed_replace(*_args):
            raise error
        monkeypatch.setattr(sbom.os, "replace", failed_replace)
    else:
        monkeypatch.setattr(sbom.tempfile, "NamedTemporaryFile", failing_temporary)
    with pytest.raises(type(error)) as caught:
        sbom._write_atomic(target, b"new inventory")
    assert caught.value is error
    assert sorted(path.name for path in tmp_path.iterdir()) == ([target.name] if existing else [])
    if existing:
        assert target.read_bytes() == b"previous inventory"


def test_successful_sbom_publication_replaces_existing_bytes_without_staging(tmp_path):
    target = tmp_path / "python.cdx.json"
    target.write_bytes(b"old")
    sbom._write_atomic(target, b"new complete inventory")
    assert target.read_bytes() == b"new complete inventory"
    assert list(tmp_path.iterdir()) == [target]
    assert target.stat().st_mode & 0o777 == 0o600


def test_slow_generator_is_terminated_and_reaped_within_its_budget(monkeypatch):
    monkeypatch.setattr(sbom, "SBOM_COMMAND_TIMEOUT_SECONDS", 0.1, raising=False)
    original = subprocess.Popen
    children = []

    def track_child(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", track_child)
    started = time.monotonic()
    with pytest.raises(sbom.SbomGenerationError, match="timed out"):
        sbom._run([sys.executable, "-c", "import time; time.sleep(2)"])
    assert time.monotonic() - started < 1.5
    assert len(children) == 1 and children[0].poll() is not None


def test_sbom_cli_reports_filesystem_failure_without_traceback_or_secret(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("EXAMPLE_TOKEN", "synthetic-test-secret")

    def fail_generation(_output):
        raise OSError(f"{sbom.ROOT}/blocked synthetic-test-secret")

    monkeypatch.setattr(sbom, "generate_sboms", fail_generation)
    assert sbom.main(["--output-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SBOM generation failed:" in captured.err
    assert "synthetic-test-secret" not in captured.err
    assert str(sbom.ROOT) not in captured.err
    assert "Traceback" not in captured.err


def test_sbom_cli_does_not_swallow_user_interruption(monkeypatch, tmp_path):
    def interrupted(_output: Path):
        raise KeyboardInterrupt("stop generation")

    monkeypatch.setattr(sbom, "generate_sboms", interrupted)
    with pytest.raises(KeyboardInterrupt, match="stop generation"):
        sbom.main(["--output-dir", str(tmp_path)])
