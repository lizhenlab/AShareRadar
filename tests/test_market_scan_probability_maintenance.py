from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from app.services import market_scan_probability_maintenance as maintenance
from app.services import market_scan_probability_fit_assessment as fit_assessment
from app.services.market_scan_probability_research import ProbabilityResearchRow


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


def _at(value: str) -> datetime:
    return datetime.fromisoformat(f"{value}T18:00:00+08:00")
