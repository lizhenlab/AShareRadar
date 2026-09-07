from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import json
from types import SimpleNamespace
from typing import Any, cast
import sqlite3

import pytest

from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE, MarketScanResultItem
from app.repositories.market_scan_mapping import decode_result_payload, encode_result_payload
from app.services import market_scan_evaluation as evaluation
from app.services.market_scan_score_dimensions import build_market_scan_score_dimensions
from app.services.trading_calendar import trading_dates_between
from tests.factories import make_kline, make_quote


def _inputs() -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], ...]]:
    signal_date = date(2026, 1, 5)
    days = trading_dates_between(signal_date - timedelta(days=140), signal_date)[-61:]
    rows = [
        make_kline(
            date=value.isoformat(), close=100, high=101, low=98, volume=1_000_000,
            data_version="signal-vintage", as_of="2026-01-05 15:00:00",
        ).model_copy(update={"open": 99.5})
        for value in days
    ]
    item = MarketScanResultItem(
        run_id=1, symbol="600001.SH", code="600001", market="SH", name="样本",
        industry="测试行业", list_date="2020-01-02", status="pending", updated_at="2026-01-05",
    )
    quote = make_quote(
        price=100, prev_close=99, high=101, low=98, turnover_rate=4,
        timestamp="2026-01-05 15:00:00",
    ).model_copy(update={"code": "600001", "name": "样本", "open": 99.5})
    dimensions = build_market_scan_score_dimensions(
        item, quote, rows, data_quality_score=100, volume_ratio=1.2, mode="official",
    )
    run = dict(
        id=1, mode="official", scope=MARKET_SCAN_FULL_MARKET_SCOPE, rule_version="rule-v1",
        quote_date="2026-01-05", data_date="2026-01-05", as_of="2026-01-05 15:30:00",
    )
    result = dict(
        symbol="600001.SH", market="SH", industry="测试行业", price=100, amount=1_300_000_000,
        is_st=0, is_new=0, list_date="2020-01-02", rank=1, raw_score=90, score=90,
        turnover_rate=4, data_quality_score=100, adjustment_mode="qfq", change_pct=1,
        volume_ratio=1.2, metrics_json=encode_result_payload(
            {}, {"components": {"score_dimensions": dimensions.details()}},
        ),
    )
    bars = tuple(
        dict(
            symbol="600001.SH",
            date=day, open=99.5 if index == 0 else 101, close=100 if index == 0 else 102,
            high=101 if index == 0 else 103, low=98 if index == 0 else 100,
            volume=1_000_000, adjustment_mode="qfq", data_version="target-vintage",
            contract_version="daily-kline.v1", as_of=day, source="test", fetched_at=day,
            fallback_used=0,
        )
        for index, day in enumerate(("2026-01-05", "2026-01-06", "2026-01-07"))
    )
    return run, result, bars


def _observe(run: dict[str, Any], result: dict[str, Any], bars: tuple[dict[str, Any], ...]) -> Any:
    return evaluation._observation_from_rows(
        cast(sqlite3.Row, run), cast(sqlite3.Row, result), cast(tuple[sqlite3.Row, ...], bars),
        ("2026-01-06", "2026-01-07"), "neutral", evaluation.EvaluationConfig(horizons=(1,)),
    )


def test_matching_overlap_allows_new_vintage_without_requiring_equal_version_labels() -> None:
    run, result, bars = _inputs()
    observation = _observe(run, result, bars)
    assert observation.returns[1] == pytest.approx(0.02)
    assert observation.execution[1].status == "modelled"


def test_company_action_rebase_retains_rank_but_rejects_all_forward_outcomes() -> None:
    run, result, bars = _inputs()
    rebased = deepcopy(bars)
    for row in rebased:
        for field in ("open", "high", "low", "close"):
            row[field] /= 2
    observation = _observe(run, result, rebased)
    assert observation.rank == 1
    assert observation.returns == {}
    assert observation.adverse == {}
    assert observation.execution[1].reason == "target_adjustment_rebase_conflict"
    assert observation.probability_labels[1].reason == "target_adjustment_rebase_conflict"


def test_missing_signal_overlap_is_not_replaced_by_frozen_close() -> None:
    run, result, bars = _inputs()
    observation = _observe(run, result, bars[1:])
    assert observation.returns == {}
    assert observation.execution[1].reason == "target_adjustment_overlap_missing"


def test_legacy_rank_without_pit_evidence_remains_auditable_without_forward_claims() -> None:
    run, result, bars = _inputs()
    result["metrics_json"] = "{}"
    observation = _observe(run, result, bars)
    assert observation.rank == 1
    assert observation.raw_score == 90
    assert observation.returns == {}
    assert observation.execution[1].reason == "signal_price_basis_evidence_missing"


@pytest.mark.parametrize("field,value", [("symbol", "600002.SH"), ("price", 101.0)])
def test_frozen_signal_evidence_is_bound_to_result_identity(field: str, value: object) -> None:
    run, result, bars = _inputs()
    result[field] = value
    observation = _observe(run, result, bars)
    assert observation.returns == {}
    assert observation.execution[1].reason == "signal_price_basis_identity_conflict"


def test_modified_frozen_signal_without_matching_digest_is_rejected() -> None:
    run, result, bars = _inputs()
    content = json.loads(result["metrics_json"])
    content["score_details"]["components"]["score_dimensions"]["point_in_time_evidence"]["payload"]["quote_price"] = 99
    result["metrics_json"] = json.dumps(content)
    observation = _observe(run, result, bars)
    assert observation.returns == {}
    assert observation.execution[1].reason == "signal_price_basis_evidence_invalid"


@pytest.mark.parametrize("field,value", [("open", 99.4), ("high", 101.1), ("low", 97.9)])
def test_equal_signal_close_does_not_mask_other_rebased_ohlc(field: str, value: float) -> None:
    run, result, bars = _inputs()
    bars[0][field] = value
    observation = _observe(run, result, bars)
    assert observation.returns == {}
    assert observation.execution[1].reason == "target_adjustment_rebase_conflict"


@pytest.mark.parametrize("field,value", [
    ("open", float("nan")), ("close", float("inf")), ("low", -1), ("high", 99),
    ("volume", -1), ("adjustment_mode", "none"), ("data_version", ""), ("contract_version", "old"),
])
def test_invalid_target_bar_cannot_create_price_or_execution_outcome(field: str, value: object) -> None:
    run, result, bars = _inputs()
    bars[1][field] = value
    observation = _observe(run, result, bars)
    assert observation.returns == {}
    assert observation.execution[1].status == "data_unavailable"


def test_shadow_history_reconstruction_accepts_current_ten_field_frozen_bars() -> None:
    _run, result, _bars = _inputs()
    history = evaluation._persisted_shadow_history(cast(sqlite3.Row, result), "600001.SH", "2026-01-05")
    assert history is not None
    assert len(history[0]) == 61
    assert history[0][-1].as_of == "2026-01-05 15:00:00"
    assert history[0][-1].source == "persisted-market-scan-point-in-time-evidence"


def test_legacy_nine_field_frozen_bar_remains_readable_for_audit() -> None:
    _run, result, _bars = _inputs()
    _, details = decode_result_payload(result["metrics_json"])
    row = details["components"]["score_dimensions"]["point_in_time_evidence"]["payload"]["bar_contract_61"][-1]
    bar = evaluation._kline_from_evidence_contract(row[:9])
    assert bar.close == 100
    assert bar.data_version == "signal-vintage"


@pytest.mark.parametrize("missing_index", [0, 1])
def test_adverse_path_requires_every_fixed_session_without_suppressing_target_close(missing_index: int) -> None:
    dates = ("2026-01-06", "2026-01-07", "2026-01-08")
    bars = [
        {"date": day, "close": 102 + index, "low": 95 - index}
        for index, day in enumerate(dates)
        if index != missing_index
    ]
    returns, adverse = evaluation._forward_performance(
        cast(list[sqlite3.Row], bars), dates, 100, (1, 2, 3),
    )
    assert returns[3] == pytest.approx(0.04)
    assert 3 not in adverse
    assert missing_index + 1 not in returns
    if missing_index == 1:
        assert adverse[1] == pytest.approx(-0.05)


def test_complete_adverse_path_keeps_all_fixed_horizon_lows() -> None:
    dates = ("2026-01-06", "2026-01-07", "2026-01-08")
    bars = [
        {"date": day, "close": 102 + index, "low": low}
        for index, (day, low) in enumerate(zip(dates, (95, 90, 99), strict=True))
    ]
    returns, adverse = evaluation._forward_performance(cast(list[sqlite3.Row], bars), dates, 100, (1, 2, 3))
    assert returns == pytest.approx({1: 0.02, 2: 0.03, 3: 0.04})
    assert adverse == pytest.approx({1: -0.05, 2: -0.10, 3: -0.10})


@pytest.mark.parametrize("scope", ["SH/SZ/BJ", "沪市 + 深市", MARKET_SCAN_FULL_MARKET_SCOPE + " "])
def test_primary_contract_requires_byte_exact_full_market_scope(scope: str) -> None:
    foreign = {
        "dimensions": {"mode": "official", "scope": scope, "rule_version": "v1"},
        "top_n": 100, "horizon_trading_days": 5, "independent_session_count": 100,
    }
    canonical = deepcopy(foreign)
    canonical["dimensions"]["scope"] = MARKET_SCAN_FULL_MARKET_SCOPE
    canonical["independent_session_count"] = 1
    assert evaluation._primary_promotion_contract({"cohorts": [foreign, canonical]}) == canonical
    assert evaluation._primary_promotion_contract({"cohorts": [foreign]}) is None
    wrong = SimpleNamespace(mode="official", scope=scope, rule_version="v1", quote_date="2026-01-05")
    full = SimpleNamespace(
        mode="official", scope=MARKET_SCAN_FULL_MARKET_SCOPE, rule_version="v1", quote_date="2026-01-05",
    )
    selected = evaluation._primary_observation_contract([wrong, full])  # type: ignore[list-item]
    assert selected is not None and selected[1] == (full,)
    assert evaluation._primary_observation_contract([wrong]) is None  # type: ignore[list-item]
