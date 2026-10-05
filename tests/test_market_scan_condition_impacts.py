"""Exact single-condition removal over the complete frozen population."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from app.market_scan_screening import compile_screen_conditions
from app.models.market_scan_screening import MarketScanScreenEvaluateRequest, MarketScanScreenEvaluationV2
from app.repositories.market_scan_screening_sql import screen_condition_sql
from app.services import market_scan_screening as screening
from tests.test_market_scan_screening import _FrozenRepository, _evaluation_projection, _item, _reseal, _rows, _run


def _dimension_rows():
    rows = []
    for index, (confidence, risk) in enumerate(((90, 10), (0, 10), (None, 10), (70, 90), (90, 0), (90, None)), start=1):
        item = _item(f"{index:06d}.SH", name=f"样本 {index}", industry="半导体", score=90, change_pct=0, confidence=confidence)
        rows.append(item.model_copy(update={"rank": index, "score_details": {"components": {"score_dimensions": {"scores": {"confidence": confidence, "risk": risk}}}}}))
    return rows


def _request(**kwargs):
    return MarketScanScreenEvaluateRequest.model_validate({
        "spec": {"status": "success", "ranges": {"confidence": {"min": 80}, "risk": {"max": 50}}},
        **kwargs,
    })


def _evaluation(**kwargs):
    rows = _dimension_rows()
    repository = _FrozenRepository(_run().model_copy(update={"total_count": len(rows)}), rows)
    return screening.MarketScanScreeningService(repository).evaluate(41, _request(**kwargs))


@pytest.mark.parametrize("page,page_size,near_limit,max_failures", [(1, 100, 20, 1), (99, 1, 0, 3), (2, 1, 1, 2)])
def test_exact_impacts_distinguish_zero_missing_and_multiple_failures(page, page_size, near_limit, max_failures):
    result = _evaluation(page=page, page_size=page_size, near_miss_limit=near_limit, near_miss_max_failures=max_failures)
    assert result.matched_count == 2
    assert [(x.condition_code, x.additional_count, x.missing_additional_count, x.matched_without_condition) for x in result.condition_impacts] == [
        ("status", 0, 0, 2), ("range.confidence", 2, 1, 4), ("range.risk", 1, 1, 3),
    ]
    examples = result.condition_impacts[1].examples
    assert [(x.symbol, x.observed_value, x.missing) for x in examples] == [("000002.SH", 0, False), ("000003.SH", None, True)]
    assert not any(x.symbol == "000004.SH" for effect in result.condition_impacts for x in effect.examples)


def test_shared_fixture_is_the_actual_complete_service_response():
    path = Path(__file__).parent / "fixtures" / "market_scan_screen_evaluation_v2.json"
    assert json.loads(path.read_text()) == _evaluation().model_dump(mode="json")


@pytest.mark.parametrize("spec", [
    {"status": None},
    {"status": "success", "ranges": {"score": {"min": 80, "max": 85}, "confidence": {"min": 70}}},
    {"status": None, "markets": ["SH", "SZ"], "industries": ["半导体", "不存在"]},
    {"status": "success", "keyword": "sh", "is_st": False, "is_new": False},
    {"status": None, "keyword": "%_"},
    {"status": "missing", "ranges": {"confidence": {"min": 0}, "risk": {"max": 50}}},
])
def test_independent_sql_removing_each_whole_condition_matches_exact_set_difference(spec):
    rows = _rows() + [x.model_copy(update={"symbol": f"70000{i}.SH", "code": f"70000{i}"}) for i, x in enumerate(_dimension_rows())]
    request = MarketScanScreenEvaluateRequest.model_validate({"spec": spec, "page": 500, "near_miss_limit": 0})
    repository = _FrozenRepository(_run().model_copy(update={"total_count": len(rows)}), rows)
    result = screening.MarketScanScreeningService(repository).evaluate(41, request)
    conditions = compile_screen_conditions(request.spec)
    with sqlite3.connect(":memory:") as conn:
        fields = list(asdict(_evaluation_projection(rows[0])))
        conn.execute(f"CREATE TABLE sample ({','.join(fields)}, metrics_json TEXT)")
        for row in rows:
            values = list(asdict(_evaluation_projection(row)).values()) + [json.dumps({"score_details": row.score_details})]
            conn.execute(f"INSERT INTO sample VALUES ({','.join('?' for _ in values)})", values)

        def sql_matches(without=None):
            fragments = [screen_condition_sql(c) for c in conditions if c.code != without]
            where = " AND ".join(clause for clause, _ in fragments) or "1 = 1"
            parameters = [v for _, values in fragments for v in values]
            return {str(row[0]) for row in conn.execute(f"SELECT symbol FROM sample WHERE {where}", parameters)}

        original = sql_matches()
        assert len(original) == result.matched_count
        for impact in result.condition_impacts:
            removed = sql_matches(impact.condition_code)
            added = removed - original
            assert impact.matched_without_condition == len(removed)
            assert impact.additional_count == len(added)
            assert {x.symbol for x in impact.examples} <= added
    assert repository.hydrated_symbols == []  # Counts cannot depend on either returned subset.


def test_conditions_are_evaluated_once_and_funnel_reuses_full_failure_map(monkeypatch):
    calls = []
    original = screening._condition_failure

    def counted(item, condition):
        calls.append((item.symbol, condition.code))
        return original(item, condition)

    monkeypatch.setattr(screening, "_condition_failure", counted)
    _evaluation()
    assert len(calls) == len(set(calls)) == 6 * 3


@pytest.mark.parametrize("rows", [[], _dimension_rows()])
def test_no_conditions_has_no_impacts_and_empty_population_conserves_counts(rows):
    request = MarketScanScreenEvaluateRequest.model_validate({"spec": {"status": None}, "near_miss_limit": 0})
    result = screening.MarketScanScreeningService(_FrozenRepository(_run(), rows)).evaluate(41, request)
    assert result.condition_impacts == []
    assert result.matched_count == len(rows)


def test_300_detail_hydration_remains_bounded_and_impacts_are_only_three_scalars():
    rows = [_item(f"{i:06d}.SH", name=str(i), industry=None, score=90 if i <= 200 else 70, change_pct=0, confidence=90) for i in range(1, 301)]
    repository = _FrozenRepository(_run(), rows)
    request = MarketScanScreenEvaluateRequest.model_validate({"spec": {"ranges": {"score": {"min": 80}}}, "page_size": 200, "near_miss_limit": 100})
    result = screening.MarketScanScreeningService(repository).evaluate(41, request)
    assert len(result.matched.items) == 200 and len(result.near_misses) == 100
    assert len(repository.hydrated_symbols) == 1 and len(repository.hydrated_symbols[0]) == 300
    impact = result.condition_impacts[1]
    assert impact.additional_count == 100 and len(impact.examples) == 3
    assert [x.symbol for x in impact.examples] == ["000201.SH", "000202.SH", "000203.SH"]


def _mutated_payload(kind: str) -> dict[str, Any]:
    payload = deepcopy(_evaluation().model_dump(mode="json"))
    impact = payload["condition_impacts"][1]
    example = impact["examples"][0]
    if kind == "unknown":
        impact["condition_code"] = "invented"
    if kind == "duplicate":
        payload["condition_impacts"][2]["condition_code"] = impact["condition_code"]
    if kind == "omitted":
        payload["condition_impacts"].pop()
    if kind == "total":
        impact["matched_without_condition"] += 1
    if kind == "missing":
        impact["missing_additional_count"] = 3
    if kind == "truncated":
        impact["examples"].pop()
    if kind == "null_zero":
        example["missing"] = True
    if kind == "wrong_run":
        example["run_id"] = 99
    if kind == "overlap":
        example.update({"symbol": "000001.SH", "code": "000001"})
    if kind == "actually_passes":
        example["observed_value"] = 99
    if kind == "numeric_bool":
        example["observed_value"] = False
    if kind == "infinite":
        example["observed_value"] = float("inf")
    if kind == "repeat_example":
        impact["examples"][1] = dict(example)
    if kind == "exclusion":
        payload["exclusion_reasons"][0]["count"] = 0
    if kind == "v1":
        payload["schema_version"] = "market-scan-screen-evaluation-v1"
    if kind != "infinite":
        _reseal(payload)
    return payload


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "omitted", "total", "missing", "truncated", "null_zero", "wrong_run", "overlap", "numeric_bool", "actually_passes", "infinite", "repeat_example", "exclusion", "v1"])
def test_v2_rejects_resealed_semantic_corruption(kind):
    with pytest.raises(ValueError):
        MarketScanScreenEvaluationV2.model_validate(_mutated_payload(kind))


@pytest.mark.parametrize("kind", ["duplicate", "unexpected", "missing", "wrong_total"])
def test_bounded_hydration_rejects_corrupted_selected_set(monkeypatch, kind):
    repository = _FrozenRepository(_run(), _rows())
    original = repository.results_page

    def corrupted(**query):
        page = original(**query)
        items = list(page.items)
        if kind == "duplicate":
            items[1] = items[0]
        if kind == "unexpected":
            items[1] = items[1].model_copy(update={"symbol": "888888.SH", "code": "888888"})
        if kind == "missing":
            items.pop()
        return page.model_copy(update={"items": items, "total": 999 if kind == "wrong_total" else page.total})

    monkeypatch.setattr(repository, "results_page", corrupted)
    with pytest.raises(RuntimeError):
        screening.MarketScanScreeningService(repository).evaluate(41, MarketScanScreenEvaluateRequest())


def test_verified_evaluation_reads_projection_and_300_details_in_one_snapshot(tmp_path, monkeypatch):
    from app.repositories import market_scan_verified_read as reads
    from app.models.market_scan import MarketScanSeed
    from app.services.cache import SQLiteCache
    from tests.test_market_scan_repository import _seed_running_run, _write

    cache = SQLiteCache(tmp_path / "300-details.sqlite3")
    seeds = [MarketScanSeed(f"{i:06d}.SH", f"{i:06d}", "SH", str(i), "半导体", "20000101") for i in range(1, 301)]
    run = _seed_running_run(cache.market_scan_repo, seeds)
    writes = [_write(seed.symbol, status="success", score=90 if i <= 200 else 70, quality=90) for i, seed in enumerate(seeds, start=1)]
    cache.market_scan_repo.save_result_batch(run.id, writes)
    cache.market_scan_repo.finish_run(run.id, "success", message="isolated fixture")
    observed = []
    for name in ("_verified_read_identity", "read_screening_rows", "_verified_market_scan_result_page"):
        original = getattr(reads, name)

        def tracked(conn, *args, _original=original, _name=name, **kwargs):
            observed.append((_name, id(conn), conn.in_transaction, conn.execute("PRAGMA query_only").fetchone()[0]))
            return _original(conn, *args, **kwargs)

        monkeypatch.setattr(reads, name, tracked)
    request = MarketScanScreenEvaluateRequest.model_validate({"spec": {"ranges": {"score": {"min": 80}}}, "page_size": 200, "near_miss_limit": 100})
    response = screening.MarketScanScreeningService(cache).evaluate(run.id, request)
    assert len(response.matched.items) == 200 and len(response.near_misses) == 100
    assert [x[0] for x in observed] == ["_verified_read_identity", "read_screening_rows", "_verified_market_scan_result_page"]
    assert len({x[1] for x in observed}) == 1
    assert all(x[2:] == (True, 1) for x in observed)
    assert response.condition_impacts[1].additional_count == 100


def test_screening_scalar_capability_is_once_only_local_and_closed(tmp_path):
    import threading
    from tests.test_strategy_execution import _environment

    cache, _, _, run_id = _environment(tmp_path)
    failures = []
    with cache.verified_market_scan_read(run_id) as verified:
        def read_elsewhere():
            try:
                verified.screening_rows()
            except RuntimeError as exc:
                failures.append(str(exc))
        thread = threading.Thread(target=read_elsewhere)
        thread.start()
        thread.join()
        assert len(failures) == 1 and "跨线程" in failures[0]
        assert len(verified.screening_rows()) == verified.run.total_count
        with pytest.raises(RuntimeError, match="只能读取一次筛选投影"):
            verified.screening_rows()
    with pytest.raises(RuntimeError, match="已关闭"):
        verified.screening_rows()


def test_screening_scalar_capability_rejects_restarted_transaction(tmp_path, monkeypatch):
    from tests.test_strategy_execution import _environment
    from tests.test_market_scan_snapshot_integrity import _capture_verified_connection

    cache, _, _, run_id = _environment(tmp_path)
    captured = _capture_verified_connection(monkeypatch)
    with cache.verified_market_scan_read(run_id) as verified:
        conn = captured[-1]
        conn.rollback()
        conn.execute("BEGIN")
        with pytest.raises(RuntimeError, match="snapshot 已失效"):
            verified.screening_rows()


def test_screening_frozen_new_listing_verification_never_refreshes_calendar(tmp_path, monkeypatch):
    from app.services.cache import SQLiteCache
    from app.services import trading_calendar
    from tests.test_market_scan_skip_contract import _valid_action_source_run

    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "false")
    _, settings, run_id, _, _ = _valid_action_source_run(tmp_path)
    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "true")
    monkeypatch.setattr(trading_calendar, "_should_auto_refresh", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(trading_calendar, "_trigger_auto_refresh", lambda *_args, **_kwargs: pytest.fail("read-only evaluation must not refresh providers"))
    response = screening.MarketScanScreeningService(SQLiteCache(settings=settings)).evaluate(run_id, MarketScanScreenEvaluateRequest())
    assert response.population_count == 101


@pytest.mark.parametrize("field,spec,passing_value", [
    ("range.score", {"status": None, "ranges": {"score": {"min": 80, "max": 85}}}, 80),
    ("range.score", {"status": None, "ranges": {"score": {"min": 80, "max": 85}}}, 85),
    ("industry", {"status": None, "industries": ["不存在"]}, "包含不存在行业"),
    ("is_st", {"status": None, "is_st": True}, True),
    ("is_new", {"status": None, "is_new": True}, True),
    ("keyword", {"status": None, "keyword": "未匹配"}, "未匹配名称"),
    ("market", {"status": None, "markets": ["BJ"]}, "BJ"),
    ("status", {"status": "skipped"}, "skipped"),
])
def test_resealed_example_that_passes_removed_condition_is_rejected(field, spec, passing_value):
    result = screening.MarketScanScreeningService(_FrozenRepository(_run(), _rows())).evaluate(
        41, MarketScanScreenEvaluateRequest.model_validate({"spec": spec, "near_miss_limit": 0}),
    )
    payload = result.model_dump(mode="json")
    examples = next(x for x in payload["condition_impacts"] if x["condition_code"] == field)["examples"]
    example = next(x for x in examples if not x["missing"])
    example["observed_value"] = passing_value
    example["missing"] = False
    if field == "keyword":
        example["name"] = passing_value
    if field == "status":
        example["status"] = passing_value
    if field == "market":
        example["market"] = passing_value
        example["symbol"] = f"{example['code']}.{passing_value}"
    _reseal(payload)
    with pytest.raises(ValueError, match="并未违反"):
        MarketScanScreenEvaluationV2.model_validate(payload)


def test_empty_population_retains_all_zero_impact_conditions():
    result = screening.MarketScanScreeningService(_FrozenRepository(_run(), [])).evaluate(41, _request())
    assert len(result.condition_impacts) == 3
    assert all(item.additional_count == item.missing_additional_count == item.matched_without_condition == 0 and not item.examples for item in result.condition_impacts)
