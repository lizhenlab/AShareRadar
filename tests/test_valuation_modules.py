from __future__ import annotations

import math
import unittest
from datetime import date, datetime, timedelta

import pytest

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.valuation_anchors import (
    VALUATION_ANCHOR_BANDS,
    peer_valuation_percentile,
    peer_valuation_sample_count,
    valuation_anchor_label,
    valuation_percentile_from_history,
)
from app.services.valuation_analysis import build_valuation_analysis
from app.services.valuation_components import (
    PEER_PERCENTILE_DELTA_RULES,
    VALUATION_PERCENTILE_DELTA_RULES,
    peer_percentile_score_delta,
    valuation_percentile_score_delta,
    valuation_summary,
)
from tests.factories import make_kline as _kline
from tests.factories import make_quote as _quote


class ValuationModuleTests(unittest.TestCase):
    def test_missing_valuation_fields_return_low_confidence_summary(self) -> None:
        quote = _quote(pe=None, pb=None, market_cap=None, timestamp="2026-05-13 15:00:00")
        klines = [_kline(date="2026-05-13", close=1300.0, volume=2000.0) for _ in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )

        valuation = build_valuation_analysis(analysis)

        self.assertEqual(valuation.summary, "估值字段不足，暂只能做低证据充分度观察。")
        self.assertIn("PE", valuation.missing_data)
        self.assertIn("PB", valuation.missing_data)
        self.assertIn("总市值", valuation.missing_data)
        self.assertTrue(valuation.evidence)
        self.assertTrue(valuation.watch_points)

    def test_valuation_percentile_score_deltas_keep_risk_direction(self) -> None:
        self.assertEqual([rule.name for rule in VALUATION_PERCENTILE_DELTA_RULES], ["very_high", "high", "very_low", "low"])
        self.assertEqual([rule.name for rule in PEER_PERCENTILE_DELTA_RULES], ["very_high", "high", "very_low", "low"])
        self.assertEqual(valuation_percentile_score_delta(90, 25.0), -10)
        self.assertEqual(valuation_percentile_score_delta(72, 25.0), -5)
        self.assertEqual(valuation_percentile_score_delta(15, 25.0), 6)
        self.assertEqual(valuation_percentile_score_delta(32, 25.0), 3)
        self.assertEqual(valuation_percentile_score_delta(40, -1.0), -10)
        self.assertEqual(valuation_percentile_score_delta(-5, 25.0), 0)
        self.assertEqual(valuation_percentile_score_delta(105, 25.0), 0)
        self.assertEqual(valuation_percentile_score_delta(40, True), -10)
        self.assertEqual(peer_percentile_score_delta(90, 25.0), -8)
        self.assertEqual(peer_percentile_score_delta(72, 25.0), -4)
        self.assertEqual(peer_percentile_score_delta(15, 25.0), 5)
        self.assertEqual(peer_percentile_score_delta(32, 25.0), 2)
        self.assertEqual(peer_percentile_score_delta(-1, 25.0), 0)
        self.assertEqual(peer_percentile_score_delta(101, 25.0), 0)

    def test_summary_distinguishes_missing_enrichment_from_missing_core_fields(self) -> None:
        enrichment_missing = ["PE历史分位", "PB历史分位", "同行PE分位", "同行PB分位"]
        many_enrichment_missing = ["价格历史分位", "PE历史分位", "PB历史分位", "同行PE分位", "同行PB分位", "行业估值分位"]
        core_missing = ["PE", "PB", "同行PE分位"]

        self.assertEqual(valuation_summary(57, enrichment_missing), "估值处在中性区间，重点看业绩和行业背景能否配合。")
        self.assertEqual(valuation_summary(57, many_enrichment_missing), "估值处在中性区间，重点看业绩和行业背景能否配合。")
        self.assertEqual(valuation_summary(57, core_missing), "估值字段不足，暂只能做低证据充分度观察。")
        self.assertEqual(valuation_summary(57, ["PB", "总市值"]), "估值处在中性区间，重点看业绩和行业背景能否配合。")

    def test_valuation_history_percentile_skips_malformed_values(self) -> None:
        quote = _quote(pe=25.0, timestamp="2026-05-13 15:00:00")
        klines = [_kline(date="2026-05-13", close=1300.0, volume=2000.0) for _ in range(80)]
        history = [{"pe": 10 + index, "quote_timestamp": f"2026-04-{index + 1:02d} 15:00:00"} for index in range(30)]
        history.extend(
            [
                {"pe": "bad", "quote_timestamp": "2026-05-01 15:00:00"},
                {"pe": None, "quote_timestamp": "2026-05-02 15:00:00"},
                {"pe": -1, "quote_timestamp": "2026-05-03 15:00:00"},
            ]
        )
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
            quote_history=history,
        )

        self.assertEqual(valuation_percentile_from_history(analysis, "pe"), 53.3)

    def test_valuation_history_percentile_uses_latest_snapshot_per_day(self) -> None:
        quote = _quote(pe=20.0, timestamp="2026-05-13 15:00:00")
        klines = [_kline(date="2026-05-13", close=1300.0, volume=2000.0) for _ in range(80)]
        history = [
            {"pe": 30.0, "quote_timestamp": f"2026-04-{index + 1:02d} 15:00:00"}
            for index in range(29)
        ]
        history.extend(
            [
                {
                    "pe": 40.0,
                    "quote_timestamp": "2026-05-01 15:00:00",
                    "fetched_at": "2026-05-01 15:01:00",
                },
                {
                    "pe": 10.0,
                    "quote_timestamp": "2026-05-01 15:00:00",
                    "fetched_at": "2026-05-01 10:00:00",
                },
            ]
        )
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
            quote_history=history,
        )

        self.assertEqual(valuation_percentile_from_history(analysis, "pe"), 0.0)

    def test_current_valuation_summary_hides_non_finite_raw_values(self) -> None:
        quote = _quote(pe=1.0, pb=1.0, market_cap=100_000_000, timestamp="2026-05-13 15:00:00").model_copy(
            update={"pe": math.inf, "pb": math.nan}
        )
        klines = [_kline(date="2026-05-13", close=1300.0, volume=2000.0) for _ in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )

        valuation = build_valuation_analysis(analysis)
        visible_text = "；".join([*valuation.evidence, *valuation.watch_points])

        self.assertIn("PE 字段异常", visible_text)
        self.assertIn("PB 字段异常", visible_text)
        self.assertNotIn("PE inf", visible_text)
        self.assertNotIn("PB nan", visible_text)

    def test_valuation_anchor_label_uses_ordered_bands_and_valuation_priority(self) -> None:
        self.assertEqual([rule.name for rule in VALUATION_ANCHOR_BANDS], ["high", "elevated", "low", "discount"])

        self.assertEqual(valuation_anchor_label(10, pe_percentile=90), "高位估值锚")
        self.assertEqual(valuation_anchor_label(90, pe_percentile=65), "偏高估值锚")
        self.assertEqual(valuation_anchor_label(90, pe_percentile=20), "低位估值锚")
        self.assertEqual(valuation_anchor_label(90, pe_percentile=35), "偏低估值锚")
        self.assertEqual(valuation_anchor_label(90, pe_percentile=50), "中性估值锚")

    def test_valuation_anchor_label_falls_back_to_price_position_or_pending(self) -> None:
        self.assertEqual(valuation_anchor_label(85), "高位价格位置锚")
        self.assertEqual(valuation_anchor_label(34.9), "偏低价格位置锚")
        self.assertEqual(valuation_anchor_label(-1), "历史锚待确认")
        self.assertEqual(valuation_anchor_label(90, pe_percentile=120), "高位价格位置锚")
        self.assertEqual(valuation_anchor_label(None), "历史锚待确认")


def _valuation_input(*, pe=None, pb=None, market_cap=None, timestamp="2026-05-13 10:00:00"):
    quote = _quote(pe=pe, pb=pb, market_cap=market_cap, timestamp=timestamp)
    klines = [_kline(date="2026-05-13", close=1300.0, volume=2000.0) for _ in range(80)]
    return build_analysis(quote, klines, data_quality=build_data_quality(
        quote, klines, now=datetime(2026, 5, 13, 16, 0, 0),
    ))


@pytest.mark.parametrize("market_cap", [None, 0, -1, 1_000_000_000, 100_000_000_000])
@pytest.mark.parametrize("pe,pb", [(None, None), (0, None), (None, 0), (0, 0)])
def test_valuation_requires_meaningful_ratios_even_with_market_cap(market_cap, pe, pb):
    valuation = build_valuation_analysis(_valuation_input(pe=pe, pb=pb, market_cap=market_cap))
    assert not valuation.score_available
    assert valuation.data_nature == "unavailable" and valuation.level == "不可用"
    assert valuation.score == 52
    assert {"PE", "PB"}.issubset(valuation.missing_data)
    assert "估值字段不足" in valuation.summary


@pytest.mark.parametrize("pe,pb", [(15, None), (None, 2), (-5, None), (None, -2)])
def test_market_cap_context_cannot_change_ratio_valuation_score(pe, pb):
    results = [build_valuation_analysis(_valuation_input(pe=pe, pb=pb, market_cap=cap))
               for cap in [None, 1_000_000_000, 100_000_000_000, 300_000_000_000]]
    assert all(item.score_available for item in results)
    assert len({item.score for item in results}) == 1
    assert "不参与估值评分" in " ".join(results[-1].evidence)
    if pe is not None and pe < 0 or pb is not None and pb < 0:
        assert results[0].score < 52


@pytest.mark.parametrize("pe,pb", [(15, 0), (0, 2), (-5, 0), (0, -2)])
def test_zero_ratio_does_not_penalize_other_meaningful_ratio(pe, pb):
    current = build_valuation_analysis(_valuation_input(pe=pe, pb=pb))
    absent = build_valuation_analysis(_valuation_input(pe=pe or None, pb=pb or None))
    assert current.score == absent.score and current.score_available
    assert "按缺失处理" in " ".join(current.evidence)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), True])
def test_dirty_ratios_cannot_become_valuation_evidence(value):
    analysis = _valuation_input(market_cap=100_000_000_000)
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"pe": value, "pb": value})})
    valuation = build_valuation_analysis(analysis)
    assert not valuation.score_available
    assert {"PE", "PB"}.issubset(valuation.missing_data)
    assert "估值字段不足" in valuation.summary


def _history_rows(*, first_day=date(2026, 3, 1), pe=100, pb=10):
    return [{"quote_timestamp": f"{first_day + timedelta(days=index)} 15:00:00", "pe": pe, "pb": pb}
            for index in range(30)]


def test_future_valuation_history_cannot_improve_old_quote_score():
    analysis = _valuation_input(pe=20, pb=2)
    baseline = build_valuation_analysis(analysis)
    future = build_valuation_analysis(analysis.model_copy(update={"quote_history": _history_rows(first_day=date(2026, 6, 1))}))
    assert future.pe_percentile is None and future.pb_percentile is None
    assert future.score == baseline.score
    admitted = build_valuation_analysis(analysis.model_copy(update={"quote_history": _history_rows()}))
    assert admitted.pe_percentile == 0 and admitted.pb_percentile == 0
    assert admitted.score == baseline.score + 10


@pytest.mark.parametrize("last_row", [
    {"quote_timestamp": "2026-05-13 10:00:01"},
    {"quote_timestamp": "2026-05-13T02:00:01Z"},
    {"quote_timestamp": "2026-05-13"},
    {"quote_timestamp": "bad"},
    {"trade_date": "2026-04-30"},
    {"quote_timestamp": "2026-04-30 15:00:00", "fetched_at": "2026-05-13T02:00:01Z"},
    {"quote_timestamp": "2026-04-30 15:00:00", "fetched_at": "bad"},
    {"quote_timestamp": "2026-04-30 15:00:00", "fetched_at": ""},
])
def test_history_cutoff_excludes_unverifiable_or_late_observation(last_row):
    analysis = _valuation_input(pe=20, pb=2)
    rows = _history_rows()[:29] + [{"pe": 100, "pb": 10, **last_row}]
    analysis = analysis.model_copy(update={"quote_history": rows})
    assert valuation_percentile_from_history(analysis, "pe") is None


def test_history_cutoff_compares_instants_and_filters_before_daily_selection():
    rows = _history_rows()[:29] + [
        {"quote_timestamp": "2026-05-13T01:59:00Z", "fetched_at": "2026-05-13T02:00:00Z", "pe": 100},
        {"quote_timestamp": "2026-05-13T02:01:00Z", "pe": 1},
    ]
    analysis = _valuation_input(pe=20).model_copy(update={"quote_history": rows})
    assert valuation_percentile_from_history(analysis, "pe") == 0


@pytest.mark.parametrize("peer_time,expected_count", [
    ("2026-06-01 15:00:00", 0), ("2026-05-13T02:00:01Z", 0),
    ("", 0), ("bad", 0), ("2026-05-13", 0),
    ("2026-05-13T02:00:00Z", 15), ("2026-05-13T03:00:00+02:00", 15),
])
def test_peer_percentile_and_count_require_verified_time_at_quote_cutoff(peer_time, expected_count):
    analysis = _valuation_input(pe=20, pb=2)
    peers = [analysis.quote.model_copy(update={"code": f"{600100 + index:06d}", "pe": 100, "pb": 10, "timestamp": peer_time})
             for index in range(15)]
    analysis = analysis.model_copy(update={"peer_quotes": peers})
    valuation = build_valuation_analysis(analysis)
    assert peer_valuation_sample_count(analysis) == valuation.peer_sample_count == expected_count
    assert peer_valuation_percentile(analysis, "pe") == (0 if expected_count else None)
    assert valuation.peer_pb_percentile == (0 if expected_count else None)


@pytest.mark.parametrize("fetched_at,expected_count", [
    ("2026-05-13T02:00:00Z", 15), ("2026-05-13T02:00:01Z", 0), ("bad", 0), ("", 0),
])
def test_peer_collection_time_is_checked_when_present(fetched_at, expected_count):
    analysis = _valuation_input(pe=20, pb=2)
    peers = [analysis.quote.model_copy(update={
        "code": f"{600100 + index:06d}", "pe": 100, "pb": 10, "fetched_at": fetched_at,
    }) for index in range(15)]
    analysis = analysis.model_copy(update={"peer_quotes": peers})
    assert peer_valuation_sample_count(analysis) == expected_count
    assert peer_valuation_percentile(analysis, "pe") == (0 if expected_count else None)


@pytest.mark.parametrize("timestamp", ["bad", "", "2026-05-13"])
def test_unverifiable_target_quote_time_excludes_all_valuation_samples(timestamp):
    analysis = _valuation_input(pe=20, pb=2, timestamp=timestamp)
    peers = [_quote(pe=100, pb=10) for _ in range(15)]
    analysis = analysis.model_copy(update={"quote_history": _history_rows(), "peer_quotes": peers})
    assert valuation_percentile_from_history(analysis, "pe") is None
    assert peer_valuation_percentile(analysis, "pe") is None
    assert peer_valuation_sample_count(analysis) == 0


if __name__ == "__main__":
    unittest.main()
