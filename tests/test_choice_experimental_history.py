from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from random import Random
import sqlite3

import pytest

from app.artifacts.io import canonical_json_bytes, exclusive_atomic_publish, sha256_hex
from app.services import choice_experimental_history as history
from app.services.choice_research import DAILY_FIELDS, make_plan, normalize
from app.services.choice_research_collect import ChoiceCollector
from app.services.choice_research_store import ChoiceBudget, ChoiceDataset
from app.services.choice_sdk import ChoiceError, ChoiceSDKClient
from app.services.experimental_direction_model import build_choice_direction_model, direction_samples
from app.services.experimental_probability_model import MODEL_DIRECTORY, ExperimentalProbabilityUnavailable, load_experimental_model
from app.services.market_scan_probability_history import ProbabilityHistoryError, load_market_scan_probability_history_manifest, trusted_probability_history_dates
from app.services.market_scan_probability_replay import historical_replay_feature_values
from tests import test_choice_research as fixtures


@pytest.fixture
def choice_source(tmp_path, monkeypatch, request):
    days = list(trusted_probability_history_dates("2026-08-21", 300))
    monkeypatch.setattr(fixtures, "SESSIONS", days)
    data = fixtures.daily_response()
    for symbol_index, columns in enumerate(data["data"].values()):
        generator = Random(917 + symbol_index)
        previous = 10.0
        for index, _day in enumerate(days):
            factor = 1.0 if index < 150 else 1.2
            opened = previous
            close = previous * (1 + generator.uniform(-.045, .045))
            values = {
                "OPEN": opened / factor, "CLOSE": close / factor,
                "HIGH": max(opened, close) * 1.01 / factor, "LOW": min(opened, close) * .99 / factor,
                "VOLUME": generator.uniform(100000, 200000), "AMOUNT": 1000000.,
                "TAFACTOR": factor, "PRECLOSE": previous / factor,
            }
            for field, value in values.items():
                columns[DAILY_FIELDS.index(field)][index] = value
            previous = close
    for symbol, index, state in [("600519.SH", 30, "连续停牌"), ("000001.SZ", 5, "未上市"), ("920006.BJ", 6, "盘中停牌")]:
        data["data"][symbol][DAILY_FIELDS.index("TRADESTATUS")][index] = state
    data["data"]["600519.SH"][DAILY_FIELDS.index("TAFACTOR")][70] = 0.0
    if getattr(request, "param", None) == "empty_bj":
        data["data"]["920006.BJ"][DAILY_FIELDS.index("TRADESTATUS")] = ["未上市"] * len(days)

    class Client(fixtures.FakeClient):
        def request(self, method, args):
            if method == "csd":
                return deepcopy(data)
            return super().request(method, args)

    source = tmp_path / "source"
    plan = make_plan(days[0], days[-1], 3, [], [])
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(source, plan) as dataset:
        ChoiceCollector(dataset, Client(), budget).run()
    monkeypatch.setattr(ChoiceSDKClient, "__enter__", lambda self: pytest.fail("offline conversion must not log into Choice"))
    return source, days, data


def test_conversion_and_reload_replay_raw_without_source_or_budget_writes(choice_source, tmp_path):
    source, days, raw = choice_source
    before = (source / "choice_research.sqlite3").read_bytes()
    budget = (tmp_path / "control" / "budget.sqlite3").read_bytes()
    built = history.build_choice_experimental_history(source, tmp_path / "history")
    actual = history.load_choice_experimental_history(built.manifest_path, built.database_path)
    assert actual.sessions == tuple(days) and len(actual.series) == 3
    assert built.summary["accepted_bars"] == 896
    assert built.summary["excluded_counts"] == {
        "invalid_adjustment_factor": 1, "not_trading": 1, "suspended": 1, "unknown_or_intraday_restricted_state": 1,
    }
    first = actual.series["600519.SH"][0]
    assert first.close == pytest.approx(raw["data"]["600519.SH"][DAILY_FIELDS.index("CLOSE")][0] / 1.2)
    assert first.adjustment_factor == pytest.approx(1 / 1.2)
    assert first.volume == raw["data"]["600519.SH"][DAILY_FIELDS.index("VOLUME")][0]
    assert first.point_in_time is False and first.open_execution_status == "unknown"
    assert first.source == history.SOURCE_NAME and first.data_version.startswith("choice-factor-qfq-v1:")
    assert actual.provenance["filter_qualified"] is False and actual.provenance["production_ranking_effect"] == "none"
    assert (source / "choice_research.sqlite3").read_bytes() == before
    assert (tmp_path / "control" / "budget.sqlite3").read_bytes() == budget
    repeated = history.build_choice_experimental_history(source, tmp_path / "history")
    assert repeated == built


def test_restricted_or_missing_factor_sessions_are_never_synthesized_or_shifted(choice_source, tmp_path):
    source, days, _ = choice_source
    built = history.build_choice_experimental_history(source, tmp_path / "history")
    loaded = history.load_choice_experimental_history(built.manifest_path, built.database_path)
    assert days[70] not in {row.date for row in loaded.series["600519.SH"]}
    samples, targets, _, receipt = direction_samples(loaded.series, loaded.sessions, offset=2)
    assert "close-d2:" + days[68] + ":600519.SH" not in targets
    assert receipt["excluded"]["missing_fixed_target"] > 0
    assert all(sample.sample_id in targets for sample in samples)


def test_positive_anchor_scaling_does_not_change_direction_features(choice_source, tmp_path):
    source, _, _ = choice_source
    built = history.build_choice_experimental_history(source, tmp_path / "history")
    rows = history.load_choice_experimental_history(built.manifest_path, built.database_path).series["000001.SZ"][-21:]
    original = historical_replay_feature_values(rows, signal_date=rows[-1].date)
    scaled = [row.model_copy(update={name: getattr(row, name) * 7.3 for name in ("open", "close", "high", "low")}) for row in rows]
    assert historical_replay_feature_values(scaled, signal_date=scaled[-1].date) == pytest.approx(original, abs=1e-12)


@pytest.mark.parametrize("choice_source", ["empty_bj"], indirect=True)
def test_fully_excluded_symbol_remains_in_research_coverage_denominator(choice_source, tmp_path):
    built = history.build_choice_experimental_history(choice_source[0], tmp_path / "history")
    loaded = history.load_choice_experimental_history(built.manifest_path, built.database_path)
    assert loaded.series["920006.BJ"] == [] and len(loaded.series) == 3
    assert "920006.BJ" in loaded.provenance["coverage"]["source_symbols"]
    assert "920006.BJ" not in loaded.provenance["coverage"]["accepted_symbols"]


def test_choice_history_cannot_masquerade_as_tencent_manifest(choice_source, tmp_path):
    built = history.build_choice_experimental_history(choice_source[0], tmp_path / "history")
    with pytest.raises(ProbabilityHistoryError):
        load_market_scan_probability_history_manifest(built.manifest_path, database_path=built.database_path)


def test_derived_database_tampering_fails_even_when_manifest_is_rehashed(choice_source, tmp_path):
    built = history.build_choice_experimental_history(choice_source[0], tmp_path / "history")
    with sqlite3.connect(built.database_path) as connection:
        connection.execute("UPDATE kline_daily SET close=close+0.01")
    with pytest.raises(ChoiceError, match="digest mismatch"):
        history.load_choice_experimental_history(built.manifest_path, built.database_path)
    document = json.loads(built.manifest_path.read_text())
    document["payload"]["database"]["sha256"] = sha256_hex(built.database_path.read_bytes())
    document["sha256"] = sha256_hex(canonical_json_bytes(document["payload"]))
    path = built.manifest_path.parent / f"choice-experimental-history-{document['sha256']}.manifest.json"
    exclusive_atomic_publish(path, canonical_json_bytes(document), max_bytes=history.MANIFEST_MAX_BYTES)
    with pytest.raises(ChoiceError, match="rows differ"):
        history.load_choice_experimental_history(path, built.database_path)


def test_manifest_authority_and_source_mutation_rejected(choice_source, tmp_path):
    source, _, _ = choice_source
    built = history.build_choice_experimental_history(source, tmp_path / "history")
    document = json.loads(built.manifest_path.read_text())
    document["payload"]["official"] = True
    document["sha256"] = sha256_hex(canonical_json_bytes(document["payload"]))
    path = built.manifest_path.parent / f"choice-experimental-history-{document['sha256']}.manifest.json"
    exclusive_atomic_publish(path, canonical_json_bytes(document), max_bytes=history.MANIFEST_MAX_BYTES)
    with pytest.raises(ChoiceError, match="does not replay"):
        history.load_choice_experimental_history(path, built.database_path)
    with sqlite3.connect(source / "choice_research.sqlite3") as connection:
        connection.execute("UPDATE records SET payload_json='{}' WHERE kind='daily'")
    with pytest.raises(ChoiceError, match="normalized records"):
        history.load_choice_experimental_history(built.manifest_path, built.database_path)


def test_first_build_rejects_receipt_timestamp_tampering(choice_source, tmp_path):
    source, _, _ = choice_source
    with sqlite3.connect(source / "choice_research.sqlite3") as connection:
        connection.execute("UPDATE requests SET captured_at='2000-01-01T00:00:00+08:00' WHERE json_extract(descriptor_json,'$.kind')='daily'")
    with pytest.raises(ChoiceError, match="timestamp differs"):
        history.build_choice_experimental_history(source, tmp_path / "history")
    assert not (tmp_path / "history").exists()


def test_changed_raw_request_options_and_incomplete_grid_fail_closed(choice_source, tmp_path):
    source, _, _ = choice_source
    with ChoiceDataset.open_readonly(source) as dataset:
        descriptor = json.loads(dataset.db.execute("SELECT descriptor_json FROM requests WHERE json_extract(descriptor_json,'$.kind')='daily'").fetchone()[0])
        result = dataset.cached(descriptor)["result"]
        plan = dataset.plan
    descriptor["args"][-1] = descriptor["args"][-1].replace("AdjustFlag=1", "AdjustFlag=3")
    with ChoiceDataset(source, plan) as dataset:
        payload = dataset.archive(descriptor, result)
        dataset.project(payload, normalize(payload))
    with pytest.raises(ChoiceError, match="request contract"):
        history.build_choice_experimental_history(source, tmp_path / "history")
    assert not (tmp_path / "history").exists()


def test_static_and_protected_paths_rejected_before_read_or_publish(choice_source, tmp_path):
    source, _, _ = choice_source
    sidecar = Path(str(source / "choice_research.sqlite3") + "-wal")
    sidecar.write_bytes(b"active")
    with pytest.raises(ChoiceError, match="sidecars"):
        history.build_choice_experimental_history(source, tmp_path / "history")
    with pytest.raises(ChoiceError, match="protected"):
        history.build_choice_experimental_history(source, tmp_path / ".workbuddy-ai" / "do-not-create")
    assert not (tmp_path / ".workbuddy-ai").exists()


def test_output_cannot_be_original_archive_or_symlink(choice_source, tmp_path):
    source, _, _ = choice_source
    with pytest.raises(ChoiceError, match="outside"):
        history.build_choice_experimental_history(source, source / "derived")
    link = tmp_path / "alias"
    link.symlink_to(source, target_is_directory=True)
    with pytest.raises(ChoiceError, match="symlink"):
        history.build_choice_experimental_history(link, tmp_path / "history")


def test_wal_format_without_sidecars_is_rejected_without_creating_them(choice_source, tmp_path):
    source, _, _ = choice_source
    database = source / "choice_research.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.close()
    assert not Path(str(database) + "-wal").exists()
    with pytest.raises(ChoiceError, match="rollback-journal"):
        history.build_choice_experimental_history(source, tmp_path / "history")
    assert not Path(str(database) + "-wal").exists() and not Path(str(database) + "-shm").exists()


@pytest.mark.parametrize("setting", ["ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "TRADE_CALENDAR_AUTO_FETCH"])
def test_offline_conversion_refuses_implicit_calendar_network_fetch(tmp_path, monkeypatch, setting):
    monkeypatch.delenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", raising=False)
    monkeypatch.delenv("TRADE_CALENDAR_AUTO_FETCH", raising=False)
    monkeypatch.setenv(setting, "1")
    monkeypatch.setattr(history.ChoiceDataset, "open_readonly", lambda _: pytest.fail("must refuse before opening the source"))
    with pytest.raises(ChoiceError, match="offline Choice research"):
        history.build_choice_experimental_history(tmp_path / "source", tmp_path / "history")
    assert not (tmp_path / "history").exists()


def test_candidate_models_build_all_three_horizons_without_changing_runtime(choice_source, tmp_path):
    built = history.build_choice_experimental_history(choice_source[0], tmp_path / "history")
    runtime = tmp_path / MODEL_DIRECTORY
    with pytest.raises(ExperimentalProbabilityUnavailable, match="不能自动替换"):
        build_choice_direction_model(built.manifest_path, built.database_path, runtime, offset=1)
    assert not runtime.exists()
    for offset in (1, 2, 5):
        path = build_choice_direction_model(built.manifest_path, built.database_path, tmp_path / "candidates", offset=offset)
        estimator, _ = load_experimental_model(path.parent, prediction_kind=f"close_d{offset}")
        assert estimator.horizon == offset and estimator.filter_qualified is False
        assert estimator.source_filename == built.database_path.name
        assert estimator.train_label_end < estimator.calibration_start
        assert "choice_and_tencent_adjustment_methods_not_assumed_equivalent" in estimator.limitations


@pytest.mark.parametrize("directory", [".workbuddy-ai", ".git", ".codex", ".agents", ".venv"])
def test_candidate_protected_output_rejected_before_source_read(tmp_path, monkeypatch, directory):
    monkeypatch.setattr(history, "load_choice_experimental_history", lambda *_args: pytest.fail("unsafe output reached source loader"))
    output = tmp_path / directory / "candidates"
    with pytest.raises(ExperimentalProbabilityUnavailable, match="受保护"):
        build_choice_direction_model(Path("unused"), Path("unused"), output, offset=1)
    assert not output.exists()


def test_candidate_output_alias_rejected_before_source_read(tmp_path, monkeypatch):
    output = tmp_path / "alias"
    original = tmp_path / "original"
    original.mkdir()
    output.symlink_to(original, target_is_directory=True)
    monkeypatch.setattr(history, "load_choice_experimental_history", lambda *_args: pytest.fail("alias output reached source loader"))
    with pytest.raises(ExperimentalProbabilityUnavailable, match="路径别名"):
        build_choice_direction_model(Path("unused"), Path("unused"), output, offset=1)
    assert not list(original.iterdir())


@pytest.mark.parametrize("target", ["source", "derived"])
def test_candidate_output_cannot_write_into_history_archives(choice_source, tmp_path, monkeypatch, target):
    from app.services import experimental_direction_model as direction

    built = history.build_choice_experimental_history(choice_source[0], tmp_path / "history")
    output = (choice_source[0] if target == "source" else built.manifest_path.parent) / "candidates"
    monkeypatch.setattr(direction, "_publish_direction_model", lambda *_args, **_kwargs: pytest.fail("archive output reached fitting"))
    with pytest.raises(ExperimentalProbabilityUnavailable, match="历史档案之外"):
        build_choice_direction_model(built.manifest_path, built.database_path, output, offset=1)
    assert not output.exists()


def test_cli_choice_requires_explicit_separate_candidate_directory(monkeypatch, tmp_path, capsys):
    from tools import build_experimental_probability as cli

    arguments = ["build", "--prediction-kind", "close_d1", "--choice-history-manifest", "choice.json", "--database", "choice.sqlite3"]
    monkeypatch.setattr(cli.sys, "argv", arguments)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2
    calls = []

    def build(manifest, database, output, *, offset):
        calls.append((manifest, database, output, offset))
        return output / "candidate.json"

    monkeypatch.setattr(cli, "build_choice_direction_model", build)
    monkeypatch.setattr(cli.sys, "argv", [*arguments, "--output-dir", str(tmp_path / "candidates")])
    assert cli.main() == 0 and len(calls) == 1 and calls[0][-1] == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["formal_filter_qualified"] is False
