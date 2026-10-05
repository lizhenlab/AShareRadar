"""Preserved inputs, basket identity and report prefixes remain inseparable."""

from contextlib import closing
from datetime import datetime
import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services import strategy_prospective_outcomes as outcomes
from app.services import strategy_prospective_plan as plans
from app.services import strategy_prospective_replay as replay
from app.services import strategy_prospective_snapshot as snapshots
from app.services.strategy_prospective_store import prospective_record
from app.services.trading_calendar import ASHARE_TIMEZONE
from tests.test_strategy_prospective_collection import _collect, _receipts, study as _study_fixture
from tests.test_strategy_template_tracking import _clone_run


@pytest.fixture
def study(tmp_path, monkeypatch):
    return _study_fixture.__wrapped__(tmp_path, monkeypatch)


def _replace_payload(study, payload):
    receipt = dict(_receipts(study)[0])
    receipt.pop("digest")
    receipt["payload"] = payload
    replaced = prospective_record(receipt)
    path = study[1] / "study" / f"receipt-{receipt['trade_date']}.json"
    path.write_bytes(canonical_json_bytes(replaced))
    return replaced


def _archive_records(study, payload):
    path = study[1] / "study" / payload["snapshot_path"]
    return [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]


def _replace_archive(study, payload, records):
    raw = b"".join(canonical_json_bytes(row) + b"\n" for row in records)
    encoded = gzip.compress(raw, mtime=0)
    digest = sha256_hex(encoded)
    name = f"snapshot-2026-07-17-{digest}.json.gz"
    (study[1] / "study" / name).write_bytes(encoded)
    payload.update(snapshot_path=name, snapshot_file_sha256=digest, snapshot_raw_bytes_sha256=sha256_hex(raw),
                   snapshot_byte_size=len(encoded), snapshot_raw_byte_size=len(raw), snapshot_result_count=len(records) - 1)
    return _replace_payload(study, payload)


def test_valid_archive_replays_without_operational_database_or_forward_table(study, monkeypatch):
    _collect(study)
    original = _receipts(study)[0]["payload"]["session"]
    study[0][0].unlink()
    connect, reads = sqlite3.connect, []
    def opened(*args, **kwargs):
        assert args == (":memory:",)
        conn = connect(*args, **kwargs)
        def authorize(action, table, *_rest):
            if action == sqlite3.SQLITE_READ:
                reads.append(table)
                if table == "kline_daily":
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn
    monkeypatch.setattr(replay.sqlite3, "connect", opened)
    session, = outcomes.prospective_captured_sessions(study[1], "study")
    assert session.run_id == original["run_id"]
    assert "market_scan_result" in reads and "kline_daily" not in reads
    assert all(position.forward_return is None for selection in session.selections for position in selection.positions)


@pytest.mark.parametrize("removed", ["all", "snapshot_path", "snapshot_result_count", "snapshot_raw_bytes_sha256", "snapshot_byte_size"])
def test_captured_receipt_cannot_omit_original_archive_proof(study, removed):
    _collect(study)
    payload = dict(_receipts(study)[0]["payload"])
    for key in list(payload):
        if key == removed or removed == "all" and key.startswith("snapshot_"):
            payload.pop(key)
    _replace_payload(study, payload)
    with pytest.raises(ValueError):
        outcomes.prospective_captured_sessions(study[1], "study")


def test_genuine_archive_from_other_run_cannot_prove_recorded_basket(study):
    _collect(study)
    payload = dict(_receipts(study)[0]["payload"])
    database, root, _clock = study
    second = _clone_run(database)
    with closing(sqlite3.connect(database[0])) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        row = conn.execute("""SELECT r.*,c.production_score_spec_hash AS declared_score_spec_hash
            FROM market_scan_run r LEFT JOIN market_scan_rule_contract c ON c.rule_version=r.rule_version WHERE r.id=?""", (second,)).fetchone()
        payload.update(snapshots.archive_strategy_scan(conn, row, root / "study"))
    _replace_payload(study, payload)
    with pytest.raises(ValueError, match="identity mismatch"):
        outcomes.prospective_captured_sessions(root, "study")


@pytest.mark.parametrize("field,value", [("target_weight", 0.99), ("signal_price", 1.0), ("symbol", "sz000001")])
def test_valid_source_cannot_be_attached_to_a_different_basket(study, field, value):
    _collect(study)
    payload = dict(_receipts(study)[0]["payload"])
    payload["session"]["selections"][0]["positions"][0][field] = value
    _replace_payload(study, payload)
    with pytest.raises(ValueError, match="archived source replay"):
        outcomes.prospective_captured_sessions(study[1], "study")


def test_basket_from_another_cash_budget_cannot_be_used_in_frozen_plan(study):
    _collect(study)
    receipt = _receipts(study)[0]
    spec = plans.read_strategy_prospective_plan(study[1], "study")["specification"]
    changed = {**spec, "notional_cash_cny": 2_000_000}
    with pytest.raises(ValueError, match="archived source replay"):
        replay.replay_prospective_captured_session(study[1] / "study", changed, receipt)


def test_rows_cannot_be_removed_even_with_recomputed_archive_and_receipt_hashes(study):
    _collect(study)
    payload = dict(_receipts(study)[0]["payload"])
    records = _archive_records(study, payload)
    records.pop()
    _replace_archive(study, payload, records)
    with pytest.raises(ValueError, match="snapshot_seal_invalid"):
        outcomes.prospective_captured_sessions(study[1], "study")


@pytest.mark.parametrize("field,value", [("quote_date", "2026-07-16"), ("data_date", "2026-07-16"), ("rule_version", "other-rule")])
def test_rehashed_header_stays_bound_to_receipt_dates_and_rule(study, field, value):
    _collect(study)
    payload = dict(_receipts(study)[0]["payload"])
    records = _archive_records(study, payload)
    records[0]["run"][field] = value
    _replace_archive(study, payload, records)
    with pytest.raises(ValueError):
        outcomes.prospective_captured_sessions(study[1], "study")


def test_duplicate_symbol_cannot_replace_full_result_set(study):
    _collect(study)
    payload = dict(_receipts(study)[0]["payload"])
    records = _archive_records(study, payload)
    records[-1] = records[1]
    _replace_archive(study, payload, records)
    with pytest.raises(ValueError, match="duplicate"):
        outcomes.prospective_captured_sessions(study[1], "study")


@pytest.mark.parametrize("mutate", ["column", "value"])
def test_archived_source_cannot_inject_sql_or_non_scalar_values(study, mutate):
    _collect(study)
    payload = dict(_receipts(study)[0]["payload"])
    records = _archive_records(study, payload)
    if mutate == "column":
        records[0]["run"]['id); DROP TABLE market_scan_run;--'] = 1
    else:
        records[0]["run"]["message"] = {"not": "sql"}
    _replace_archive(study, payload, records)
    with pytest.raises(ValueError, match="invalid SQL columns|non-SQL"):
        outcomes.prospective_captured_sessions(study[1], "study")


def _append_next_missing(study):
    _database, root, clock = study
    day = plans.read_strategy_prospective_plan(root, "study")["specification"]["signal_dates"][1]
    clock["now"] = datetime.fromisoformat(f"{day}T21:00:00+08:00")
    plans.append_strategy_prospective_receipt(root, "study", day, {
        "status": "missing", "session": None, "available_at": None, "reason": "not_captured_before_deadline",
    })


@pytest.mark.parametrize("kind", ["outcomes", "evidence"])
def test_report_uses_one_prefix_even_when_next_receipt_commits_during_calculation(study, monkeypatch, kind):
    _collect(study)
    first = _receipts(study)[0]
    as_of = study[2]["now"].isoformat()
    if kind == "outcomes":
        def evaluate(*_args, **kwargs):
            assert kwargs["as_of"].isoformat() == as_of
            _append_next_missing(study)
            return {"cohorts": []}
        monkeypatch.setattr(outcomes, "evaluate_strategy_template_net_returns", evaluate)
        result = outcomes.evaluate_prospective_execution(study[1], "study")
    else:
        def collect(*_args, **kwargs):
            assert kwargs["as_of"].isoformat() == as_of
            _append_next_missing(study)
            return {"status": "unavailable"}
        monkeypatch.setattr(outcomes, "collect_strategy_execution_research", collect)
        result = outcomes.collect_prospective_execution_evidence(study[1], "study", None)
    report = json.loads(Path(result["report_path"]).read_text())
    assert len(_receipts(study)) == 2
    assert report["head_digest"] == first["digest"] and report["receipt_count"] == 1
    assert report["as_of"] == report["generated_at"] == as_of
    assert report["source_statuses"] == [{key: first[key] for key in ("trade_date", "status", "digest")}]


def test_empty_prefix_is_bound_to_plan_head(study):
    _database, root, clock = study
    clock["now"] = datetime(2026, 7, 16, 17, tzinfo=ASHARE_TIMEZONE)
    result = outcomes.evaluate_prospective_execution(root, "study")
    report = json.loads(Path(result["report_path"]).read_text())
    assert report["head_digest"] == report["plan_digest"]
    assert report["receipt_count"] == 0 and report["source_statuses"] == []


@pytest.mark.parametrize("key,value", [
    ("snapshot_result_count", 0), ("snapshot_raw_bytes_sha256", "0" * 64),
    ("snapshot_raw_byte_size", 1), ("snapshot_byte_size", True), ("snapshot_path", "../snapshot-forged.json.gz"),
])
def test_archive_proof_fields_are_required_exact_and_bounded(study, key, value):
    _collect(study)
    payload = dict(_receipts(study)[0]["payload"])
    payload[key] = value
    _replace_payload(study, payload)
    with pytest.raises(ValueError):
        outcomes.prospective_captured_sessions(study[1], "study")


@pytest.mark.parametrize("mutation", ["forward_label", "row_run", "counts", "row_count", "gzip"])
def test_consistent_outer_hash_does_not_bypass_raw_archive_validation(study, mutation):
    _collect(study)
    payload = dict(_receipts(study)[0]["payload"])
    records = _archive_records(study, payload)
    if mutation == "forward_label":
        records[0]["forward_prices_read"] = True
    elif mutation == "row_run":
        records[1]["row"]["run_id"] += 1
    elif mutation == "counts":
        payload["source_result_counts"]["skipped_count"] += 1
    _replace_archive(study, payload, records)
    if mutation == "row_count":
        payload["snapshot_result_count"] += 1
    elif mutation == "gzip":
        encoded = b"not gzip"
        digest = sha256_hex(encoded)
        name = f"snapshot-2026-07-17-{digest}.json.gz"
        (study[1] / "study" / name).write_bytes(encoded)
        payload.update(snapshot_path=name, snapshot_file_sha256=digest, snapshot_byte_size=len(encoded))
    _replace_payload(study, payload)
    with pytest.raises(ValueError):
        outcomes.prospective_captured_sessions(study[1], "study")
