from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from app.artifacts.io import ArtifactContentConflictError, ArtifactIOError, canonical_json_bytes, sha256_hex
from app.services import choice_history_comparison as comparison
from app.services.choice_research import DAILY_FIELDS, OPTIONS, make_plan, normalize, request
from app.services.choice_research_store import ChoiceDataset
from app.services.choice_sdk import ChoiceError, ChoiceSDKClient
from app.services.market_scan_probability_history import ProbabilityHistoryConfig, ProbabilityHistoryError, backfill_market_scan_probability_history
from tests import test_choice_research as choice_fixtures
from tests import test_market_scan_probability_history as tencent_fixtures
from tools import compare_choice_tencent_history as cli


@pytest.fixture(autouse=True)
def no_implicit_provider_calls(monkeypatch):
    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "0")
    monkeypatch.setenv("TRADE_CALENDAR_AUTO_FETCH", "0")
    monkeypatch.setattr(ChoiceSDKClient, "__enter__", lambda self: pytest.fail("comparison must not log into the SDK"))


def _synthetic_series(count=100, *, action=75):
    days = [(date(2024, 1, 1) + timedelta(days=index)).isoformat() for index in range(count)]
    rows, tencent = {}, {}
    cash_offset = (10 + (action - 1) * .2) * (1 - 1 / 1.2)
    for index, day in enumerate(days):
        factor = 1.0 if index < action else 1.2
        close = (10 + index * .2) / factor
        row = {"OPEN": close * .995, "HIGH": close + .5 / factor, "LOW": close - .5 / factor, "CLOSE": close,
               "TAFACTOR": factor, "PRECLOSE": (10 + (index - 1) * .2) / factor,
               "VOLUME": 100000 + index * 100, "AMOUNT": close * 100000, "quality": "traded_bar_not_execution_proof"}
        rows[day] = row
        offset = cash_offset if index < action else 0.0
        prices = {name: row[name.upper()] - offset for name in ("open", "high", "low", "close")}
        tencent[day] = comparison._Bar(date=day, **prices, volume=row["VOLUME"] / 100)
    return {"920006.BJ": rows}, {"920006.BJ": tencent}, days


@pytest.fixture
def archived_choice(tmp_path, monkeypatch):
    days = list(tencent_fixtures.trusted_probability_history_dates(tencent_fixtures.ANCHOR_DATE, 90))
    monkeypatch.setattr(choice_fixtures, "SESSIONS", days)
    descriptor = request("daily", "csd", [",".join(choice_fixtures.SYMBOLS), ",".join(DAILY_FIELDS), days[0], days[-1],
        f"Period=1,AdjustFlag=1,FillData=0,Order=1,{OPTIONS}"], symbols=choice_fixtures.SYMBOLS, fields=DAILY_FIELDS, sessions=days)
    directory = tmp_path / "choice-source"
    plan = make_plan(days[0], days[-1], 3, [], [])
    with ChoiceDataset(directory, plan) as source:
        payload = source.archive(descriptor, choice_fixtures.daily_response())
        source.project(payload, normalize(payload))
    return directory, descriptor, days


@pytest.fixture
def archives(archived_choice, tmp_path):
    source, _, _ = archived_choice
    symbols = tuple(choice_fixtures.SYMBOLS)
    dummy_source = tencent_fixtures._source_database(tmp_path / "fake-input.sqlite3", symbols)
    days = tencent_fixtures.trusted_probability_history_dates(tencent_fixtures.ANCHOR_DATE)
    built = asyncio.run(backfill_market_scan_probability_history(
        dummy_source, tmp_path / "tencent-source" / "history.sqlite3", tmp_path / "tencent-manifests",
        config=ProbabilityHistoryConfig(symbol_limit=3, symbols=symbols, minimum_symbols_per_market=1, minimum_symbols_total=3),
        provider=tencent_fixtures.FakeHistoryProvider(days), generated_at="2026-08-11T09:00:00+00:00",
    ))
    return source, built.database_path, built.manifest_path


def test_forward_factor_ratio_matches_preclose_and_inverse_does_not():
    choice, _, days = _synthetic_series()
    result = comparison.factor_reference_comparison(choice, days)
    assert result["valid_adjacent_pairs"] == 99 and result["factor_changes"] == 1
    assert result["change_market_counts"] == {"BJ": 1}
    assert result["abs_ratio_error_ppm_changed"]["max"] < 1e-8
    assert result["abs_inverse_ratio_error_ppm_changed"]["min"] > 100000
    assert result["abs_price_residual_yuan_all"]["max"] < 1e-12
    assert "not_native_qfq" in result["interpretation"]


def test_factor_comparison_does_not_bridge_missing_reference_days():
    choice, _, days = _synthetic_series()
    del choice["920006.BJ"][days[74]]
    choice["920006.BJ"][days[40]]["PRECLOSE"] = None
    result = comparison.factor_reference_comparison(choice, days)
    assert result["valid_adjacent_pairs"] == 96 and result["factor_changes"] == 0


def test_all_eleven_features_cancel_positive_anchor_scale_and_ignore_future():
    choice, _, days = _synthetic_series()
    before = deepcopy(choice)
    result = comparison.scale_invariance_comparison(choice, days)
    assert result["valid_fixed_session_windows"] == 80 and result["invalid_or_restricted_windows"] == 0
    assert result["tested_windows"] == 4 and len(result["feature_names"]) == 11
    assert max(result["last_traded_anchor_vs_signal_anchor_max_abs_error"].values()) < 1e-12
    assert result["appended_future_bar_max_abs_error"] == 0
    assert "not_pit_or_cost_label_invariance" in result["conclusion_scope"]
    assert choice == before


def test_additive_prices_change_features_even_when_all_direction_labels_agree():
    choice, tencent, days = _synthetic_series()
    result = comparison._shared_comparison(choice, tencent, days)
    symbol = result["symbols"][0]
    assert symbol["anchor"]["date"] == days[-1] and symbol["anchor"]["tencent_price_multiplier"] == 1
    assert symbol["ohlc_abs_yuan"]["max"] > .5
    assert result["feature_absolute_differences"]["close_return_20d"]["max"] > .001
    assert symbol["feature_windows"]["compared_fixed_session_windows"] == 80
    assert [symbol["direction_labels"][str(h)]["compared"] for h in (1, 2, 5)] == [39, 38, 35]
    assert all(symbol["direction_labels"][str(h)]["sign_disagreements"] == 0 for h in (1, 2, 5))
    assert len(symbol["constant_offset_segments"]) == 2
    assert max(row["constant_offset_max_abs_residual"] for row in symbol["constant_offset_segments"]) < 1e-12


def test_volume_unit_conversion_reports_real_scope_difference_without_fixing_it():
    choice, tencent, days = _synthetic_series()
    day = days[70]
    before = choice["920006.BJ"][day]["VOLUME"]
    original = tencent["920006.BJ"][day]
    tencent["920006.BJ"][day] = comparison.replace(original, volume=original.volume * 1.25)
    symbol = comparison._shared_comparison(choice, tencent, days)["symbols"][0]
    assert symbol["choice_volume_over_tencent"]["median"] == 100
    assert symbol["volume_over_100_shares_disagreement_count"] == 1
    assert symbol["volume_over_one_percent_disagreement_count"] == 1
    assert symbol["largest_volume_differences"][0]["relative_difference_pct"] == pytest.approx(-20)
    assert choice["920006.BJ"][day]["VOLUME"] == before


@pytest.mark.parametrize("missing_side", ["choice", "tencent"])
def test_fixed_windows_and_horizon_targets_never_shift_over_a_missing_day(missing_side):
    choice, tencent, days = _synthetic_series()
    if missing_side == "choice":
        choice["920006.BJ"][days[70]]["quality"] = "suspended"
    else:
        del tencent["920006.BJ"][days[70]]
    symbol = comparison._shared_comparison(choice, tencent, days)["symbols"][0]
    assert symbol["common_grid_sessions"] == 100 and symbol["compared_sessions"] == 99
    assert symbol["adjacent_session_return_diff_bps"]["count"] == 97
    assert symbol["feature_windows"] == {"compared_fixed_session_windows": 59, "missing_or_restricted_windows": 21}
    assert [symbol["direction_labels"][str(h)]["compared"] for h in (1, 2, 5)] == [17, 16, 13]


@pytest.mark.parametrize("quality", ["suspended", "not_trading", "unknown_or_intraday_restricted_state", "invalid_adjustment_factor"])
def test_unlisted_suspended_and_invalid_rows_are_not_filled_for_scale_windows(quality):
    choice, _, days = _synthetic_series()
    choice["920006.BJ"][days[70]]["quality"] = quality
    result = comparison.scale_invariance_comparison(choice, days)
    assert result["valid_fixed_session_windows"] == 59 and result["invalid_or_restricted_windows"] == 21


def test_readonly_comparison_verifies_real_raw_replay_and_deep_manifest(archives):
    report = comparison.compare_choice_tencent_history(*archives)
    assert report["schema_version"] == comparison.COMPARISON_SCHEMA
    assert report["input_hashes_unchanged"] is True and report["sdk_requests"] == 0
    assert report["choice"]["daily_rows_verified"] == 270
    assert report["choice"]["daily_raw_requests_verified"] == 1
    assert report["tencent"]["deep_manifest_verified"] is True
    assert report["shared_comparison"]["market_counts"] == {"BJ": 1, "SH": 1, "SZ": 1}
    assert report["source_equivalence"] == "not_established" and report["production_ranking_effect"] == "none"
    assert all(report[name] is False for name in ("official", "formal_equivalent_pit", "filter_qualified", "runtime_model_replacement"))
    assert len(report["input_sha256"]) == 5
    for name, digest in report["input_sha256"].items():
        assert sha256_hex(Path(name).read_bytes()) == digest
    assert comparison.compare_choice_tencent_history(*archives) == report


@pytest.mark.parametrize("target", ["record", "timestamp", "count", "raw", "plan", "orphan"])
def test_changed_choice_receipts_fail_closed(archived_choice, target):
    source, descriptor, _ = archived_choice
    if target == "raw":
        (source / "raw" / f"{ChoiceDataset.key(descriptor)}.json").write_text("{}")
    elif target == "plan":
        plan = json.loads((source / "plan.json").read_text())
        plan["official"] = True
        (source / "plan.json").write_bytes(canonical_json_bytes(plan))
    else:
        statements = {
            "record": "UPDATE records SET payload_json='{}' WHERE kind='daily'",
            "timestamp": "UPDATE requests SET captured_at='2000-01-01T00:00:00+08:00'",
            "count": "UPDATE requests SET record_count=record_count+1",
            "orphan": "INSERT INTO records VALUES ('orphan',0,'daily','600519.SH','2099-01-01','{}')",
        }
        with sqlite3.connect(source / "choice_research.sqlite3") as connection:
            connection.execute(statements[target])
    with pytest.raises((ChoiceError, ArtifactIOError, KeyError)):
        comparison._read_choice(source, {})


@pytest.mark.parametrize("change", ["adjustment", "fill", "duplicate"])
def test_changed_request_contract_or_duplicate_daily_observations_fail(archived_choice, change):
    source, descriptor, _ = archived_choice
    with ChoiceDataset.open_readonly(source) as dataset:
        plan, payload = dataset.plan, dataset.cached(descriptor)
    result = deepcopy(payload["result"])
    changed = deepcopy(descriptor)
    if change == "duplicate":
        changed["symbols"] = [choice_fixtures.SYMBOLS[0]]
        changed["args"][0] = changed["symbols"][0]
        result["codes"] = changed["symbols"]
        result["data"] = {changed["symbols"][0]: result["data"][changed["symbols"][0]]}
    else:
        old, new = ("AdjustFlag=1", "AdjustFlag=3") if change == "adjustment" else ("FillData=0", "FillData=1")
        changed["args"][-1] = changed["args"][-1].replace(old, new)
    with ChoiceDataset(source, plan) as dataset:
        raw = dataset.archive(changed, result)
        dataset.project(raw, normalize(raw))
    with pytest.raises(ChoiceError):
        comparison._read_choice(source, {})


def test_tencent_manifest_mutation_and_changes_during_comparison_are_rejected(archives, monkeypatch):
    source, database, manifest = archives
    old = manifest.read_bytes()
    document = json.loads(old)
    document["payload"]["database"]["sha256"] = "0" * 64
    manifest.write_bytes(canonical_json_bytes(document))
    with pytest.raises(ProbabilityHistoryError):
        comparison.compare_choice_tencent_history(*archives)
    manifest.write_bytes(old)
    original = comparison._shared_comparison

    def changed_after_read(*args):
        result = original(*args)
        with (source / "plan.json").open("ab") as handle:
            handle.write(b"\n")
        return result

    monkeypatch.setattr(comparison, "_shared_comparison", changed_after_read)
    with pytest.raises(ChoiceError, match="changed during"):
        comparison.compare_choice_tencent_history(source, database, manifest)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_comparison_refuses_database_sidecars(archived_choice, suffix):
    database = archived_choice[0] / "choice_research.sqlite3"
    Path(str(database) + suffix).write_bytes(b"external writer")
    with pytest.raises(ChoiceError, match="without sidecars"):
        comparison._file_digest(database, database=True)


def test_comparison_refuses_wal_header_aliases_and_protected_paths(archived_choice, tmp_path):
    database = archived_choice[0] / "choice_research.sqlite3"
    content = bytearray(database.read_bytes())
    content[18:20] = b"\x02\x02"
    database.write_bytes(content)
    with pytest.raises(ChoiceError, match="rollback-journal"):
        comparison._file_digest(database, database=True)
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(database)
    with pytest.raises(ChoiceError, match="aliases"):
        comparison._safe_path(alias)
    with pytest.raises(ChoiceError, match="protected"):
        comparison._safe_path(tmp_path / ".workbuddy-ai" / "report.json")


@pytest.mark.parametrize("variable", ["ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "TRADE_CALENDAR_AUTO_FETCH"])
def test_offline_calendar_guard_runs_before_inputs_are_read(monkeypatch, tmp_path, variable):
    monkeypatch.delenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", raising=False)
    monkeypatch.setenv(variable, "1")
    monkeypatch.setattr(comparison, "_file_digest", lambda *args, **kwargs: pytest.fail("guard must precede file reads"))
    with pytest.raises(ChoiceError, match="AUTO_FETCH=0"):
        comparison.compare_choice_tencent_history(tmp_path / "choice", tmp_path / "tencent", tmp_path / "manifest")
    assert os.environ[variable] == "1"


def _arguments(tmp_path):
    return argparse.Namespace(choice_dir=tmp_path / "choice-source", tencent_database=tmp_path / "tencent-source" / "history.sqlite3",
        tencent_manifest=tmp_path / "manifests" / "manifest.json", output_dir=tmp_path / "reports")


def test_output_is_content_addressed_exclusive_and_identical_repeat_is_idempotent(tmp_path):
    args = _arguments(tmp_path)
    report = {"source_equivalence": "not_established", "production_ranking_effect": "none", "read_only": True}
    target = cli._publish_report(report, args)
    content = canonical_json_bytes(report)
    assert target.name == f"choice-tencent-comparison-{sha256_hex(content)}.json"
    assert target.read_bytes() == content and cli._publish_report(report, args) == target
    target.write_bytes(b"existing unrelated bytes")
    with pytest.raises(ArtifactContentConflictError):
        cli._publish_report(report, args)
    assert target.read_bytes() == b"existing unrelated bytes"


@pytest.mark.parametrize("target", ["choice", "database", "manifest", "alias", "protected"])
def test_report_cannot_be_written_into_source_archives_or_protected_paths(tmp_path, target):
    args = _arguments(tmp_path)
    locations = {"choice": args.choice_dir, "database": args.tencent_database.parent, "manifest": args.tencent_manifest.parent,
                 "protected": tmp_path / ".workbuddy-ai", "alias": tmp_path / "alias"}
    if target == "alias":
        (tmp_path / "outside").mkdir()
        locations[target].symlink_to(tmp_path / "outside", target_is_directory=True)
    args.output_dir = locations[target] / "reports"
    with pytest.raises(ChoiceError):
        cli._publish_report({}, args)
    assert not args.output_dir.exists()


@pytest.mark.parametrize("output", [False, True])
def test_cli_optional_output_flag_and_default_stdout(monkeypatch, tmp_path, capsys, output):
    args = _arguments(tmp_path)
    report = {"sdk_requests": 0, "source_equivalence": "not_established", "runtime_model_replacement": False}
    monkeypatch.setattr(cli, "compare_choice_tencent_history", lambda *unused: report)
    argv = ["comparison", "--choice-dir", str(args.choice_dir), "--tencent-database", str(args.tencent_database),
            "--tencent-manifest", str(args.tencent_manifest)]
    if output:
        argv.extend(["--output-dir", str(args.output_dir)])
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 0
    printed = json.loads(capsys.readouterr().out)
    if output:
        assert printed["status"] == "comparison_published" and Path(printed["report"]).exists()
    else:
        assert printed == report and not args.output_dir.exists()


def test_cli_verification_failure_does_not_publish_or_expose_payload(monkeypatch, tmp_path, capsys):
    args = _arguments(tmp_path)
    monkeypatch.setattr(cli, "parser", lambda: type("Parser", (), {"parse_args": lambda self: args})())

    def fail(*unused):
        raise ProbabilityHistoryError("secret_token=must_not_leak")

    monkeypatch.setattr(cli, "compare_choice_tencent_history", fail)
    assert cli.main() == 2
    output = capsys.readouterr()
    assert "secret_token" not in output.err and json.loads(output.err)["error_type"] == "ProbabilityHistoryError"
    assert not args.output_dir.exists()
