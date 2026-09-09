"""Risk must reflect loss magnitude and frequency, including constant losses."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
import math
import json
from pathlib import Path

import pytest

from app.services.market_scan_score_dimensions import (
    MARKET_SCAN_DIMENSION_ALGORITHM_VERSION,
    MARKET_SCAN_DIMENSION_LEGACY_V4_ALGORITHM_VERSION,
    market_scan_dimension_spec,
    verify_market_scan_point_in_time_evidence_context,
)
from app.services.market_scan_scoring import score_market_scan_item
from app.services.market_scan_shadow_scoring import (
    SHADOW_SCORE_VARIANTS,
    ShadowScoreReplayError,
    replay_shadow_score_details,
    score_shadow_market,
    stable_shadow_spec_hash,
    verify_shadow_score_batch,
)
from app.services.return_risk import downside_deviation_pct, downside_deviation_spec
from app.services.trading_calendar import trading_dates_between
from tests.factories import make_kline
from tests.test_market_scan_scoring import DATA_DATE, _freeze_legacy_dimension_fixture, _item, _persisted_item, _quote
from tests.test_market_scan_shadow_scoring import _item as _shadow_item


def _closes(returns):
    values = [100.0]
    for value in returns:
        values.append(values[-1] * (1 + value))
    return values


@pytest.mark.parametrize("returns,expected", [
    ([0.0] * 20, 0.0),
    ([0.03] * 20, 0.0),
    ([-0.05] * 20, 5.0),
    ([0.0] * 19 + [-0.1], 10 / math.sqrt(20)),
    ([-0.1] + [0.0] * 19, 10 / math.sqrt(20)),
    ([-0.1, 0.1] * 10, 10 / math.sqrt(2)),
    ([-0.02, -0.04, 0.0, 0.06], math.sqrt(5)),
])
def test_target_deviation_uses_all_returns_and_preserves_loss_size(returns, expected):
    assert downside_deviation_pct(_closes(returns)) == pytest.approx(expected)


@pytest.mark.parametrize("scale", [0.0001, 1, 10000])
def test_downside_deviation_is_invariant_to_positive_price_scale(scale):
    closes = _closes([-0.01, 0.03, -0.04, 0.0] * 5)
    assert downside_deviation_pct([value * scale for value in closes]) == pytest.approx(math.sqrt(4.25))


def test_larger_losses_and_more_loss_days_increase_target_deviation():
    one_loss = downside_deviation_pct(_closes([-0.05] + [0.0] * 19))
    larger_loss = downside_deviation_pct(_closes([-0.10] + [0.0] * 19))
    more_losses = downside_deviation_pct(_closes([-0.05] * 20))
    assert 0 < one_loss < larger_loss < more_losses


@pytest.mark.parametrize("invalid", [0.0, -1.0, math.inf, -math.inf, math.nan, True])
def test_invalid_prices_are_not_silently_removed_from_the_denominator(invalid):
    with pytest.raises(ValueError, match="有限正数"):
        downside_deviation_pct([100.0, invalid, 90.0])


@pytest.mark.parametrize("closes", [[], [100.0], [1e-300, 1e300]])
def test_no_history_and_nonfinite_returns_fail_explicitly(closes):
    with pytest.raises(ValueError):
        downside_deviation_pct(closes)


def _snapshot(returns, mode="official"):
    closes = [100.0] * 40 + _closes(returns)
    dates = trading_dates_between(date(2026, 1, 1), DATA_DATE)[-61:]
    rows = [
        make_kline(
            date=day.isoformat(), close=close, high=close + 1, low=close - 1,
            volume=1_000_000, source="test-qfq", as_of=DATA_DATE.isoformat(),
            data_version=f"test|qfq|{DATA_DATE.isoformat()}",
        )
        for day, close in zip(dates, closes, strict=True)
    ]
    price, previous = closes[-1], closes[-2]
    as_of = datetime(2026, 7, 17, 16, 30) if mode == "official" else datetime(2026, 7, 20, 8)
    quote = _quote().model_copy(update={
        "price": price, "prev_close": previous, "open": price,
        "high": price + 1, "low": price - 1,
        "change": price - previous, "change_pct": (price / previous - 1) * 100,
    })
    result = score_market_scan_item(
        _item(), quote, rows, as_of=as_of, completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE, expected_quote_date=DATA_DATE,
        min_history_rows=61, min_data_quality_score=0, mode=mode,
    )
    return result, rows, as_of


@pytest.mark.parametrize("mode", ["official", "preopen"])
def test_public_scan_and_shadow_share_the_completed_twenty_return_risk(mode):
    result, rows, as_of = _snapshot([-0.05] * 20, mode)
    dimensions = result.score_details["components"]["score_dimensions"]
    assert dimensions["raw_features"]["downside_volatility_20d_pct"] == pytest.approx(5.0)
    assert dimensions["scores"]["risk"] >= 50
    assert dimensions["algorithm"] == MARKET_SCAN_DIMENSION_ALGORITHM_VERSION
    assert dimensions["point_in_time_evidence"]["payload"]["dimension_spec"]["downside_deviation"] == downside_deviation_spec()
    item = _persisted_item(result)
    assert verify_market_scan_point_in_time_evidence_context(
        dimensions["point_in_time_evidence"], item=item, expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(), expected_as_of=as_of.isoformat(), expected_mode=mode,
    )
    shadow = score_shadow_market((replace(_shadow_item(1), rows=tuple(rows), price=rows[-1].close),))
    assert shadow.results[0].details["inputs"]["downside_volatility20_pct"] == pytest.approx(5.0)
    verify_shadow_score_batch(shadow)


def test_one_loss_inside_window_counts_while_a_loss_outside_it_does_not():
    single, _, _ = _snapshot([0.0] * 19 + [-0.1])
    raw = single.score_details["components"]["score_dimensions"]["raw_features"]
    assert raw["downside_volatility_20d_pct"] == pytest.approx(10 / math.sqrt(20))
    flat, rows, _ = _snapshot([0.0] * 20)
    rows[-22] = rows[-22].model_copy(update={"close": 1000, "open": 1000, "high": 1001, "low": 999})
    shadow = score_shadow_market((replace(_shadow_item(1), rows=tuple(rows), price=100),))
    assert shadow.results[0].details["inputs"]["downside_volatility20_pct"] == 0
    assert flat.score_details["components"]["score_dimensions"]["raw_features"]["downside_volatility_20d_pct"] == 0


def test_single_loss_reaches_risk_and_reduces_balanced_research_utility():
    result, _, _ = _snapshot([0.0] * 19 + [-0.1])
    current = result.score_details["components"]["score_dimensions"]
    old = deepcopy(current)
    _freeze_legacy_dimension_fixture(old)
    assert old["raw_features"]["downside_volatility_20d_pct"] == 0
    assert current["scores"]["risk"] > old["scores"]["risk"]
    assert current["scores"]["decision_utility"]["balanced"] < old["scores"]["decision_utility"]["balanced"]


@pytest.mark.parametrize("location", ["outer", "score_spec"])
def test_dimension_version_cannot_be_relabelled_across_envelope_boundaries(location):
    result, _, as_of = _snapshot([-0.05] * 20)
    item = _persisted_item(result)
    dimensions = item.score_details["components"]["score_dimensions"]
    if location == "outer":
        dimensions["algorithm"] = MARKET_SCAN_DIMENSION_LEGACY_V4_ALGORITHM_VERSION
    else:
        item.score_details["score_spec"]["research_dimensions"]["algorithm"] = MARKET_SCAN_DIMENSION_LEGACY_V4_ALGORITHM_VERSION
    assert not verify_market_scan_point_in_time_evidence_context(
        dimensions["point_in_time_evidence"], item=item, expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(), expected_as_of=as_of.isoformat(),
    )


@pytest.mark.parametrize("variant", SHADOW_SCORE_VARIANTS)
def test_shadow_candidates_bind_formula_and_reject_rehashed_legacy_spec(variant):
    batch = score_shadow_market((_shadow_item(1),), variant=variant)
    assert batch.spec["components"]["risk_penalty"]["downside_deviation"] == downside_deviation_spec()
    verify_shadow_score_batch(batch)
    details = deepcopy(batch.results[0].details)
    del details["score_spec"]["components"]["risk_penalty"]["downside_deviation"]
    details["score_spec_hash"] = stable_shadow_spec_hash(details["score_spec"])
    with pytest.raises(ShadowScoreReplayError, match="不是已注册候选版本"):
        replay_shadow_score_details(details)


def test_unknown_dimension_algorithm_is_rejected():
    with pytest.raises(ValueError, match="未注册"):
        market_scan_dimension_spec(algorithm_version="unknown")


def test_real_prechange_shadow_evidence_is_not_relabelled_as_current():
    fixture = Path(__file__).parent / "fixtures" / "market_scan_shadow_downside_legacy.json"
    old = json.loads(fixture.read_text())
    before = deepcopy(old)
    assert stable_shadow_spec_hash(old["details"]["score_spec"]) == old["spec_hash"]
    with pytest.raises(ShadowScoreReplayError, match="不是已注册候选版本"):
        replay_shadow_score_details(old["details"])
    assert old == before
