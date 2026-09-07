from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import select
import subprocess
import sys
from threading import Event

import pytest

from app.artifacts.io import ArtifactIOError
from app.services import market_scan_research_runner as runner
from app.services.market_scan_trial_registry import create_trial_registry, load_trial_registry
from tests.test_market_scan_research_runner import research_fixture


@pytest.mark.parametrize("options", [{"resume": True}, {"replay_only": True}, {}])
def test_another_execution_cannot_cancel_an_active_trial(tmp_path, monkeypatch, options) -> None:
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    entered, released = Event(), Event()
    original_evaluate = runner._evaluate_trial

    def paused_evaluation(dataset, variant, *args):
        if variant == "production_v5":
            entered.set()
            assert released.wait(timeout=10), "test did not release running trial"
        return original_evaluate(dataset, variant, *args)

    monkeypatch.setattr(runner, "_evaluate_trial", paused_evaluation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(runner.run_registered_research, tmp_path, "experiment", dataset, synthetic_rows=rows)
        try:
            assert entered.wait(timeout=10), "first execution did not reach the barrier"
            before = load_trial_registry(tmp_path, "experiment")
            assert before.statuses == {"production_v5": "running"}
            with pytest.raises(ValueError, match="already active"):
                runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, **options)
            assert load_trial_registry(tmp_path, "experiment") == before
        finally:
            released.set()
        report = active.result(timeout=10)
    assert set(report["registry"]["statuses"].values()) == {"succeeded"}
    assert len(load_trial_registry(tmp_path, "experiment").events) == 8


def test_process_exit_releases_lease_and_resume_retains_interrupted_receipt(tmp_path, monkeypatch) -> None:
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    script = """
from pathlib import Path
import sys
from app.services.market_scan_trial_registry import trial_registry_execution_lease, start_trial
with trial_registry_execution_lease(Path(sys.argv[1]), 'experiment'):
    start_trial(Path(sys.argv[1]), 'experiment', 'production_v5')
    print('lease acquired', flush=True)
    sys.stdin.buffer.read(1)
"""
    with subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                          cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            assert ready and process.stdout.readline() == b"lease acquired\n"
            before = load_trial_registry(tmp_path, "experiment")
            with pytest.raises(ValueError, match="already active"):
                runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, resume=True)
            assert load_trial_registry(tmp_path, "experiment") == before
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    lock = tmp_path / "experiment" / ".executor.lock"
    original_inode = lock.stat().st_ino
    report = runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows, resume=True)
    assert report["registry"]["statuses"]["production_v5"] == "cancelled"
    assert list(report["registry"]["statuses"].values()).count("succeeded") == 3
    assert len(load_trial_registry(tmp_path, "experiment").events) == 8
    assert lock.stat().st_ino == original_inode


@pytest.mark.parametrize("mutation", ["symlink", "nonempty"])
def test_untrusted_execution_lock_rejected_before_any_attempt(tmp_path, monkeypatch, mutation) -> None:
    _, dataset, rows, contract = research_fixture(monkeypatch)
    create_trial_registry(tmp_path, "experiment", contract)
    lock = tmp_path / "experiment" / ".executor.lock"
    if mutation == "symlink":
        target = tmp_path / "elsewhere"
        target.write_bytes(b"")
        lock.symlink_to(target)
    else:
        lock.write_bytes(b"forged")
    with pytest.raises(ArtifactIOError):
        runner.run_registered_research(tmp_path, "experiment", dataset, synthetic_rows=rows)
    assert not tuple(lock.parent.glob("event-*.json"))
