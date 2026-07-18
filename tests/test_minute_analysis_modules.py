from __future__ import annotations

from app.models.schemas import MinuteAnalysisReport, MinuteKline, MinuteSupportResistance
from app.services.datahub_cache import _normalize_minute_interval
from app.services.minute_analysis import (
    MINUTE_WARNING_RULES,
    MOMENTUM_RULES,
    build_unavailable_minute_analysis_report,
    _compact_unavailable_reason,
    _momentum_label,
    _minute_levels,
    _minute_trend_label,
    _minute_t_plan,
    _volume_pulse,
    _t_plan_zones,
    build_minute_analysis_report,
)


def test_datahub_supports_documented_minute_intervals() -> None:
    supported = ["1m", "5m", "15m", "30m", "60m"]

    assert [_normalize_minute_interval(interval) for interval in supported] == supported


def test_minute_t_plan_defensive_rule_wins_on_volume_selloff() -> None:
    plan = _minute_t_plan(
        _rows(),
        100,
        [_level("近端支撑", 99.5)],
        [_level("近端压力", 101.0)],
        "震荡偏强",
        "放量回落",
        1.4,
    )

    assert plan.suitability == "不适合主动做T"
    assert plan.style == "防守型"
    assert plan.confidence == 48
    assert "不适合主动做T" in plan.summary


def test_minute_t_plan_uses_range_style_for_oscillation() -> None:
    plan = _minute_t_plan(
        _rows(),
        100,
        [_level("近端支撑", 99.5)],
        [_level("近端压力", 101.0)],
        "震荡偏强",
        "量能平稳",
        1.4,
    )

    assert plan.suitability == "仅底仓可做T"
    assert plan.style == "区间型"
    assert plan.low_zone == "99.35-99.50"
    assert plan.high_zone == "101.00-101.15"


def test_minute_t_plan_uses_trend_rolling_style_for_non_oscillation() -> None:
    plan = _minute_t_plan(
        _rows(),
        100,
        [_level("近端支撑", 99.5)],
        [_level("近端压力", 101.0)],
        "盘中偏强",
        "量能平稳",
        1.4,
    )

    assert plan.suitability == "仅底仓可做T"
    assert plan.style == "趋势滚动型"


def test_minute_t_plan_waits_when_range_is_too_narrow() -> None:
    plan = _minute_t_plan(
        _rows(),
        100,
        [_level("近端支撑", 99.8)],
        [_level("近端压力", 100.3)],
        "震荡偏强",
        "量能平稳",
        0.7,
    )

    assert plan.suitability == "等待更大区间"
    assert plan.style == "窄幅等待型"
    assert "区间宽度约 0.50%" in plan.summary


def test_minute_warning_rule_order_is_explicit() -> None:
    assert [rule.message for rule in MINUTE_WARNING_RULES] == [
        "当前分钟K线来自缓存或兜底结果，做T区间需要降权。",
        "当前不适合主动做T，避免为了交易而交易。",
        "分钟量能显示放量回落，先防守再考虑高抛低吸。",
        "盘中振幅偏窄，做T空间可能不足。",
    ]


def test_momentum_rule_order_is_explicit() -> None:
    assert [rule.label for rule in MOMENTUM_RULES] == [
        "短线加速",
        "短线走弱",
        "温和转强",
        "温和转弱",
    ]


def test_unavailable_report_defaults_blank_interval_to_5m() -> None:
    report = build_unavailable_minute_analysis_report("600519.SH", interval="", reason="")

    assert report.interval == "5m"
    assert report.klines == []
    assert report.availability == "unavailable"
    assert report.reason_code == "provider_failure"
    assert report.t_plan.style == "数据不可用"
    assert report.t_plan.low_zone == "不可用"
    assert report.t_plan.high_zone == "不可用"
    assert report.t_plan.confidence == 0


def test_t_plan_zones_fall_back_to_recent_intraday_extremes() -> None:
    rows = _rows(lows=[100, 99.8, 99.7, 99.6, 99.4, 99.2, 99.1, 98.8], highs=[100.3, 100.4, 100.6, 100.7, 100.8, 101.1, 101.0, 100.9])

    zones = _t_plan_zones(rows, 100, [], [])

    assert zones.support == 98.8
    assert zones.resistance == 101.1
    assert zones.low_zone == "98.65-98.80"
    assert zones.high_zone == "101.10-101.25"
    assert round(zones.width_pct, 2) == 2.3


def test_t_plan_zones_wait_when_support_and_resistance_are_inverted() -> None:
    rows = _rows()

    zones = _t_plan_zones(rows, 100, [_level("近端支撑", 101)], [_level("近端压力", 99)])
    plan = _minute_t_plan(rows, 100, [_level("近端支撑", 101)], [_level("近端压力", 99)], "震荡偏强", "量能平稳", 1.4)

    assert zones.low_zone == "待确认"
    assert zones.high_zone == "待确认"
    assert zones.width_pct == 0
    assert plan.suitability == "等待更大区间"
    assert plan.confidence <= 40
    assert "区间待确认" in plan.summary


def test_minute_t_plan_is_conservative_without_price_or_sample() -> None:
    plan = _minute_t_plan([], 0, [], [], "盘中震荡", "量能待确认", 0)

    assert plan.low_zone == "不可用"
    assert plan.high_zone == "不可用"
    assert plan.suitability == "暂停做T判断"
    assert plan.style == "数据不可用"
    assert plan.confidence == 0
    assert "暂不能形成做T参考" in plan.summary


def test_unavailable_reason_rules_keep_specific_network_priority() -> None:
    assert _compact_unavailable_reason("Max retries exceeded: ProxyError('Unable to connect to proxy')") == "网络代理连接失败"
    assert _compact_unavailable_reason("akshare: AKShare 依赖不可用：numpy.core.multiarray failed to import") == "AKShare 依赖环境异常"
    assert _compact_unavailable_reason("akshare: 最近失败，短暂冷却中") == "数据源短暂冷却中"
    assert _compact_unavailable_reason("ReadTimeout: request timed out after 5s") == "行情接口超时"
    assert _compact_unavailable_reason("RemoteDisconnected: remote end closed connection without response") == "行情接口远端断开"
    assert _compact_unavailable_reason("") == "数据源连接失败"


def test_minute_trend_label_uses_ordered_trend_rules() -> None:
    assert _minute_trend_label(102, 101, 100, 100.5) == "盘中偏强"
    assert _minute_trend_label(98, 99, 100, 99.5) == "盘中转弱"
    assert _minute_trend_label(101, 100.5, 100, 101) == "震荡偏强"
    assert _minute_trend_label(99, 99.5, 100, 99) == "震荡偏弱"
    assert _minute_trend_label(100, 99, 99.5, 98.5) == "盘中震荡"


def test_volume_pulse_requires_recent_volume_and_labels_direction() -> None:
    assert _volume_pulse(_rows(closes=[100, 100.1, 100.2, 100.1, 100.2, 100.3, 100.8, 101.1], volumes=[100] * 5 + [250, 260, 270])) == "放量上攻"
    assert _volume_pulse(_rows(closes=[101, 100.9, 100.8, 100.7, 100.6, 100.2, 99.8, 99.5], volumes=[100] * 5 + [250, 260, 270])) == "放量回落"
    assert _volume_pulse(_rows(volumes=[100] * 5 + [40, 45, 42])) == "明显缩量"
    assert _volume_pulse(_rows(volumes=[100] * 5 + [0, 0, 0])) == "量能待确认"
    dirty_rows = _rows(
        closes=[100, 100.1, 100.2, 100.1, 100.2, 100.3, 100.8, 101.1],
        volumes=[100] * 5 + [250, 260, 270],
    )
    dirty_rows[5] = dirty_rows[5].model_copy(update={"volume": float("inf")})
    assert _volume_pulse(dirty_rows) == "量能待确认"


def test_momentum_label_uses_ordered_thresholds() -> None:
    assert _momentum_label(_rows(closes=[100, 100, 100, 101])) == "短线加速"
    assert _momentum_label(_rows(closes=[101, 101, 101, 100])) == "短线走弱"
    assert _momentum_label(_rows(closes=[100, 100, 100, 100.3])) == "温和转强"
    assert _momentum_label(_rows(closes=[100, 100, 100, 99.7])) == "温和转弱"
    assert _momentum_label(_rows(closes=[100, 100, 100, 100.1])) == "动量平稳"


def test_minute_levels_keep_near_levels_closer_than_defensive_levels() -> None:
    rows = _rows(
        lows=[96.8, 97.2, 97.6, 98.1, 98.5, 98.8, 99.1, 99.4],
        highs=[100.2, 100.5, 100.8, 101.1, 101.4, 101.8, 102.2, 102.6],
    )

    supports = _minute_levels(rows, 100, "support")
    resistances = _minute_levels(rows, 100, "resistance")

    assert [level.label for level in supports] == ["近端支撑", "防守支撑"]
    assert supports[0].price > supports[1].price
    assert [level.label for level in resistances] == ["近端压力", "强压力"]
    assert resistances[0].price < resistances[1].price


def test_minute_levels_ignore_nonfinite_or_nonpositive_price_candidates() -> None:
    rows = _rows(
        lows=[0, 0, 0, 0, 0, 99.1, 99.2, 99.3],
        highs=[100.4, 100.4, 100.4, 100.4, 100.4, 100.5, 100.6, 100.7],
    )
    rows[:5] = [row.model_copy(update={"high": float("inf")}) for row in rows[:5]]

    supports = _minute_levels(rows, 100, "support")
    resistances = _minute_levels(rows, 100, "resistance")

    assert supports
    assert resistances
    assert all(90 < level.price < 100 for level in supports)
    assert all(100 < level.price < 110 for level in resistances)


def test_minute_report_filters_invalid_rows_before_short_sample_gate() -> None:
    valid_rows = _rows()[:7]
    invalid_latest = valid_rows[-1].model_copy(update={"close": float("inf"), "high": float("inf")})

    report = build_minute_analysis_report("600519.SH", valid_rows + [invalid_latest])

    assert report.sample_count == len(valid_rows)
    assert report.klines == valid_rows
    assert report.latest_price == valid_rows[-1].close
    assert report.availability == "unavailable"
    assert report.reason_code == "insufficient_valid_samples"
    assert "过滤无效数据后" in report.summary
    assert report.supports == []
    assert report.resistances == []
    assert report.t_plan.low_zone == "不可用"
    assert report.t_plan.high_zone == "不可用"


def test_minute_report_filters_inconsistent_minute_rows_before_analysis() -> None:
    valid_rows = _rows()
    invalid_rows = [
        MinuteKline(
            timestamp="2026-05-15 09:30:00",
            open=100,
            close=101,
            high=100.5,
            low=99.8,
            volume=1000,
            amount=10_000_000,
            interval="5m",
            source="测试分钟线",
        )
    ]

    report = build_minute_analysis_report("600519.SH", invalid_rows + valid_rows, interval="5M")

    assert report.interval == "5m"
    assert report.sample_count == len(valid_rows)
    assert report.klines == valid_rows
    assert report.latest_price == valid_rows[-1].close


def test_minute_report_uses_shared_minute_kline_sanity_filter() -> None:
    valid_rows = _rows()
    invalid_rows = [
        valid_rows[0].model_copy(update={"high": float("inf")}),
        valid_rows[1].model_copy(update={"volume": -1}),
        valid_rows[2].model_copy(update={"amount": -1}),
        valid_rows[3].model_copy(update={"volume": float("inf")}),
        valid_rows[4].model_copy(update={"amount": float("inf")}),
        valid_rows[5].model_copy(update={"turnover_rate": -0.01}),
        valid_rows[6].model_copy(update={"turnover_rate": float("nan")}),
    ]

    report = build_minute_analysis_report("600519.SH", invalid_rows + valid_rows)

    assert report.sample_count == len(valid_rows)
    assert report.klines == valid_rows
    assert report.latest_price == valid_rows[-1].close


def test_minute_report_returns_sorted_deduped_analysis_rows_with_provenance() -> None:
    rows = _rows()[:9]
    revised = rows[3].model_copy(
        update={
            "source": "修订分钟线",
            "from_cache": True,
            "fallback_used": True,
        }
    )
    invalid = rows[4].model_copy(update={"low": rows[4].high + 1})

    report = build_minute_analysis_report("600519.SH", [*reversed(rows), revised, invalid])

    assert report.sample_count == len(report.klines) == 9
    assert [row.timestamp for row in report.klines] == sorted({row.timestamp for row in rows})
    assert report.latest_price == rows[-1].close
    assert report.availability == "degraded"
    assert report.klines[3].source == "修订分钟线"
    assert report.klines[3].interval == "5m"
    assert report.klines[3].from_cache is True
    assert report.klines[3].fallback_used is True
    assert invalid not in report.klines


def test_minute_report_filters_blank_and_unparseable_timestamps() -> None:
    valid_rows = _rows()[:7]
    invalid_timestamps = ["", "   ", "not-a-timestamp"]
    invalid_rows = [
        valid_rows[index].model_copy(update={"timestamp": timestamp})
        for index, timestamp in enumerate(invalid_timestamps)
    ]

    report = build_minute_analysis_report("600519.SH", valid_rows + invalid_rows)

    assert report.sample_count == len(report.klines) == 7
    assert report.klines == valid_rows
    assert report.availability == "unavailable"
    assert report.reason_code == "insufficient_valid_samples"


def test_minute_report_sorts_supported_timestamp_formats_by_actual_time() -> None:
    rows = _rows()[:8]
    timestamp_values = [
        "2026-05-15T01:25:00Z",
        "2026/05/15 09:30",
        "202605150935",
        "2026-05-15 09:40:00",
        "2026-05-15T09:45:00",
        "2026-05-15T01:50:00Z",
        "2026-05-15T09:55:00+0800",
        "20260515100000",
    ]
    mixed_rows = [row.model_copy(update={"timestamp": timestamp}) for row, timestamp in zip(reversed(rows), reversed(timestamp_values))]

    report = build_minute_analysis_report("600519.SH", mixed_rows)

    assert report.sample_count == len(report.klines) == 8
    assert [row.timestamp for row in report.klines] == timestamp_values
    assert report.reason_code == "complete"


def test_minute_report_does_not_merge_short_samples_across_trading_dates() -> None:
    today_rows = _rows()[:7]
    yesterday_rows = [
        row.model_copy(update={"timestamp": row.timestamp.replace("2026-05-15", "2026-05-14")})
        for row in _rows()[:7]
    ]

    report = build_minute_analysis_report("600519.SH", [*yesterday_rows, *today_rows])

    assert report.availability == "unavailable"
    assert report.reason_code == "insufficient_samples"
    assert report.sample_count == len(report.klines) == 7
    assert report.klines == today_rows
    assert "最新交易日" in report.availability_reason


def test_minute_report_uses_only_latest_date_for_all_intraday_analysis() -> None:
    yesterday_rows = [
        row.model_copy(
            update={
                "timestamp": row.timestamp.replace("2026-05-15", "2026-05-14"),
                "open": 10 + index,
                "close": 10.2 + index,
                "high": 10.5 + index,
                "low": 9.5 + index,
                "volume": 10_000 + index * 1_000,
            }
        )
        for index, row in enumerate(_rows()[:8])
    ]
    today_rows = [
        row.model_copy(
            update={
                "open": 100 + index,
                "close": 100.2 + index,
                "high": 100.5 + index,
                "low": 99.5 + index,
                "volume": 1_000 + index * 100,
            }
        )
        for index, row in enumerate(_rows()[:8])
    ]

    report = build_minute_analysis_report("600519.SH", [*reversed(today_rows), *reversed(yesterday_rows)])

    assert report.availability == "ok"
    assert report.sample_count == len(report.klines) == 8
    assert report.klines == today_rows
    assert report.intraday_change_pct == 7.2
    assert report.intraday_range_pct == 7.46
    assert all(level.price >= min(row.low for row in today_rows) for level in [*report.supports, *report.resistances])


def test_minute_report_assigns_utc_rows_by_shanghai_local_date() -> None:
    rows = _rows()[:9]
    timestamp_values = ["2026-05-14T15:59:00Z", *[f"2026-05-14T16:0{index}:00Z" for index in range(8)]]
    utc_rows = [row.model_copy(update={"timestamp": timestamp}) for row, timestamp in zip(rows, timestamp_values)]

    report = build_minute_analysis_report("600519.SH", list(reversed(utc_rows)))

    assert report.sample_count == len(report.klines) == 8
    assert report.klines == utc_rows[1:]
    assert report.klines[0].timestamp == "2026-05-14T16:00:00Z"
    assert report.reason_code == "complete"


def test_minute_report_dedupes_equivalent_timezones_and_keeps_last_valid_revision() -> None:
    rows = _rows()[:8]
    original = rows[3].model_copy(update={"timestamp": "2026/05/15 10:03:00", "source": "原始分钟线"})
    revision = rows[3].model_copy(update={"timestamp": "2026-05-15T02:03:00Z", "source": "有效修订"})
    dirty_revision = rows[3].model_copy(
        update={
            "timestamp": "20260515100300",
            "source": "无效修订",
            "low": rows[3].high + 1,
        }
    )
    input_rows = [row for index, row in enumerate(rows) if index != 3]

    report = build_minute_analysis_report("600519.SH", [*input_rows, original, revision, dirty_revision])

    assert report.sample_count == len(report.klines) == 8
    matching_rows = [row for row in report.klines if row.source in {"原始分钟线", "有效修订", "无效修订"}]
    assert len(matching_rows) == 1
    assert matching_rows[0] is revision
    assert matching_rows[0].timestamp == "2026-05-15T02:03:00Z"
    assert report.reason_code == "complete"


def test_minute_report_ignores_dirty_future_rows_when_selecting_latest_date() -> None:
    latest_valid_rows = _rows()[:8]
    dirty_future_ohlc = latest_valid_rows[-1].model_copy(
        update={
            "timestamp": "2026-05-16 09:30:00",
            "low": latest_valid_rows[-1].high + 1,
        }
    )
    dirty_future_timestamp = latest_valid_rows[-1].model_copy(update={"timestamp": "2026-05-17 25:00:00"})

    report = build_minute_analysis_report(
        "600519.SH",
        [dirty_future_timestamp, *reversed(latest_valid_rows), dirty_future_ohlc],
    )

    assert report.sample_count == len(report.klines) == 8
    assert report.klines == latest_valid_rows
    assert report.updated_at == latest_valid_rows[-1].timestamp
    assert report.reason_code == "complete"


def test_minute_report_json_roundtrip_preserves_kline_contract() -> None:
    report = build_minute_analysis_report("600519.SH", _rows())

    restored = MinuteAnalysisReport.model_validate_json(report.model_dump_json())

    assert restored == report
    assert restored.sample_count == len(restored.klines)
    assert restored.klines[0].timestamp == report.klines[0].timestamp


def test_minute_report_marks_empty_rows_unavailable() -> None:
    report = build_minute_analysis_report("600519.SH", [])

    assert report.availability == "unavailable"
    assert report.reason_code == "empty_data"
    assert report.sample_count == 0
    assert report.klines == []
    assert "返回为空" in report.availability_reason


def test_minute_report_requires_eight_valid_price_samples() -> None:
    seven_sample_report = build_minute_analysis_report("600519.SH", _rows()[:7])
    eight_sample_report = build_minute_analysis_report("600519.SH", _rows()[:8])

    assert seven_sample_report.availability == "unavailable"
    assert seven_sample_report.reason_code == "insufficient_samples"
    assert seven_sample_report.klines == _rows()[:7]
    assert seven_sample_report.t_plan.low_zone == "不可用"
    assert eight_sample_report.availability == "ok"
    assert eight_sample_report.reason_code == "complete"
    assert eight_sample_report.sample_count == 8
    assert eight_sample_report.klines == _rows()[:8]


def test_minute_report_marks_cache_or_fallback_data_degraded() -> None:
    rows = [row.model_copy(update={"from_cache": True, "fallback_used": True}) for row in _rows()]

    report = build_minute_analysis_report("600519.SH", rows)

    assert report.availability == "degraded"
    assert report.reason_code == "cache_or_fallback"
    assert report.supports
    assert report.resistances
    assert report.t_plan.low_zone != "不可用"
    assert report.t_plan.high_zone != "不可用"
    assert "仍可参考" in report.availability_reason
    assert report.missing_data == ["实时分钟K线（当前使用缓存或兜底数据）"]


def test_minute_report_marks_missing_key_volume_input_degraded() -> None:
    rows = _rows(volumes=[100] * 13 + [0, 0, 0])

    report = build_minute_analysis_report("600519.SH", rows)

    assert report.availability == "degraded"
    assert report.reason_code == "missing_volume"
    assert report.volume_pulse == "量能待确认"
    assert report.missing_data == ["有效分钟成交量"]
    assert "趋势、支撑压力和价格区间仍可用" in report.availability_reason
    assert "量能及量价结论不可用" in report.availability_reason


def test_minute_report_keeps_business_risk_available() -> None:
    closes = [100.6 - index * 0.08 for index in range(16)]
    rows = _rows(
        lows=[99] * 16,
        highs=[101] * 16,
        closes=closes,
        volumes=[100] * 13 + [300, 300, 300],
    )

    report = build_minute_analysis_report("600519.SH", rows)

    assert report.trend_label == "盘中转弱"
    assert report.t_plan.suitability == "不适合主动做T"
    assert report.availability == "ok"
    assert report.reason_code == "complete"
    assert report.missing_data == []


def test_minute_report_legacy_constructor_defaults_to_unavailable() -> None:
    current = build_minute_analysis_report("600519.SH", _rows())
    legacy_payload = current.model_dump(exclude={"availability", "availability_reason", "reason_code", "klines"})

    report = MinuteAnalysisReport.model_validate(legacy_payload)

    assert report.availability == "unavailable"
    assert report.reason_code == "legacy_status_missing"
    assert report.klines == []
    assert "按不可用处理" in report.availability_reason
    assert report.supports == []
    assert report.resistances == []
    assert report.t_plan.low_zone == "不可用"
    assert report.t_plan.high_zone == "不可用"
    assert report.t_plan.confidence == 0


def _rows(
    *,
    lows: list[float] | None = None,
    highs: list[float] | None = None,
    closes: list[float] | None = None,
    volumes: list[float] | None = None,
) -> list[MinuteKline]:
    lows = lows or [99.6, 99.7, 99.8, 99.9, 99.95, 99.85, 99.75, 99.65] * 2
    highs = highs or [100.4, 100.5, 100.6, 100.7, 100.8, 100.9, 101.0, 101.1] * 2
    closes = closes or [100] * len(lows)
    volumes = volumes or [1000 + index * 20 for index in range(len(lows))]
    return [
        MinuteKline(
            timestamp=f"2026-05-15 10:{index:02d}:00",
            open=100,
            close=close,
            high=high,
            low=low,
            volume=volume,
            amount=10_000_000,
            interval="5m",
            source="测试分钟线",
        )
        for index, (low, high, close, volume) in enumerate(zip(lows, highs, closes, volumes))
    ]


def _level(label: str, price: float) -> MinuteSupportResistance:
    return MinuteSupportResistance(
        label=label,
        price=price,
        strength=60,
        reason="测试价位",
    )
