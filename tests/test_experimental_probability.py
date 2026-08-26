from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
from random import Random
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_market_scan_experimental_read_admission, get_market_scanner
from app.api.market_scan_read_admission import MarketScanHeavyReadAdmission
from app.api.routes.market_scan import router
from app.artifacts.io import canonical_json_bytes, exclusive_atomic_publish, sha256_hex
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE, MarketScanResultItem, MarketScanRun
from app.services.experimental_probability_model import (
    MODEL_DIRECTORY, MODEL_FEATURE_NAMES, ExperimentalEstimator, ExperimentalProbabilityUnavailable,
    experimental_probability, fit_experimental_estimator, load_experimental_model,
)
from app.services.market_scan_experimental_probability import _validate_signal_date, experimental_results
from app.services.market_scan_probability_history import trusted_probability_history_dates
from app.services.market_scan_probability import PROBABILITY_CALIBRATOR_VERSION, PROBABILITY_MODEL_VERSION
from app.services.market_scan_probability_replay import HISTORICAL_REPLAY_FEATURE_NAMES


def _estimator() -> ExperimentalEstimator:
    return ExperimentalEstimator.model_validate({
        "generated_at": "2026-08-25T16:00:00+08:00", "source_filename": "historical-test.json",
        "source_sha256": "a" * 64, "source_integrity_digest": "b" * 64, "training_symbols": ["600001.SH"],
        "train_session_count": 200, "calibration_session_count": 40, "train_record_count": 400, "calibration_record_count": 80,
        "train_signal_end": "2026-05-01", "train_label_end": "2026-05-11", "calibration_start": "2026-05-12",
        "calibration_end": "2026-07-23", "latest_label_date": "2026-07-31", "expires_after_signal_date": "2026-10-29",
        "base_rate": 0.4, "model": {
            "version": PROBABILITY_MODEL_VERSION, "feature_names": list(MODEL_FEATURE_NAMES),
            "means": [0.] * 11, "scales": [1.] * 11, "coefficients": [0.1] * 11,
            "intercept": 0., "l2_strength": 1., "iterations": 3, "converged": True,
        }, "calibrator": {
            "version": PROBABILITY_CALIBRATOR_VERSION, "intercept": 0., "slope": 1.,
            "iterations": 3, "converged": True, "fit_partition": "calibration_only",
        }, "historical_recipe_evaluation": {"status": "insufficient_data"}, "cost_contract": {}, "limitations": ["not_production"],
    })


def _publish(directory: Path, estimator: ExperimentalEstimator) -> Path:
    payload = estimator.model_dump()
    digest = sha256_hex(canonical_json_bytes(payload))
    target = directory / f"experimental-h5-{digest}.json"
    exclusive_atomic_publish(target, canonical_json_bytes({"payload": payload, "sha256": digest}), max_bytes=256 * 1024)
    return target


def test_model_artifact_is_explicitly_non_authorizing_and_tamper_fails_closed(tmp_path):
    expected = _estimator()
    target = _publish(tmp_path, expected)
    actual, _digest = load_experimental_model(tmp_path)
    assert actual == expected
    assert actual.filter_qualified is False
    assert actual.production_ranking_effect == "none"
    document = json.loads(target.read_text())
    document["payload"]["model"]["intercept"] += 1
    target.write_text(json.dumps(document))
    with pytest.raises(ExperimentalProbabilityUnavailable, match="校验失败"):
        load_experimental_model(tmp_path)


def test_model_contract_rejects_nonfinite_weights_wrong_feature_order_and_overlapping_dates():
    for field, value in (("filter_qualified", True), ("train_label_end", "2026-05-12"), ("expires_after_signal_date", "2099-01-01")):
        data = _estimator().model_dump()
        data[field] = value
        with pytest.raises(ValueError):
            ExperimentalEstimator.model_validate(data)
    for field, value in (("scales", [0.] * 11), ("coefficients", [float("nan")] * 11), ("feature_names", list(reversed(MODEL_FEATURE_NAMES)))):
        data = _estimator().model_dump()
        data["model"][field] = value
        with pytest.raises(ValueError):
            ExperimentalEstimator.model_validate(data)


def test_prediction_feature_order_matches_model_and_outliers_never_get_guessed_probability():
    model = _estimator()
    assert experimental_probability(model, (0.,) * 11) == 0.5
    with pytest.raises(ExperimentalProbabilityUnavailable, match="distribution"):
        experimental_probability(model, (100.,) * 11)
    with pytest.raises(ExperimentalProbabilityUnavailable, match="数量"):
        experimental_probability(model, (0.,))
    with pytest.raises(ExperimentalProbabilityUnavailable, match="有限"):
        experimental_probability(model, (float("nan"),) * 11)
    with pytest.raises(ExperimentalProbabilityUnavailable, match="未来训练"):
        _validate_signal_date(model, "2026-07-31")
    with pytest.raises(ExperimentalProbabilityUnavailable, match="已过期|已收盘"):
        _validate_signal_date(model, "2027-01-01")


def test_fit_keeps_independent_calibration_and_purges_target_overlap():
    random = Random(17)
    rows = []
    for offset in range(180):
        day = date(2025, 1, 1) + timedelta(days=offset)
        for number in range(4):
            rows.append({"symbol": f"60000{number}.SH", "sample_id": f"{day}:{number}", "signal_date": day.isoformat(),
                         "feature_values": [random.uniform(-1, 1) for _ in HISTORICAL_REPLAY_FEATURE_NAMES],
                         "outcomes": [{"horizon": 5, "status": "modelled", "net_return": .02 if number % 2 else -.02,
                                       "target_session_date": (day + timedelta(days=6)).isoformat()}]})
    source = {"records": rows, "cost_contract": {}, "limitations": [],
              "probability_fit": {"horizons": {"5": {"status": "insufficient_data", "limitations": ["weak_skill"]}}}}
    before = deepcopy(source)
    model = fit_experimental_estimator(source, source_filename="test.json", source_sha256="c" * 64, source_integrity_digest="d" * 64)
    assert model.train_session_count == 134 and model.calibration_session_count == 40
    assert model.train_label_end < model.calibration_start
    assert model.historical_recipe_evaluation["status"] == "insufficient_data"
    assert model.filter_qualified is False and source == before


def _database(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE kline_daily (symbol TEXT,adjustment_mode TEXT,date TEXT,open REAL,close REAL,high REAL,low REAL,volume REAL,as_of TEXT,data_version TEXT,contract_version TEXT,fallback_used INTEGER,source TEXT,fetched_at TEXT)")
    symbols = ["600001.SH", "000001.SZ", "920001.BJ", "600002.SH"]
    days = trusted_probability_history_dates("2026-08-25", 21)
    for number, symbol in enumerate(symbols):
        count = 20 if number == 3 else 21
        for offset in range(count):
            day = date.fromisoformat(days[offset])
            close = 10 + offset * .01 * (number + 1)
            volume = 0 if number == 2 and offset == 20 else 1000
            connection.execute("INSERT INTO kline_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                symbol, "qfq", day.isoformat(), close, close, close + .1, close - .1, volume,
                day.isoformat(), "qfq-v1", "contract-v1", 0, "test", "2026-08-26 10:00:00",
            ))
    connection.commit()
    connection.close()
    _publish(tmp_path / MODEL_DIRECTORY, _estimator())
    run = MarketScanRun.model_construct(id=3, data_date="2026-08-25", rule_version="v5", snapshot_digest="f" * 64,
                                       scope=MARKET_SCAN_FULL_MARKET_SCOPE, status="success", success_count=4)
    items = [MarketScanResultItem.model_construct(symbol=symbol, name=symbol, market=symbol[-2:], rank=number + 1,
                                                 score=80 - number, raw_score=80. - number, status="success") for number, symbol in enumerate(symbols)]
    return path, run, items


def test_runtime_filter_rank_pagination_and_read_only_feature_cutoff(tmp_path):
    database, run, items = _database(tmp_path)
    before, original = database.read_bytes(), deepcopy(items)
    result = experimental_results(database, run, items, page_size=1)
    assert result["total"] == 2 and result["page_count"] == 2
    assert result["coverage"]["unavailable_reasons"] == {"no_volume_on_signal_date": 1, "missing_exact_date_or_21_bars": 1}
    first = result["items"][0]
    assert first["experimental_rank"] == 1 and result["formal_filter_qualified"] is False
    second = experimental_results(database, run, items, page=2, page_size=1)["items"][0]
    assert first["probability"] >= second["probability"]
    assert experimental_results(database, run, items, minimum=1.)["total"] == 0
    assert experimental_results(database, run, items, market="SZ", keyword="000001")["total"] == 1
    baseline = experimental_results(database, run, items, sort="base_rank")
    assert [row["base_rank"] for row in baseline["items"]] == [1, 2]
    assert database.read_bytes() == before and items == original
    connection = sqlite3.connect(database)
    connection.execute("UPDATE kline_daily SET date='2026-08-26',close=999 WHERE symbol='600002.SH'")
    connection.commit()
    connection.close()
    changed = experimental_results(database, run, items, page_size=1)
    assert changed["items"] == result["items"]


@pytest.mark.parametrize("parameters", [{"minimum": float("nan")}, {"minimum": True}, {"sort": "rank"}, {"page_size": 10000}])
def test_runtime_query_rejects_invalid_controls(tmp_path, parameters):
    database, run, items = _database(tmp_path)
    with pytest.raises(ExperimentalProbabilityUnavailable):
        experimental_results(database, run, items, **parameters)


def test_api_requires_explicit_experimental_acknowledgment_and_separate_parameters():
    class Scanner:
        def __init__(self):
            self.calls = []
            self.unavailable = False

        def experimental_probability_results(self, run_id, **filters):
            if self.unavailable:
                raise ExperimentalProbabilityUnavailable("实验模型已过期")
            self.calls.append((run_id, filters))
            return {"mode": "personal_experimental", "filters": filters, "production_ranking_effect": "none"}

    scanner = Scanner()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_market_scanner] = lambda: scanner
    app.dependency_overrides[get_market_scan_experimental_read_admission] = lambda: MarketScanHeavyReadAdmission()
    with TestClient(app) as client:
        uri = "/api/market-scans/3/experimental-probability"
        assert client.get(uri).status_code == 422 and not scanner.calls
        assert client.get(uri + "?acknowledge_experimental=true&min_probability=2").status_code == 422
        response = client.get(uri + "?acknowledge_experimental=true&min_probability=0.4&market=BJ&sort=probability&page=2")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert scanner.calls[0] == (3, {"prediction_kind": "net_h5", "minimum": .4, "market": "BJ", "keyword": "", "sort": "probability", "page": 2, "page_size": 50})
        for kind in ["close_d1", "close_d2", "close_d5"]:
            assert client.get(uri + f"?acknowledge_experimental=true&prediction_kind={kind}").status_code == 200
            assert scanner.calls[-1][1]["prediction_kind"] == kind
        assert client.get(uri + "?acknowledge_experimental=true&prediction_kind=h1").status_code == 422
        scanner.unavailable = True
        missing = client.get(uri + "?acknowledge_experimental=true")
        assert missing.status_code == 422 and missing.json()["detail"] == "实验模型已过期"


def test_model_rejects_symlink_directory_and_malformed_artifact(tmp_path):
    real = tmp_path / "models"
    target = _publish(real, _estimator())
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ExperimentalProbabilityUnavailable, match="路径"):
        load_experimental_model(alias)
    target.write_text('{"payload": NaN}')
    with pytest.raises(ExperimentalProbabilityUnavailable, match="校验失败"):
        load_experimental_model(real)


@pytest.mark.parametrize("column", ["rank", "amount"])
def test_lean_experimental_projection_reverifies_entire_snapshot_in_one_read_transaction(tmp_path, monkeypatch, column):
    from app.db.market_scan_integrity import MarketScanSnapshotSealError
    from app.repositories import market_scan_experimental as repository
    from tests.test_strategy_execution import _disable_market_scan_immutability, _environment

    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    original = repository.require_publication_market_scan_snapshot
    calls = []

    def verify(connection, identifier):
        assert connection.in_transaction and connection.execute("PRAGMA query_only").fetchone()[0] == 1
        calls.append(identifier)
        return original(connection, identifier)

    monkeypatch.setattr(repository, "require_publication_market_scan_snapshot", verify)
    run, items = repository.read_experimental_candidates(cache.path, run_id)
    assert len(items) == run.success_count == 4
    assert [item.rank for item in items] == [1, 2, 3, 4]
    repository.read_experimental_candidates(cache.path, run_id)
    assert calls == [run_id, run_id], "a previous verification never grants cached authority"
    with sqlite3.connect(cache.path) as connection:
        _disable_market_scan_immutability(connection)
        connection.execute(f"UPDATE market_scan_result SET {column}={column}+1 WHERE run_id=?", (run_id,))
    with pytest.raises(MarketScanSnapshotSealError, match="摘要不一致"):
        repository.read_experimental_candidates(cache.path, run_id)


def test_offline_builder_verifies_before_fitting_and_publishes_bound_model(tmp_path, monkeypatch):
    from app.services import experimental_probability_model as service

    source = tmp_path / "source.json"
    source.write_text('{"historical": true}', encoding="utf-8")
    payload, expected, calls = {"verified": True}, _estimator(), []

    def verify(document):
        assert document == {"historical": True}
        calls.append("verify")
        return {"payload": payload, "integrity": {"integrity_digest": "b" * 64}}

    def fit(document, **receipt):
        assert calls == ["verify"] and document is payload
        assert receipt == {"source_filename": source.name, "source_sha256": sha256_hex(source.read_bytes()),
                           "source_integrity_digest": "b" * 64}
        calls.append("fit")
        return expected

    monkeypatch.setattr(service, "verify_historical_replay_artifact", verify)
    monkeypatch.setattr(service, "fit_experimental_estimator", fit)
    directory = tmp_path / "models"
    target = service.build_experimental_model(source, directory)
    assert calls == ["verify", "fit"] and target.is_file()
    assert service.load_experimental_model(directory)[0] == expected
    assert source.read_text() == '{"historical": true}'


def test_offline_builder_rejects_nonobject_before_verification(tmp_path, monkeypatch):
    from app.services import experimental_probability_model as service

    source = tmp_path / "source.json"
    source.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(service, "verify_historical_replay_artifact", lambda _: pytest.fail("invalid source must not reach replay"))
    with pytest.raises(ExperimentalProbabilityUnavailable, match="JSON object"):
        service.build_experimental_model(source, tmp_path / "models")
    assert not (tmp_path / "models").exists()


@pytest.mark.parametrize("mutation", ["missing", "too_many", "envelope", "future"])
def test_model_loader_rejects_missing_excessive_or_future_versions(tmp_path, mutation):
    if mutation == "too_many":
        for number in range(33):
            (tmp_path / f"experimental-h5-{number:064x}.json").write_text("{}", encoding="utf-8")
    elif mutation == "envelope":
        (tmp_path / f"experimental-h5-{'0' * 64}.json").write_text("[]", encoding="utf-8")
    elif mutation == "future":
        model = _estimator().model_copy(update={"generated_at": "2099-01-01T00:00:00+08:00"})
        _publish(tmp_path, model)
    with pytest.raises(ExperimentalProbabilityUnavailable):
        load_experimental_model(tmp_path)


@pytest.mark.parametrize("field,value", [("generated_at", "2026-08-25T16:00:00"), ("feature_version", "unknown")])
def test_model_rejects_naive_clock_and_unknown_features(field, value):
    payload = _estimator().model_dump()
    payload[field] = value
    with pytest.raises(ValueError):
        ExperimentalEstimator.model_validate(payload)


def test_model_rejects_unknown_calibrator():
    payload = _estimator().model_dump()
    payload["calibrator"]["version"] = "unknown"
    with pytest.raises(ValueError, match="calibrator version"):
        ExperimentalEstimator.model_validate(payload)


@pytest.mark.parametrize("count", [1, 180])
def test_experimental_fit_refuses_short_or_single_class_history(count):
    rows = [{"symbol": "600001.SH", "sample_id": str(offset),
             "signal_date": (date(2025, 1, 1) + timedelta(days=offset)).isoformat(),
             "feature_values": [0.] * len(HISTORICAL_REPLAY_FEATURE_NAMES),
             "outcomes": [{"horizon": 5, "status": "modelled", "net_return": .01,
                           "target_session_date": (date(2025, 1, 7) + timedelta(days=offset)).isoformat()}]}
            for offset in range(count)]
    rows.append({"outcomes": [{"horizon": 5, "status": "data_unavailable"}]})
    with pytest.raises(ExperimentalProbabilityUnavailable):
        fit_experimental_estimator({"records": rows}, source_filename="source.json",
                                   source_sha256="a" * 64, source_integrity_digest="b" * 64)


@pytest.mark.parametrize("explicit_directory", [False, True])
def test_experimental_cli_binds_paths_and_reports_non_authorizing_result(tmp_path, monkeypatch, capsys, explicit_directory):
    from tools import build_experimental_probability as cli

    source, target, calls = tmp_path / "source.json", tmp_path / "model.json", []
    directory = tmp_path / "models" if explicit_directory else cli.ROOT / "data" / MODEL_DIRECTORY

    def build(received_source, received_directory):
        calls.append((received_source, received_directory))
        return target

    monkeypatch.setattr(cli, "build_experimental_model", build)
    arguments = ["build", "--source", str(source)]
    if explicit_directory:
        arguments.extend(["--output-dir", str(directory)])
    monkeypatch.setattr(cli.sys, "argv", arguments)
    assert cli.main() == 0 and calls == [(source, directory)]
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report == {"status": "experimental_model_built", "path": str(target),
                      "prediction_kind": "net_h5",
                      "formal_filter_qualified": False, "production_ranking_effect": "none"}
