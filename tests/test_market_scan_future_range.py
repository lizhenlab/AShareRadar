from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import sqlite3
from typing import Any, cast

import pytest

from app.db.schema import initialize_schema
from app.db.market_scan_integrity import seal_market_scan_snapshot
from app.models.market_scan import MARKET_SCAN_TOP100_REFRESH_SCOPE, MarketScanResultItem
from app.repositories.market_scan_mapping import encode_result_payload
import app.services.market_scan_future_range as future_range_module
from app.services.market_scan_future_range import (
    FUTURE_RANGE_CENTER_PROXY,
    FutureRangeConfig,
    FutureRangeResearchError,
    evaluate_market_scan_future_range,
)
from app.services.market_scan_score_dimensions import build_market_scan_score_dimensions
from app.services.trading_calendar import trading_dates_between
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.market_scan_probability_artifact import build_probability_artifact, write_probability_artifact
from tests.market_scan_test_support import action_pass_publication_diagnostics
from tests.market_scan_test_support import distribution_degraded_publication_diagnostics
from app.services.market_scan_future_range_artifact import build_future_range_artifact
from tests.factories import make_kline, make_quote
from tests.test_market_scan_probability_artifact import _payload as _probability_payload


SIGNAL_DATE = date(2025, 12, 31)
TARGET_DATES = (date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7))


def test_future_range_uses_fixed_sessions_point_in_time_bar_and_execution_costs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "future-range.sqlite3"
    _initialize(path)
    run_id = _seed_run(path, scope=FULL_MARKET_SCOPE, with_targets=True, suspended_second_symbol=True)
    monkeypatch.setattr(future_range_module, "next_trade_dates", lambda _value, _count: TARGET_DATES)
    before = path.read_bytes()

    report = evaluate_market_scan_future_range(
        path,
        config=FutureRangeConfig(
            minimum_sample_size=1,
            minimum_session_count=1,
            complete_run_coverage=0.5,
            bootstrap_samples=100,
        ),
        run_ids=[run_id],
        generated_at="2026-01-08T08:00:00+00:00",
    )

    assert path.read_bytes() == before
    payload = cast(list[dict[str, Any]], report["reports"])[0]
    assert payload["config"]["center_proxy"] == FUTURE_RANGE_CENTER_PROXY
    assert payload["config"]["bootstrap_method"] == "ordered_moving_block_by_signal_date"
    records = cast(list[dict[str, Any]], payload["records"])
    first = records[0]
    assert first["d_bar"]["close"] == 100  # 来自冻结 evidence，而不是由未来标签倒推
    assert first["source_evidence"]["target_adjustment_continuity"] == "verified"
    one, two, three = first["offsets"]
    assert one["target_session_date"] == "2026-01-05"
    assert one["d1_open_reference"]["cumulative_path"]["daily_bar_path_unknown"] is True
    assert one["daily_bar_path_unknown"] is True
    assert one["execution"]["status"] == "data_unavailable"
    assert one["execution"]["reason"] == "A_share_T_plus_1_no_same_session_exit"
    assert two["execution"]["status"] == "modelled"
    assert two["execution"]["entry_date"] == "2026-01-05"
    assert two["execution"]["exit_date"] == "2026-01-06"
    assert two["execution"]["net_return"] < two["execution"]["gross_return"]
    assert three["execution"]["exit_date"] == "2026-01-07"
    assert two["execution"]["cost_model_version"]
    benchmark_values = [record["offsets"][1]["execution"]["net_excess_return"] for record in records]
    assert sum(value for value in benchmark_values if value is not None) == pytest.approx(0)
    second = records[1]
    assert second["offsets"][1]["target_session_date"] == "2026-01-06"
    assert second["offsets"][1]["fixed_session_status"] == "unavailable"
    assert second["offsets"][1]["reason"] == "target_session_suspended_or_zero_volume"
    assert second["offsets"][2]["d1_open_reference"]["status"] == "unavailable"
    assert payload["probability_context"]["status"] == "not_available"
    groups = payload["groups"]
    assert {item["group_type"] for item in groups} == {"all", "top_n", "decile"}
    assert {item["group_value"] for item in groups if item["group_type"] == "top_n"} == {"20", "50", "100"}
    all_d1 = next(item for item in groups if item["group_type"] == "all" and item["session_offset"] == 1)
    assert all_d1["metrics"]["level_shift_hlc3_proxy"]["sample_size"] == 2
    assert all_d1["metrics"]["level_shift_hlc3_proxy"]["ci95"] is None  # block=3，单个独立日期不伪造CI
    ic = next(item for item in payload["rank_ic"] if item["session_offset"] == 1 and item["metric"] == "level_shift_hlc3_proxy")
    assert ic["mean_rank_ic"] == pytest.approx(1)
    assert all(item["status"] == "insufficient_data" for item in payload["monotonicity"])
    block_ci = future_range_module._bootstrap_ci(
        (0.01, 0.02, -0.01, 0.03, 0.04),
        FutureRangeConfig(minimum_sample_size=1, minimum_session_count=1, bootstrap_samples=100),
        "ordered-sessions",
    )
    assert block_ci is not None and block_ci[0] <= block_ci[1]


def test_future_range_rejects_distribution_degraded_action_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "future-range-ineligible.sqlite3"
    _initialize(path)
    run_id = _seed_run(
        path,
        scope=FULL_MARKET_SCOPE,
        with_targets=True,
        action_eligible=False,
    )

    report = evaluate_market_scan_future_range(
        path,
        config=FutureRangeConfig(minimum_sample_size=1),
        run_ids=[run_id],
        generated_at="2026-01-08T08:00:00+00:00",
    )

    payload = cast(list[dict[str, Any]], report["reports"])[0]
    assert payload["status"] == "insufficient_data"
    source = cast(dict[str, Any], payload["source"])
    exclusions = cast(list[dict[str, Any]], source["exclusions"])
    assert exclusions[0]["reason"] == "market_scan_snapshot_integrity_failed"
    assert "评分分布门禁" in exclusions[0]["detail"]


def test_future_range_rejects_top100_refresh_and_adjustment_rebase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "eligibility.sqlite3"
    _initialize(path)
    full_run = _seed_run(path, scope=FULL_MARKET_SCOPE, with_targets=True)
    top100_run = _seed_run(path, scope=MARKET_SCAN_TOP100_REFRESH_SCOPE, with_targets=False)
    monkeypatch.setattr(future_range_module, "next_trade_dates", lambda _value, _count: TARGET_DATES)
    with pytest.raises(FutureRangeResearchError, match="canonical official published"):
        evaluate_market_scan_future_range(path, run_ids=[top100_run])

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE kline_daily SET close = close * 0.5, open = open * 0.5, high = high * 0.5, low = low * 0.5 "
            "WHERE date = ?",
            (SIGNAL_DATE.isoformat(),),
        )
    report = evaluate_market_scan_future_range(
        path,
        config=FutureRangeConfig(minimum_sample_size=1, minimum_session_count=1, complete_run_coverage=0.5),
        run_ids=[full_run],
    )
    payload = cast(list[dict[str, Any]], report["reports"])[0]
    assert payload["status"] == "insufficient_data"
    assert all(
        record["source_evidence"]["target_adjustment_continuity"] == "target_adjustment_rebase_conflict"
        for record in payload["records"]
    )
    assert all(record["offsets"][0]["target_bar"] is None for record in payload["records"])
    assert build_future_range_artifact(payload, generated_at=str(payload["generated_at"]))["payload"]["status"] == "insufficient_data"  # type: ignore[index]


def test_future_range_rejects_tampered_published_snapshot_before_result_use(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tampered-snapshot.sqlite3"
    _initialize(path)
    run_id = _seed_run(path, scope=FULL_MARKET_SCOPE, with_targets=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        for trigger in (
            "trg_market_scan_published_run_immutable",
            "trg_market_scan_published_run_no_delete",
            "trg_market_scan_published_result_no_update",
            "trg_market_scan_published_result_no_delete",
            "trg_market_scan_published_result_no_insert",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(
            """
            UPDATE market_scan_result SET amount = amount + 1
            WHERE run_id = ? AND symbol = '600001.SH'
            """,
            (run_id,),
        )

    report = evaluate_market_scan_future_range(path, run_ids=[run_id])

    payload = cast(list[dict[str, Any]], report["reports"])[0]
    assert payload["status"] == "insufficient_data"
    assert payload["records"] == []
    assert payload["source"]["calendar_error"] == "market_scan_snapshot_integrity_failed"
    assert payload["source"]["exclusions"][0]["reason"] == (
        "market_scan_snapshot_integrity_failed"
    )


def test_future_range_keeps_not_mature_records_without_zero_fabrication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "not-mature.sqlite3"
    _initialize(path)
    run_id = _seed_run(path, scope=FULL_MARKET_SCOPE, with_targets=False)
    monkeypatch.setattr(future_range_module, "next_trade_dates", lambda _value, _count: TARGET_DATES)

    report = evaluate_market_scan_future_range(path, run_ids=[run_id])

    payload = cast(list[dict[str, Any]], report["reports"])[0]
    assert payload["status"] == "insufficient_data"
    assert payload["records"]
    assert all(
        offset["fixed_session_status"] == "not_mature"
        and offset["level_shift"] is None
        and offset["target_bar"] is None
        for record in payload["records"]
        for offset in record["offsets"]
    )


def test_future_range_probability_is_oos_cohort_bound_and_keeps_original_calibration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _probability_payload(status="calibrated_shadow")
    metadata = payload["studies"][0]["metadata"]  # type: ignore[index]
    payload["feature_evidence"][0]["quote_date"] = "2026-08-01"  # type: ignore[index]
    payload["records"][0]["details"]["quote_date"] = "2026-08-01"  # type: ignore[index]
    payload["records"][0]["details"]["fold_id"] = None  # type: ignore[index]
    cohort = {"mode": "official", "scope": FULL_MARKET_SCOPE, "rule_version": "rule-v1"}
    metadata["cohort_contract"] = cohort
    artifact = build_probability_artifact(payload, generated_at="2026-08-11T10:00:00+08:00")
    probability_path = tmp_path / "probability.json"
    write_probability_artifact(probability_path, artifact, database_path=tmp_path / "unused.sqlite3")
    index = future_range_module._load_probability_context((probability_path,))
    prediction = index[(29, "600519.SH")]
    evidence = future_range_module._EvidenceResult(None, "e" * 64, "v2", None)
    matching_run = future_range_module._Run(
        29, "official", FULL_MARKET_SCOPE, "rule-v1", "2026-08-01", "2026-08-01", "2026-08-01 15:00:00",
    )
    assert future_range_module._matched_probability(prediction, evidence, matching_run)["status"] == "calibrated_shadow"
    wrong_cohort = future_range_module._Run(
        29, "official", FULL_MARKET_SCOPE, "rule-v2", "2026-08-01", "2026-08-01", "2026-08-01 15:00:00",
    )
    assert future_range_module._matched_probability(prediction, evidence, wrong_cohort)["status"] == "not_available"

    database = tmp_path / "range.sqlite3"
    _initialize(database)
    run_id = _seed_run(database, scope=FULL_MARKET_SCOPE, with_targets=True)
    monkeypatch.setattr(future_range_module, "next_trade_dates", lambda _value, _count: TARGET_DATES)
    report = evaluate_market_scan_future_range(
        database,
        config=FutureRangeConfig(minimum_sample_size=1, minimum_session_count=1, complete_run_coverage=0.5),
        run_ids=[run_id],
    )["reports"][0]
    records = cast(list[dict[str, Any]], report["records"])  # type: ignore[index]
    for record in records:
        record["probability"] = prediction
    context = future_range_module._probability_report_context(
        records, FutureRangeConfig(minimum_sample_size=1, minimum_session_count=1),
    )
    assert context["status"] == "available"
    original = context["original_target_calibration"][0]
    assert original["calibration_summary"]["brier_score"] == pytest.approx(0.2)
    assert original["semantics"] == "original_binary_target_calibration_not_range_outcome_calibration"
    comparisons = context["range_outcome_comparisons"]
    assert comparisons
    assert all(item["semantics"] == "range_outcome_comparison_not_probability_recalibration" for item in comparisons)


def test_future_range_canonicalizes_same_date_and_never_mixes_rule_cohorts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "cohorts.sqlite3"
    _initialize(database)
    stale = _seed_run(database, scope=FULL_MARKET_SCOPE, with_targets=True, rule_version="rule-v1")
    canonical = _seed_run(database, scope=FULL_MARKET_SCOPE, with_targets=True, rule_version="rule-v1")
    other_rule = _seed_run(database, scope=FULL_MARKET_SCOPE, with_targets=True, rule_version="rule-v2")
    monkeypatch.setattr(future_range_module, "next_trade_dates", lambda _value, _count: TARGET_DATES)

    evaluation = evaluate_market_scan_future_range(
        database,
        config=FutureRangeConfig(minimum_sample_size=1, minimum_session_count=1, complete_run_coverage=0.5),
    )

    reports = cast(list[dict[str, Any]], evaluation["reports"])
    assert {item["run"]["run_id"] for item in reports} == {canonical, other_rule}
    assert stale not in {item["run"]["run_id"] for item in reports}
    for report in reports:
        rule = report["run"]["rule_version"]
        assert report["source"]["context_canonical_run_count"] == 1
        assert all(item["cohort"]["rule_version"] == rule for item in report["groups"])


def _initialize(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        initialize_schema(conn)


def _seed_run(
    path: Path,
    *,
    scope: str,
    with_targets: bool,
    suspended_second_symbol: bool = False,
    rule_version: str = "rule-v1",
    action_eligible: bool = True,
) -> int:
    timestamp = f"{SIGNAL_DATE.isoformat()}T08:00:00Z"
    with closing(sqlite3.connect(path)) as conn, conn:
        run_id = conn.execute(
            """
            INSERT INTO market_scan_run (
                status, trigger, mode, rule_version, as_of, data_date, quote_date, scope,
                total_count, processed_count, success_count, publication_diagnostics_json,
                created_at, updated_at, finished_at
            ) VALUES ('success','manual','official',?,?,?,?,?,2,2,2,?,?,?,?)
            """,
            (
                rule_version, f"{SIGNAL_DATE.isoformat()} 15:00:00", SIGNAL_DATE.isoformat(), SIGNAL_DATE.isoformat(),
                scope,
                (
                    action_pass_publication_diagnostics()
                    if action_eligible
                    else distribution_degraded_publication_diagnostics()
                ).model_dump_json(),
                timestamp,
                timestamp,
                timestamp,
            ),
        ).lastrowid
        assert run_id is not None
        for rank, symbol in enumerate(("600001.SH", "000002.SZ"), start=1):
            _seed_result(conn, int(run_id), symbol, rank)
            _seed_signal_overlap(conn, symbol)
            if with_targets:
                _seed_targets(conn, symbol, rank, suspended_second_symbol)
        seal_market_scan_snapshot(conn, int(run_id), sealed_at=timestamp)
    return int(run_id)


def _seed_result(conn: sqlite3.Connection, run_id: int, symbol: str, rank: int) -> None:
    code, market = symbol.split(".")
    rows = _history_rows()
    item = MarketScanResultItem(
        run_id=run_id, symbol=symbol, code=code, market=market, name=f"样本{code}",
        industry="测试行业", list_date="2020-01-02", status="pending", updated_at="2026-01-02",
    )
    quote = make_quote(
        price=100, prev_close=99, high=101, low=98, turnover_rate=4,
        timestamp=f"{SIGNAL_DATE.isoformat()} 15:00:00",
    ).model_copy(update={"code": code, "market": market, "name": item.name, "open": 99.5, "amount": 1_000_000_000})
    dimensions = build_market_scan_score_dimensions(
        item, quote, rows, data_quality_score=100, volume_ratio=1.2, mode="official",
    )
    payload = encode_result_payload({}, {"components": {"score_dimensions": dimensions.details()}})
    conn.execute(
        """
        INSERT INTO market_scan_result (
            run_id,symbol,code,market,name,industry,list_date,status,rank,score,raw_score,
            trend_score,price,change_pct,turnover_rate,volume_ratio,amount,data_quality_score,
            is_st,is_new,adjustment_mode,metrics_json,updated_at
        ) VALUES (?,?,?,?,?,?,?,'success',?,?,?,?,?,?,?,?,?,100,0,0,'qfq',?,?)
        """,
        (
            run_id, symbol, code, market, item.name, item.industry, item.list_date, rank,
            100-rank, 100-rank/10, 100-rank, 100, 1, 4, 1.2, 1_000_000_000,
            payload, f"{SIGNAL_DATE.isoformat()}T08:00:00Z",
        ),
    )


def _history_rows() -> list[Any]:
    days = list(trading_dates_between(SIGNAL_DATE - timedelta(days=140), SIGNAL_DATE)[-61:])
    assert len(days) == 61
    return [
        make_kline(
            date=value.isoformat(), close=94 + index * 0.1, high=95 + index * 0.1,
            low=93 + index * 0.1, volume=1_000_000, data_version="signal-qfq-v1",
            as_of=f"{SIGNAL_DATE.isoformat()} 15:00:00",
        )
        for index, value in enumerate(days)
    ]


def _seed_signal_overlap(conn: sqlite3.Connection, symbol: str) -> None:
    row = _history_rows()[-1]
    _insert_bar(conn, symbol, row.date, row.open, row.high, row.low, row.close, row.volume)


def _seed_targets(conn: sqlite3.Connection, symbol: str, rank: int, suspended: bool) -> None:
    direction = 1 if rank == 1 else -1
    for offset, target_date in enumerate(TARGET_DATES, start=1):
        close = 100 + direction * offset * 2
        volume = 0 if suspended and rank == 2 and offset == 2 else 1_000_000
        _insert_bar(conn, symbol, target_date.isoformat(), close - direction, close + 1, close - 2, close, volume)


def _insert_bar(
    conn: sqlite3.Connection,
    symbol: str,
    row_date: str,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO kline_daily (
            symbol,adjustment_mode,date,open,close,high,low,volume,as_of,data_version,
            contract_version,fallback_used,source,fetched_at
        ) VALUES (?,'qfq',?,?,?,?,?,?,?,'target-qfq-v1','daily-kline.v1',0,'test',?)
        """,
        (symbol, row_date, open_price, close, high, low, volume, row_date, f"{row_date}T08:00:00Z"),
    )
