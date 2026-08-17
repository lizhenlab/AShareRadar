from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, cast

import pytest

from app.db.schema import initialize_schema
from app.db.market_scan_integrity import market_scan_snapshot_digest, seal_market_scan_snapshot
from app.services import market_scan_evaluation as evaluation
from app.services import market_scan_evaluation_metrics as evaluation_metrics
from app.services.market_scan_evaluation import (
    DEFAULT_HORIZONS,
    DEFAULT_TOP_SIZES,
    EvaluationConfig,
    evaluate_market_scan_rankings,
    evaluate_market_scan_shadow_comparison,
    evaluate_market_scan_shadow_rankings,
)
from app.services.market_scan_evaluation_compact import compact_shadow_comparison_report
from app.services.market_scan_evaluation_statistics import (
    benjamini_hochberg,
    moving_block_bootstrap_p_value,
)
from app.services.market_scan_probability_artifact import (
    PROBABILITY_RESULT_CONTRACT_VERSION,
    load_probability_artifact,
    replay_probability_artifact_set,
)
from app.services.market_scan_probability_store import MarketScanProbabilityStore
from app.services.market_scan_evaluation_exposure import (
    ExposureItem,
    board,
    exposure_audit,
    industry_taxonomy_quality,
    liquidity_bucket,
    market_regime,
    mean_optional,
    normalize_industry,
    quality_bucket,
    regime_overlay,
    scan_time_bucket,
)
from tools import evaluate_market_scan_probability as probability_cli


def test_exposure_helpers_cover_empty_invalid_and_market_boundary_contracts() -> None:
    empty_snapshot = type(
        "Snapshot",
        (),
        {"id": 1, "rule_version": "v1", "quote_date": "2026-01-01", "exposures": (), "regime": "unexpected"},
    )()
    config = type("Config", (), {"top_sizes": (20,)})()
    assert exposure_audit([empty_snapshot], config) == []  # type: ignore[list-item,arg-type]
    assert regime_overlay([empty_snapshot])[0]["position_size_multiplier"] == 0.5  # type: ignore[list-item]

    assert normalize_industry(None) == "UNKNOWN"
    assert normalize_industry(" 信息传输、软件和信息技术服务业 ") == "信息技术"
    mixed = (
        ExposureItem("600001.SH", 1, "SH_MAIN", "制造业", 1.0, None),
        ExposureItem("600002.SH", 2, "SH_MAIN", "半导体", 2.0, 1.0),
    )
    assert industry_taxonomy_quality(mixed) == {
        "unknown_count": 0,
        "broad_category_count": 1,
        "mixed_granularity": True,
        "neutralization_ready": False,
    }
    assert industry_taxonomy_quality((mixed[1],))["neutralization_ready"] is True
    assert mean_optional([None, float("nan")]) is None

    row_type = type("Row", (dict,), {})
    assert market_regime([row_type(change_pct=-2.0)]) == "weak"  # type: ignore[arg-type]
    assert quality_bucket(None) == "unknown"
    assert quality_bucket("70") == "low"
    assert liquidity_bucket(object()) == "low"
    assert liquidity_bucket(50_000_000) == "low"
    assert liquidity_bucket(500_000_000) == "medium"
    assert liquidity_bucket(2_000_000_000) == "high"
    assert scan_time_bucket("invalid", "intraday") == "unknown"
    assert scan_time_bucket("2026-01-01 10:00:00", "intraday") == "morning"
    assert scan_time_bucket("2026-01-01 13:00:00", "intraday") == "afternoon"
    assert scan_time_bucket("2026-01-01 10:00:00", "official") == "after_close"


def test_evaluation_projects_v5_continuous_trend_and_keeps_v4_feature_alias() -> None:
    components = {
        "leader_score": {"base": 50, "trend_delta": 2, "score": 52},
        "final_score": {
            "quality_penalty": 0.0,
            "base": 53.303336,
            "continuous_trend_adjustment": 1.303336,
            "raw": 53.303336,
            "rounded": 53,
            "score": 53,
        },
        "continuous_trend": {
            "score": 0.662917,
            "normalized_inputs": {"return_20d_pct": 0.75},
        },
    }

    values = evaluation._production_score_component_values(components)

    assert values["final_continuous_trend_adjustment"] == pytest.approx(1.303336)
    assert values["refinement_return_20d_pct"] == pytest.approx(0.75)
    assert scan_time_bucket("2026-01-01 08:00:00", "preopen") == "preopen"
    assert board("920001.BJ", "BJ") == "BSE"
    assert board("688001.SH", "SH") == "STAR"
    assert board("300001.SZ", "SZ") == "CHINEXT"
    assert board("600001.SH", "SH") == "SH_MAIN"


def test_shadow_promotion_primary_contract_never_selects_preopen_research() -> None:
    def cohort(mode: str, sessions: int) -> dict[str, object]:
        return {
            "dimensions": {
                "mode": mode,
                "scope": "SH/SZ/BJ listed A-shares",
                "rule_version": "v1",
            },
            "top_n": 100,
            "horizon_trading_days": 5,
            "independent_session_count": sessions,
        }

    selected = evaluation._primary_promotion_contract(  # noqa: SLF001
        {"cohorts": [cohort("official", 10), cohort("preopen", 11)]}
    )

    assert selected is not None
    assert selected["dimensions"] == {
        "mode": "official",
        "scope": "SH/SZ/BJ listed A-shares",
        "rule_version": "v1",
    }


def test_bh_fdr_counts_null_hypotheses_and_preserves_insufficient_results() -> None:
    adjusted, rejected = benjamini_hochberg(
        (0.01, 0.03, None, 0.20),
        alpha=0.05,
    )

    assert adjusted == pytest.approx((0.04, 0.06, None, 0.2666666667), nan_ok=True)
    assert rejected == (True, False, None, False)
    assert benjamini_hochberg((), alpha=0.05) == ((), ())
    with pytest.raises(ValueError, match="alpha"):
        benjamini_hochberg((0.1,), alpha=0)
    with pytest.raises(ValueError, match="p-values"):
        benjamini_hochberg((float("nan"),), alpha=0.05)


def test_session_block_bootstrap_is_deterministic_and_null_when_insufficient() -> None:
    positive = moving_block_bootstrap_p_value(
        tuple(0.02 + index / 10_000 for index in range(40)),
        samples=300,
        block_length=5,
        seed_text="candidate-a",
        minimum_count=40,
    )
    replayed = moving_block_bootstrap_p_value(
        tuple(0.02 + index / 10_000 for index in range(40)),
        samples=300,
        block_length=5,
        seed_text="candidate-a",
        minimum_count=40,
    )

    assert positive == replayed
    assert positive is not None and positive < 0.05
    assert moving_block_bootstrap_p_value(
        (0.1,) * 39,
        samples=300,
        block_length=5,
        seed_text="candidate-a",
        minimum_count=40,
    ) is None


def test_read_only_forward_evaluation_uses_frozen_rank_and_complete_future_days(tmp_path: Path) -> None:
    assert evaluation._calibration_metrics is evaluation_metrics.calibration_metrics  # noqa: SLF001
    assert evaluation._calibration_record is evaluation_metrics.calibration_record  # noqa: SLF001
    assert evaluation._calibration_bucket is evaluation_metrics.calibration_bucket  # noqa: SLF001
    path = tmp_path / "evaluation.sqlite3"
    _initialize(path)
    run_id = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH", "000002.SZ", "920003.BJ"),
        changes=(2.0, 1.5, 1.0),
    )
    _seed_forward_prices(
        path,
        dates=("2026-01-06", "2026-01-07", "2026-01-08"),
        closes={
            "600001.SH": (110, 120, 130),
            "000002.SZ": (105, 110, 115),
            "920003.BJ": (90, 80, 70),
        },
        lows={
            "600001.SH": (95, 105, 115),
            "000002.SZ": (98, 103, 108),
            "920003.BJ": (85, 75, 65),
        },
    )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(
            top_sizes=(2, 3),
            horizons=(1, 3),
            minimum_sample_size=2,
            minimum_session_count=1,
            complete_day_coverage=1.0,
        ),
        run_ids=[run_id],
    )

    normalized = json.loads(json.dumps(report))
    normalized["generated_at"] = "<normalized>"
    normalized["source"]["database"] = "<normalized>"
    normalized["probability_research"] = "<normalized: independently clocked evidence>"
    normalized_digest = hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    calibration_digest = hashlib.sha256(
        json.dumps(
            report["calibration"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert normalized_digest == "5c30f5a2c4006d9110375fcf34b1b7120649729c161ce036acc460cc6eca102a"
    assert calibration_digest == "8f36b6d7a1035c20d245135b234cc2896e9b20d8750c7787b829e1cdf9519bb2"

    assert report["status"] == "ok"
    source = cast(dict[str, Any], report["source"])
    assert source["database"] == str(path.resolve())
    assert source["published_run_count"] == 1
    assert source["eligible_run_count"] == 1
    assert source["independent_session_count"] == 1
    assert source["observation_count"] == 3
    assert source["read_only"] is True
    assert source["ranking_source"] == "persisted_market_scan_result"
    assert source["forward_price_source"] == "persisted_qfq_kline_daily"
    contract = {"mode": "official", "scope": "SH/SZ/BJ", "rule_version": "rule-v1"}
    metric = _cohort(report, dimensions=contract, top_n=2, horizon=3)
    assert metric["status"] == "ok"
    assert metric["sample_size"] == 2
    assert metric["independent_session_count"] == 1
    assert metric["average_return"] == pytest.approx(0.225)
    assert metric["median_return"] == pytest.approx(0.225)
    assert metric["positive_return_rate"] == 1
    assert metric["equal_weight_market_return"] == pytest.approx(0.05)
    assert metric["equal_weight_market_excess_return"] == pytest.approx(0.175)
    assert metric["session_maximum_drawdown"] == 0
    assert metric["maximum_adverse_excursion"] == pytest.approx(-0.05)
    runs = cast(list[dict[str, Any]], report["runs"])
    cohorts = cast(list[dict[str, Any]], report["cohorts"])
    assert runs[0]["available_horizons"] == [1, 3]
    assert any(item["dimensions"] == {**contract, "market": "BJ"} for item in cohorts)
    assert any(item["dimensions"] == {**contract, "board": "BSE"} for item in cohorts)
    assert any(item["dimensions"] == {**contract, "segment": "regular"} for item in cohorts)
    assert any(item["dimensions"] == {**contract, "liquidity": "high"} for item in cohorts)
    assert any(item["dimensions"] == {**contract, "scan_time": "after_close"} for item in cohorts)
    assert any(item["dimensions"] == {**contract, "regime": "strong"} for item in cohorts)
    assert any(item["dimensions"] == {**contract, "quality": "high"} for item in cohorts)
    exposure = cast(list[dict[str, Any]], report["exposure_audit"])[0]
    assert exposure["policy"] == "audit-only-no-naive-sector-quota"
    assert exposure["taxonomy_quality"]["mixed_granularity"] is False


def test_probability_horizons_use_fixed_exchange_sessions_not_kline_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = tuple(date(2026, 1, 6) + timedelta(days=offset) for offset in range(26))
    monkeypatch.setattr(evaluation, "next_trade_dates", lambda value, count: fixed[:count])
    misleading_bars = {
        "600001.SH": (
            cast(sqlite3.Row, {"date": "2026-01-07"}),
            cast(sqlite3.Row, {"date": "2026-01-08"}),
        ),
    }

    selected = evaluation._eligible_trading_dates(  # noqa: SLF001
        misleading_bars,
        snapshot_count=5_500,
        quote_date="2026-01-05",
        config=EvaluationConfig(complete_day_coverage=1.0),
    )

    assert selected[0] == "2026-01-06"
    assert selected[1] == "2026-01-07"
    assert len(selected) == 26


def test_evaluation_reports_insufficient_samples_and_same_rule_rank_turnover(tmp_path: Path) -> None:
    path = tmp_path / "stability.sqlite3"
    _initialize(path)
    first = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH", "000002.SZ", "920003.BJ"),
    )
    second = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-10",
        ranks=("000002.SZ", "600001.SH", "300004.SZ"),
    )
    third = _seed_run(
        path,
        mode="official",
        rule_version="rule-v2",
        quote_date="2026-01-15",
        ranks=("600001.SH", "000002.SZ", "300004.SZ"),
    )
    for dates in (
        ("2026-01-06", "2026-01-07", "2026-01-08"),
        ("2026-01-11", "2026-01-12", "2026-01-13"),
        ("2026-01-16", "2026-01-17", "2026-01-18"),
    ):
        _seed_forward_prices(
            path,
            dates=dates,
            closes={symbol: (101, 102, 103) for symbol in ("600001.SH", "000002.SZ", "920003.BJ", "300004.SZ")},
        )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(top_sizes=(2,), horizons=(1, 3), minimum_sample_size=5),
    )

    assert report["status"] == "insufficient_data"  # 不跨 rule_version 拼接样本门槛
    stability = cast(list[dict[str, Any]], report["stability"])
    pair = next(
        item
        for item in stability
        if item["previous_run_id"] == first and item["current_run_id"] == second
    )
    assert pair["comparable"] is True
    assert pair["overlap_rate"] == 1
    assert pair["turnover_rate"] == 0
    assert pair["rank_stability"] == -1
    mismatch = next(
        item
        for item in stability
        if item["previous_run_id"] == second and item["current_run_id"] == third
    )
    assert mismatch["comparable"] is False
    assert mismatch["status"] == "insufficient_data"
    monotonicity = cast(list[dict[str, Any]], report["monotonicity"])
    assert all(item["status"] == "insufficient_data" for item in monotonicity)
    hysteresis = cast(list[dict[str, Any]], report["hysteresis"])
    buffered = next(item for item in hysteresis if item["previous_run_id"] == first and item["current_run_id"] == second)
    assert buffered["hold_rank_threshold"] > buffered["buy_rank_threshold"]
    assert buffered["hysteresis_turnover_rate"] <= buffered["baseline_turnover_rate"]

    default_report = evaluate_market_scan_rankings(path, run_ids=[first])
    assert DEFAULT_TOP_SIZES == (20, 50, 100)
    assert DEFAULT_HORIZONS == (1, 3, 5, 10, 20)
    assert default_report["status"] == "insufficient_data"


def test_evaluation_keeps_only_the_last_same_contract_session_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deduplicated-session.sqlite3"
    _initialize(path)
    first = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH", "000002.SZ"),
    )
    second = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("000002.SZ", "600001.SH"),
    )
    _seed_forward_prices(
        path,
        dates=("2026-01-06",),
        closes={"600001.SH": (101,), "000002.SZ": (99,)},
    )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(top_sizes=(2,), horizons=(1,), minimum_sample_size=2, minimum_session_count=1),
        run_ids=[first, second],
    )

    assert report["source"]["published_run_count"] == 1  # type: ignore[index]
    runs = cast(list[dict[str, Any]], report["runs"])
    assert [item["run_id"] for item in runs] == [second]


def test_evaluation_cli_emits_json_without_mutating_database(tmp_path: Path) -> None:
    path = tmp_path / "cli.sqlite3"
    _initialize(path)
    run_id = _seed_run(
        path,
        mode="intraday",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH", "000002.SZ"),
    )
    _seed_forward_prices(
        path,
        dates=("2026-01-06", "2026-01-07", "2026-01-08"),
        closes={"600001.SH": (101, 102, 103), "000002.SZ": (99, 100, 101)},
    )
    before = path.read_bytes()

    completed = subprocess.run(
        [
            sys.executable,
            "tools/evaluate_market_scan.py",
            "--database",
            str(path),
            "--mode",
            "intraday",
            "--run-id",
            str(run_id),
            "--minimum-sample-size",
            "2",
            "--minimum-session-count",
            "1",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["source"]["read_only"] is True
    assert payload["source"]["published_run_count"] == 1
    assert path.read_bytes() == before


def test_probability_cli_persists_null_shadow_records_without_mutating_database(tmp_path: Path) -> None:
    path = tmp_path / "probability-cli.sqlite3"
    output_dir = tmp_path / "probability-artifacts"
    report_path = tmp_path / "probability-research-summary.json"
    _initialize(path)
    run_id = _seed_run(
        path,
        mode="official",
        rule_version="production-v4",
        quote_date="2026-01-05",
        ranks=("600001.SH", "000002.SZ"),
    )
    _seed_forward_prices(
        path,
        dates=("2026-01-06", "2026-01-07", "2026-01-08"),
        closes={"600001.SH": (101, 102, 103), "000002.SZ": (99, 100, 101)},
    )
    before = path.read_bytes()
    with sqlite3.connect(path) as connection:
        ranking_before = connection.execute(
            "SELECT symbol, rank, score, raw_score FROM market_scan_result WHERE run_id = ? ORDER BY rank",
            (run_id,),
        ).fetchall()

    command = [
        sys.executable,
        "tools/evaluate_market_scan_probability.py",
        "--database",
        str(path),
        "--output-dir",
        str(output_dir),
        "--report",
        str(report_path),
        "--run-id",
        str(run_id),
        "--bootstrap-samples",
        "100",
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(completed.stdout)
    artifact = load_probability_artifact(Path(summary["artifact"]))
    records = cast(list[dict[str, Any]], artifact["payload"]["records"])  # type: ignore[index]

    assert summary["status"] == "insufficient_data"
    assert summary["record_count"] == 12
    assert summary["database_read_only"] is True
    assert summary["database_bytes_unchanged"] is True
    assert summary["database_sha256_before"] == summary["database_sha256_after"]
    assert summary["credible_probability_available"] is False
    assert summary["calibrated_shadow_horizons"] == []
    assert summary["production_rule"] is None
    assert summary["production_score_contract"] == summary["production_score_contracts"][0]
    assert summary["production_ranking_effect"] == "none"
    assert summary["automatic_promotion"] is False
    assert summary["full_input_replay_verified"] is True
    assert summary["artifact_set_replay"]["run_ids"] == [run_id]
    assert summary["artifact_set_replay"]["study_count"] == 6
    assert set(summary["horizons"]) == {"1", "5", "20"}
    assert json.loads(report_path.read_text(encoding="utf-8")) == summary
    assert len(records) == 12
    assert all(item["status"] == "insufficient_data" and item["probability"] is None for item in records)
    set_replay = replay_probability_artifact_set(
        [Path(item["artifact"]) for item in summary["artifacts"]]
    )
    assert set_replay["run_ids"] == [run_id]
    assert set_replay["study_count"] == 6
    restarted_research, restarted_records = MarketScanProbabilityStore(output_dir).run_projection(run_id)
    assert restarted_research["record_contract_version"] == PROBABILITY_RESULT_CONTRACT_VERSION
    restarted_result = cast(dict[str, Any], restarted_records["600001.SH"]["1"])[
        "net_excess_positive"
    ]
    assert restarted_result["status"] == "insufficient_data"
    assert restarted_result["probability"] is None
    assert cast(dict[str, Any], restarted_result["calibration_summary"])["brier_score"] is None
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        ranking_after = connection.execute(
            "SELECT symbol, rank, score, raw_score FROM market_scan_result WHERE run_id = ? ORDER BY rank",
            (run_id,),
        ).fetchall()
    assert ranking_after == ranking_before

    outside = tmp_path / "probability-cli-outside"
    outside.mkdir()
    alias = tmp_path / "probability-cli-alias"
    alias.symlink_to(outside, target_is_directory=True)
    output_index = command.index("--output-dir") + 1
    report_index = command.index("--report") + 1
    for index, hostile_output in enumerate((alias, alias / "not-created"), start=1):
        hostile_command = list(command)
        hostile_command[output_index] = str(hostile_output)
        hostile_command[report_index] = str(tmp_path / f"hostile-report-{index}.json")
        failed = subprocess.run(
            hostile_command,
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
    assert list(outside.iterdir()) == []
    assert not (outside / "not-created").exists()

    with tempfile.TemporaryDirectory(prefix="ashare-probability-cli-", dir="/tmp") as raw_output:
        tmp_command = list(command)
        tmp_command[output_index] = raw_output
        tmp_command[report_index] = str(Path(raw_output) / "summary.json")
        tmp_completed = subprocess.run(
            tmp_command,
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
        tmp_summary = json.loads(tmp_completed.stdout)
        assert load_probability_artifact(Path(tmp_summary["artifact"]))["payload"]


def test_probability_cli_summary_projects_unique_v5_contract_and_exposes_mixed_contracts() -> None:
    v4 = {
        "production_score_rule_version": "full-market-score-v4",
        "production_score_spec_hash": "4" * 64,
    }
    v5 = {
        "production_score_rule_version": "full-market-score-v5",
        "production_score_spec_hash": "5" * 64,
    }

    def summary(contracts: list[dict[str, object]]) -> dict[str, object]:
        research = {
            "status": "insufficient_data",
            "cohorts": [
                {"production_score_contract": contract}
                for contract in contracts
            ],
        }
        return probability_cli._summary(  # noqa: SLF001
            [],
            [],
            research,
            artifact_set_replay=None,
            database_digest_before="a" * 64,
            database_digest_after="a" * 64,
        )

    current = summary([v5, dict(v5)])
    assert current["production_rule"] == "full-market-score-v5"
    assert current["production_score_contract"] == v5
    assert current["production_score_contracts"] == [v5]

    mixed = summary([v5, v4])
    assert mixed["production_rule"] is None
    assert mixed["production_score_contract"] is None
    assert mixed["production_score_contracts"] == [v4, v5]


def test_probability_research_freezes_verified_new_stock_no_limit_profile(tmp_path: Path) -> None:
    path = tmp_path / "probability-new-stock.sqlite3"
    _initialize(path)
    run_id = _seed_run(
        path,
        mode="official",
        rule_version="production-v4",
        quote_date="2026-01-05",
        ranks=("688001.SH",),
    )
    with sqlite3.connect(path) as connection:
        _disable_market_scan_immutability(connection)
        connection.execute(
            "UPDATE market_scan_result SET list_date = ?, is_new = 1 WHERE run_id = ?",
            ("2026-01-05", run_id),
        )
        _reseal_market_scan_snapshot(connection, run_id)
        connection.commit()
    _seed_forward_prices(
        path,
        dates=("2026-01-06", "2026-01-07", "2026-01-08"),
        closes={"688001.SH": (101, 102, 103)},
    )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(bootstrap_samples=100),
        run_ids=[run_id],
    )
    research = cast(dict[str, Any], report["probability_research"])
    evidence = cast(dict[str, Any], research["horizons"])["1"]["net_excess_positive"]
    record = next(
        item
        for item in cast(list[dict[str, Any]], research["records"])
        if item["run_id"] == run_id
        and item["symbol"] == "688001.SH"
        and item["horizon"] == 1
        and item["target"] == "net_excess_positive"
    )
    features = dict(zip(evidence["feature_names"], record["feature_values"], strict=True))

    assert features["is_st"] == 0
    assert features["is_new"] == 1
    assert features["price_limit_pct"] == 0
    assert features["price_limit_profile_verified"] == 1
    assert features["price_limit_profile_uncertain"] == 0
    assert features["price_limit_absent"] == 1
    assert features["new_stock_no_limit_phase"] == 1


def test_rank_evaluation_rejects_a_tampered_published_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "tampered-evaluation.sqlite3"
    _initialize(path)
    run_id = _seed_run(
        path,
        mode="official",
        rule_version="production-v4",
        quote_date="2026-01-05",
        ranks=("600001.SH",),
    )
    with closing(sqlite3.connect(path)) as conn, conn:
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_result SET raw_score = 1 WHERE run_id = ?",
            (run_id,),
        )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(bootstrap_samples=100),
        run_ids=[run_id],
    )

    quality = cast(dict[str, object], report["evaluation_quality"])
    failures = cast(list[dict[str, object]], quality["run_failures"])
    assert report["status"] == "insufficient_data"
    assert quality["evaluated_run_count"] == 0
    assert quality["rejected_run_count"] == 1
    assert failures[0]["run_id"] == run_id
    assert failures[0]["reason_code"] == "MarketScanSnapshotSealError"


def test_one_cross_section_cannot_satisfy_independent_session_gate(tmp_path: Path) -> None:
    path = tmp_path / "independent-sessions.sqlite3"
    _initialize(path)
    symbols = tuple(f"{600000 + index:06d}.SH" for index in range(50))
    run_id = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=symbols,
    )
    _seed_forward_prices(
        path,
        dates=("2026-01-06",),
        closes={symbol: (101,) for symbol in symbols},
    )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(
            top_sizes=(50,),
            horizons=(1,),
            minimum_sample_size=30,
            minimum_session_count=2,
        ),
        run_ids=[run_id],
    )

    metric = _cohort(
        report,
        dimensions={"mode": "official", "scope": "SH/SZ/BJ", "rule_version": "rule-v1"},
        top_n=50,
        horizon=1,
    )
    assert metric["sample_size"] == 50
    assert metric["independent_session_count"] == 1
    assert metric["status"] == "insufficient_data"
    assert metric["insufficient_reasons"] == ["minimum_session_count"]


def test_rank_ic_deciles_and_clustered_metrics_use_sessions(tmp_path: Path) -> None:
    path = tmp_path / "rank-diagnostics.sqlite3"
    _initialize(path)
    symbols = tuple(f"{600000 + index:06d}.SH" for index in range(10))
    for quote_date, forward_date in (("2026-01-05", "2026-01-06"), ("2026-01-12", "2026-01-13")):
        _seed_run(
            path,
            mode="official",
            rule_version="rule-v1",
            quote_date=quote_date,
            ranks=symbols,
        )
        _seed_forward_prices(
            path,
            dates=(forward_date,),
            closes={symbol: (120 - index * 2,) for index, symbol in enumerate(symbols)},
        )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(
            top_sizes=(2,),
            horizons=(1,),
            minimum_sample_size=1,
            minimum_session_count=2,
            bootstrap_samples=200,
        ),
    )

    contract = {"mode": "official", "scope": "SH/SZ/BJ", "rule_version": "rule-v1"}
    metric = _cohort(report, dimensions=contract, top_n=2, horizon=1)
    assert metric["status"] == "ok"
    assert metric["independent_session_count"] == 2
    assert len(metric["session_return_confidence_interval_95"]) == 2  # type: ignore[arg-type]
    rank_ic = cast(list[dict[str, Any]], report["rank_ic"])[0]
    assert rank_ic["status"] == "ok"
    assert rank_ic["independent_session_count"] == 2
    assert rank_ic["mean_rank_ic"] == pytest.approx(1)
    deciles = cast(list[dict[str, Any]], report["deciles"])[0]
    assert deciles["status"] == "ok"
    assert deciles["monotonic"] is True


def test_stability_uses_frozen_rankings_even_when_current_run_has_no_forward_data(tmp_path: Path) -> None:
    path = tmp_path / "ranking-only-stability.sqlite3"
    _initialize(path)
    first = _seed_run(
        path,
        mode="intraday",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH", "000002.SZ"),
    )
    second = _seed_run(
        path,
        mode="intraday",
        rule_version="rule-v1",
        quote_date="2026-01-10",
        ranks=("600001.SH", "000002.SZ"),
    )
    _seed_forward_prices(
        path,
        dates=("2026-01-06",),
        closes={"600001.SH": (101,), "000002.SZ": (99,)},
    )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(top_sizes=(2,), horizons=(1,), minimum_sample_size=1),
    )
    pair = next(
        item
        for item in cast(list[dict[str, Any]], report["stability"])
        if item["previous_run_id"] == first and item["current_run_id"] == second
    )
    assert pair["ranking_evidence_available"] is True
    assert pair["overlap_rate"] == 1
    assert pair["turnover_rate"] == 0


def test_execution_metrics_apply_next_open_t1_costs_and_tradeability(tmp_path: Path) -> None:
    path = tmp_path / "execution.sqlite3"
    _initialize(path)
    run_id = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH",),
    )
    _seed_bars(
        path,
        "600001.SH",
        (
            ("2026-01-05", 100, 101, 99, 100, 1_000),
            ("2026-01-06", 100, 102, 99, 101, 1_000),
            ("2026-01-07", 110, 112, 109, 111, 1_000),
        ),
    )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(
            top_sizes=(1,),
            horizons=(1,),
            minimum_sample_size=1,
            minimum_session_count=1,
        ),
        run_ids=[run_id],
    )
    metric = _cohort(
        report,
        dimensions={"mode": "official", "scope": "SH/SZ/BJ", "rule_version": "rule-v1"},
        top_n=1,
        horizon=1,
    )
    execution = cast(dict[str, Any], metric["execution"])
    assert execution["status_counts"] == {"modelled": 1}
    assert execution["modelled_sample_size"] == 1
    assert 0 < execution["average_net_return"] < 0.10
    assert execution["average_cost_drag"] > 0

    locked_path = tmp_path / "locked.sqlite3"
    _initialize(locked_path)
    locked_run = _seed_run(
        locked_path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH",),
    )
    _seed_bars(
        locked_path,
        "600001.SH",
        (
            ("2026-01-05", 100, 101, 99, 100, 1_000),
            ("2026-01-06", 110, 110, 110, 110, 1_000),
            ("2026-01-07", 111, 112, 109, 111, 1_000),
        ),
    )
    locked_report = evaluate_market_scan_rankings(
        locked_path,
        config=EvaluationConfig(
            top_sizes=(1,),
            horizons=(1,),
            minimum_sample_size=1,
            minimum_session_count=1,
        ),
        run_ids=[locked_run],
    )
    locked_metric = _cohort(
        locked_report,
        dimensions={"mode": "official", "scope": "SH/SZ/BJ", "rule_version": "rule-v1"},
        top_n=1,
        horizon=1,
    )
    assert locked_metric["execution"]["status_counts"] == {"unfilled": 1}  # type: ignore[index]


def test_shadow_evaluation_is_read_only_replayable_and_never_auto_promotes(tmp_path: Path) -> None:
    path = tmp_path / "shadow.sqlite3"
    _initialize(path)
    symbols = ("600001.SH", "600002.SH", "600003.SH")
    run_id = _seed_run(
        path,
        mode="official",
        rule_version="production-v4",
        quote_date="2026-01-05",
        ranks=symbols,
    )
    for index, symbol in enumerate(symbols):
        _seed_shadow_history(path, symbol, end=date(2026, 1, 5), slope=(index + 1) * 0.001)
    _seed_forward_prices(
        path,
        dates=("2026-01-06",),
        closes={symbol: (101 + index,) for index, symbol in enumerate(symbols)},
    )
    before = path.read_bytes()
    config = EvaluationConfig(
        top_sizes=(2,),
        horizons=(1,),
        minimum_sample_size=1,
        minimum_session_count=2,
        bootstrap_samples=200,
    )

    shadow = evaluate_market_scan_shadow_rankings(
        path,
        variant="v5_full",
        config=config,
        run_ids=[run_id],
    )
    comparison = evaluate_market_scan_shadow_comparison(
        path,
        config=config,
        run_ids=[run_id],
        variants=("v5_full", "v5_without_overextension"),
    )

    assert shadow["source"]["ranking_source"] == "reconstructed-read-only-shadow-score"  # type: ignore[index]
    assert shadow["shadow"]["production_mutation"] is False  # type: ignore[index]
    assert shadow["shadow"]["input_integrity"]["eligible_for_promotion_evidence"] is False  # type: ignore[index]
    evidence = shadow["shadow"]["run_evidence"]  # type: ignore[index]
    assert evidence[0]["scored_count"] == 3
    assert evidence[0]["ranking_digest"]
    assert comparison["status"] == "insufficient_data"
    assert comparison["promotion"]["automatic_promotion"] is False  # type: ignore[index]
    assert comparison["promotion"]["point_in_time_input_integrity_verified"] is False  # type: ignore[index]
    assert comparison["promotion"]["gate_version"] == "full-market-shadow-promotion-gate-v2"  # type: ignore[index]
    assert comparison["promotion"]["eligible_candidates"] == []  # type: ignore[index]
    multiple_testing = comparison["promotion"]["multiple_testing_control"]  # type: ignore[index]
    assert multiple_testing["method"] == "benjamini-hochberg-fdr"
    assert multiple_testing["status"] == "insufficient_data"
    assert multiple_testing["tested_hypothesis_count"] == 0
    assert multiple_testing["pbo"]["status"] == "not_computed"
    assert multiple_testing["deflated_sharpe_ratio"]["status"] == "not_computed"
    gates = comparison["promotion"]["candidate_gates"]  # type: ignore[index]
    assert set(gates) == {"v5_full", "v5_without_overextension"}
    assert all("primary_contract" in gate["failed_criteria"] for gate in gates.values())
    assert all(
        "bh_fdr_primary_net_excess_improvement" in gate["failed_criteria"]
        for gate in gates.values()
    )
    assert "候选评分历史输入缺少可验证的扫描时点快照" in comparison["promotion"]["blocking_reasons"]  # type: ignore[index]
    assert comparison["promotion"]["conclusion"] == "候选评分已实现并可持续积累影子证据，但暂不晋级生产。"  # type: ignore[index]
    completed = subprocess.run(
        [
            sys.executable,
            "tools/evaluate_market_scan_shadow.py",
            "--database",
            str(path),
            "--run-id",
            str(run_id),
            "--variant",
            "v5_full",
            "--compact",
            "--minimum-sample-size",
            "1",
            "--minimum-session-count",
            "2",
            "--bootstrap-samples",
            "200",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    cli_payload = json.loads(completed.stdout)
    assert cli_payload["schema_version"] == "market-scan-shadow-comparison-v2"
    assert cli_payload["artifact_projection"]["schema_version"] == (  # type: ignore[index]
        "market-scan-shadow-comparison-compact-v1"
    )
    assert "probability_research" not in cli_payload["candidates"]["v5_full"]  # type: ignore[index]
    assert cli_payload["promotion"]["automatic_promotion"] is False
    delta = shadow["rank_delta_vs_production"]
    assert delta["status"] == "ok"
    assert delta["common_symbol_count"] == 3
    assert delta["top20_overlap_ratio"] == 1
    assert path.read_bytes() == before


def test_shadow_compact_projection_keeps_product_evidence_and_drops_large_records() -> None:
    report: dict[str, object] = {
        "schema_version": "market-scan-shadow-comparison-v2",
        "generated_at": "2026-08-12T00:00:00Z",
        "status": "insufficient_data",
        "promotion": {"automatic_promotion": False, "candidate_gates": {}},
        "production": {
            "status": "insufficient_data",
            "source": {"database": "/private/tmp/local.sqlite3", "read_only": True},
            "cohorts": [{
                "dimensions": {"mode": "official", "scope": "full", "rule_version": "v4"},
                "top_n": 100,
            }],
            "probability_research": {"records": [{"symbol": "600001.SH"}]},
        },
        "candidates": {
            "v5_5_bounded_nonlinear_stability": {
                "status": "insufficient_data",
                "source": {
                    "database": "/private/tmp/local.sqlite3",
                    "independent_session_count": 1,
                },
                "cohorts": [{
                    "dimensions": {"mode": "official", "scope": "full", "rule_version": "v5.5"},
                    "top_n": 100,
                    "horizon_trading_days": 5,
                }],
                "shadow": {
                    "spec_hash": "a" * 64,
                    "run_evidence": [{
                        "run_id": 71,
                        "candidate_id": "v5.5",
                        "scored_count": 5_499,
                        "ranking_digest": "b" * 64,
                        "normalization": {"huge": [1, 2, 3]},
                    }],
                },
                "rank_delta_vs_production": {"compared_item_count": 5_499},
                "probability_research": {"records": [{"symbol": "600001.SH"}]},
            }
        },
    }

    compact = compact_shadow_comparison_report(report)

    assert compact["schema_version"] == "market-scan-shadow-comparison-v2"
    assert compact["artifact_projection"]["schema_version"] == (  # type: ignore[index]
        "market-scan-shadow-comparison-compact-v1"
    )
    production = cast(dict[str, object], compact["production"])
    candidate = cast(dict[str, dict[str, object]], compact["candidates"])[
        "v5_5_bounded_nonlinear_stability"
    ]
    assert cast(list[dict[str, object]], production["cohorts"])[0]["top_n"] == 100
    assert production["source"] == {"read_only": True}
    assert candidate["rank_delta_vs_production"] == {"compared_item_count": 5_499}
    assert cast(dict[str, object], candidate["source"])["independent_session_count"] == 1
    assert "database" not in cast(dict[str, object], candidate["source"])
    shadow = cast(dict[str, object], candidate["shadow"])
    assert cast(list[dict[str, object]], shadow["run_evidence"])[0] == {
        "run_id": 71,
        "candidate_id": "v5.5",
        "scored_count": 5_499,
        "ranking_digest": "b" * 64,
    }
    assert "probability_research" not in production
    assert "probability_research" not in candidate


def test_shadow_evaluation_excludes_one_invalid_symbol_without_losing_the_run(tmp_path: Path) -> None:
    path = tmp_path / "shadow-isolation.sqlite3"
    _initialize(path)
    symbols = ("600001.SH", "600002.SH", "600003.SH")
    run_id = _seed_run(
        path,
        mode="official",
        rule_version="production-v4",
        quote_date="2026-01-05",
        ranks=symbols,
    )
    for index, symbol in enumerate(symbols):
        _seed_shadow_history(path, symbol, end=date(2026, 1, 5), slope=(index + 1) * 0.001)
    with closing(sqlite3.connect(path)) as conn, conn:
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_result SET price = 150 WHERE run_id = ? AND symbol = ?",
            (run_id, symbols[0]),
        )
        _reseal_market_scan_snapshot(conn, run_id)
    _seed_forward_prices(
        path,
        dates=("2026-01-06",),
        closes={symbol: (101,) for symbol in symbols},
    )

    report = evaluate_market_scan_shadow_rankings(
        path,
        variant="v5_3_skip5_residual_volume_lifecycle",
        config=EvaluationConfig(
            top_sizes=(2,),
            horizons=(1,),
            minimum_sample_size=1,
            minimum_session_count=1,
            bootstrap_samples=200,
        ),
        run_ids=[run_id],
    )

    quality = cast(dict[str, Any], report["evaluation_quality"])
    assert quality["evaluated_run_count"] == 1
    assert quality["rejected_run_count"] == 0
    assert quality["expected_item_count"] == 3
    assert quality["scored_item_count"] == 2
    assert quality["excluded_item_count"] == 1
    assert quality["exclusion_reason_counts"] == {"invalid_shadow_input": 1}
    evidence = cast(list[dict[str, Any]], report["shadow"]["run_evidence"])
    assert evidence[0]["scored_count"] == 2


def _cohort(
    report: dict[str, object],
    *,
    dimensions: dict[str, str],
    top_n: int,
    horizon: int,
) -> dict[str, object]:
    cohorts = cast(list[dict[str, Any]], report["cohorts"])
    return next(
        item
        for item in cohorts
        if item["dimensions"] == dimensions
        and item["top_n"] == top_n
        and item["horizon_trading_days"] == horizon
    )


def _initialize(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        initialize_schema(conn)


def _seed_run(
    path: Path,
    *,
    mode: str,
    rule_version: str,
    quote_date: str,
    ranks: tuple[str, ...],
    changes: tuple[float, ...] | None = None,
) -> int:
    timestamp = f"{quote_date}T08:00:00.000000Z"
    with closing(sqlite3.connect(path)) as conn, conn:
        run_id = conn.execute(
            """
            INSERT INTO market_scan_run (
                status, trigger, mode, rule_version, as_of, data_date, quote_date,
                scope, total_count, processed_count, success_count,
                created_at, updated_at, finished_at
            ) VALUES ('success', 'manual', ?, ?, ?, ?, ?, 'SH/SZ/BJ', ?, ?, ?, ?, ?, ?)
            """,
            (
                mode,
                rule_version,
                f"{quote_date} 15:00:00",
                quote_date,
                quote_date,
                len(ranks),
                len(ranks),
                len(ranks),
                timestamp,
                timestamp,
                timestamp,
            ),
        ).lastrowid
        assert run_id is not None
        for index, symbol in enumerate(ranks, start=1):
            code, market = symbol.split(".")
            conn.execute(
                """
                INSERT INTO market_scan_result (
                    run_id, symbol, code, market, name, status, rank, score, raw_score,
                    price, change_pct, data_quality_score, amount, turnover_rate,
                    list_date, is_st, is_new, adjustment_mode, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'success', ?, ?, ?, 100, ?, ?, 1000000000, 4,
                          '2020-01-02', 0, 0, 'qfq', ?)
                """,
                (
                    run_id,
                    symbol,
                    code,
                    market,
                    f"样本{code}",
                    index,
                    100 - index,
                    100 - index / 10,
                    changes[index - 1] if changes else 0,
                    95 if index == 1 else 85 if index == 2 else 75,
                    timestamp,
                ),
            )
        seal_market_scan_snapshot(conn, int(run_id))
    return int(run_id)


def _disable_market_scan_immutability(conn: sqlite3.Connection) -> None:
    for trigger in (
        "trg_market_scan_published_run_immutable",
        "trg_market_scan_published_run_no_delete",
        "trg_market_scan_published_result_no_update",
        "trg_market_scan_published_result_no_delete",
        "trg_market_scan_published_result_no_insert",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _reseal_market_scan_snapshot(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute(
        "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
        (market_scan_snapshot_digest(conn, run_id), run_id),
    )


def _seed_forward_prices(
    path: Path,
    *,
    dates: tuple[str, ...],
    closes: dict[str, tuple[float, ...]],
    lows: dict[str, tuple[float, ...]] | None = None,
) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        for symbol, prices in closes.items():
            low_prices = (lows or {}).get(symbol, prices)
            for row_date, close, low in zip(dates, prices, low_prices, strict=True):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO kline_daily (
                        symbol, adjustment_mode, date, open, close, high, low, volume,
                        as_of, data_version, contract_version, fallback_used, source, fetched_at
                    ) VALUES (?, 'qfq', ?, ?, ?, ?, ?, 1000, ?, 'test-v1', 'test-v1', 0, 'test', ?)
                    """,
                    (
                        symbol,
                        row_date,
                        close,
                        close,
                        close,
                        low,
                        row_date,
                        f"{row_date}T08:00:00.000000Z",
                    ),
                )


def _seed_bars(
    path: Path,
    symbol: str,
    rows: tuple[tuple[str, float, float, float, float, float], ...],
) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        for row_date, open_price, high, low, close, volume in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO kline_daily (
                    symbol, adjustment_mode, date, open, close, high, low, volume,
                    as_of, data_version, contract_version, fallback_used, source, fetched_at
                ) VALUES (?, 'qfq', ?, ?, ?, ?, ?, ?, ?, 'test-v1', 'test-v1', 0, 'test', ?)
                """,
                (
                    symbol,
                    row_date,
                    open_price,
                    close,
                    high,
                    low,
                    volume,
                    row_date,
                    f"{row_date}T08:00:00.000000Z",
                ),
            )


def _seed_shadow_history(path: Path, symbol: str, *, end: date, slope: float) -> None:
    dates: list[date] = []
    cursor = end
    while len(dates) < 70:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= timedelta(days=1)
    dates.reverse()
    rows = []
    for index, row_date in enumerate(dates):
        close = 100 / (1 + slope * (len(dates) - 1)) * (1 + slope * index)
        rows.append((row_date.isoformat(), close - 0.2, close + 0.5, close - 0.5, close, 1_000_000.0))
    _seed_bars(path, symbol, tuple(rows))
