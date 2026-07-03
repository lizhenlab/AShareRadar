from __future__ import annotations

import math

from app.models.schemas import ReplayCase
from app.services.research_replay import (
    MIN_REPLAY_KLINES,
    _detect_replay_pattern,
    _replay_case,
    _replay_case_note,
    _replay_outcome,
    _replay_pattern_context,
    _replay_pattern_index_is_valid,
    _replay_pattern_note,
    _replay_success_rate,
    _replay_stats,
    build_replay_analysis,
)
from tests.factories import make_kline, make_quote


def test_replay_analysis_requires_minimum_kline_count() -> None:
    analysis = _analysis_with_klines([make_kline(date=f"2026-05-{index + 1:02d}") for index in range(MIN_REPLAY_KLINES - 1)])

    replay = build_replay_analysis(analysis)

    assert replay.sample_count == 0
    assert replay.window_days == MIN_REPLAY_KLINES - 1
    assert f"至少需要{MIN_REPLAY_KLINES}根日K" in replay.notes[0]


def test_replay_analysis_treats_non_positive_window_as_empty() -> None:
    analysis = _analysis_with_klines([make_kline(date=f"2026-05-{index + 1:02d}") for index in range(MIN_REPLAY_KLINES + 5)])

    replay = build_replay_analysis(analysis, window_days=0)

    assert replay.sample_count == 0
    assert replay.window_days == 0


def test_replay_case_ignores_zero_entry_price() -> None:
    rows = [make_kline(date=f"2026-04-{index + 1:02d}", close=100, high=102, low=98, volume=1000) for index in range(20)]
    rows.append(make_kline(date="2026-05-01", close=0, high=100, low=0, volume=2500))
    rows.extend(make_kline(date=f"2026-05-{index + 2:02d}", close=95, high=96, low=94, volume=1000) for index in range(10))

    assert _replay_case(rows, 20) is None


def test_replay_case_keeps_recent_signal_pending_until_forward_return_matures() -> None:
    rows = [
        make_kline(date=f"2026-04-{index + 1:02d}", close=100 + index * 0.2, high=101 + index * 0.2, low=99, volume=1000)
        for index in range(20)
    ]
    rows.append(make_kline(date="2026-05-01", close=108, high=109, low=104, volume=1800))

    case = _replay_case(rows, 20)

    assert case is not None
    assert case.pattern == "放量突破"
    assert case.forward_5d_return is None
    assert case.outcome == "待确认"


def test_replay_case_keeps_invalid_forward_bar_pending() -> None:
    rows = [
        make_kline(date=f"2026-04-{index + 1:02d}", close=100 + index * 0.2, high=101 + index * 0.2, low=99, volume=1000)
        for index in range(20)
    ]
    rows.append(make_kline(date="2026-05-01", close=108, high=109, low=104, volume=1800))
    for index in range(10):
        day = make_kline(date=f"2026-05-{index + 2:02d}", close=109 + index, high=110 + index, low=108 + index, volume=1100)
        rows.append(day.model_copy(update={"high": math.inf}) if index == 4 else day)

    case = _replay_case(rows, 20)

    assert case is not None
    assert case.forward_3d_return is not None
    assert case.forward_5d_return is None
    assert case.forward_10d_return is not None
    assert case.outcome == "待确认"


def test_replay_success_rate_excludes_pending_cases() -> None:
    cases = [
        ReplayCase(date="2026-05-01", pattern="放量突破", entry_price=10, forward_5d_return=3.0, outcome="有效", note=""),
        ReplayCase(date="2026-05-02", pattern="放量突破", entry_price=10, forward_5d_return=None, outcome="待确认", note=""),
    ]

    assert _replay_success_rate(cases) == 100.0


def test_replay_outcome_treats_non_finite_return_as_pending() -> None:
    assert _replay_outcome(math.nan) == "待确认"
    assert _replay_outcome(math.inf) == "待确认"


def test_replay_stats_and_success_rate_ignore_non_finite_forward_returns() -> None:
    cases = [
        ReplayCase(date="2026-05-01", pattern="放量突破", entry_price=10, forward_5d_return=math.nan, outcome="有效", note=""),
        ReplayCase(date="2026-05-02", pattern="放量突破", entry_price=10, forward_5d_return=math.inf, outcome="有效", note=""),
        ReplayCase(date="2026-05-03", pattern="放量突破", entry_price=10, forward_5d_return=3.0, outcome="有效", note=""),
        ReplayCase(date="2026-05-04", pattern="放量突破", entry_price=10, forward_5d_return=-1.0, outcome="震荡", note=""),
    ]

    stats = _replay_stats(cases)

    assert _replay_success_rate(cases) == 50.0
    assert stats[0].sample_count == 4
    assert stats[0].win_rate == 50.0
    assert stats[0].avg_forward_5d_return == 1.0
    assert "另有 2 次待确认" in stats[0].note


def test_replay_stats_uses_only_valid_forward_returns_for_win_rate() -> None:
    cases = [
        ReplayCase(date="2026-05-01", pattern="放量突破", entry_price=10, forward_5d_return=3.0, outcome="有效", note=""),
        ReplayCase(date="2026-05-02", pattern="放量突破", entry_price=10, forward_5d_return=None, outcome="待确认", note=""),
        ReplayCase(date="2026-05-03", pattern="放量突破", entry_price=10, forward_5d_return=-2.0, outcome="震荡", note=""),
    ]

    stats = _replay_stats(cases)

    assert stats[0].win_rate == 50.0
    assert stats[0].avg_forward_5d_return == 0.5
    assert "待确认" in stats[0].note


def test_replay_analysis_detects_volume_breakout_case() -> None:
    rows = [make_kline(date=f"2026-04-{index + 1:02d}", close=100 + index * 0.2, high=101 + index * 0.2, low=99, volume=1000) for index in range(20)]
    rows.append(make_kline(date="2026-05-01", close=108, high=109, low=104, volume=1800))
    rows.extend(make_kline(date=f"2026-05-{index + 2:02d}", close=109 + index, high=110 + index, low=108 + index, volume=1100) for index in range(12))
    analysis = _analysis_with_klines(rows)

    replay = build_replay_analysis(analysis, window_days=len(rows))

    assert replay.sample_count >= 1
    assert replay.cases[-1].pattern == "放量突破"
    assert replay.cases[-1].outcome == "有效"


def test_replay_pattern_ignores_malformed_lookback_bar() -> None:
    rows = [make_kline(date=f"2026-04-{index + 1:02d}", close=100, high=101, low=99, volume=1000) for index in range(20)]
    rows[5] = make_kline(date="2026-04-06", close=100, high=0, low=99, volume=1000)
    rows.append(make_kline(date="2026-05-01", close=105, high=106, low=104, volume=1800))

    assert _detect_replay_pattern(rows, 20) is None


def test_replay_pattern_context_rejects_non_finite_price_values() -> None:
    rows = [make_kline(date=f"2026-04-{index + 1:02d}", close=100, high=101, low=99, volume=1000) for index in range(20)]
    rows[5] = rows[5].model_copy(update={"close": math.nan})
    rows.append(make_kline(date="2026-05-01", close=105, high=106, low=104, volume=1800))

    assert _replay_pattern_context(rows, 20) is None

    clean_rows = [make_kline(date=f"2026-04-{index + 1:02d}", close=100, high=101, low=99, volume=1000) for index in range(20)]
    clean_rows.append(make_kline(date="2026-05-01", close=105, high=math.inf, low=104, volume=1800))

    assert _replay_pattern_context(clean_rows, 20) is None


def test_replay_pattern_context_builds_window_and_volume_metrics() -> None:
    rows = [
        make_kline(
            date=f"2026-04-{index + 1:02d}",
            close=100 + index * 0.2,
            high=101 + index * 0.4,
            low=96 - index * 0.1,
            volume=1000,
        )
        for index in range(20)
    ]
    rows.append(make_kline(date="2026-05-01", close=110, high=112, low=108, volume=2500))

    context = _replay_pattern_context(rows, 20)

    assert context is not None
    assert context.current is rows[20]
    assert context.previous is rows[19]
    assert context.high_20 == max(item.high for item in rows[:20])
    assert context.low_20 == min(item.low for item in rows[:20])
    assert context.volume_ratio == 2.5


def test_replay_pattern_context_rejects_invalid_boundaries_and_empty_volume_window() -> None:
    rows = [
        make_kline(date=f"2026-04-{index + 1:02d}", close=100, high=101, low=99, volume=0)
        for index in range(20)
    ]
    rows.append(make_kline(date="2026-05-01", close=105, high=106, low=104, volume=1800))

    assert not _replay_pattern_index_is_valid(rows, 19)
    assert not _replay_pattern_index_is_valid(rows, len(rows))
    assert _replay_pattern_context(rows, 20) is None


def test_replay_pattern_context_requires_complete_valid_volume_window() -> None:
    rows = [
        make_kline(date=f"2026-04-{index + 1:02d}", close=100, high=101, low=99, volume=1000)
        for index in range(20)
    ]
    rows[18] = rows[18].model_copy(update={"volume": math.inf})
    rows.append(make_kline(date="2026-05-01", close=105, high=106, low=104, volume=1800))

    assert _replay_pattern_context(rows, 20) is None


def test_support_rebound_must_close_back_near_support() -> None:
    rows = [make_kline(date=f"2026-04-{index + 1:02d}", close=100, high=103, low=100, volume=1000) for index in range(20)]
    rows.append(make_kline(date="2026-05-01", close=96, high=98, low=94, volume=1200))

    assert _detect_replay_pattern(rows, 20) is None


def test_replay_case_note_distinguishes_pending_outcome() -> None:
    assert "暂不纳入稳定性判断" in _replay_case_note("支撑反弹", "待确认")


def test_replay_pattern_note_keeps_rule_priority_explicit() -> None:
    assert "尚未积累完整5日回看" in _replay_pattern_note("放量突破", 2, 80.0, 3.2, evaluated_count=0)
    assert "相对有效" in _replay_pattern_note("放量突破", 5, 65.0, 1.2)
    assert "稳定性不足" in _replay_pattern_note("放量回撤", 5, 40.0, 1.2)
    assert "历史表现中性" in _replay_pattern_note("支撑反弹", 5, 50.0, 0.5)


def test_replay_pattern_note_bounds_completed_count_to_observed_samples() -> None:
    over_count_note = _replay_pattern_note("放量突破", 2, 80.0, 3.2, evaluated_count=10)
    negative_count_note = _replay_pattern_note("放量突破", 2, 80.0, 3.2, evaluated_count=-1)

    assert "样本只有 2 次" in over_count_note
    assert "待确认" not in over_count_note
    assert "尚未积累完整5日回看" in negative_count_note


def test_replay_pattern_note_does_not_promote_non_finite_metrics() -> None:
    note = _replay_pattern_note("放量突破", 5, math.inf, math.nan, evaluated_count=5)

    assert "相对有效" not in note
    assert "稳定性不足" in note


def _analysis_with_klines(klines):
    quote = make_quote()
    from app.services.analysis import build_analysis
    from app.services.data_quality import build_data_quality

    return build_analysis(quote, klines, data_quality=build_data_quality(quote, klines))
