import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event

import pytest

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, sha256_hex
from app.services import market_scan_prospective as protocol
from app.services import market_scan_prospective_contract as contract
from app.services.market_scan_prospective_store import digested_record
from app.services.market_scan_trial_registry_lock import trial_registry_write_lock
from tools import manage_market_scan_prospective as cli


def specification() -> dict[str, object]:
    return {
        "start_date": "2026-09-08", "end_date": "2026-09-09",
        "trading_dates": ["2026-09-08", "2026-09-09"],
        "daily_cutoff": "18:00:00", "timezone": "Asia/Shanghai",
        "batch_slot": "daily-close",
        "selection_rule": "latest-published-official-full-market-before-cutoff",
        "missing_policy": "retain-null-no-backfill",
        "candidates": [{"candidate_id": "v5", "specification": {"version": 5}},
                       {"candidate_id": "candidate", "specification": {"version": 6}}],
        "statistics": {
            "reference": "v5", "family": ["candidate"], "target": "mean_daily_net_return_improvement",
            "alternative": "greater", "method": "benjamini-yekutieli-fdr",
            "block_length": 6, "minimum_dates": 40, "bootstrap_samples": 10000, "alpha": .05,
        },
        "code_manifest": {"app/utils/clock.py": sha256_hex(Path("app/utils/clock.py").read_bytes())},
        "execution_policy": {"account": "v3", "initial_cash": 1000000, "cost_profile": "base"},
    }


def clock(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(protocol, "utc_now", lambda: datetime.fromisoformat(text).astimezone(UTC))


def input_file(tmp_path: Path, day: str, *, available: str | None = None) -> Path:
    path = tmp_path / f"source-{day}.json"
    path.write_bytes(canonical_json_bytes({
        "schema_version": "market-scan-prospective-input-v1", "trade_date": day,
        "batch_slot": "daily-close", "actual_run_id": "run-123",
        "published_at": f"{day}T17:00:00+08:00", "available_at": available or f"{day}T17:01:00+08:00",
        "payload": {"symbol": "600000", "value": 42},
    }))
    return path


def test_complete_protocol_keeps_missing_and_late_unverified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock(monkeypatch, "2026-09-07T12:00:00+08:00")
    plan = protocol.create_prospective_plan(tmp_path / "plans", "p", specification())
    clock(monkeypatch, "2026-09-08T18:01:00+08:00")
    source = input_file(tmp_path, "2026-09-08")
    receipt = protocol.record_prospective_input(tmp_path / "plans", "p", "2026-09-08", input_path=source)
    assert receipt["status"] == "late"
    assert receipt["input_sha256"] == sha256_hex(source.read_bytes())
    clock(monkeypatch, "2026-09-09T18:01:00+08:00")
    protocol.record_prospective_input(tmp_path / "plans", "p", "2026-09-09", reason="collection failed")
    seal = protocol.seal_prospective_inputs(tmp_path / "plans", "p")
    verified = protocol.verify_prospective_plan(tmp_path / "plans", "p", expected_plan_digest=str(plan["digest"]))
    assert verified["input_seal_digest"] == seal["digest"]
    assert verified["machine_promotion_eligible"] is False
    assert verified["input_admission_verified"] is False
    assert verified["statuses"] == {"2026-09-08": "late", "2026-09-09": "missing"}


@pytest.fixture
def planned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    clock(monkeypatch, "2026-09-07T12:00:00+08:00")
    root = tmp_path / "plans"
    protocol.create_prospective_plan(root, "p", specification())
    return root


def missing_day(root: Path, monkeypatch: pytest.MonkeyPatch, day: str) -> dict[str, object]:
    clock(monkeypatch, f"{day}T18:01:00+08:00")
    return protocol.record_prospective_input(root, "p", day, reason="provider unavailable")


@pytest.mark.parametrize("change", ["statistic_float", "candidate_float", "execution_float"])
def test_creation_retry_requires_exact_json_types(planned: Path, change: str) -> None:
    spec = specification()
    if change == "statistic_float":
        spec["statistics"]["block_length"] = 6.0
    elif change == "candidate_float":
        spec["candidates"][0]["specification"]["version"] = 5.0
    else:
        spec["execution_policy"]["initial_cash"] = 1000000.0
    with pytest.raises(ValueError, match="different frozen specification"):
        protocol.create_prospective_plan(planned, "p", spec)


def sealed_plan(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    missing_day(root, monkeypatch, "2026-09-08")
    missing_day(root, monkeypatch, "2026-09-09")
    return protocol.seal_prospective_inputs(root, "p")


def rewrite(path: Path, **changes: object) -> None:
    item = json.loads(path.read_bytes())
    item.update(changes)
    item.pop("digest")
    path.write_bytes(canonical_json_bytes(digested_record(item)))


@pytest.mark.parametrize("change", [
    {"trading_dates": ["2026-09-09"]}, {"trading_dates": ["2026-09-08", "2026-09-08", "2026-09-09"]},
    {"end_date": "2027-01-05"}, {"start_date": "2026-09-07", "trading_dates": ["2026-09-07", "2026-09-08", "2026-09-09"]},
    {"daily_cutoff": "18:00"}, {"daily_cutoff": "18:00:00+08:00"}, {"daily_cutoff": "bogus"},
    {"timezone": "UTC"}, {"missing_policy": "drop-missing"}, {"selection_rule": "best-return"},
    {"batch_slot": "../escape"}, {"candidates": []}, {"code_manifest": {}},
    {"code_manifest": {"../outside.py": "a" * 64}}, {"code_manifest": {"app/utils/clock.py": "a" * 64}},
    {"execution_policy": {}}, {"extra": 1},
])
def test_invalid_future_contract_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]) -> None:
    clock(monkeypatch, "2026-09-07T12:00:00+08:00")
    spec = specification() | change
    with pytest.raises(ValueError):
        protocol.create_prospective_plan(tmp_path / "plans", "p", spec)
    assert not (tmp_path / "plans/p/plan.json").exists()


@pytest.mark.parametrize("change", [
    {"family": []}, {"family": ["v5", "candidate"]}, {"reference": "absent"}, {"method": "none"},
    {"block_length": True}, {"block_length": 5}, {"minimum_dates": 39}, {"bootstrap_samples": 999},
    {"alpha": .1}, {"alpha": float("nan")}, {"alternative": "two-sided"},
])
def test_entire_family_and_statistical_contract_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]) -> None:
    clock(monkeypatch, "2026-09-07T12:00:00+08:00")
    spec = specification()
    spec["statistics"] = dict(spec["statistics"]) | change
    with pytest.raises((ValueError, ArtifactIOError)):
        protocol.create_prospective_plan(tmp_path / "plans", "p", spec)


def test_creation_retry_recovers_identity_after_future_period(planned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = (planned / "p/plan.json").read_bytes()
    clock(monkeypatch, "2026-09-10T12:00:00+08:00")
    protocol.create_prospective_plan(planned, "p", specification())
    assert (planned / "p/plan.json").read_bytes() == original
    changed = specification() | {"batch_slot": "other"}
    with pytest.raises(ValueError, match="different"):
        protocol.create_prospective_plan(planned, "p", changed)


@pytest.mark.parametrize("day", ["2026-09-07", "2026-09-09", "2026-09-10"])
def test_skipped_or_outside_day_cannot_enter_chain(planned: Path, monkeypatch: pytest.MonkeyPatch, day: str) -> None:
    clock(monkeypatch, "2026-09-10T12:00:00+08:00")
    with pytest.raises(ValueError, match="next predeclared"):
        protocol.record_prospective_input(planned, "p", day, reason="missing")
    assert not list((planned / "p").glob("receipt-*.json"))


def test_missing_cannot_be_replaced_or_used_to_skip_a_day(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing_day(planned, monkeypatch, "2026-09-08")
    with pytest.raises(ValueError, match="next predeclared"):
        protocol.record_prospective_input(planned, "p", "2026-09-08", input_path=input_file(tmp_path, "2026-09-08"))
    with pytest.raises(ValueError, match="every predeclared"):
        protocol.seal_prospective_inputs(planned, "p")


@pytest.mark.parametrize("observed,reason", [("2026-09-08T17:59:59+08:00", "failed"), ("2026-09-08T18:01:00+08:00", " ")])
def test_missing_requires_elapsed_deadline_and_reason(planned: Path, monkeypatch: pytest.MonkeyPatch, observed: str, reason: str) -> None:
    clock(monkeypatch, observed)
    with pytest.raises(ValueError, match="missing requires"):
        protocol.record_prospective_input(planned, "p", "2026-09-08", reason=reason)


@pytest.mark.parametrize("change", [
    {"trade_date": "2026-09-09"}, {"batch_slot": "other"}, {"actual_run_id": "../bad"},
    {"available_at": "2026-09-08T17:30:00"}, {"available_at": "2026-09-08T19:00:00+08:00"},
    {"published_at": "2026-09-07T17:00:00+08:00"}, {"published_at": "2026-09-08T18:00:00+08:00"},
    {"payload": {}}, {"unexpected": True},
])
def test_input_identity_and_time_claims_are_strict(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: dict[str, object]) -> None:
    clock(monkeypatch, "2026-09-08T17:30:00+08:00")
    path = input_file(tmp_path, "2026-09-08")
    path.write_bytes(canonical_json_bytes(json.loads(path.read_bytes()) | change))
    with pytest.raises(ValueError):
        protocol.record_prospective_input(planned, "p", "2026-09-08", input_path=path)


def test_recording_clock_is_taken_after_read_not_before(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = input_file(tmp_path, "2026-09-08")
    clock(monkeypatch, "2026-09-08T17:59:59+08:00")
    original = protocol.read_regular_file

    def slow_read(*args, **kwargs):
        encoded = original(*args, **kwargs)
        clock(monkeypatch, "2026-09-08T18:00:01+08:00")
        return encoded

    monkeypatch.setattr(protocol, "read_regular_file", slow_read)
    item = protocol.record_prospective_input(planned, "p", "2026-09-08", input_path=path)
    assert item["status"] == "late"


def test_original_bytes_survive_source_changes(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = input_file(tmp_path, "2026-09-08")
    raw = b" \n" + path.read_bytes() + b"\n"
    path.write_bytes(raw)
    clock(monkeypatch, "2026-09-08T17:30:00+08:00")
    item = protocol.record_prospective_input(planned, "p", "2026-09-08", input_path=path)
    assert item["status"] == "local-on-time-unverified"
    assert base64.b64decode(item["input_base64"]) == raw
    path.write_text("now different")
    assert protocol.verify_prospective_plan(planned, "p")["receipts"][0]["input_sha256"] == sha256_hex(raw)


def test_same_size_same_mtime_tampering_detected(planned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_day(planned, monkeypatch, "2026-09-08")
    path = planned / "p/receipt-00000001.json"
    before = path.stat()
    path.write_bytes(path.read_bytes().replace(b"provider unavailable", b"provider availabless"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    with pytest.raises(ValueError, match="digest mismatch"):
        protocol.verify_prospective_plan(planned, "p")


@pytest.mark.parametrize("change", [{"trade_date": "2026-09-09"}, {"sequence": True}, {"previous_digest": "0" * 64}, {"plan_digest": "0" * 64}])
def test_rehashed_receipt_still_must_obey_chain_and_calendar(planned: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]) -> None:
    missing_day(planned, monkeypatch, "2026-09-08")
    rewrite(planned / "p/receipt-00000001.json", **change)
    with pytest.raises(ValueError):
        protocol.verify_prospective_plan(planned, "p")


def test_removing_a_middle_receipt_cannot_hide_missingness(planned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sealed_plan(planned, monkeypatch)
    (planned / "p/receipt-00000001.json").unlink()
    with pytest.raises(ValueError, match="sequence is incomplete"):
        protocol.verify_prospective_plan(planned, "p")


def test_caller_anchor_detects_rehashed_complete_plan(planned: Path) -> None:
    plan = json.loads((planned / "p/plan.json").read_bytes())
    changed = deepcopy(plan["specification"])
    changed["batch_slot"] = "rewritten"
    rewrite(planned / "p/plan.json", specification=changed)
    with pytest.raises(ValueError, match="retained digest anchor"):
        protocol.verify_prospective_plan(planned, "p", expected_plan_digest=plan["digest"])


def test_clock_rollback_is_rejected(planned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_day(planned, monkeypatch, "2026-09-08")
    clock(monkeypatch, "2026-09-08T17:00:00+08:00")
    with pytest.raises(ValueError, match="clock moved backwards"):
        protocol.record_prospective_input(planned, "p", "2026-09-09", reason="missing")


def test_seal_waits_for_last_deadline_and_is_idempotent(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing_day(planned, monkeypatch, "2026-09-08")
    clock(monkeypatch, "2026-09-09T17:30:00+08:00")
    protocol.record_prospective_input(planned, "p", "2026-09-09", input_path=input_file(tmp_path, "2026-09-09"))
    with pytest.raises(ValueError, match="final collection deadline"):
        protocol.seal_prospective_inputs(planned, "p")
    clock(monkeypatch, "2026-09-09T18:01:00+08:00")
    first = protocol.seal_prospective_inputs(planned, "p")
    assert protocol.seal_prospective_inputs(planned, "p") == first
    with pytest.raises(ValueError, match="already sealed"):
        protocol.record_prospective_input(planned, "p", "2026-09-09", reason="replace")


def test_result_binding_requires_sealed_inputs_and_is_immutable(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = tmp_path / "result.json"
    result.write_text('{"claimed_alpha":100}')
    with pytest.raises(ValueError, match="before input sealing"):
        protocol.bind_prospective_result(planned, "p", result)
    seal = sealed_plan(planned, monkeypatch)
    first = protocol.bind_prospective_result(planned, "p", result)
    assert first["input_seal_digest"] == seal["digest"]
    assert protocol.bind_prospective_result(planned, "p", result) == first
    verified = protocol.verify_prospective_plan(planned, "p", expected_seal_digest=seal["digest"])
    assert verified["result_evaluation_verified"] is False
    result.write_text('{"claimed_alpha":200}')
    with pytest.raises(ValueError, match="different result"):
        protocol.bind_prospective_result(planned, "p", result)


def test_untrusted_result_cannot_precede_input_seal_even_with_valid_hash(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sealed_plan(planned, monkeypatch)
    result = tmp_path / "result.json"
    result.write_text("result")
    protocol.bind_prospective_result(planned, "p", result)
    rewrite(planned / "p/result.json", recorded_at="2026-09-08T18:00:00+08:00")
    with pytest.raises(ValueError, match="precedes input sealing"):
        protocol.verify_prospective_plan(planned, "p")


def test_two_thread_writers_commit_one_daily_receipt(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clock(monkeypatch, "2026-09-08T17:30:00+08:00")
    path = input_file(tmp_path, "2026-09-08")
    entered = Event()

    def attempt():
        entered.set()
        try:
            protocol.record_prospective_input(planned, "p", "2026-09-08", input_path=path)
            return "committed"
        except ValueError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        with trial_registry_write_lock(planned / "p"):
            first, second = pool.submit(attempt), pool.submit(attempt)
            assert entered.wait(2)
            assert not first.done() and not second.done()
        assert sorted([first.result(timeout=5), second.result(timeout=5)]) == ["committed", "duplicate"]
    assert len(list((planned / "p").glob("receipt-*.json"))) == 1


def test_killed_atomic_writer_leaves_no_receipt_and_retry_becomes_late(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = input_file(tmp_path, "2026-09-08")
    code = """
import os, sys
from datetime import datetime
from pathlib import Path
import app.artifacts.io as io
from app.services import market_scan_prospective as protocol
protocol.utc_now = lambda: datetime.fromisoformat('2026-09-08T17:30:00+08:00')
io._link_exclusively_at = lambda *a, **kw: os._exit(23)
protocol.record_prospective_input(Path(sys.argv[1]), 'p', '2026-09-08', input_path=Path(sys.argv[2]))
"""
    completed = subprocess.run([sys.executable, "-c", code, str(planned), str(path)], timeout=10)
    assert completed.returncode == 23
    assert list((planned / "p").glob(".receipt-*.tmp"))
    assert protocol.verify_prospective_plan(planned, "p")["recorded_day_count"] == 0
    clock(monkeypatch, "2026-09-08T18:01:00+08:00")
    receipt = protocol.record_prospective_input(planned, "p", "2026-09-08", input_path=path)
    assert receipt["status"] == "late"


@pytest.mark.parametrize("target", ["root", "plan", "receipt", "staging", "input"])
def test_symlinks_are_rejected_at_namespace_and_input_boundaries(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str) -> None:
    missing_day(planned, monkeypatch, "2026-09-08")
    if target == "input":
        alias = tmp_path / "alias.json"
        alias.symlink_to(input_file(tmp_path, "2026-09-09"))
        clock(monkeypatch, "2026-09-09T17:30:00+08:00")
        with pytest.raises((ValueError, ArtifactIOError)):
            protocol.record_prospective_input(planned, "p", "2026-09-09", input_path=alias)
        return
    if target in {"root", "plan"}:
        original = planned if target == "root" else planned / "p"
        moved = original.with_name(original.name + "-moved")
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    else:
        name = "receipt-00000001.json" if target == "receipt" else ".receipt-00000002.json.0123456789abcdef.tmp"
        (planned / "p" / name).unlink(missing_ok=True)
        (planned / "p" / name).symlink_to(tmp_path / "outside")
    with pytest.raises((ValueError, OSError)):
        protocol.verify_prospective_plan(planned, "p")


def test_cli_output_failure_can_recover_via_verify(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock(monkeypatch, "2026-09-08T18:01:00+08:00")

    def broken_output(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(cli, "_write_output", broken_output)
    common = ["--plan-root", str(planned), "--plan-id", "p"]
    assert cli.main(["record", *common, "--trade-date", "2026-09-08", "--missing", "--reason", "failed"]) == 1
    assert "操作已提交" in capsys.readouterr().err
    monkeypatch.undo()
    assert cli.main(["verify", *common]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["receipt"]["statuses"] == {"2026-09-08": "missing"}
    assert result["machine_promotion_eligible"] is False


def test_cli_rejects_output_in_plan_namespace_before_mutation(planned: Path) -> None:
    assert cli.main(["record", "--plan-root", str(planned), "--plan-id", "p", "--trade-date", "2026-09-08",
                     "--missing", "--reason", "failed", "--output", str(planned / "unexpected.json")]) == 1
    assert not list((planned / "p").glob("receipt-*.json"))


def test_frozen_calendar_survives_normal_bundled_calendar_extension(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = (planned / "p/plan.json").read_bytes()
    snapshot = json.loads(contract.CALENDAR_PATH.read_bytes())
    snapshot["trade_dates"].append("2027-01-04")
    snapshot["max_date"] = "2027-01-04"
    snapshot["trade_date_count"] += 1
    updated = tmp_path / "updated-calendar.json"
    updated.write_bytes(canonical_json_bytes(snapshot))
    monkeypatch.setattr(contract, "CALENDAR_PATH", updated)
    monkeypatch.setattr(protocol, "CALENDAR_PATH", updated)
    assert protocol.verify_prospective_plan(planned, "p")["local_integrity"] == "verified"
    assert protocol.create_prospective_plan(planned, "p", specification())["digest"] == json.loads(original)["digest"]
    assert (planned / "p/plan.json").read_bytes() == original


def test_frozen_calendar_and_code_audit_does_not_require_current_sources(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(contract, "CALENDAR_PATH", tmp_path / "absent")
    monkeypatch.setattr(contract, "PROJECT_ROOT", tmp_path / "absent-repo")
    assert protocol.verify_prospective_plan(planned, "p")["local_integrity"] == "verified"


def test_creation_calendar_digest_binds_the_original_validated_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = contract.CALENDAR_PATH.read_bytes()
    local = tmp_path / "bundled.json"
    local.write_bytes(snapshot)
    monkeypatch.setattr(protocol, "CALENDAR_PATH", local)
    original = protocol.read_regular_file

    def changing_calendar(path, **kwargs):
        encoded = original(path, **kwargs)
        if path == local:
            local.write_text("changed after guarded read")
        return encoded

    monkeypatch.setattr(protocol, "read_regular_file", changing_calendar)
    clock(monkeypatch, "2026-09-07T12:00:00+08:00")
    plan = protocol.create_prospective_plan(tmp_path / "plans", "p", specification())
    assert plan["calendar_sha256"] == sha256_hex(snapshot)
    assert base64.b64decode(plan["calendar_base64"]) == snapshot


def test_two_process_writers_cannot_duplicate_a_calendar_day(planned: Path, tmp_path: Path) -> None:
    path = input_file(tmp_path, "2026-09-08")
    code = """
import sys
from datetime import datetime
from pathlib import Path
from app.services import market_scan_prospective as protocol
protocol.utc_now = lambda: datetime.fromisoformat('2026-09-08T17:30:00+08:00')
print('ready', flush=True)
try:
    protocol.record_prospective_input(Path(sys.argv[1]), 'p', '2026-09-08', input_path=Path(sys.argv[2]))
except ValueError:
    sys.exit(3)
"""
    processes = []
    try:
        with trial_registry_write_lock(planned / "p"):
            processes = [subprocess.Popen([sys.executable, "-c", code, str(planned), str(path)], stdout=subprocess.PIPE, text=True) for _ in range(2)]
            for process in processes:
                assert process.stdout.readline().strip() == "ready"
                assert process.poll() is None
        assert sorted(process.wait(timeout=10) for process in processes) == [0, 3]
        assert protocol.verify_prospective_plan(planned, "p")["recorded_day_count"] == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()


def test_cli_create_record_seal_bind_and_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    clock(monkeypatch, "2026-09-07T12:00:00+08:00")
    root = tmp_path / "cli-plans"
    spec = tmp_path / "spec.json"
    spec.write_bytes(canonical_json_bytes(specification()))
    common = ["--plan-root", str(root), "--plan-id", "p"]
    output = tmp_path / "created.json"
    assert cli.main(["create", *common, "--spec", str(spec), "--output", str(output)]) == 0
    plan = json.loads(output.read_bytes())["receipt"]
    for day in ("2026-09-08", "2026-09-09"):
        clock(monkeypatch, f"{day}T18:01:00+08:00")
        assert cli.main(["record", *common, "--trade-date", day, "--missing", "--reason", "collector failed"]) == 0
    assert cli.main(["seal", *common]) == 0
    result = tmp_path / "result.json"
    result.write_text("{\"claim\": true}")
    assert cli.main(["bind-result", *common, "--result", str(result)]) == 0
    assert cli.main(["verify", *common, "--expected-plan-digest", plan["digest"]]) == 0
    assert '"timestamp_assurance":"local-only-unverified"' in capsys.readouterr().out
    assert cli.main(["verify", *common, "--output", str(output)]) == 1


@pytest.mark.parametrize("replacement", ["not-base64!", base64.b64encode(b"different bytes").decode()])
def test_rehashed_wrapper_cannot_hide_corrupted_preserved_input(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replacement: str) -> None:
    clock(monkeypatch, "2026-09-08T17:30:00+08:00")
    protocol.record_prospective_input(planned, "p", "2026-09-08", input_path=input_file(tmp_path, "2026-09-08"))
    rewrite(planned / "p/receipt-00000001.json", input_base64=replacement)
    with pytest.raises(ValueError, match="base64|digest mismatch"):
        protocol.verify_prospective_plan(planned, "p")


def test_rehashed_late_status_cannot_be_upgraded_to_on_time(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clock(monkeypatch, "2026-09-08T18:01:00+08:00")
    protocol.record_prospective_input(planned, "p", "2026-09-08", input_path=input_file(tmp_path, "2026-09-08"))
    rewrite(planned / "p/receipt-00000001.json", status="local-on-time-unverified")
    with pytest.raises(ValueError, match="late inputs cannot be backfilled"):
        protocol.verify_prospective_plan(planned, "p")


@pytest.mark.parametrize("change", [{"receipt_count": True}, {"statuses": {}}, {"head_digest": "f" * 64}])
def test_rehashed_seal_must_still_bind_every_day(planned: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]) -> None:
    sealed_plan(planned, monkeypatch)
    rewrite(planned / "p/input-seal.json", **change)
    with pytest.raises(ValueError):
        protocol.verify_prospective_plan(planned, "p")


def test_unexpected_namespace_file_is_not_silently_admitted(planned: Path) -> None:
    (planned / "p/receipt-backfill.json").write_text("{}")
    with pytest.raises(ValueError, match="unexpected prospective namespace"):
        protocol.verify_prospective_plan(planned, "p")


def test_duplicate_json_keys_cannot_enter_an_input_receipt(planned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clock(monkeypatch, "2026-09-08T17:30:00+08:00")
    path = input_file(tmp_path, "2026-09-08")
    path.write_bytes(path.read_bytes().replace(b'"value":42', b'"value":42,"value":43'))
    with pytest.raises(ArtifactIOError):
        protocol.record_prospective_input(planned, "p", "2026-09-08", input_path=path)


def test_cli_help_explains_local_evidence_boundary(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--help"])
    assert stopped.value.code == 0
    assert "仅本地证据" in capsys.readouterr().out


def test_cli_interrupt_after_commit_keeps_recoverable_receipt(planned: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    clock(monkeypatch, "2026-09-08T18:01:00+08:00")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_write_output", interrupted)
    assert cli.main(["record", "--plan-root", str(planned), "--plan-id", "p", "--trade-date", "2026-09-08",
                     "--missing", "--reason", "failed"]) == 130
    assert "verify" in capsys.readouterr().err
    assert protocol.verify_prospective_plan(planned, "p")["recorded_day_count"] == 1
