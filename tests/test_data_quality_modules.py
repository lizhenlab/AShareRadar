from __future__ import annotations

import math
import unittest
from datetime import datetime

from app.services.data_quality import build_data_quality
from app.services.data_quality_components import (
    QUOTE_FIELD_RULES,
    DataQualityScoreState,
    apply_quote_field_quality,
    data_quality_level,
)
from app.models.schemas import KlineQuality
from app.services.data_quality_kline import KLINE_LEVEL_RULES, KLINE_PENALTY_RULES, assess_kline_quality, kline_quality_penalty
from app.services.data_quality_time import quote_delay_seconds, quote_freshness_penalty
from tests.factories import make_kline
from tests.factories import make_quote as _quote


class DataQualityModuleTests(unittest.TestCase):
    def test_quote_only_quality_does_not_penalize_missing_klines(self) -> None:
        quote = _quote(timestamp="2026-05-13 15:00:00")

        quality = build_data_quality(quote, [], require_kline=False, now=datetime(2026, 5, 13, 16, 0, 0))

        self.assertIsNone(quality.kline_quality)
        self.assertNotIn("K线缺失", quality.anomalies)
        self.assertNotIn("K线数量不足", quality.anomalies)
        self.assertEqual(quality.kline_count, 0)

    def test_quote_field_anomalies_are_collected_together(self) -> None:
        quote = _quote(price=120.0, prev_close=100.0, high=110.0, low=90.0, change_pct=1.0, timestamp="2026-05-13 15:00:00")

        quality = build_data_quality(quote, [], require_kline=False, now=datetime(2026, 5, 13, 16, 0, 0))

        self.assertIn("现价高于最高价", quality.anomalies)
        self.assertIn("涨跌幅口径偏差", quality.anomalies)
        self.assertLess(quality.score, 70)

    def test_quote_field_quality_rules_keep_order_and_penalties(self) -> None:
        self.assertEqual(
            [rule.name for rule in QUOTE_FIELD_RULES],
            [
                "price_invalid",
                "prev_close_missing",
                "high_low_invalid",
                "high_low_inverted",
                "price_above_high",
                "price_below_low",
                "change_pct_invalid",
                "change_pct_mismatch",
            ],
        )
        state = DataQualityScoreState()

        apply_quote_field_quality(
            state,
            _quote(price=120.0, prev_close=100.0, high=110.0, low=90.0, change_pct=1.0),
        )

        self.assertEqual(state.score, 68)
        self.assertEqual(state.anomalies, ["现价高于最高价", "涨跌幅口径偏差"])

    def test_missing_prev_close_does_not_emit_change_pct_mismatch(self) -> None:
        state = DataQualityScoreState()

        apply_quote_field_quality(state, _quote(price=100.0, prev_close=0.0, high=101.0, low=99.0, change_pct=99.0))

        self.assertEqual(state.score, 90)
        self.assertEqual(state.anomalies, ["昨收价缺失"])

    def test_invalid_quote_price_suppresses_derived_price_anomalies(self) -> None:
        state = DataQualityScoreState()

        apply_quote_field_quality(
            state,
            _quote(price=-1.0, prev_close=100.0, high=101.0, low=99.0, change_pct=-101.0),
        )

        self.assertEqual(state.score, 65)
        self.assertEqual(state.anomalies, ["报价价格异常"])

    def test_non_finite_quote_fields_are_flagged_without_derived_mismatch(self) -> None:
        state = DataQualityScoreState()
        quote = _quote(price=100.0, prev_close=100.0, high=101.0, low=99.0, change_pct=0.0).model_copy(
            update={"price": math.nan, "prev_close": math.inf, "change": math.nan, "change_pct": math.nan}
        )

        apply_quote_field_quality(state, quote)

        self.assertEqual(state.anomalies, ["报价价格异常", "昨收价缺失", "涨跌幅异常"])
        self.assertNotIn("涨跌幅口径偏差", state.anomalies)

    def test_invalid_high_low_fields_block_range_checks(self) -> None:
        state = DataQualityScoreState()

        apply_quote_field_quality(state, _quote(price=120.0, prev_close=100.0, high=0.0, low=99.0, change_pct=20.0))

        self.assertEqual(state.anomalies, ["高低价异常"])

    def test_inverted_high_low_suppresses_price_boundary_rules(self) -> None:
        state = DataQualityScoreState()

        apply_quote_field_quality(state, _quote(price=120.0, prev_close=100.0, high=110.0, low=115.0, change_pct=20.0))

        self.assertEqual(state.score, 70)
        self.assertEqual(state.anomalies, ["高低价倒挂"])

    def test_quote_boundary_values_are_not_penalized(self) -> None:
        state = DataQualityScoreState()

        apply_quote_field_quality(state, _quote(price=101.0, prev_close=100.0, high=100.0, low=80.0, change_pct=1.3))

        self.assertEqual(state.score, 100)
        self.assertEqual(state.anomalies, [])

    def test_consistency_notes_become_anomalies_when_sources_disagree(self) -> None:
        quote = _quote(timestamp="2026-05-13 15:00:00")

        quality = build_data_quality(
            quote,
            [],
            consistency_level="存在差异",
            consistency_notes=["多源最大价格差异 2.00%。"],
            consistency_penalty=9,
            require_kline=False,
            now=datetime(2026, 5, 13, 16, 0, 0),
        )

        self.assertIn("多源最大价格差异 2.00%。", quality.notes)
        self.assertIn("多源最大价格差异 2.00%。", quality.anomalies)
        self.assertLessEqual(quality.score, 91)

    def test_consistency_anomaly_level_without_notes_gets_label(self) -> None:
        quality = build_data_quality(
            _quote(timestamp="2026-05-13 15:00:00"),
            [],
            consistency_level="字段异常",
            consistency_notes=[],
            consistency_penalty=12,
            require_kline=False,
            now=datetime(2026, 5, 13, 16, 0, 0),
        )

        self.assertIn("多源字段异常", quality.anomalies)
        self.assertLessEqual(quality.score, 88)

    def test_negative_consistency_penalty_does_not_raise_score(self) -> None:
        quality = build_data_quality(
            _quote(source="腾讯行情·缓存", timestamp="2026-05-13 15:00:00"),
            [],
            consistency_penalty=-50,
            require_kline=False,
            now=datetime(2026, 5, 13, 16, 0, 0),
        )

        self.assertEqual(quality.score, 92)

    def test_quote_source_cache_and_fallback_notes_are_precise(self) -> None:
        short_cache = build_data_quality(
            _quote(source="腾讯行情·短时缓存", timestamp="2026-05-13 15:00:00"),
            [],
            require_kline=False,
            now=datetime(2026, 5, 13, 16, 0, 0),
        )
        fallback_cache = build_data_quality(
            _quote(source="腾讯行情·兜底缓存", timestamp="2026-05-13 15:00:00"),
            [],
            require_kline=False,
            now=datetime(2026, 5, 13, 16, 0, 0),
        )

        self.assertIn("当前报价来自短时缓存，需结合报价时间确认新鲜度。", short_cache.notes)
        self.assertIn("当前报价来自兜底缓存，说明实时行情源本轮不可用。", fallback_cache.notes)
        self.assertIn("报价兜底缓存", fallback_cache.anomalies)
        self.assertLess(fallback_cache.score, short_cache.score)

    def test_quote_cache_flags_are_used_when_source_text_is_plain(self) -> None:
        short_cache = build_data_quality(
            _quote(source="腾讯行情", timestamp="2026-05-13 15:00:00").model_copy(update={"from_cache": True}),
            [],
            require_kline=False,
            now=datetime(2026, 5, 13, 16, 0, 0),
        )
        fallback_cache = build_data_quality(
            _quote(source="腾讯行情", timestamp="2026-05-13 15:00:00").model_copy(
                update={"from_cache": True, "fallback_used": True}
            ),
            [],
            require_kline=False,
            now=datetime(2026, 5, 13, 16, 0, 0),
        )

        self.assertIn("当前报价来自缓存，已结合报价时间评估新鲜度。", short_cache.notes)
        self.assertIn("当前报价来自兜底缓存，说明实时行情源本轮不可用。", fallback_cache.notes)
        self.assertIn("报价兜底缓存", fallback_cache.anomalies)

    def test_required_missing_klines_only_reports_missing_not_count_shortage(self) -> None:
        quality = build_data_quality(
            _quote(timestamp="2026-05-13 15:00:00"),
            [],
            require_kline=True,
            now=datetime(2026, 5, 13, 16, 0, 0),
        )

        self.assertIn("K线缺失", quality.anomalies)
        self.assertNotIn("K线数量不足", quality.anomalies)
        self.assertIn("缺少K线数据，趋势、买卖点和做T参考都需要降级。", quality.notes)

    def test_quality_score_is_clamped_at_zero(self) -> None:
        quote = _quote(
            source="本地演示数据",
            price=-1.0,
            prev_close=0.0,
            high=0.0,
            low=0.0,
            change_pct=0.0,
            timestamp="bad-time",
        ).model_copy(update={"change_pct": math.nan})
        quality = build_data_quality(
            quote,
            [],
            consistency_level="存在差异",
            consistency_notes=["多源最大价格差异 50.00%。"],
            consistency_penalty=80,
            require_kline=True,
            now=datetime(2026, 5, 13, 16, 0, 0),
        )

        self.assertEqual(quality.score, 0)
        self.assertEqual(quality.level, "较弱")

    def test_data_quality_level_boundaries_are_stable(self) -> None:
        self.assertEqual(data_quality_level(85), "优秀")
        self.assertEqual(data_quality_level(70), "良好")
        self.assertEqual(data_quality_level(50), "一般")
        self.assertEqual(data_quality_level(49), "较弱")

    def test_trading_session_penalizes_quote_from_previous_trade_day(self) -> None:
        penalty, notes, anomalies = quote_freshness_penalty("2026-05-12 15:00:00", datetime(2026, 5, 13, 10, 0, 0))

        self.assertEqual(penalty, 24)
        self.assertIn("报价滞后", anomalies)
        self.assertEqual(notes, ["交易时段仍在使用 2026-05-12 的报价，落后当前应参考交易日约 1 个交易日。"])

    def test_same_day_future_quote_time_is_penalized(self) -> None:
        now = datetime(2026, 5, 13, 10, 0, 0)

        penalty, notes, anomalies = quote_freshness_penalty("2026-05-13 10:05:00", now)

        self.assertEqual(penalty, 12)
        self.assertEqual(anomalies, ["报价时间超前"])
        self.assertEqual(notes, ["报价时间 10:05:00 晚于当前检查时间 10:00:00，需核对行情源时间。"])
        self.assertIsNone(quote_delay_seconds("2026-05-13 10:05:00", now=now))

    def test_midday_break_accepts_morning_close_snapshot(self) -> None:
        penalty, notes, anomalies = quote_freshness_penalty("2026-05-13 11:30:00", datetime(2026, 5, 13, 12, 0, 0))

        self.assertEqual(penalty, 0)
        self.assertEqual(anomalies, [])
        self.assertEqual(notes, ["午间休市阶段使用上午最新行情快照。"])

    def test_current_fallback_kline_is_downgraded_to_general(self) -> None:
        quality = assess_kline_quality(
            [make_kline(date="2026-05-13", fallback_used=True)],
            now=datetime(2026, 5, 13, 16, 0, 0),
        )
        penalty, anomalies = kline_quality_penalty(quality)

        self.assertEqual(quality.level, "一般")
        self.assertEqual(quality.days_behind_expected, 0)
        self.assertIn("K线来自兜底缓存，说明实时K线源本轮不可用。", quality.notes)
        self.assertEqual(penalty, 12)
        self.assertEqual(anomalies, ["K线兜底缓存"])

    def test_kline_quality_level_rules_keep_priority(self) -> None:
        self.assertEqual(
            [rule.name for rule in KLINE_LEVEL_RULES],
            ["demo_source", "severely_stale", "stale", "fallback_cache"],
        )

        quality = assess_kline_quality(
            [make_kline(date="2026-05-13", source="demo-source", fallback_used=True)],
            now=datetime(2026, 5, 13, 16, 0, 0),
        )

        self.assertEqual(quality.level, "较弱")

    def test_future_kline_date_is_downgraded(self) -> None:
        quality = assess_kline_quality(
            [make_kline(date="2026-05-14")],
            now=datetime(2026, 5, 13, 16, 0, 0),
        )
        penalty, anomalies = kline_quality_penalty(quality)

        self.assertEqual(quality.level, "较弱")
        self.assertIsNone(quality.days_behind_expected)
        self.assertEqual(penalty, 25)
        self.assertEqual(anomalies, ["K线日期超前"])
        self.assertEqual(quality.notes, ["K线最新日期为 2026-05-14，晚于当前可接受交易日 2026-05-13，需核对数据源日期。"])

    def test_intraday_current_kline_date_is_allowed_before_close(self) -> None:
        quality = assess_kline_quality(
            [make_kline(date="2026-05-13")],
            now=datetime(2026, 5, 13, 12, 0, 0),
        )
        penalty, anomalies = kline_quality_penalty(quality)

        self.assertEqual(quality.level, "良好")
        self.assertEqual(quality.latest_expected_date, "2026-05-12")
        self.assertEqual(quality.latest_allowed_date, "2026-05-13")
        self.assertEqual(penalty, 0)
        self.assertEqual(anomalies, [])
        self.assertEqual(quality.notes, ["K线包含当前交易日盘中数据，收盘前仍需结合实时行情校验。"])

    def test_kline_quality_uses_latest_parsable_date_not_input_tail(self) -> None:
        quality = assess_kline_quality(
            [
                make_kline(date="2026-05-13", source="腾讯行情"),
                make_kline(date="bad-date", source="本地演示数据"),
                make_kline(date="2026-05-10", source="本地演示数据"),
            ],
            now=datetime(2026, 5, 13, 16, 0, 0),
        )
        penalty, anomalies = kline_quality_penalty(quality)

        self.assertEqual(quality.last_date, "2026-05-13")
        self.assertEqual(quality.source, "腾讯行情")
        self.assertEqual(quality.level, "良好")
        self.assertEqual(penalty, 0)
        self.assertEqual(anomalies, [])
        self.assertNotIn("演示K线", quality.notes)

    def test_kline_quality_detects_future_date_even_when_not_tail(self) -> None:
        quality = assess_kline_quality(
            [
                make_kline(date="2026-05-14", source="腾讯行情"),
                make_kline(date="2026-05-13", source="腾讯行情"),
            ],
            now=datetime(2026, 5, 13, 16, 0, 0),
        )
        penalty, anomalies = kline_quality_penalty(quality)

        self.assertEqual(quality.last_date, "2026-05-14")
        self.assertEqual(quality.level, "较弱")
        self.assertEqual(penalty, 25)
        self.assertEqual(anomalies, ["K线日期超前"])

    def test_kline_penalty_rules_are_exclusive_and_terminal(self) -> None:
        self.assertEqual(
            [rule.name for rule in KLINE_PENALTY_RULES],
            [
                "missing",
                "invalid_date",
                "future_date",
                "severely_stale",
                "stale",
                "slightly_stale",
                "fallback_cache",
                "demo_source",
            ],
        )
        self.assertEqual(kline_quality_penalty(_kline_quality(level="缺失", days=6, fallback_used=True, source="demo")), (25, ["K线缺失"]))
        self.assertEqual(kline_quality_penalty(_kline_quality(last_date=None, days=6, fallback_used=True, source="demo")), (25, ["K线日期异常"]))
        self.assertEqual(kline_quality_penalty(_kline_quality(days=5)), (30, ["K线严重滞后"]))
        self.assertEqual(kline_quality_penalty(_kline_quality(days=4)), (18, ["K线滞后"]))
        self.assertEqual(kline_quality_penalty(_kline_quality(days=1)), (8, ["K线轻微滞后"]))
        self.assertEqual(
            kline_quality_penalty(_kline_quality(days=5, fallback_used=True, source="demo-source")),
            (77, ["K线严重滞后", "K线兜底缓存", "演示K线"]),
        )

    def test_demo_kline_source_is_always_weak(self) -> None:
        quality = assess_kline_quality(
            [make_kline(date="2026-05-13", source="本地演示数据")],
            now=datetime(2026, 5, 13, 16, 0, 0),
        )
        penalty, anomalies = kline_quality_penalty(quality)

        self.assertEqual(quality.level, "较弱")
        self.assertIn("K线来源为演示数据，不能作为真实行情依据。", quality.notes)
        self.assertEqual(penalty, 35)
        self.assertEqual(anomalies, ["演示K线"])


def _kline_quality(
    *,
    level: str = "良好",
    last_date: str | None = "2026-05-13",
    days: int | None = 0,
    fallback_used: bool = False,
    source: str | None = "测试K线",
) -> KlineQuality:
    return KlineQuality(
        level=level,
        source=source,
        last_date=last_date,
        latest_expected_date="2026-05-13",
        days_behind_expected=days,
        fallback_used=fallback_used,
    )


if __name__ == "__main__":
    unittest.main()
