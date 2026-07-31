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


def test_shadow_score_ignores_rows_after_data_date() -> None:
    base = _item(1)
    future = make_kline(date="2026-07-20", close=1000, high=1001, low=999)
    with_future = ShadowScoreInput(**{**base.__dict__, "rows": (*base.rows, future)})

    assert score_shadow_market((base,)).results[0].raw_score == score_shadow_market((with_future,)).results[0].raw_score


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_shadow_score_rejects_non_finite_and_short_history(value: float) -> None:
    base = _item(1)
    with pytest.raises(ValueError, match="非有限数"):
        score_shadow_market((ShadowScoreInput(**{**base.__dict__, "amount": value}),))
    with pytest.raises(ValueError, match="不足60根"):
        score_shadow_market((ShadowScoreInput(**{**base.__dict__, "rows": base.rows[-59:]}),))


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

    object.__setattr__(batch, "normalization", {**batch.normalization, "input_digest": "bad"})
    with pytest.raises(ShadowScoreReplayError, match="归一化摘要"):
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
) -> ShadowScoreInput:
    rows = _rows(slope=slope, volatility=volatility)
    return ShadowScoreInput(
        symbol=symbol or f"{600000 + index:06d}.SH",
        market=market,
        quote_date=DATA_DATE.isoformat(),
        data_date=DATA_DATE.isoformat(),
        price=price if price is not None else rows[-1].close,
        change_pct=change_pct,
        turnover_rate=turnover_rate,
        amount=amount,
        volume_ratio=volume_ratio,
        data_quality_score=data_quality_score,
        rows=rows,
        list_date="2020-01-02",
        is_st=is_st,
        is_new=is_new,
        quote_fallback_used=quote_fallback_used,
        kline_fallback_used=kline_fallback_used,
        metadata_degraded=metadata_degraded,
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
