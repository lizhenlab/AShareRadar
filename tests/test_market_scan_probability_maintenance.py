from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import runpy
import sqlite3
import sys
from types import SimpleNamespace
from typing import cast

import pytest

from app.services import market_scan_probability_maintenance as maintenance
from app.services import market_scan_probability_fit_assessment as fit_assessment
from app.services.market_scan_probability_research import ProbabilityResearchRow
from tools import maintain_market_scan_probability as maintenance_cli


class _Cache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...], str]] = []
        self.rows: dict[str, list] = {}

    def get_klines_by_dates_many(self, symbols, dates, adjustment_mode="qfq"):
        identity = tuple(symbols), tuple(dates), adjustment_mode
        self.calls.append(identity)
        required = set(identity[1])
        return {symbol: [row for row in self.rows.get(symbol, ()) if row.date in required] for symbol in identity[0]}


def test_read_only_probability_maintenance_cli_cache_reads_only_requested_dates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE kline_daily (
                symbol TEXT NOT NULL,
                adjustment_mode TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL NOT NULL,
                close REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                volume REAL NOT NULL,
                as_of TEXT NOT NULL,
                data_version TEXT NOT NULL,
                contract_version TEXT NOT NULL,
                fallback_used INTEGER NOT NULL,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO kline_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "000001.SZ", "qfq", date, 10.0, close, 10.5, 9.5, 1000.0,
                    f"{date}T15:00:00+08:00", "daily-v1", "kline-v1", 0,
                    "fixture", f"{date}T16:00:00+08:00",
                )
                for date, close in (("2026-08-11", 10.1), ("2026-08-12", 10.2))
            ],
        )

    cache = maintenance_cli._ReadOnlyKlineCache(database)  # noqa: SLF001 - CLI adapter contract
    empty = cache.get_klines_by_dates_many(("000001.SZ", "600000.SH"), ())
    selected = cache.get_klines_by_dates_many(
        ("000001.SZ", "600000.SH"),
        ("2026-08-12", "2026-08-12"),
    )

    assert empty == {"000001.SZ": [], "600000.SH": []}
    assert [row.date for row in selected["000001.SZ"]] == ["2026-08-12"]
    assert selected["000001.SZ"][0].close == pytest.approx(10.2)
    assert selected["000001.SZ"][0].from_cache is True
    assert selected["600000.SH"] == []


@pytest.mark.parametrize(("failed_count", "expected_code"), ((0, 0), (2, 1)))
def test_probability_maintenance_cli_main_reports_summary_and_failure_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed_count: int,
    expected_code: int,
) -> None:
    captured: dict[str, object] = {}

    def maintain(cache, **kwargs):
        captured.update(cache=cache, **kwargs)
        return SimpleNamespace(failed_count=failed_count, published_count=3)

    monkeypatch.setattr(maintenance_cli, "maintain_market_scan_probability", maintain)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "maintain-market-scan-probability",
            "--database", str(tmp_path / "runtime.sqlite3"),
            "--source-dir", str(tmp_path / "sources"),
            "--outcome-dir", str(tmp_path / "outcomes"),
            "--as-of-date", "2026-08-13",
        ],
    )

    assert maintenance_cli.main() == expected_code
    output = json.loads(capsys.readouterr().out)

    assert output == {"failed_count": failed_count, "published_count": 3}
    assert isinstance(captured["cache"], maintenance_cli._ReadOnlyKlineCache)  # noqa: SLF001
    assert captured["as_of_date"] == "2026-08-13"
    assert captured["source_directory"] == tmp_path / "sources"
    assert captured["outcome_directory"] == tmp_path / "outcomes"


def test_probability_maintenance_cli_bootstraps_repo_import_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = str(Path(maintenance_cli.__file__).resolve().parent.parent)
    monkeypatch.setattr(sys, "path", [value for value in sys.path if value != root])

    namespace = runpy.run_path(str(Path(maintenance_cli.__file__).resolve()))

    assert namespace["ROOT"] == Path(root)
    assert sys.path[0] == root


def test_initial_before_maturity_publishes_once_without_reading_bars(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, cache, published = _service(tmp_path, monkeypatch, [_source(71, "2026-08-11")])

    first = service.run(now=_at("2026-08-12"), as_of_date="2026-08-12")
    second = service.run(now=_at("2026-08-12"), as_of_date="2026-08-12")

    assert (first.published_count, second.skipped_count) == (1, 1)
    assert cache.calls == []
    assert len(published) == 1


def test_initial_mature_reads_only_exact_dates_and_horizon_milestones(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, cache, published = _service(tmp_path, monkeypatch, [_source(71, "2026-08-11")])
    monkeypatch.setattr(maintenance, "_first_target_mature", lambda *_args: True)
    monkeypatch.setattr(maintenance, "probability_outcome_required_dates", lambda *_args, **_kwargs: ("d0", "d1"))

    first = service.run(now=_at("2026-08-13"), as_of_date="2026-08-13")
    latest = service._outcome_cache.clear()  # noqa: SLF001
    service._outcome_snapshot = None  # noqa: SLF001
    second = service.run(now=_at("2026-08-19"), as_of_date="2026-08-19")

    assert latest is None
    assert first.published_count == second.published_count == 1
    assert cache.calls == [(('000001.SZ',), ('d0', 'd1'), 'qfq')] * 2
    assert len(published) == 2


def test_missing_same_asof_does_not_reread_and_retry_is_bounded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, cache, _published = _service(tmp_path, monkeypatch, [_source(71, "2026-08-11")])
    monkeypatch.setattr(maintenance, "_first_target_mature", lambda *_args: True)
    monkeypatch.setattr(maintenance, "probability_outcome_required_dates", lambda *_args, **_kwargs: ("d0",))

    service.run(now=_at("2026-08-13"), as_of_date="2026-08-13")
    service.run(now=_at("2026-08-14"), as_of_date="2026-08-14")
    same_day = service.run(now=_at("2026-08-14"), as_of_date="2026-08-14")
    for day in ("15", "18", "19", "20", "21", "22"):
        service.run(now=_at(f"2026-08-{day}"), as_of_date=f"2026-08-{day}")

    assert same_day.skipped_count == 1
    assert len(cache.calls) <= 6


def test_canonical_source_and_incremental_manifest_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sources = [
        _source(70, "2026-08-11", as_of="2026-08-11T16:10:00+08:00"),
        _source(71, "2026-08-11", as_of="2026-08-11T16:00:00+08:00"),
    ]
    service, _cache, published = _service(tmp_path, monkeypatch, sources)
    loads: list[Path] = []
    original = maintenance.load_probability_source_snapshot

    def tracked(path):
        loads.append(Path(path))
        return original(path)

    monkeypatch.setattr(maintenance, "load_probability_source_snapshot", tracked)
    first = service.run(now=_at("2026-08-12"), as_of_date="2026-08-12")
    load_count = len(loads)
    second = service.run(now=_at("2026-08-12"), as_of_date="2026-08-12")

    assert first.source_count == second.source_count == 1
    assert [item["source"]["run_id"] for item in published] == [70]
    assert len(loads) == load_count


def test_manifest_refresh_fails_closed_on_directory_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = _Cache(tmp_path / "runtime.sqlite3")
    service = maintenance.MarketScanProbabilityMaintenanceService(
        cache,
        source_directory=tmp_path / "source",
        outcome_directory=tmp_path / "outcome",
    )
    calls = 0

    def changing(_directory, _pattern):
        nonlocal calls
        calls += 1
        return ((1, calls, calls, calls), ())

    monkeypatch.setattr(maintenance, "_directory_snapshot", changing)
    with pytest.raises(maintenance.ProbabilitySourceError, match="持续变化"):
        service.run(now=_at("2026-08-12"), as_of_date="2026-08-12")


def test_outcome_manifest_refresh_skips_only_typed_legacy_semantic_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome_dir = tmp_path / "outcomes"
    outcome_dir.mkdir()
    drift = outcome_dir / f"market-scan-probability-outcomes-run-71-through-2026-08-13-{'a' * 64}.json.gz"
    drift.write_bytes(b"intact-legacy-shape")
    service = maintenance.MarketScanProbabilityMaintenanceService(
        _Cache(tmp_path / "runtime.sqlite3"),
        source_directory=tmp_path / "sources",
        outcome_directory=outcome_dir,
    )

    def load(path: str | Path) -> dict[str, object]:
        if Path(path) == drift:
            raise maintenance.ProbabilityOutcomeSemanticDriftError(
                "legacy rule profile",
                run_id=71,
                as_of_date="2026-08-13",
                generated_at="2026-08-13T18:00:00+08:00",
                integrity_digest="a" * 64,
                source_digest=f"{71:064x}",
            )
        raise maintenance.ProbabilityOutcomeError("invalid outcome")

    monkeypatch.setattr(maintenance, "load_probability_outcome_artifact", load)
    assert service._outcome_manifests() == ()  # noqa: SLF001

    invalid = outcome_dir / f"market-scan-probability-outcomes-run-72-through-2026-08-13-{'b' * 64}.json.gz"
    invalid.write_bytes(b"invalid")
    with pytest.raises(maintenance.ProbabilityOutcomeError, match="invalid outcome"):
        service._outcome_manifests()  # noqa: SLF001


def test_latest_semantic_drift_is_terminal_across_service_restarts_and_newer_valid_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = maintenance._SourceManifest(  # noqa: SLF001
        path=tmp_path / "source.json.gz",
        run_id=71,
        quote_date="2026-08-11",
        as_of="2026-08-11T16:00:00+08:00",
        captured_at="2026-08-11T16:05:00+08:00",
        cohort=("official", "全市场A股", "v1"),
        digest=f"{71:064x}",
    )
    old_valid = _outcome_manifest_fixture(
        tmp_path / "old-valid.json.gz",
        as_of_date="2026-08-11",
        generated_at="2026-08-11T18:00:00+08:00",
    )
    drift = maintenance._OutcomeSemanticDriftManifest(  # noqa: SLF001
        path=tmp_path / "drift.json.gz",
        run_id=71,
        as_of_date="2026-08-13",
        generated_at="2026-08-13T18:00:00+08:00",
        digest="d" * 64,
        source_digest=source.digest,
    )
    cache = _Cache(tmp_path / "runtime.sqlite3")
    published = 0

    def forbidden_publish(*_args, **_kwargs):
        nonlocal published
        published += 1
        raise AssertionError("terminal legacy outcome must not publish")

    monkeypatch.setattr(maintenance, "publish_built_probability_outcome_artifact", forbidden_publish)

    def fresh_service() -> maintenance.MarketScanProbabilityMaintenanceService:
        service = maintenance.MarketScanProbabilityMaintenanceService(cache)
        monkeypatch.setattr(service, "_source_manifests", lambda: (source,))

        def outcomes() -> tuple[maintenance._OutcomeManifest, ...]:  # noqa: SLF001
            service._semantic_drift_by_run = {71: drift}  # noqa: SLF001
            return (old_valid,)

        monkeypatch.setattr(service, "_outcome_manifests", outcomes)
        return service

    summaries = [
        fresh_service().run(now=_at("2026-08-13"), as_of_date="2026-08-13")
        for _attempt in range(2)
    ]

    assert [(item.due_count, item.skipped_count, item.failed_count) for item in summaries] == [
        (0, 1, 0),
        (0, 1, 0),
    ]
    assert cache.calls == []
    assert published == 0
    newer_valid = _outcome_manifest_fixture(
        tmp_path / "newer-valid.json.gz",
        as_of_date="2026-08-14",
        generated_at="2026-08-14T18:00:00+08:00",
    )
    assert maintenance._terminal_semantic_drift(source, newer_valid, drift) is False  # noqa: SLF001
    recovery = maintenance.MarketScanProbabilityMaintenanceService(cache)
    monkeypatch.setattr(recovery, "_source_manifests", lambda: (source,))

    def recovery_outcomes() -> tuple[maintenance._OutcomeManifest, ...]:  # noqa: SLF001
        recovery._semantic_drift_by_run = {71: drift}  # noqa: SLF001
        return (old_valid, newer_valid)

    published_recovery: list[dict[str, object]] = []
    monkeypatch.setattr(recovery, "_outcome_manifests", recovery_outcomes)
    monkeypatch.setattr(maintenance, "load_probability_source_snapshot", lambda _path: _source(71, "2026-08-11"))
    monkeypatch.setattr(maintenance, "_source_kline_rows", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(maintenance, "build_probability_outcome_artifact", _build_outcome)
    monkeypatch.setattr(
        maintenance,
        "publish_built_probability_outcome_artifact",
        lambda _directory, candidate: published_recovery.append(cast(dict[str, object], candidate)),
    )

    recovered = recovery.run(now=_at("2026-08-19"), as_of_date="2026-08-19")

    assert (recovered.due_count, recovered.published_count, recovered.failed_count) == (1, 1, 0)
    assert len(published_recovery) == 1


def test_maintenance_isolates_one_source_failure_and_reports_degraded_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, _cache, published = _service(
        tmp_path,
        monkeypatch,
        [_source(71, "2026-08-11"), _source(72, "2026-08-12")],
    )
    original_build = maintenance.build_probability_outcome_artifact

    def build(source, rows, **kwargs):
        if source["payload"]["run"]["run_id"] == 72:
            raise RuntimeError("  provider   secret\nfailed  ")
        return original_build(source, rows, **kwargs)

    monkeypatch.setattr(maintenance, "build_probability_outcome_artifact", build)

    summary = service.run(now=_at("2026-08-12"), as_of_date="2026-08-12")

    assert summary.published_count == 1
    assert len(published) == 1
    assert summary.failed_count == 1
    assert summary.degraded is True
    assert summary.failures == ("run 72: provider secret failed",)
    assert "失败 1 个" in summary.message()


def test_fit_maintenance_publishes_once_then_reuses_corpus_digest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = _Cache(tmp_path / "runtime.sqlite3")
    service = maintenance.MarketScanProbabilityMaintenanceService(
        cache,
        source_directory=tmp_path / "source",
        outcome_directory=tmp_path / "outcome",
    )
    source_path = tmp_path / "source.json.gz"
    outcome_path = tmp_path / "outcome.json.gz"
    source = maintenance._SourceManifest(  # noqa: SLF001 - orchestration boundary fixture
        path=source_path,
        run_id=71,
        quote_date="2026-08-11",
        as_of="2026-08-11T16:00:00+08:00",
        captured_at="2026-08-11T16:30:00+08:00",
        cohort=("official", "全市场A股", "v1"),
        digest="a" * 64,
    )
    outcome = maintenance._OutcomeManifest(  # noqa: SLF001 - orchestration boundary fixture
        path=outcome_path,
        run_id=71,
        as_of_date="2026-09-09",
        generated_at="2026-09-09T18:00:00+08:00",
        digest="b" * 64,
        source_digest="a" * 64,
        horizon_quality={},
        state_digest="c" * 64,
    )
    monkeypatch.setattr(service, "_source_manifests", lambda: (source,))
    monkeypatch.setattr(service, "_outcome_manifests", lambda: (outcome,))
    monkeypatch.setattr(maintenance, "_ready_fit_cohorts", lambda *_args: [[(source, outcome)]])
    built: list[tuple[object, object]] = []
    published: list[object] = []

    def build(source_paths, outcome_paths, **_kwargs):
        built.append((tuple(source_paths), tuple(outcome_paths)))
        return {"assessment": True}

    monkeypatch.setattr(maintenance, "build_bounded_probability_fit_assessment", build)
    monkeypatch.setattr(
        maintenance,
        "publish_probability_fit_assessment",
        lambda _directory, artifact: published.append(artifact),
    )

    first = service.run(now=_at("2026-09-09"), as_of_date="2026-09-09")
    second = service.run(now=_at("2026-09-09"), as_of_date="2026-09-09")

    assert (first.fit_assessment_count, first.fit_status) == (1, "projection_pending")
    assert (second.fit_assessment_count, second.fit_status) == (0, "unchanged")
    assert built == [((source_path,), (outcome_path,))]
    assert published == [{"assessment": True}]


def test_fit_maintenance_failure_is_fail_closed_without_losing_batch_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, _cache, _published = _service(tmp_path, monkeypatch, [])
    pair = (SimpleNamespace(cohort=("official", "scope", "v1")), SimpleNamespace())
    monkeypatch.setattr(maintenance, "_ready_fit_cohorts", lambda *_args: [[pair]])
    monkeypatch.setattr(
        service,
        "_maintain_fit_cohort",
        lambda _pairs: (_ for _ in ()).throw(RuntimeError("fit failed")),
    )

    summary = service.run(now=_at("2026-08-12"), as_of_date="2026-08-12")

    assert summary.fit_assessment_count == 0
    assert summary.fit_status == "failed"
    assert summary.failed_count == 1
    assert summary.failures == ("fit assessment: fit failed",)


def test_bounded_fit_assessment_is_replayed_compact_and_never_selection_qualified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / f"market-scan-probability-source-run-71-{'a' * 64}.json.gz"
    outcome = tmp_path / f"market-scan-probability-outcomes-run-71-{'b' * 64}.json.gz"
    rows = tuple(
        ProbabilityResearchRow(
            run_id=71,
            symbol=symbol,
            session_date="2026-08-11",
            features={"feature": 1.0},
            labels={},
            mature_horizons=frozenset(),
            dimensions={},
            source_evidence_digest="c" * 64,
            mode="official",
            scope="全市场A股",
            rule_version="full-market-scan-v6:test",
        )
        for symbol in ("600000.SH", "000001.SZ", "920001.BJ")
    )
    evidence = {
        "status": "calibrated_shadow",
        "fit_status": "fitted_oos",
        "selection_qualified": True,
        "selection_qualification": {"passed": True},
        "counts": {},
        "training_cutoff": "2026-08-11",
        "evidence_digest": "e" * 64,
        "input_digest": "f" * 64,
        "deterministic_replay_verified": True,
        "promotion_gates": {},
        "limitations": [],
    }
    research = {
        "cohorts": [{
            "cohort_contract": {
                "mode": "official", "scope": "全市场A股", "rule_version": "full-market-scan-v6:test",
            },
        }],
        "horizons": {
            str(horizon): {target: dict(evidence) for target in ("net_excess_positive", "absolute_net_positive")}
            for horizon in (1, 5, 20)
        },
        "research_digest": "d" * 64,
    }
    monkeypatch.setattr(
        fit_assessment,
        "probability_research_rows_from_outcome_artifacts",
        lambda *_args: rows,
    )
    captured: dict[str, object] = {}

    def build(_rows, **kwargs):
        captured.update(kwargs)
        return research

    monkeypatch.setattr(fit_assessment, "build_probability_research", build)

    artifact = fit_assessment.build_bounded_probability_fit_assessment(
        [source],
        [outcome],
        generated_at="2026-08-12T18:00:00+08:00",
        bootstrap_samples=10,
    )
    payload = cast(dict[str, object], artifact["payload"])

    assert captured["include_records"] is False
    assert payload["fit_status"] == "sampled_oos_assessment"
    assert payload["records_included"] is False
    assert payload["fit_selection_qualified"] is False
    assert cast(dict[str, object], payload["fit_selection_qualification"])["passed"] is False
    horizon = cast(dict, cast(dict, payload["horizons"])["5"])["net_excess_positive"]
    assert horizon["selection_qualified"] is False
    published = fit_assessment.publish_probability_fit_assessment(tmp_path / "fits", artifact)
    loaded = fit_assessment.load_probability_fit_assessment(cast(str, published["path"]))
    assert loaded == artifact

    for mutate in (
        lambda value: cast(dict, value["payload"]).pop("members"),
        lambda value: cast(dict, value["payload"]).__setitem__("input_pair_digest", "0" * 64),
        lambda value: _fit_horizon(value).__setitem__("selection_qualified", True),
        lambda value: _fit_horizon(value).__setitem__("counts", {"tampered": 1}),
        lambda value: _fit_horizon(value).__setitem__("evidence_digest", "0" * 64),
        lambda value: _fit_horizon(value).__setitem__("training_cutoff", "2026-08-10"),
        lambda value: _fit_horizon(value).__setitem__("promotion_gates", {"passed": True}),
        lambda value: cast(dict, value["payload"]).__setitem__("research_digest", "0" * 64),
    ):
        tampered = deepcopy(artifact)
        mutate(tampered)
        tampered_payload = cast(dict[str, object], tampered["payload"])
        cast(dict[str, object], tampered["integrity"])["integrity_digest"] = fit_assessment.sha256_hex(
            fit_assessment.canonical_json_text(tampered_payload)
        )
        with pytest.raises(ValueError):
            fit_assessment.verify_probability_fit_assessment(tampered)


def test_probability_fit_requires_all_three_horizons_to_reach_conservative_floor() -> None:
    def manifest(*, h1: bool = True):
        return SimpleNamespace(horizon_quality={
            "1": {"available_for_study": h1},
            "5": {"available_for_study": True},
            "20": {"available_for_study": True},
        })

    incomplete = [manifest(h1=index > 0) for index in range(260)]

    assert fit_assessment.probability_fit_corpus_ready(incomplete) is False
    assert fit_assessment.probability_fit_corpus_ready([manifest() for _index in range(260)]) is True


def _fit_horizon(artifact: dict[str, object]) -> dict[str, object]:
    payload = cast(dict[str, object], artifact["payload"])
    horizons = cast(dict[str, object], payload["horizons"])
    horizon = cast(dict[str, object], horizons["5"])
    return cast(dict[str, object], horizon["net_excess_positive"])


def _service(tmp_path: Path, monkeypatch, sources):
    source_dir, outcome_dir = tmp_path / "source", tmp_path / "outcome"
    source_dir.mkdir()
    outcome_dir.mkdir()
    artifacts = {}
    for source in sources:
        path = source_dir / f"market-scan-probability-source-run-{source['run_id']}-{source['digest']}.json.gz"
        path.write_bytes(b"source")
        artifacts[path] = source
    cache = _Cache(tmp_path / "runtime.sqlite3")
    published: list[dict[str, object]] = []
    outcomes: dict[Path, dict[str, object]] = {}
    monkeypatch.setattr(maintenance, "load_probability_source_snapshot", lambda path: artifacts[Path(path)])
    monkeypatch.setattr(maintenance, "load_probability_outcome_artifact", lambda path: outcomes[Path(path)])
    monkeypatch.setattr(maintenance, "build_probability_outcome_artifact", _build_outcome)

    def publish(directory, candidate):
        payload = cast(dict, candidate["payload"])
        source = cast(dict, payload["source"])
        path = Path(directory) / f"market-scan-probability-outcomes-run-{source['run_id']}-{len(outcomes)}.json.gz"
        path.write_bytes(b"outcome")
        outcomes[path] = candidate
        published.append(payload)
        return {"path": str(path), "digest": candidate["integrity"]["integrity_digest"]}

    monkeypatch.setattr(maintenance, "publish_built_probability_outcome_artifact", publish)
    return maintenance.MarketScanProbabilityMaintenanceService(
        cache,
        source_directory=source_dir,
        outcome_directory=outcome_dir,
    ), cache, published


def _source(run_id: int, quote_date: str, *, as_of: str | None = None):
    timestamp = as_of or f"{quote_date}T16:00:00+08:00"
    return {
        "run_id": run_id,
        "quote_date": quote_date,
        "as_of": timestamp,
        "captured_at": timestamp,
        "digest": f"{run_id:064x}",
        "payload": {
            "run": {"run_id": run_id, "quote_date": quote_date, "as_of": timestamp},
            "cohort": {"mode": "official", "scope": "全市场A股", "rule_version": "v1"},
            "records": [{"symbol": "000001.SZ"}],
        },
        "integrity": {"integrity_digest": f"{run_id:064x}"},
    }


def _build_outcome(source, rows, *, generated_at, as_of_date):
    source_run = source["payload"]["run"]
    has_rows = bool(rows)
    horizons = {
        str(horizon): {
            "target_session_date": {1: "2026-08-13", 5: "2026-08-19", 20: "2026-09-09"}[horizon],
            "mature": as_of_date >= {1: "2026-08-13", 5: "2026-08-19", 20: "2026-09-09"}[horizon],
            "data_unavailable_record_count": 0 if has_rows else 1,
        }
        for horizon in (1, 5, 20)
    }
    payload = {
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "source": {
            "run_id": source_run["run_id"],
            "integrity_digest": source["integrity"]["integrity_digest"],
        },
        "quality": {"horizons": horizons},
        "records": [],
    }
    return {
        "generated_at": generated_at,
        "payload": payload,
        "integrity": {"integrity_digest": maintenance.stable_probability_hash(payload)},
    }


def _outcome_manifest_fixture(
    path: Path,
    *,
    as_of_date: str,
    generated_at: str,
) -> maintenance._OutcomeManifest:  # noqa: SLF001
    return maintenance._OutcomeManifest(  # noqa: SLF001
        path=path,
        run_id=71,
        as_of_date=as_of_date,
        generated_at=generated_at,
        digest=as_of_date.replace("-", "").ljust(64, "0"),
        source_digest=f"{71:064x}",
        horizon_quality={
            "1": {"target_session_date": "2026-08-13", "mature": True},
            "5": {"target_session_date": "2026-08-19", "mature": False},
            "20": {"target_session_date": "2026-09-09", "mature": False},
        },
        state_digest="s" * 64,
    )


def _at(value: str) -> datetime:
    return datetime.fromisoformat(f"{value}T18:00:00+08:00")
