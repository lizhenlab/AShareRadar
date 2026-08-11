from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from typing import Any, cast

import pytest

from app.services.market_scan_shadow_scoring import (
    SHADOW_SCORE_CANDIDATE_VERSION,
    ShadowScoreInput,
    ShadowScoreReplayError,
    replay_shadow_score_details,
    score_shadow_market,
    verify_shadow_score_batch,
)
from app.services.trading_calendar import is_trading_day
from tests.factories import make_kline


DATA_DATE = date(2026, 7, 17)


def test_shadow_score_is_deterministic_and_input_order_independent() -> None:
    items = tuple(_item(index, slope=(index - 20) / 5_000) for index in range(40))

    first = score_shadow_market(items)
    second = score_shadow_market(tuple(reversed(items)))

    assert first.candidate_id == second.candidate_id
    assert first.candidate_id.startswith(SHADOW_SCORE_CANDIDATE_VERSION)
    assert [(item.symbol, item.rank, item.raw_score) for item in first.results] == [
        (item.symbol, item.rank, item.raw_score) for item in second.results
    ]
    assert first.normalization == second.normalization
    assert all(replay_shadow_score_details(item.details) == item.raw_score for item in first.results)


def test_shadow_normalization_smoothly_blends_global_and_board_percentiles() -> None:
    main = tuple(
        _item(index, symbol=f"{600000 + index:06d}.SH", slope=(index - 20) / 5_000)
        for index in range(40)
    )
    star_29 = tuple(
        _item(100 + index, symbol=f"{688000 + index:06d}.SH", slope=(index - 10) / 6_000)
        for index in range(29)
    )
    before = score_shadow_market((*main, *star_29))
    after = score_shadow_market(
        (*main, *star_29, _item(200, symbol="688099.SH", slope=0.0005))
    )
    target = star_29[10].symbol
    before_result = next(item for item in before.results if item.symbol == target)
    after_result = next(item for item in after.results if item.symbol == target)
    before_score = before_result.details["components"]["normalized_trend"]  # type: ignore[index]
    after_score = after_result.details["components"]["normalized_trend"]  # type: ignore[index]
    before_summary = before.normalization["groups"]["STAR"]  # type: ignore[index]
    after_summary = after.normalization["groups"]["STAR"]  # type: ignore[index]

    assert before_summary["board_weight"] < after_summary["board_weight"]
    assert after_summary["board_weight"] - before_summary["board_weight"] < 0.01
    assert abs(after_score - before_score) < 3
    assert before.normalization["method"] == "hierarchical-global-board-midrank-percentile-shrinkage"


def test_shadow_global_component_preserves_cross_board_comparability() -> None:
    weak_star = _item(1, symbol="688001.SH", slope=-0.002)
    strong_main = _item(2, symbol="600002.SH", slope=0.004)

    batch = score_shadow_market((weak_star, strong_main))
    normalized = {
        item.symbol: item.details["components"]["normalized_trend"]  # type: ignore[index]
        for item in batch.results
    }

    assert normalized[strong_main.symbol] > normalized[weak_star.symbol]


def test_shadow_volume_confirmation_is_rebuilt_from_validated_history() -> None:
    base = _item(1, volume_ratio=0.2)
    changed_report = ShadowScoreInput(**{**base.__dict__, "volume_ratio": 5.0})

    first = score_shadow_market((base,)).results[0]
    second = score_shadow_market((changed_report,)).results[0]

    assert first.raw_score == second.raw_score
    assert first.details["inputs"]["reported_volume_ratio"] == 0.2  # type: ignore[index]
    assert second.details["inputs"]["reported_volume_ratio"] == 5.0  # type: ignore[index]
    assert first.details["inputs"]["derived_volume_ratio"] == second.details["inputs"]["derived_volume_ratio"]  # type: ignore[index]


def test_shadow_quality_and_special_status_are_penalty_only() -> None:
    clean = _item(1, data_quality_score=100)
    degraded = _item(
        2,
        data_quality_score=70,
        quote_fallback_used=True,
        kline_fallback_used=True,
        metadata_degraded=True,
        is_st=True,
        is_new=True,
    )
    batch = score_shadow_market((clean, degraded))
    by_symbol = {item.symbol: item for item in batch.results}

    assert by_symbol[clean.symbol].details["components"]["normalized_trend"] == by_symbol[degraded.symbol].details["components"]["normalized_trend"]  # type: ignore[index]
    assert by_symbol[clean.symbol].raw_score > by_symbol[degraded.symbol].raw_score
    penalties = by_symbol[degraded.symbol].details["components"]["penalties"]  # type: ignore[index]
    assert penalties["confidence"] > 0
    assert penalties["special_status"] == 8


def test_shadow_ablation_versions_remove_only_the_named_penalty() -> None:
    stretched = _item(
        1,
        quote_date="2026-07-20",
        price=18,
        change_pct=9.8,
        amount=6_000_000,
        turnover_rate=25,
        volume_ratio=2.5,
        volatility=0.08,
    )

    full = score_shadow_market((stretched,), variant="v5_full").results[0]
    no_overextension = score_shadow_market((stretched,), variant="v5_without_overextension").results[0]
    no_risk = score_shadow_market((stretched,), variant="v5_without_risk").results[0]
    no_liquidity = score_shadow_market((stretched,), variant="v5_without_liquidity").results[0]

    assert no_overextension.raw_score >= full.raw_score
    assert no_risk.raw_score >= full.raw_score
    assert no_liquidity.raw_score >= full.raw_score
    assert len({item.candidate_id for item in (full, no_overextension, no_risk, no_liquidity)}) == 4


def test_shadow_v53_preregisters_residual_skip5_and_volume_lifecycle_candidates() -> None:
    items = tuple(
        _item(index, slope=0.0005 * (index + 1), volume_ratio=1.2 + index * 0.3)
        for index in range(5)
    )

    baseline = score_shadow_market(items, variant="v5_2_baseline")
    residual = score_shadow_market(items, variant="v5_3_residual_momentum")
    skip5 = score_shadow_market(items, variant="v5_3_skip5_residual_momentum")
    lifecycle = score_shadow_market(items, variant="v5_3_skip5_residual_volume_lifecycle")

    assert baseline.spec["normalization"]["method"] == "hierarchical-global-board-midrank-percentile-shrinkage"
    assert residual.spec["normalization"]["method"] == "market-board-centered-residual-midrank-percentile"
    assert skip5.spec["normalization"]["factor"] == "skip5_momentum"
    assert lifecycle.spec["final_score"]["formula"] == "normalized_alpha + volume_lifecycle_delta - enabled_penalties"
    assert len({batch.candidate_id for batch in (baseline, residual, skip5, lifecycle)}) == 4
    assert all(item.details["components"]["raw_normalization_factor"] is not None for item in lifecycle.results)


def test_shadow_v54_uses_replayable_multilevel_residuals_with_industry_quality_gate() -> None:
    items = (
        _item(1, symbol="600001.SH", market="SH", industry="制造业"),
        _item(2, symbol="688002.SH", market="SH", industry="半导体制造业"),
        _item(3, symbol="300003.SZ", market="SZ", industry="半导体制造业"),
        _item(4, symbol="000004.SZ", market="SZ", industry="银行业"),
        _item(5, symbol="830005.BJ", market="BJ", industry="专用设备制造业"),
    )

    batch = score_shadow_market(items, variant="v5_4_skip5_multilevel_residual")

    assert batch.candidate_id.startswith("full-market-shadow-score-v5.4:")
    assert batch.normalization["method"] == (
        "sequential-shrunk-market-board-industry-liquidity-residual-midrank-percentile"
    )
    assert set(batch.normalization["steps"]) == {"market", "board", "industry", "liquidity"}
    industry_groups = batch.normalization["steps"]["industry"]["groups"]
    assert industry_groups["制造业"]["eligible"] is False
    assert industry_groups["半导体制造业"]["eligible"] is True
    assert all(replay_shadow_score_details(item.details) == item.raw_score for item in batch.results)


def test_shadow_v54_neutralizes_volume_lifecycle_without_time_aligned_intraday_volume() -> None:
    official = _item(1, volume_ratio=2.5, industry="软件业")
    intraday = _item(
        1,
        quote_date="2026-07-20",
        volume_ratio=2.5,
        mode="intraday",
        industry="软件业",
    )

    official_result = score_shadow_market(
        (official,), variant="v5_4_skip5_multilevel_residual_volume_lifecycle"
    ).results[0]
    intraday_result = score_shadow_market(
        (intraday,), variant="v5_4_skip5_multilevel_residual_volume_lifecycle"
    ).results[0]

    official_context = official_result.details["components"]["volume_context"]
    intraday_context = intraday_result.details["components"]["volume_context"]
    assert official_context["alignment"] == "same-completed-session"
    assert intraday_context["alignment"] == "intraday-time-aligned-volume-unavailable-neutralized"
    assert intraday_context["applied_delta"] == 0
    assert intraday_result.details["components"]["volume_confirmation_delta"] == 0
    assert replay_shadow_score_details(intraday_result.details) == intraday_result.raw_score


def test_shadow_v54_persists_explicit_risk_and_capacity_constraints() -> None:
    constrained = _item(
        1,
        amount=5_000_000,
        turnover_rate=40,
        is_st=True,
        is_new=True,
        industry="软件业",
    )

    result = score_shadow_market(
        (constrained,), variant="v5_4_skip5_multilevel_residual"
    ).results[0]
    components = result.details["components"]

    assert components["penalties"]["tradability"] > 0
    assert components["applied_penalties"]["liquidity"] is False
    assert components["applied_penalties"]["tradability"] is True
    assert {"special_treatment", "new_stock", "capacity_above_one_percent_of_amount", "turnover_extreme"}.issubset(
        components["explicit_constraints"]["flags"]
    )


def test_shadow_score_ignores_rows_after_data_date() -> None:
    base = _item(1)
    future = make_kline(date="2026-07-20", close=1000, high=1001, low=999)
    with_future = ShadowScoreInput(**{**base.__dict__, "rows": (*base.rows, future)})

    assert score_shadow_market((base,)).results[0].raw_score == score_shadow_market((with_future,)).results[0].raw_score


def test_shadow_score_uses_exact_sixty_session_return_and_derived_daily_change() -> None:
    base = _item(1, change_pct=-9.0)

    result = score_shadow_market((base,)).results[0]
    inputs = cast(dict[str, Any], result.details["inputs"])
    completed = [
        row
        for row in base.rows
        if row.date <= base.data_date and is_trading_day(date.fromisoformat(row.date))
    ]

    assert inputs["return60_pct"] == pytest.approx(
        (base.price / completed[-61].close - 1) * 100,
        abs=1e-8,
    )
    assert inputs["reported_change_pct"] == -9.0
    assert inputs["derived_change_pct"] == pytest.approx(
        (base.price / completed[-2].close - 1) * 100,
        abs=1e-8,
    )


def test_shadow_score_rejects_conflicting_same_day_bars_regardless_of_input_order() -> None:
    base = _item(1)
    latest = base.rows[-1]
    conflicting = latest.model_copy(
        update={"close": latest.close + 1, "high": latest.high + 1}
    )

    for rows in ((*base.rows, conflicting), (conflicting, *base.rows)):
        with pytest.raises(ValueError, match="冲突日K"):
            score_shadow_market((ShadowScoreInput(**{**base.__dict__, "rows": rows}),))


@pytest.mark.parametrize(
    "updates",
    [
        {"volume_ratio": 0},
        {"turnover_rate": -0.1},
        {"turnover_rate": None},
        {"market": "SZ"},
        {"quote_date": "2026-07-16"},
    ],
)
def test_shadow_score_rejects_non_comparable_snapshot_inputs(updates: dict[str, object]) -> None:
    base = _item(1)

    with pytest.raises(ValueError, match="准入条件|不一致|早于"):
        score_shadow_market((ShadowScoreInput(**{**base.__dict__, **updates}),))


def test_shadow_price_limit_uses_effective_listing_rules_instead_of_new_stock_label() -> None:
    seasoned_new = _item(1, is_new=True, list_date="2026-07-01", change_pct=9.5)
    listing_day = _item(2, is_new=True, list_date=DATA_DATE.isoformat(), change_pct=9.5)

    seasoned_inputs = score_shadow_market((seasoned_new,)).results[0].details["inputs"]
    listing_inputs = score_shadow_market((listing_day,)).results[0].details["inputs"]

    assert seasoned_inputs["price_limit_pct"] == 10
    assert seasoned_inputs["price_limit_quality"] == "ok"
    assert listing_inputs["price_limit_pct"] is None
    assert listing_inputs["price_limit_quality"] == "ok"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_shadow_score_rejects_non_finite_and_short_history(value: float) -> None:
    base = _item(1)
    with pytest.raises(ValueError, match="非有限数"):
        score_shadow_market((ShadowScoreInput(**{**base.__dict__, "amount": value}),))
    with pytest.raises(ValueError, match="不足61根"):
        score_shadow_market((ShadowScoreInput(**{**base.__dict__, "rows": base.rows[-60:]}),))


def test_shadow_score_handles_board_rules_single_price_and_zero_volume() -> None:
    star = _item(1, symbol="688001.SH", market="SH")
    chinext = _item(2, symbol="300002.SZ", market="SZ")
    bse = _item(3, symbol="920003.BJ", market="BJ")
    single_price_rows = list(star.rows)
    latest = single_price_rows[-1]
    single_price_rows[-1] = latest.__class__(
        **{
            **latest.__dict__,
            "open": latest.close,
            "high": latest.close,
            "low": latest.close,
        }
    )
    star = ShadowScoreInput(**{**star.__dict__, "rows": tuple(single_price_rows)})

    batch = score_shadow_market((star, chinext, bse))

    assert {item.symbol: item.board for item in batch.results} == {
        "688001.SH": "STAR",
        "300002.SZ": "CHINEXT",
        "920003.BJ": "BSE",
    }
    zero_volume_rows = list(bse.rows)
    latest = zero_volume_rows[-1]
    zero_volume_rows[-1] = latest.__class__(**{**latest.__dict__, "volume": 0})
    with pytest.raises(ValueError, match="无成交"):
        score_shadow_market(
            (ShadowScoreInput(**{**bse.__dict__, "rows": tuple(zero_volume_rows)}),)
        )


def test_shadow_replay_detects_corrupted_components_and_batch_context() -> None:
    batch = score_shadow_market(tuple(_item(index) for index in range(3)))
    corrupted = deepcopy(batch.results[0].details)
    cast(dict[str, Any], corrupted["components"])["raw_score"] += 1

    with pytest.raises(ShadowScoreReplayError, match="重放不一致"):
        replay_shadow_score_details(corrupted)

    corrupted_input = deepcopy(batch.results[0].details)
    cast(dict[str, Any], corrupted_input["inputs"])["return20_pct"] += 10
    with pytest.raises(ShadowScoreReplayError, match="趋势因子无法从输入重放"):
        replay_shadow_score_details(corrupted_input)

    object.__setattr__(batch, "normalization", {**batch.normalization, "input_digest": "bad"})
    with pytest.raises(ShadowScoreReplayError, match="归一化无法重放"):
        verify_shadow_score_batch(batch)


def test_shadow_batch_replay_rejects_tampered_result_and_group_context() -> None:
    candidate = score_shadow_market(tuple(_item(index) for index in range(3)))
    object.__setattr__(candidate.results[0], "candidate_id", "tampered")
    with pytest.raises(ShadowScoreReplayError, match="候选上下文"):
        verify_shadow_score_batch(candidate)

    batch = score_shadow_market(tuple(_item(index) for index in range(3)))
    details_normalization = cast(dict[str, Any], batch.results[0].details["normalization"])
    details_normalization["group_summary"] = {"tampered": True}
    with pytest.raises(ShadowScoreReplayError, match="归一化上下文"):
        verify_shadow_score_batch(batch)


def _item(
    index: int,
    *,
    symbol: str | None = None,
    market: str = "SH",
    slope: float = 0.001,
    price: float | None = None,
    change_pct: float = 1.5,
    amount: float = 200_000_000,
    turnover_rate: float | None = 4,
    volume_ratio: float = 1.2,
    data_quality_score: int = 100,
    volatility: float = 0.01,
    is_st: bool = False,
    is_new: bool = False,
    quote_fallback_used: bool = False,
    kline_fallback_used: bool = False,
    metadata_degraded: bool = False,
    list_date: str | None = "2020-01-02",
    quote_date: str = DATA_DATE.isoformat(),
    mode: str = "official",
    industry: str | None = None,
) -> ShadowScoreInput:
    rows = _rows(slope=slope, volatility=volatility)
    return ShadowScoreInput(
        symbol=symbol or f"{600000 + index:06d}.SH",
        market=market,
        quote_date=quote_date,
        data_date=DATA_DATE.isoformat(),
        price=price if price is not None else rows[-1].close,
        change_pct=change_pct,
        turnover_rate=turnover_rate,
        amount=amount,
        volume_ratio=volume_ratio,
        data_quality_score=data_quality_score,
        rows=rows,
        list_date=list_date,
        is_st=is_st,
        is_new=is_new,
        quote_fallback_used=quote_fallback_used,
        kline_fallback_used=kline_fallback_used,
        metadata_degraded=metadata_degraded,
        mode=cast(Any, mode),
        industry=industry,
    )


def _rows(*, slope: float, volatility: float) -> tuple:
    dates: list[date] = []
    cursor = DATA_DATE
    while len(dates) < 90:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= timedelta(days=1)
    dates.reverse()
    rows = []
    for index, row_date in enumerate(dates):
        wave = volatility if index % 2 == 0 else -volatility / 2
        close = 10 * (1 + slope * index) * (1 + wave)
        rows.append(
            make_kline(
                date=row_date.isoformat(),
                close=close,
                high=close * (1 + volatility),
                low=close * (1 - volatility) - 0.6,
                volume=1_000_000 + index * 1_000,
            )
        )
    return tuple(rows)
