from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta

import pytest

from app.models.market_scan import MarketScanResultItem
from app.models.schemas import Kline, StockInfo
from app.services.market_scan_scoring import (
    FULL_MARKET_SCORE_TIE_BREAK,
    MarketScanDataMissing,
    MarketScanReplayError,
    MarketScanSkipped,
    completed_market_scan_klines,
    market_scan_score_spec,
    rank_score_details,
    replay_score_details,
    score_market_scan_item,
    stable_score_spec_hash,
)
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence
from app.services.market_scan_universe import build_market_scan_universe
from tests.factories import make_kline, make_quote, make_stock_info


AS_OF = datetime(2026, 7, 17, 16, 30)
DATA_DATE = date(2026, 7, 17)
PREOPEN_AS_OF = datetime(2026, 7, 20, 8, 0)


def test_market_scan_score_is_deterministic_and_keeps_metadata_tags() -> None:
    item = _item(is_st=True, is_new=True, list_date=None)
    quote = _quote(fallback_used=True)
    rows = _rows(DATA_DATE, 80)
    rows[-1] = rows[-1].model_copy(update={"fallback_used": True})

    first = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    second = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )

    assert first == second
    assert first.status == "success"
    assert all(0 <= value <= 100 for value in (first.score, first.trend_score, first.leader_score, first.data_quality_score))  # type: ignore[operator]
    assert {"ST", "新股", "兜底行情", "兜底K线", "上市日期未知"}.issubset(first.tags)
    assert {"close", "ma5", "ma20", "ma60", "high20", "low20", "volume_ratio"} == set(first.metrics)
    assert first.data_date == DATA_DATE.isoformat()
    assert first.adjustment_mode == "qfq"
    assert first.quote_fallback_used is True
    assert first.kline_fallback_used is True
    assert first.metadata_degraded is True
    assert first.degradation_reasons == (
        "quote_fallback",
        "kline_fallback",
        "list_date_missing",
    )
    assert "趋势强度" in (first.reason or "")
    assert "非上涨概率" in (first.reason or "")


def test_preopen_score_matches_the_same_completed_official_snapshot() -> None:
    item = _item()
    quote = _quote()
    rows = _rows(DATA_DATE, 80)

    official = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        expected_quote_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        mode="official",
    )
    preopen = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=PREOPEN_AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        expected_quote_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        mode="preopen",
    )

    assert preopen.score == official.score
    assert preopen.raw_score == official.raw_score
    assert preopen.data_date == DATA_DATE.isoformat()
    assert preopen.quote_timestamp == quote.timestamp


def test_preopen_score_keeps_quote_and_kline_date_gates_strict() -> None:
    kwargs = {
        "as_of": PREOPEN_AS_OF,
        "completed_cutoff": DATA_DATE,
        "expected_data_date": DATA_DATE,
        "expected_quote_date": DATA_DATE,
        "min_history_rows": 60,
        "min_data_quality_score": 0,
        "mode": "preopen",
    }

    with pytest.raises(MarketScanDataMissing, match="报价日期.*不一致"):
        score_market_scan_item(
            _item(),
            _quote(timestamp="2026-07-16 15:00:00"),
            _rows(DATA_DATE, 80),
            **kwargs,
        )

    with pytest.raises(MarketScanDataMissing, match="早于应有交易日"):
        score_market_scan_item(
            _item(),
            _quote(),
            _rows(date(2026, 7, 16), 80),
            **kwargs,
        )


def test_preopen_score_requires_quote_close_to_match_completed_daily_close() -> None:
    quote = _quote().model_copy(
        update={"price": 10.7, "change": 0.7, "change_pct": 7.0},
    )

    with pytest.raises(MarketScanDataMissing, match="盘前复盘报价收盘价与上一完成交易日日K"):
        score_market_scan_item(
            _item(),
            quote,
            _rows(DATA_DATE, 80),
            as_of=PREOPEN_AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            expected_quote_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
            mode="preopen",
        )


def test_market_scan_persists_separate_score_dimensions_and_verifiable_point_in_time_evidence() -> None:
    result = score_market_scan_item(
        _item(),
        _quote(),
        _rows(DATA_DATE, 80),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )

    dimensions = result.score_details["components"]["score_dimensions"]
    scores = dimensions["scores"]
    evidence = dimensions["point_in_time_evidence"]

    assert dimensions["semantics"]["alpha"] == "ordinal-research-score-not-return-probability"
    assert all(0 <= scores[key] <= 100 for key in ("alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"))
    assert set(scores["decision_utility"]) == {"conservative", "balanced", "aggressive"}
    assert verify_market_scan_point_in_time_evidence(evidence) is True
    assert len(evidence["payload"]["bar_contract_61"]) == 61

    corrupted = deepcopy(evidence)
    corrupted["payload"]["quote_price"] += 1
    assert verify_market_scan_point_in_time_evidence(corrupted) is False


def test_market_scan_score_is_neutral_to_short_cache_but_not_fallback() -> None:
    rows = _rows(DATA_DATE, 80)
    direct = score_market_scan_item(
        _item(),
        _quote(),
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    cached = score_market_scan_item(
        _item(),
        _quote().model_copy(update={"source": "腾讯行情·短时缓存", "from_cache": True}),
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    fallback = score_market_scan_item(
        _item(),
        _quote(fallback_used=True).model_copy(update={"source": "腾讯行情·兜底缓存", "from_cache": True}),
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )

    assert cached.data_quality_score == direct.data_quality_score
    assert cached.raw_score == direct.raw_score
    assert cached.score == direct.score
    assert fallback.data_quality_score < direct.data_quality_score  # type: ignore[operator]
    assert fallback.raw_score < direct.raw_score  # type: ignore[operator]


def test_market_scan_metadata_degradation_keeps_industry_and_list_date_reasons_distinct() -> None:
    result = score_market_scan_item(
        _item(industry=None, list_date=None),
        _quote(),
        _rows(DATA_DATE, 80),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )

    assert result.metadata_degraded is True
    assert {"行业未知", "上市日期未知"}.issubset(result.tags)
    assert result.degradation_reasons == ("industry_missing", "list_date_missing")


def test_market_scan_v4_spec_serializes_penalty_only_quality_and_continuous_refinement() -> None:
    spec = market_scan_score_spec(min_data_quality_score=50)

    assert spec["schema_version"] == 4
    assert spec["rule_version"] == "full-market-score-v4"
    assert spec["leader_profile"] == {
        "algorithm": "leader-score-additive-v1",
        "profile_id": "full-market-trend-only-v1",
        "base": 50,
        "trend_weight": 1.0,
        "rules": [],
    }
    assert spec["data_quality_policy"]["cached_quote"] == "neutral"
    assert spec["volume_ratio"] == {"recent_window": 5, "base_window": 20, "min_count": 6, "precision": 2}
    assert spec["final_score"]["quality_policy"] == "penalty-only"
    assert spec["final_score"]["quality_penalty_per_missing_point"] == 0.15
    assert spec["ranking"]["refinement"]["algorithm"] == "bounded-medium-term-refinement-v1"
    assert spec["ranking"]["refinement"]["max_rank_discount"] < spec["ranking"]["base_score_minimum_step"]
    assert spec["rounding"]["raw_score_decimals"] == 6
    assert spec["eligibility"]["single_price_session_excluded"] is True
    assert spec["eligibility"]["valid_quote_fields_required"] is True
    assert spec["eligibility"]["max_change_pct_gap"] == 0.3
    assert tuple(tuple(entry) for entry in spec["ranking"]["tie_break"]) == FULL_MARKET_SCORE_TIE_BREAK
    assert FULL_MARKET_SCORE_TIE_BREAK == (("raw_score", "desc"), ("symbol", "asc"))


def test_market_scan_replay_validates_raw_score_and_compatibly_replays_v2_v3_specs() -> None:
    result = score_market_scan_item(
        _item(),
        _quote(),
        _rows(DATA_DATE, 80),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    replay = replay_score_details(result.score_details)

    assert replay.score_spec_schema_version == 4
    assert replay.raw_score == result.raw_score
    assert replay.tie_break == (("raw_score", "desc"), ("symbol", "asc"))

    corrupted_raw = deepcopy(result.score_details)
    corrupted_raw["ranking"]["tie_break_values"]["raw_score"] += 0.1
    with pytest.raises(MarketScanReplayError, match="raw_score"):
        replay_score_details(corrupted_raw)

    v3 = _as_v3_score_details(result.score_details)
    assert replay_score_details(v3).score_spec_schema_version == 3

    legacy = deepcopy(v3)
    legacy_spec = legacy["score_spec"]
    legacy_spec["schema_version"] = 2
    legacy_spec["algorithms"]["volume_ratio"] = "recent-volume-ratio-v1"
    legacy_spec["algorithms"]["data_quality"] = "data-quality-v1"
    legacy_spec["algorithms"]["final_score"] = "weighted-leader-quality-v1"
    legacy_spec["leader_profile"].pop("profile_id")
    legacy_spec.pop("volume_ratio")
    legacy_spec.pop("data_quality_policy")
    legacy_spec["rounding"].pop("raw_score_decimals")
    legacy_spec["ranking"]["tie_break"] = [entry for entry in legacy_spec["ranking"]["tie_break"] if entry[0] != "raw_score"]
    legacy["ranking"]["tie_break"] = [entry for entry in legacy["ranking"]["tie_break"] if entry[0] != "raw_score"]
    legacy["ranking"]["tie_break_values"].pop("raw_score")
    legacy["score_spec_hash"] = stable_score_spec_hash(legacy_spec)

    assert replay_score_details(legacy).score_spec_schema_version == 2


def test_market_scan_replay_uses_continuous_medium_term_structure_before_symbol() -> None:
    slow = score_market_scan_item(
        _item(),
        _quote(),
        _trend_rows(DATA_DATE, 80, first_close=6.1, last_close=10.5),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    fast = score_market_scan_item(
        _item(),
        _quote(),
        _trend_rows(DATA_DATE, 80, first_close=6.0, last_close=10.5),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    higher = _score_details_with_symbol(fast.score_details, "600002.SH")
    lower = _score_details_with_symbol(slow.score_details, "600001.SH")

    assert fast.score == slow.score
    assert fast.trend_score == slow.trend_score
    assert higher["components"]["final_score"]["raw"] > lower["components"]["final_score"]["raw"]
    assert rank_score_details([("600001.SH", lower), ("600002.SH", higher)]) == {
        "600002.SH": 1,
        "600001.SH": 2,
    }


def test_completed_market_scan_klines_excludes_future_invalid_and_identical_duplicates() -> None:
    earlier = make_kline(date="2026-07-16", close=10)
    replacement = earlier.model_copy(update={"source": "另一个来源"})
    current = make_kline(date="2026-07-17", close=12)
    future = make_kline(date="2026-07-18", close=13)
    invalid_date = make_kline(date="2026/07/17", close=14)

    rows = completed_market_scan_klines(
        [earlier, future, invalid_date, replacement, current],
        DATA_DATE,
    )

    assert [(row.date, row.close) for row in rows] == [
        ("2026-07-16", 10),
        ("2026-07-17", 12),
    ]


def test_completed_market_scan_klines_rejects_conflicting_same_day_bars_regardless_of_order() -> None:
    first = make_kline(date="2026-07-16", close=10)
    conflicting = make_kline(date="2026-07-16", close=11)

    for rows in ([first, conflicting], [conflicting, first]):
        with pytest.raises(MarketScanDataMissing, match="存在冲突日K"):
            completed_market_scan_klines(rows, DATA_DATE)


def test_market_scan_rejects_provider_bar_after_expected_trading_date() -> None:
    rows = [*_rows(DATA_DATE, 79), make_kline(date="2026-07-20", close=13)]

    with pytest.raises(MarketScanDataMissing, match="晚于应有交易日"):
        score_market_scan_item(
            _item(),
            _quote(),
            rows,
            as_of=datetime(2026, 7, 20, 16, 30),
            completed_cutoff=date(2026, 7, 20),
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


@pytest.mark.parametrize(
    ("item_factory", "quote_factory", "rows_factory", "expected_exception", "message"),
    [
        (
            lambda: _item(),
            lambda: _quote(code="000001", market="SZ"),
            lambda: _rows(DATA_DATE, 80),
            MarketScanDataMissing,
            "代码不匹配",
        ),
        (lambda: _item(), lambda: _quote(), lambda: _rows(DATA_DATE, 59), MarketScanSkipped, "日K不足"),
        (
            lambda: _item(),
            lambda: _quote().model_copy(update={"volume": 0.0, "amount": 0.0}),
            lambda: _rows(date(2026, 7, 16), 80),
            MarketScanSkipped,
            "可能停牌",
        ),
        (
            lambda: _item(),
            lambda: _quote(timestamp="2026-07-16 15:00:00"),
            lambda: _rows(DATA_DATE, 80),
            MarketScanDataMissing,
            "与完整交易日",
        ),
        (
            lambda: _item(),
            lambda: _quote(),
            lambda: [row.model_copy(update={"adjustment_mode": "none"}) for row in _rows(DATA_DATE, 80)],
            MarketScanDataMissing,
            "不是一致的前复权",
        ),
    ],
)
def test_market_scan_score_rejects_non_comparable_data(
    item_factory,
    quote_factory,
    rows_factory,
    expected_exception: type[Exception],
    message: str,
) -> None:
    with pytest.raises(expected_exception, match=message):
        score_market_scan_item(
            item_factory(),
            quote_factory(),
            rows_factory(),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_current_trading_quote_does_not_turn_stale_kline_into_suspension() -> None:
    with pytest.raises(MarketScanDataMissing, match="当日报价存在有效成交"):
        score_market_scan_item(
            _item(),
            _quote(),
            _rows(date(2026, 7, 16), 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_current_zero_liquidity_quote_and_bar_are_classified_as_possible_suspension() -> None:
    rows = _rows(DATA_DATE, 80)
    rows[-1] = rows[-1].model_copy(update={"volume": 0.0})

    with pytest.raises(MarketScanSkipped, match="可能停牌"):
        score_market_scan_item(
            _item(),
            _quote().model_copy(update={"volume": 0.0, "amount": 0.0}),
            rows,
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_rejects_quote_after_the_batch_as_of_boundary() -> None:
    with pytest.raises(MarketScanDataMissing, match="晚于批次截止时点"):
        score_market_scan_item(
            _item(),
            _quote(timestamp="2026-07-17 17:00:00"),
            _rows(DATA_DATE, 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


@pytest.mark.parametrize(
    ("quote_update", "message"),
    [
        ({"volume": 0}, "成交量或成交额"),
        ({"amount": 0}, "成交量或成交额"),
        ({"turnover_rate": None}, "缺少换手率"),
    ],
)
def test_market_scan_score_rejects_missing_rankable_quote_liquidity(
    quote_update: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(MarketScanDataMissing, match=message):
        score_market_scan_item(
            _item(),
            _quote().model_copy(update=quote_update),
            _rows(DATA_DATE, 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_skips_single_price_session_with_uncertain_execution() -> None:
    locked = _quote().model_copy(update={"open": 10.5, "high": 10.5, "low": 10.5, "price": 10.5})

    with pytest.raises(MarketScanSkipped, match="全天单一价格"):
        score_market_scan_item(
            _item(),
            locked,
            _rows(DATA_DATE, 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_score_rejects_missing_recent_kline_volume() -> None:
    rows = _rows(DATA_DATE, 80)
    rows[-5] = rows[-5].model_copy(update={"volume": 0})

    with pytest.raises(MarketScanDataMissing, match="连续有效成交量"):
        score_market_scan_item(
            _item(),
            _quote(),
            rows,
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


@pytest.mark.parametrize(
    ("quote_update", "message"),
    [
        ({"open": 10.9}, "OHLC"),
        ({"turnover_rate": -0.1}, "OHLC"),
        ({"change_pct": -5.0}, "涨跌幅"),
    ],
)
def test_market_scan_score_rejects_malformed_or_internally_inconsistent_quotes(
    quote_update: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(MarketScanDataMissing, match=message):
        score_market_scan_item(
            _item(),
            _quote().model_copy(update=quote_update),
            _rows(DATA_DATE, 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_score_rejects_quote_and_same_day_kline_close_mismatch() -> None:
    rows = _rows(DATA_DATE, 80)
    rows[-1] = rows[-1].model_copy(update={"close": 10.0, "high": 10.2})

    with pytest.raises(MarketScanDataMissing, match="收盘价偏差"):
        score_market_scan_item(
            _item(),
            _quote(),
            rows,
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_universe_deduplicates_and_marks_st_new_and_delisted() -> None:
    rows = [
        make_stock_info("600519", "SH").model_copy(
            update={"name": "*ST茅台", "industry": "白酒", "list_date": "2026-07-01"}
        ),
        make_stock_info("600519", "SH").model_copy(update={"name": "重复股"}),
        make_stock_info("000001", "SZ").model_copy(update={"name": "退市样本"}),
        make_stock_info("920066", "BJ").model_copy(update={"name": "北交样本", "list_date": "20240703"}),
        make_stock_info("600000", "SH").model_copy(update={"symbol": "000001.SZ", "name": "字段冲突"}),
        make_stock_info("600001", "SH").model_copy(
            update={"symbol": "600001.SZ", "market": "SZ", "name": "交易所错配"}
        ),
        make_stock_info("900901", "SH").model_copy(update={"name": "沪市B股"}),
        make_stock_info("200002", "SZ").model_copy(update={"name": "深市B股"}),
        StockInfo(
            symbol="123456.HK",
            code="123456",
            market="HK",
            name="非A股",
            source="test",
            updated_at="2026-07-17 16:00:00",
        ),
    ]

    universe = build_market_scan_universe(rows, data_date=DATA_DATE, new_stock_days=120)

    assert [seed.symbol for seed in universe.seeds] == ["920066.BJ", "600519.SH"]
    assert universe.excluded_count == 7
    by_symbol = {seed.symbol: seed for seed in universe.seeds}
    assert by_symbol["600519.SH"].is_st is True
    assert by_symbol["600519.SH"].is_new is True
    assert by_symbol["600519.SH"].list_date == "2026-07-01"
    assert by_symbol["600519.SH"].metadata_source == rows[0].source
    assert by_symbol["920066.BJ"].is_new is False
    assert by_symbol["920066.BJ"].list_date == "2024-07-03"


def _score_details_with_symbol(details: dict[str, object], symbol: str) -> dict[str, object]:
    updated = deepcopy(details)
    updated["ranking"]["tie_break_values"]["symbol"] = symbol
    return updated


def _as_v3_score_details(details: dict[str, object]) -> dict[str, object]:
    updated = deepcopy(details)
    spec = updated["score_spec"]
    inputs = updated["inputs"]
    components = updated["components"]
    ranking = updated["ranking"]
    leader_score = components["leader_score"]["score"]
    quality_score = inputs["data_quality_score"]
    raw_score = round(leader_score * 0.85 + quality_score * 0.15, 4)
    rounded_score = round(raw_score)
    v3_tie_break = [
        ["score", "desc"],
        ["raw_score", "desc"],
        ["trend_score", "desc"],
        ["change_pct", "desc"],
        ["amount", "desc"],
        ["symbol", "asc"],
    ]

    spec["schema_version"] = 3
    spec["rule_version"] = "full-market-score-v3"
    spec["algorithms"].pop("rank_refinement")
    spec["algorithms"]["trend_score"] = "trend-score-v1"
    spec["algorithms"]["final_score"] = "weighted-trend-quality-v2"
    spec["eligibility"].pop("quote_kline_close_consistency")
    spec["eligibility"].pop("quote_timestamp_not_after_as_of")
    spec["eligibility"].pop("single_price_session_excluded")
    spec["eligibility"].pop("valid_quote_fields_required")
    spec["eligibility"].pop("max_change_pct_gap")
    spec["final_score"] = {
        "formula": "leader_score * leader_weight + data_quality_score * quality_weight",
        "weights": {"leader_score": 0.85, "data_quality_score": 0.15},
        "clamp": [0, 100],
    }
    spec["rounding"]["component_stage"] = "after-trend-weight-and-final-weighted-sum"
    spec["rounding"]["raw_score_decimals"] = 4
    spec["ranking"] = {"tie_break": v3_tie_break}
    for key in tuple(inputs):
        if key.startswith("rank_"):
            inputs.pop(key)
    components.pop("rank_refinement")
    components["final_score"] = {
        "weighted_terms": {
            "leader_score": leader_score * 0.85,
            "data_quality_score": quality_score * 0.15,
        },
        "raw": raw_score,
        "rounded": rounded_score,
        "score": rounded_score,
    }
    ranking["tie_break"] = v3_tie_break
    ranking["tie_break_values"] = {
        "score": rounded_score,
        "raw_score": raw_score,
        "trend_score": inputs["trend_score"],
        "change_pct": inputs["change_pct"],
        "amount": inputs["amount"],
        "symbol": ranking["tie_break_values"]["symbol"],
    }
    updated["score_spec_hash"] = stable_score_spec_hash(spec)
    return updated


def _item(
    *,
    is_st: bool = False,
    is_new: bool = False,
    industry: str | None = "白酒",
    list_date: str | None = "2001-08-27",
) -> MarketScanResultItem:
    return MarketScanResultItem(
        run_id=1,
        symbol="600519.SH",
        code="600519",
        market="SH",
        name="贵州茅台",
        industry=industry,
        list_date=list_date,
        is_st=is_st,
        is_new=is_new,
        status="pending",
        updated_at="2026-07-17 16:30:00",
    )


def _quote(
    *,
    code: str = "600519",
    market: str = "SH",
    timestamp: str = "2026-07-17 15:00:00",
    fallback_used: bool = False,
):
    return make_quote(
        price=10.5,
        prev_close=10.0,
        high=10.8,
        low=9.9,
        change_pct=5.0,
        turnover_rate=4.5,
        timestamp=timestamp,
    ).model_copy(
        update={
            "code": code,
            "market": market,
            "name": "贵州茅台",
            "open": 10.1,
            "amount": 900_000_000,
            "change": 0.5,
            "fallback_used": fallback_used,
        }
    )


def _rows(latest: date, count: int) -> list[Kline]:
    return _trend_rows(latest, count, first_close=10.5 - (count - 1) * 0.04, last_close=10.5)


def _trend_rows(latest: date, count: int, *, first_close: float, last_close: float) -> list[Kline]:
    days: list[date] = []
    cursor = latest
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    step = (last_close - first_close) / max(1, count - 1)
    return [
        make_kline(
            date=day.isoformat(),
            close=first_close + index * step,
            volume=1_000_000 + index * 20_000,
            source="test-qfq",
            as_of=latest.isoformat(),
            data_version=f"test|qfq|{latest.isoformat()}",
        )
        for index, day in enumerate(days)
    ]
