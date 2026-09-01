from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from app.artifacts.io import ArtifactContentConflictError, canonical_json_bytes, exclusive_atomic_publish, sha256_hex
from app.services.choice_research import DAILY_FIELDS, REFERENCE_FIELDS, make_plan, normalize, request
from app.services.choice_research_audit import audit_choice_research, publish_choice_audit
from app.services.choice_research_collect import ChoiceCollector
from app.services.choice_research_store import ChoiceBudget, ChoiceDataset
from app.services.choice_research_supplement import ChoiceSupplementCollector, make_supplement_plan
from app.services.choice_research_universe import ChoiceUniverseCollector, make_universe_plan
from app.services.choice_sdk import ChoiceError
from tools import audit_choice_research as command


SYMBOLS = ["600519.SH", "000001.SZ", "920006.BJ"]
SESSIONS = ["2026-08-20", "2026-08-21", "2026-08-24"]
CAPTURED = datetime.fromisoformat("2026-08-26T12:00:00+08:00")
AUDITED = datetime.fromisoformat("2026-08-26T20:47:15+08:00")


def _quota_response(csd=500000, css=500000, ctr=10):
    fields = ["FUNCENAME", "SECUTYPE", "PERIOD", "STARTDATE", "ENDDATE", "THRESHOLD",
              "USEDDATA", "AVAILABEDATA", "EFFECTIVEDATE"]
    return {"error_code": 0, "indicators": fields, "data": {
        str(index): [function, kind, "W", "2026-08-24", "2026-08-30", str(threshold),
                     str(threshold - units), str(units), "2026-09-08 23:59:59"]
        for index, (function, kind, threshold, units) in enumerate([
            ("EM_CSD", "全品种", 500000, csd), ("EM_CSS", "全品种", 500000, css),
            ("EM_CTR", "分红送转", 10, ctr),
        ])
    }}


def _row(symbol, day):
    first, second = SESSIONS[:2]
    result = {"OPEN": 10.0, "HIGH": 11.0, "LOW": 9.0, "CLOSE": 10.5, "VOLUME": 1000, "AMOUNT": 10000,
              "PRECLOSE": 10.0, "TURN": 1.0, "TRADESTATUS": "正常交易", "HIGHLIMIT": "否", "LOWLIMIT": "否",
              "ISSTSTOCK": "否", "ISXSTSTOCK": "否", "TAFACTOR": 1.0,
              "NAME": "retrospective name", "HISNAME": "retrospective historical name", "STATUS": "L",
              "LISTDATE": second if symbol.endswith("BJ") else first,
              "PRECLOSEEXCH": 10.0, "LIMITUPPRICE": 11.0, "LIMITDOWNPRICE": 9.0}
    if symbol.endswith("SH") and day in {first, second} or symbol.endswith("BJ") and day == second:
        result.update(LIMITUPPRICE=None, LIMITDOWNPRICE=None)
    if symbol.endswith("SH") and day == second:
        result.update(TRADESTATUS="盘中停牌", SUSPENDREASON="交易异常波动", SUSPENDSDATE=day, SUSPENDEDATE=day)
    if symbol.endswith("SZ") and day == first:
        result.update(TRADESTATUS="连续停牌", VOLUME=0, AMOUNT=0, PRECLOSEEXCH=0, LIMITUPPRICE=0, LIMITDOWNPRICE=0,
                      SUSPENDREASON="停牌", SUSPENDSDATE=day, SUSPENDEDATE=day)
    if symbol.endswith("BJ") and day == first:
        result.update(TRADESTATUS="未上市", OPEN=None, HIGH=None, LOW=None, CLOSE=None, VOLUME=None, AMOUNT=None,
                      PRECLOSEEXCH=None, LIMITUPPRICE=None, LIMITDOWNPRICE=None)
    return result


class ArchivedFixtureClient:
    """Only fixture construction uses this in-memory client; the audit never does."""

    def request(self, method, args):
        result = {"error_code": 0, "codes": [], "indicators": [], "dates": [], "data": {}}
        if method == "datastatistics":
            return _quota_response()
        if method == "tradedates":
            result["dates"] = SESSIONS
        elif method == "sector":
            result.update(codes=SYMBOLS, indicators=["SECUCODE", "SECURITYSHORTNAME"], dates=[args[1]],
                          data=[value for symbol in SYMBOLS for value in (symbol, "retrospective name")])
        elif method == "csd":
            result.update(indicators=DAILY_FIELDS, dates=SESSIONS,
                          data={symbol: [[_row(symbol, day)[field] for day in SESSIONS] for field in DAILY_FIELDS]
                                for symbol in args[0].split(",")})
        elif method == "css":
            fields = args[1].split(",")
            day = args[2].split("TradeDate=", 1)[1].split(",")[0] if "TradeDate=" in args[2] else SESSIONS[-1]
            result.update(indicators=fields, dates=[day] if "TradeDate=" in args[2] else [],
                          data={symbol: [_row(symbol, day).get(field) for field in fields] for symbol in args[0].split(",")})
        else:
            raise AssertionError(method)
        return result


@pytest.fixture
def archives(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.choice_research_store.market_now", lambda: CAPTURED)
    monkeypatch.setattr("app.services.choice_research_collect.market_now", lambda: CAPTURED)
    paths = [tmp_path / name for name in ("history", "supplement", "universe", "control")]
    client = ArchivedFixtureClient()
    with ChoiceBudget(paths[3]) as budget, ChoiceDataset(paths[0], make_plan(SESSIONS[0], SESSIONS[-1], 3, [], [])) as base:
        ChoiceCollector(base, client, budget).run()
        with ChoiceDataset(paths[1], make_supplement_plan(base, recent_sessions=1, event_limit=0)) as supplement:
            ChoiceSupplementCollector(supplement, client, budget).run()
            with ChoiceDataset(paths[2], make_universe_plan(base, [supplement])) as universe:
                ChoiceUniverseCollector(universe, client, budget).run()
        budget.reserve("css", 180)
    return paths


def _tree_snapshot(paths):
    return {str(path): (path.stat().st_mtime_ns, sha256_hex(path.read_bytes()))
            for root in paths for path in root.rglob("*") if path.is_file()}


def _audit(paths):
    return audit_choice_research(*paths, audited_at=AUDITED)


def _snapshot_file(directory, value, prefix=""):
    encoded = canonical_json_bytes(value)
    path = directory / f"{prefix}{sha256_hex(encoded)}.json"
    exclusive_atomic_publish(path, encoded, max_bytes=1024 * 1024)
    return path


def test_audit_explains_listing_and_halt_observations_without_mutation_or_sdk(archives, monkeypatch):
    before = _tree_snapshot(archives)
    forbidden = Mock(side_effect=AssertionError("audit attempted SDK or mutation"))
    monkeypatch.setattr("app.services.choice_sdk.ChoiceSDKClient.__enter__", forbidden)
    monkeypatch.setattr("app.services.choice_sdk.ChoiceSDKClient.request", forbidden)
    monkeypatch.setattr("app.services.choice_research_store.ChoiceBudget.__enter__", forbidden)
    monkeypatch.setattr("app.services.choice_research_store.ChoiceDataset.__init__", forbidden)
    result = _audit(archives)
    assert _tree_snapshot(archives) == before
    forbidden.assert_not_called()
    assert result["read_only"] and result["raw_replay_verified"]
    assert not result["official"] and not result["formal_equivalent_pit"] and not result["filter_qualified"]
    assert result["production_ranking_effect"] == "none"
    evidence = result["reference_audit"]
    assert evidence["reference_null_or_zero_by_nontrading_state"] == {"not_trading": 1, "suspended": 1}
    assert evidence["priority_anomaly_count"] == evidence["offline_explained_count"] == 3
    assert evidence["priority_quality_counts"] == {"traded_bar_not_execution_proof": 2, "unknown_or_intraday_restricted_state": 1}
    assert evidence["missing_daily_pairs"] == evidence["missing_reference_pairs"] == []
    assert result["universe_coverage"]["universe_dates_missing"] == []
    for item in evidence["priority_anomalies"]:
        assert not item["price_limit_regime_verified"] and item["execution_eligible"] is None
        assert item["daily"]["values"]["VOLUME"] > 0
        assert Path(item["reference"]["receipt"]["raw_path"]).is_file()
        assert item["listing_context"]["listing_date_is_retrospectively_reported"] is True
    plan = result["minimal_recheck_plan"]
    assert plan["required_css_requests"] == [] and plan["required_css_cells"] == 0
    assert plan["optional_css_cells"] == 4 and plan["optional_css_spot_checks"][0]["recommended"] is False
    assert plan["csd_paused"] and plan["ctr_paused"] and not plan["execute_automatically"]


def test_audit_preserves_uncertain_units_and_never_claims_settlement(archives):
    _snapshot_file(archives[3] / "account", {"queried_at": AUDITED.isoformat(), "result": _quota_response(100, 1000, 4)})
    result = _audit(archives)
    quota = result["quota_reconciliation"]
    assert quota["exact_request_binding_proven"] is quota["provider_settlement_proven"] is False
    assert quota["reservations_released"] == 0
    css = quota["functions"]["EM_CSS"]
    assert css["reserved_minus_completed_estimated_units"] == 180
    assert css["safe_units_at_snapshot"] == 1000 - css["reserved_units"]
    assert quota["functions"]["EM_CSD"]["status"] == "paused"
    assert quota["functions"]["EM_CSD"]["safe_units_at_snapshot"] == 0
    assert quota["ambiguous_receipt_candidate_count"] > 0
    assert all(not item["exact_request_binding_proven"] and not item["provider_settlement_proven"] for item in quota["reservation_candidates"])
    assert all("request_key" in item and "raw_path" in item for item in quota["receipt_calls"])


def test_known_units_and_server_statistics_never_release_local_reservations():
    from app.services.choice_research_audit import _function_quota, _quota_rows
    first = {"path": "first.json", "queried_at": CAPTURED.isoformat(), "quota": _quota_rows({"result": _quota_response(499922, 499947, 8)})}
    latest = {"path": "last.json", "queried_at": AUDITED.isoformat(), "quota": _quota_rows({"result": _quota_response(359082, 392783, 4)})}
    rows = [{"period": "2026-08-24/2026-08-30", "function": function, "units": units, "created_at": CAPTURED.isoformat()}
            for function, units in [("EM_CSD", 421680), ("EM_CSS", 107216), ("EM_CTR", 6)]]
    calls = [{"function": row["function"], "captured_at": row["created_at"], "outcome": "completed_raw_replay_verified",
              "estimated_units": row["units"] - (180 if row["function"] == "EM_CSS" else 0)} for row in rows]
    results = {name: _function_quota(name, rows, calls, [first, latest], AUDITED) for name in ["EM_CSD", "EM_CSS", "EM_CTR"]}
    assert results["EM_CSD"]["first_to_latest_reported_decrease"] == 140840
    assert results["EM_CSD"]["completed_receipt_estimated_units"] == 421680
    assert results["EM_CSD"]["safe_units_at_snapshot"] == 0
    assert results["EM_CSS"]["safe_units_at_snapshot"] == 92784
    assert results["EM_CSS"]["reserved_minus_completed_estimated_units"] == 180
    assert results["EM_CTR"]["first_to_latest_reported_decrease"] == 4
    assert results["EM_CTR"]["reserved_units"] == 6 and results["EM_CTR"]["safe_units_at_snapshot"] == 0
    assert all(not item["provider_settlement_proven"] for item in results.values())


def test_missing_latest_quota_does_not_fall_back_to_an_older_package(archives):
    response = _quota_response()
    response["data"] = {"0": response["data"]["0"]}
    _snapshot_file(archives[3] / "account", {"queried_at": AUDITED.isoformat(), "result": response})
    quota = _audit(archives)["quota_reconciliation"]["functions"]["EM_CSS"]
    assert quota["status"] == "paused_missing_quota" and quota["safe_units_at_snapshot"] == 0


@pytest.mark.parametrize("status", [None, "unknown", "连续停牌"])
def test_listing_window_does_not_explain_unknown_or_contradictory_trading_state(status):
    from app.services.choice_research_audit import _anomaly
    bar = {"values": {**_row(SYMBOLS[0], SESSIONS[0]), "TRADESTATUS": status, "quality": "unknown_or_intraday_restricted_state"}}
    result = _anomaly(SYMBOLS[0], SESSIONS[0], bar, None, None, {"initial_gap_sessions": [SESSIONS[0]]})
    assert result["archive_pattern_explained"] is False and result["execution_eligible"] is None


@pytest.mark.parametrize("target", ["raw", "projection", "capture_timestamp", "source_binding", "account_hash"])
def test_tampered_evidence_fails_closed_before_claiming_a_verified_audit(archives, target):
    if target == "raw":
        path = next((archives[0] / "raw").glob("*.json"))
        data = json.loads(path.read_text())
        data["payload"]["captured_at"] = "2000-01-01T00:00:00+08:00"
        path.write_text(json.dumps(data))
    elif target in {"projection", "capture_timestamp"}:
        with sqlite3.connect(archives[0] / "choice_research.sqlite3") as db:
            if target == "projection":
                db.execute("UPDATE records SET payload_json='{}' WHERE kind='daily'")
            else:
                db.execute("UPDATE requests SET captured_at='2000-01-01T00:00:00+08:00'")
    elif target == "source_binding":
        path = archives[1] / "plan.json"
        data = json.loads(path.read_text())
        data["source_receipts_sha256"] = "0" * 64
        path.write_text(json.dumps(data))
    else:
        path = next((archives[0] / "account").glob("*.json"))
        path.write_text("{}")
    before = _tree_snapshot(archives)
    with pytest.raises(ChoiceError):
        _audit(archives)
    assert _tree_snapshot(archives) == before


def test_orphan_valid_response_is_reported_without_projection_or_releasing_budget(archives):
    descriptor = request("execution_reference", "css", [SYMBOLS[0], ",".join(REFERENCE_FIELDS), f"TradeDate={SESSIONS[0]},{'RECVtimeout=15'}"],
                         as_of=SESSIONS[0], symbols=[SYMBOLS[0]], fields=REFERENCE_FIELDS)
    with ChoiceDataset.open_readonly(archives[1]) as dataset:
        payload = dataset.archive(descriptor, {"error_code": 0, "indicators": REFERENCE_FIELDS, "dates": [SESSIONS[0]],
                                               "data": {SYMBOLS[0]: [10, None, None]}})
        assert normalize(payload)
    before = _tree_snapshot(archives)
    result = _audit(archives)
    assert _tree_snapshot(archives) == before
    orphan = result["quota_reconciliation"]["orphan_raw_responses"]
    assert len(orphan) == 1 and orphan[0]["outcome"] == "unprojected_valid_response"
    assert result["reservations_released"] == 0


def test_unexplained_reference_gap_requires_only_a_minimal_css_check(archives):
    # A coherent, fully replayable archive can still have an unexplained source
    # gap after the listing window. Rebuild only this synthetic fixture response.
    with ChoiceDataset.open_readonly(archives[0]) as dataset:
        row = dataset.db.execute("SELECT descriptor_json FROM requests WHERE json_extract(descriptor_json,'$.kind')='metadata' AND json_extract(descriptor_json,'$.as_of')=?", (SESSIONS[-1],)).fetchone()
        descriptor = json.loads(row[0])
        key = dataset.key(descriptor)
        payload = dataset.cached(descriptor)
    payload["result"]["data"][SYMBOLS[1]][descriptor["fields"].index("LIMITUPPRICE")] = None
    raw = canonical_json_bytes({"payload": payload, "sha256": sha256_hex(canonical_json_bytes(payload))})
    (archives[0] / "raw" / f"{key}.json").write_bytes(raw)
    records = normalize(payload)
    with sqlite3.connect(archives[0] / "choice_research.sqlite3") as db:
        db.execute("UPDATE requests SET raw_sha256=?,records_digest=? WHERE request_key=?", (sha256_hex(raw), sha256_hex(canonical_json_bytes([list(record) for record in records])), key))
        for ordinal, record in enumerate(records):
            db.execute("UPDATE records SET payload_json=? WHERE request_key=? AND ordinal=?", (canonical_json_bytes(record[3]).decode(), key, ordinal))
    # Updated derived bindings are intentionally not forged. Test the reference
    # classification helper after the existing raw replay has verified the fixture.
    from app.services.choice_research_audit import _reference_audit, _verify_dataset
    with ChoiceDataset.open_readonly(archives[0]) as base, ChoiceDataset.open_readonly(archives[1]) as supplement:
        _, receipts = _verify_dataset(base)
        _, extra = _verify_dataset(supplement)
        evidence = _reference_audit(base, supplement, [receipts, extra])
    anomaly = next(item for item in evidence["priority_anomalies"] if item["symbol"] == SYMBOLS[1])
    assert anomaly["offline_explanation"] == "unexplained_reference_gap"
    assert anomaly["archive_pattern_explained"] is False
    assert anomaly["minimal_css_reference_recheck"]["estimated_cells"] == 3
    assert anomaly["minimal_css_reference_recheck"]["request"]["args"][1] == ",".join(REFERENCE_FIELDS)


def test_audit_refuses_wal_archives_symlinks_and_missing_directories(archives, tmp_path):
    alias = tmp_path / "history-alias"
    alias.symlink_to(archives[0], target_is_directory=True)
    with pytest.raises(ChoiceError):
        _audit([alias, *archives[1:]])
    missing = tmp_path / "absent"
    with pytest.raises((ChoiceError, OSError)):
        _audit([missing, *archives[1:]])
    assert not missing.exists()
    with sqlite3.connect(archives[0] / "choice_research.sqlite3") as db:
        db.execute("PRAGMA journal_mode=WAL")
    with pytest.raises(ChoiceError, match="rollback-journal"):
        _audit(archives)


def test_audit_does_not_open_non_content_addressed_account_files(archives, monkeypatch):
    path = archives[0] / "account" / "credentials.json"
    path.write_text("must not be read")
    from app.services import choice_research_audit as audit_module
    original = audit_module.read_regular_file

    def guarded_read(source, **kwargs):
        assert Path(source) != path
        return original(source, **kwargs)

    monkeypatch.setattr(audit_module, "read_regular_file", guarded_read)
    assert _audit(archives)["raw_replay_verified"] is True


def test_report_publication_is_explicit_immutable_and_outside_inputs(archives, tmp_path):
    report = _audit(archives)
    before = _tree_snapshot(archives)
    output = tmp_path / "audit-output"
    saved = publish_choice_audit(report, output, archives)
    assert saved.name == f"choice-research-audit-{sha256_hex(saved.read_bytes())}.json"
    stat = saved.stat()
    assert publish_choice_audit(report, output, archives) == saved
    assert saved.stat().st_mtime_ns == stat.st_mtime_ns
    assert _tree_snapshot(archives) == before
    for target in [archives[0], archives[1] / "reports", archives[3] / "reports", tmp_path / ".git"]:
        with pytest.raises(ChoiceError):
            publish_choice_audit(report, target, archives)
    alias = tmp_path / "alias"
    alias.symlink_to(output, target_is_directory=True)
    with pytest.raises(ChoiceError):
        publish_choice_audit(report, alias, archives)
    saved.write_text("different bytes")
    with pytest.raises(ArtifactContentConflictError):
        publish_choice_audit(report, output, archives)


def test_cli_default_stdout_only_and_optional_full_report(archives, tmp_path, monkeypatch, capsys):
    args = ["audit_choice_research.py", "--audited-at", AUDITED.isoformat(), "--compact"]
    for option, path in zip(["--history-dir", "--supplement-dir", "--universe-dir", "--control-dir"], archives, strict=True):
        args.extend([option, str(path)])
    monkeypatch.setattr("sys.argv", args)
    before = _tree_snapshot(archives)
    assert command.main() == 0
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["read_only"] is True and "report_path" not in stdout
    assert "receipt_calls" not in stdout["quota_reconciliation"]
    assert _tree_snapshot(archives) == before
    output = tmp_path / "reports"
    monkeypatch.setattr("sys.argv", [*args, "--output-dir", str(output)])
    assert command.main() == 0
    saved = Path(json.loads(capsys.readouterr().out)["report_path"])
    assert "receipt_calls" in json.loads(saved.read_text())["quota_reconciliation"]
    assert _tree_snapshot(archives) == before


def test_cli_failure_never_echoes_untrusted_response_or_credentials(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["audit_choice_research.py"])
    monkeypatch.setattr(command, "audit_choice_research", Mock(side_effect=ChoiceError("do not expose arbitrary credential-like input")))
    assert command.main() == 2
    result = json.loads(capsys.readouterr().err)
    assert result["raw_replay_verified"] is False and "error" not in result
