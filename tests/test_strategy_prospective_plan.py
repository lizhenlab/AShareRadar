from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from app.artifacts.io import ArtifactIOError, canonical_json_bytes
from app.services import strategy_prospective_plan as protocol
from app.services import strategy_prospective_store as storage
from app.services.strategy_prospective_store import prospective_record


@pytest.fixture
def environment(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "app").mkdir(parents=True)
    (source / "tools").mkdir()
    (source / "app" / "example.py").write_text("VALUE = 1\n")
    (source / "tools" / "example.py").write_text("VALUE = 2\n")
    monkeypatch.setattr(protocol, "PROJECT_ROOT", source)
    monkeypatch.setattr(protocol, "get_settings", lambda: SimpleNamespace(market_scan_min_data_quality_score=80))
    clock(monkeypatch, "2026-09-19T12:00:00+08:00")
    return tmp_path / "plans", source


def clock(monkeypatch, value):
    monkeypatch.setattr(protocol, "utc_now", lambda: datetime.fromisoformat(value).astimezone(UTC))


def create(root, **kwargs):
    arguments = {"start_date": "2026-09-21", "end_date": "2026-12-31", **kwargs}
    return protocol.create_strategy_prospective_plan(root, "fixed", **arguments)


def payload(day="2026-09-21", **changes):
    return {
        "status": "captured", "available_at": f"{day}T17:00:00+08:00",
        "session": {"signal_date": day, "published_at": f"{day}T16:40:00+08:00", "run_id": 1},
        **changes,
    }


def missing():
    return {"status": "missing", "session": None, "available_at": None, "reason": "no original scan before deadline"}


def rewrite(path, **changes):
    item = json.loads(path.read_bytes())
    item.update(changes)
    item.pop("digest")
    path.write_bytes(canonical_json_bytes(prospective_record(item)))


def test_plan_freezes_real_calendar_templates_costs_code_and_score_contract(environment):
    root, source = environment
    plan = create(root)
    spec = plan["specification"]
    assert plan["schema_version"] == "strategy-template-prospective-plan-v1"
    assert len(spec["signal_dates"]) == 7
    assert sum(value is not None for value in spec["exit_dates"].values()) == 6
    assert spec["signal_dates"][0] == "2026-09-21"
    assert spec["exit_dates"][spec["signal_dates"][-1]] is None
    assert len(spec["templates"]) == 3
    assert all(item["strategy_spec"]["execution_policy"] for item in spec["templates"])
    assert spec["inference_contract"]["family_size"] == 3
    assert spec["inference_contract"]["bootstrap_samples"] == 2000
    assert spec["inference_contract"]["block_length_anchors"] == 2
    assert set(spec["source_manifest"]) == {"app/example.py", "tools/example.py"}
    assert len(spec["production_score_spec_hash"]) == 64
    assert protocol.read_strategy_prospective_plan(root, "fixed") == plan
    status = protocol.strategy_prospective_status(root, "fixed")
    assert status["calendar_end"] == "2026-12-31"
    assert status["status"] == "waiting_for_signal"
    assert status["planned_anchor_count"] == 7 and status["matureable_anchor_count"] == 6
    assert status["adoptable_template_id"] is None and status["promotion_eligible"] is False


def test_creation_is_idempotent_after_start_but_different_parameters_rejected(environment, monkeypatch):
    root, _source = environment
    plan = create(root)
    clock(monkeypatch, "2026-09-22T12:00:00+08:00")
    assert create(root) == plan
    with pytest.raises(ValueError, match="different parameters"):
        create(root, horizon=5)


@pytest.mark.parametrize("change", [
    {"start_date": "2026-09-19"}, {"end_date": "2027-01-04"}, {"start_date": "2026-12-31", "end_date": "2026-12-01"},
    {"start_date": "2026-09-26", "end_date": "2026-09-27"}, {"start_date": "20260921"},
    {"horizon": True}, {"horizon": 2}, {"horizon": 10.0}, {"notional_cash_cny": True},
    {"notional_cash_cny": float("nan")}, {"notional_cash_cny": float("inf")}, {"notional_cash_cny": 10**400},
    {"notional_cash_cny": 9999}, {"notional_cash_cny": 10000.001}, {"cutoff_local": "15:14:59"},
    {"cutoff_local": "20:00"}, {"cutoff_local": "20:00:00+08:00"}, {"cutoff_local": None},
])
def test_invalid_plan_arguments_fail_closed(environment, change):
    root, _source = environment
    with pytest.raises((ValueError, ArtifactIOError)):
        create(root, **change)


def test_receipts_capture_incomplete_missing_and_late_preserve_chain(environment, monkeypatch):
    root, _source = environment
    plan = create(root, horizon=1, end_date="2026-09-30")
    dates = plan["specification"]["signal_dates"]
    clock(monkeypatch, dates[0] + "T17:10:00+08:00")
    first = protocol.append_strategy_prospective_receipt(root, "fixed", dates[0], payload(dates[0]))
    assert first["status"] == "captured" and first["previous_digest"] == plan["digest"]
    clock(monkeypatch, dates[1] + "T17:10:00+08:00")
    second = protocol.append_strategy_prospective_receipt(root, "fixed", dates[1], payload(dates[1], status="source_incomplete", session=None))
    assert second["status"] == "source_incomplete" and second["previous_digest"] == first["digest"]
    clock(monkeypatch, dates[2] + "T20:00:01+08:00")
    third = protocol.append_strategy_prospective_receipt(root, "fixed", dates[2], missing())
    clock(monkeypatch, dates[3] + "T21:00:00+08:00")
    fourth = protocol.append_strategy_prospective_receipt(root, "fixed", dates[3], payload(dates[3]))
    assert third["status"] == "missing" and fourth["status"] == "late"
    assert protocol.read_strategy_prospective_receipts(root, "fixed") == (first, second, third, fourth)
    state = protocol.strategy_prospective_status(root, "fixed")
    assert state["missing_count"] == 1 and state["receipt_count"] == 4
    assert state["status"] == "collection_complete" and state["next_signal_date"] is None


def test_exact_payload_retry_retains_original_time_and_missing_never_backfills(environment, monkeypatch):
    root, _source = environment
    create(root)
    clock(monkeypatch, "2026-09-21T20:01:00+08:00")
    receipt = protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", missing())
    clock(monkeypatch, "2026-09-22T20:01:00+08:00")
    assert protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", missing()) == receipt
    with pytest.raises(ValueError, match="different receipt"):
        protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload())


@pytest.mark.parametrize("when,change,error", [
    ("2026-09-20T17:10:00+08:00", {}, "future"),
    ("2026-09-21T17:10:00+08:00", {"available_at": "2026-09-21T17:11:00+08:00"}, "availability"),
    ("2026-09-21T17:10:00+08:00", {"available_at": "2026-09-21T17:00:00"}, "timezone"),
    ("2026-09-21T17:10:00+08:00", {"session": None}, "requires a frozen session"),
    ("2026-09-21T17:10:00+08:00", {"session": {"signal_date": "2026-09-22"}}, "different signal date"),
    ("2026-09-21T17:10:00+08:00", {"session": {"published_at": "2026-09-21T17:05:00+08:00"}}, "publication"),
    ("2026-09-21T17:10:00+08:00", {"recorded_at": "2026-09-21T17:10:00+08:00"}, "external recorded_at"),
    ("2026-09-21T17:10:00+08:00", {"status": "success"}, "invalid status"),
])
def test_invalid_receipt_timing_and_identity_rejected(environment, monkeypatch, when, change, error):
    root, _source = environment
    create(root)
    clock(monkeypatch, when)
    with pytest.raises(ValueError, match=error):
        protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload(**change))


@pytest.mark.parametrize("when,available", [
    ("2026-09-21T15:10:00+08:00", "2026-09-21T15:05:00+08:00"),
    ("2026-09-21T17:10:00+08:00", "2026-09-21T15:10:00+08:00"),
    ("2026-09-21T21:00:00+08:00", "2026-09-21T20:10:00+08:00"),
])
def test_outside_collection_window_is_permanently_late(environment, monkeypatch, when, available):
    root, _source = environment
    create(root)
    clock(monkeypatch, when)
    content = payload(available_at=available, session={"signal_date": "2026-09-21"})
    assert protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", content)["status"] == "late"


def test_missing_requires_deadline_and_reason_and_next_day_order(environment, monkeypatch):
    root, _source = environment
    create(root)
    clock(monkeypatch, "2026-09-21T20:00:00+08:00")
    with pytest.raises(ValueError, match="elapsed deadline"):
        protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", missing())
    clock(monkeypatch, "2026-09-21T20:01:00+08:00")
    with pytest.raises(ValueError, match="elapsed deadline"):
        protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", {**missing(), "reason": ""})
    with pytest.raises(ValueError, match="fixed signal schedule"):
        protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-22", missing())
    status = protocol.strategy_prospective_status(root, "fixed")
    assert status["status"] == "missing_receipts_due" and status["overdue_count"] == status["due_count"] == 1


def test_source_drift_stops_append_but_retains_readonly_history(environment, monkeypatch):
    root, source = environment
    plan = create(root)
    (source / "tools" / "new.py").write_text("CHANGED = True\n")
    with pytest.raises(ValueError, match="source code drift"):
        protocol.read_strategy_prospective_plan(root, "fixed")
    assert protocol.read_strategy_prospective_plan(root, "fixed", verify_code=False) == plan
    assert protocol.strategy_prospective_status(root, "fixed")["status"] == "source_drift"
    clock(monkeypatch, "2026-09-21T17:10:00+08:00")
    with pytest.raises(ValueError, match="source code drift"):
        protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload())


def test_calendar_updates_do_not_rewrite_original_schedule(environment, tmp_path, monkeypatch):
    root, _source = environment
    plan = create(root)
    monkeypatch.setattr(protocol, "CALENDAR_PATH", tmp_path / "missing-calendar.json")
    assert protocol.read_strategy_prospective_plan(root, "fixed") == plan


def test_receipt_digest_tamper_and_rehashed_chain_tamper_are_rejected(environment, monkeypatch):
    root, _source = environment
    create(root)
    clock(monkeypatch, "2026-09-21T17:10:00+08:00")
    receipt = protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload())
    filename = root / "fixed" / "receipt-2026-09-21.json"
    damaged = {**receipt, "status": "missing"}
    filename.write_bytes(canonical_json_bytes(damaged))
    with pytest.raises(ValueError, match="digest"):
        protocol.read_strategy_prospective_plan(root, "fixed")
    filename.write_bytes(canonical_json_bytes(receipt))
    rewrite(filename, previous_digest="0" * 64)
    with pytest.raises(ValueError, match="hash chain"):
        protocol.read_strategy_prospective_plan(root, "fixed")


@pytest.mark.parametrize("change,error", [
    ({"sequence": True}, "sequence"), ({"trade_date": "2026-09-22"}, "sequence"),
    ({"recorded_at": "2026-09-22T17:10:00+08:00"}, "times"), ({"status": "late"}, "status disagrees"),
])
def test_rehashed_receipt_semantic_tamper_rejected(environment, monkeypatch, change, error):
    root, _source = environment
    create(root)
    clock(monkeypatch, "2026-09-21T17:10:00+08:00")
    protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload())
    rewrite(root / "fixed" / "receipt-2026-09-21.json", **change)
    with pytest.raises(ValueError, match=error):
        protocol.read_strategy_prospective_plan(root, "fixed")


def test_orphan_snapshot_and_research_outputs_are_not_receipts(environment):
    root, _source = environment
    create(root)
    directory = root / "fixed"
    (directory / ("snapshot-2026-09-21-" + "a" * 64 + ".json.gz")).write_bytes(b"orphan")
    (directory / ".receipt-2026-09-21.json.0123456789abcdef.tmp").write_bytes(b"incomplete")
    (directory / "research").mkdir()
    assert protocol.read_strategy_prospective_receipts(root, "fixed") == ()
    (directory / "unexpected.json").write_text("{}")
    with pytest.raises(ValueError, match="unexpected entry"):
        protocol.read_strategy_prospective_plan(root, "fixed")


def test_paths_and_research_symlinks_are_rejected(environment, tmp_path):
    root, _source = environment
    with pytest.raises(ValueError):
        protocol.create_strategy_prospective_plan(root, "../escape", start_date="2026-09-21", end_date="2026-12-31")
    create(root)
    (root / "fixed" / "research").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe entry"):
        protocol.read_strategy_prospective_plan(root, "fixed")


def test_concurrent_equal_append_commits_once(environment, monkeypatch):
    root, _source = environment
    create(root)
    clock(monkeypatch, "2026-09-21T17:10:00+08:00")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload()), range(2)))
    assert results[0] == results[1]
    assert len(protocol.read_strategy_prospective_receipts(root, "fixed")) == 1


@pytest.mark.parametrize("field,value,error", [
    ("signal_dates", [], "schedule mismatch"), ("timezone", "UTC", "policies differ"),
    ("source_manifest", {}, "manifest is empty"), ("source_manifest", {"../outside.py": "0" * 64}, "unsafe path"),
    ("production_score_spec_hash", "0" * 64, "score contract mismatch"),
    ("inference_contract", {}, "inference identity"), ("templates", [], "three templates"),
    ("source_contracts", {}, "validation errors"),
])
def test_rehashed_plan_contract_corruption_rejected(environment, field, value, error):
    root, _source = environment
    plan = create(root)
    rewrite(root / "fixed" / "plan.json", specification={**plan["specification"], field: value})
    with pytest.raises(ValueError, match=error):
        protocol.read_strategy_prospective_plan(root, "fixed", verify_code=False)


@pytest.mark.parametrize("change,error", [
    ({"plan_id": "another"}, "identity mismatch"),
    ({"recorded_at": "2026-09-21T00:00:00+08:00"}, "frozen before"),
    ({"calendar_base64": "!not-base64"}, "encoding is invalid"),
    ({"calendar_sha256": "0" * 64}, "calendar digest mismatch"),
    ({"calendar_base64": None}, "encoding is invalid"),
    ({"schema_version": "other"}, "identity or digest mismatch"),
])
def test_plan_identity_and_calendar_corruption_rejected(environment, change, error):
    root, _source = environment
    create(root)
    rewrite(root / "fixed" / "plan.json", **change)
    with pytest.raises(ValueError, match=error):
        protocol.read_strategy_prospective_plan(root, "fixed")


def test_inner_template_fingerprint_is_validated_even_after_rehash(environment):
    root, _source = environment
    plan = create(root)
    spec = plan["specification"]
    spec["templates"][0]["strategy_fingerprint"] = "0" * 64
    rewrite(root / "fixed" / "plan.json", specification=spec)
    with pytest.raises(ValueError, match="frozen template fingerprint"):
        protocol.read_strategy_prospective_plan(root, "fixed", verify_code=False)


@pytest.mark.parametrize("change,error", [
    ({"trade_dates": []}, "calendar is unavailable"),
    ({"trade_date_count": 0}, "inconsistent sessions"),
    ({"max_date": "2027-12-31"}, "inconsistent bounds"),
])
def test_damaged_bundled_calendar_cannot_initialize_plan(environment, tmp_path, monkeypatch, change, error):
    root, _source = environment
    calendar = json.loads(protocol.CALENDAR_PATH.read_bytes())
    calendar.update(change)
    path = tmp_path / "calendar.json"
    path.write_bytes(canonical_json_bytes(calendar))
    monkeypatch.setattr(protocol, "CALENDAR_PATH", path)
    with pytest.raises(ValueError, match=error):
        create(root)
    assert not (root / "fixed" / "plan.json").exists()


def test_signal_receipt_cannot_exist_before_plan_initialization(environment):
    root, _source = environment
    (root / "fixed").mkdir(parents=True)
    (root / "fixed" / "receipt-2026-09-21.json").write_text("{}")
    with pytest.raises(ValueError, match="uninitialized"):
        create(root)


def test_nonprefix_receipt_files_are_rejected(environment, monkeypatch):
    root, _source = environment
    create(root)
    clock(monkeypatch, "2026-09-21T17:10:00+08:00")
    protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload())
    (root / "fixed" / "receipt-2026-09-21.json").rename(root / "fixed" / "receipt-2026-09-22.json")
    with pytest.raises(ValueError, match="incomplete or unexpected"):
        protocol.read_strategy_prospective_receipts(root, "fixed")


def test_backward_clock_between_validation_and_recording_rejected(environment, monkeypatch):
    root, _source = environment
    create(root)
    timestamps = iter([datetime(2026, 9, 21, tzinfo=UTC), datetime(2026, 9, 18, tzinfo=UTC)])
    monkeypatch.setattr(protocol, "utc_now", lambda: next(timestamps))
    with pytest.raises(ValueError, match="clock moved backwards"):
        protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload())


def test_oversized_payload_and_invalid_reader_flags_rejected(environment, monkeypatch):
    root, _source = environment
    create(root)
    monkeypatch.setattr(protocol, "MAX_RECEIPT_BYTES", 10)
    with pytest.raises(ValueError, match="payload is oversized"):
        protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload())
    for reader in (protocol.read_strategy_prospective_plan, protocol.read_strategy_prospective_receipts, protocol.read_strategy_prospective_state):
        with pytest.raises(ValueError, match="must be boolean"):
            reader(root, "fixed", verify_code=1)


def test_modified_lock_file_and_reserved_roots_fail_closed(environment):
    root, _source = environment
    create(root)
    (root / "fixed" / ".writer.lock").write_bytes(b"replaced")
    with pytest.raises(ValueError, match="lock must be empty"):
        protocol.read_strategy_prospective_plan(root, "fixed")
    with pytest.raises(ValueError, match="path is unsafe"):
        create(root / ".git")


@pytest.mark.parametrize("kind", ["empty", "symlink", "missing_directory"])
def test_source_manifest_cannot_omit_source_trees(environment, tmp_path, kind):
    root, source = environment
    if kind == "empty":
        (source / "app" / "example.py").unlink()
        (source / "tools" / "example.py").unlink()
    elif kind == "symlink":
        (source / "app" / "linked_package").symlink_to(tmp_path, target_is_directory=True)
    else:
        (source / "tools" / "example.py").unlink()
        (source / "tools").rmdir()
    with pytest.raises(ValueError, match="source (manifest|directory)"):
        create(root)


def test_calendar_encoding_has_explicit_size_budget(monkeypatch):
    monkeypatch.setattr(storage, "MAX_PLAN_BYTES", 3)
    with pytest.raises(ValueError, match="oversized"):
        storage.encode_calendar(b"four")
    with pytest.raises(ValueError, match="encoding is invalid"):
        storage.decode_calendar("aaaaaaaa", "0" * 64)


def test_due_at_market_close_and_missing_payload_cannot_claim_source(environment, monkeypatch):
    root, _source = environment
    create(root)
    clock(monkeypatch, "2026-09-21T15:15:00+08:00")
    assert protocol.strategy_prospective_status(root, "fixed")["status"] == "collection_due"
    clock(monkeypatch, "2026-09-21T20:00:01+08:00")
    for changes in ({"available_at": "2026-09-21T17:00:00+08:00"}, {"reason": {"message": "missing"}}):
        with pytest.raises(ValueError, match="elapsed deadline"):
            protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", {**missing(), **changes})


def test_state_reads_one_verified_prefix_when_a_writer_appends_during_read(environment, monkeypatch):
    root, _source = environment
    plan = create(root)
    clock(monkeypatch, "2026-09-21T17:10:00+08:00")
    validate = protocol._validate_plan
    appended = False

    def append_after_validation(*args, **kwargs):
        nonlocal appended
        validate(*args, **kwargs)
        if not appended:
            appended = True
            protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload())

    monkeypatch.setattr(protocol, "_validate_plan", append_after_validation)
    frozen_plan, receipts = protocol.read_strategy_prospective_state(root, "fixed")
    assert frozen_plan == plan and receipts == ()
    assert len(protocol.read_strategy_prospective_state(root, "fixed")[1]) == 1


def test_live_calendar_axis_drift_stops_capture_but_preserves_frozen_history(environment, monkeypatch):
    root, _source = environment
    plan = create(root)
    live_dates = protocol.trading_dates_between
    monkeypatch.setattr(protocol, "trading_dates_between", lambda *a, **kw: tuple(day for day in live_dates(*a, **kw) if day != date(2026, 9, 22)))
    with pytest.raises(protocol.StrategyProspectiveCalendarError, match="session axis"):
        protocol.read_strategy_prospective_plan(root, "fixed")
    assert protocol.read_strategy_prospective_plan(root, "fixed", verify_code=False) == plan
    status = protocol.strategy_prospective_status(root, "fixed")
    assert status["status"] == "calendar_drift" and status["calendar_matches_frozen"] is False
    clock(monkeypatch, "2026-09-21T17:10:00+08:00")
    with pytest.raises(protocol.StrategyProspectiveCalendarError):
        protocol.append_strategy_prospective_receipt(root, "fixed", "2026-09-21", payload())


def test_live_calendar_extension_cannot_make_a_frozen_unknown_exit_mature(environment, monkeypatch):
    root, _source = environment
    plan = create(root)
    final = plan["specification"]["signal_dates"][-1]
    live_future = protocol.next_trade_dates

    def extended(day, count, **kwargs):
        if day.isoformat() == final:
            return tuple(day + timedelta(days=offset) for offset in range(1, count + 1))
        return live_future(day, count, **kwargs)

    monkeypatch.setattr(protocol, "next_trade_dates", extended)
    with pytest.raises(protocol.StrategyProspectiveCalendarError, match="coverage boundary"):
        protocol.read_strategy_prospective_state(root, "fixed")
    frozen = protocol.read_strategy_prospective_plan(root, "fixed", verify_code=False)
    assert frozen["specification"]["exit_dates"][final] is None
    assert protocol.strategy_prospective_status(root, "fixed")["matureable_anchor_count"] == 6


@pytest.mark.parametrize("function,error", [
    ("trading_dates_between", "prospective range"), ("next_trade_dates", "holding path"),
])
def test_unavailable_live_calendar_fails_before_plan_publication(environment, monkeypatch, function, error):
    root, _source = environment

    def unavailable(*_args, **_kwargs):
        raise protocol.TradingCalendarCoverageError("synthetic unavailable calendar")

    monkeypatch.setattr(protocol, function, unavailable)
    with pytest.raises(protocol.StrategyProspectiveCalendarError, match=error):
        create(root)
    assert not (root / "fixed" / "plan.json").exists()


def test_each_live_next_session_path_must_match_original_calendar(environment, monkeypatch):
    root, _source = environment
    create(root, horizon=1, end_date="2026-09-30")
    live_future = protocol.next_trade_dates
    monkeypatch.setattr(protocol, "next_trade_dates", lambda *a, **kw: tuple(reversed(live_future(*a, **kw))))
    with pytest.raises(protocol.StrategyProspectiveCalendarError, match="holding path"):
        protocol.read_strategy_prospective_state(root, "fixed")
