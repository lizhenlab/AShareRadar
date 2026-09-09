from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.analysis import SignalItem
from app.services.analysis import build_analysis
from app.services.analysis_signal_advice import action_advice
from app.services.analysis_signal_quality import gate_signal_items, quality_blocks_active_signals
from app.services.data_quality import build_data_quality
from app.services.research_features import build_feature_snapshot
from app.services.research_t_strategy import build_t_strategy_assistant_report
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_quote
from tests.test_indicator_atr_window import _rows


HARD_ANOMALIES = ('K线严重滞后', 'K线兜底缓存', '演示K线', '演示行情', '报价严重滞后')


def _inputs(*, fallback=False, cached=False):
    rows = [row.model_copy(update={
        'open': 10 + index * .1, 'close': 10 + index * .1,
        'high': 10.1 + index * .1, 'low': 9.9 + index * .1,
        'fallback_used': fallback, 'from_cache': cached,
    }) for index, row in enumerate(_rows([.1] * 80))]
    quote = make_quote(
        price=17.9, prev_close=17.8, high=18, low=17.8,
        change_pct=round(.1 / 17.8 * 100, 2), turnover_rate=3,
        timestamp='2026-09-07 15:15:00',
    ).model_copy(update={'open': 17.8})
    quality = build_data_quality(quote, rows, now=datetime(2026, 9, 7, 15, 16))
    return quote, rows, quality


@pytest.mark.parametrize('anomaly', HARD_ANOMALIES)
@pytest.mark.parametrize('score', [49, 50, 69, 70, 88, 100])
def test_hard_anomaly_precedes_score_band_for_points_and_action(anomaly, score):
    quote, _rows_list, quality = _inputs()
    quality = quality.model_copy(update={'score': score, 'anomalies': [anomaly]})
    assert quality_blocks_active_signals(quality) is True
    original = [SignalItem(title='原积极信号', level='积极', reason='原观察条件')]

    assert [item.title for item in gate_signal_items(original, quality, 'buy')] == ['暂停新增买点']
    assert [item.title for item in gate_signal_items(original, quality, 't')] == ['暂停做T', '已有底仓才做T']
    assert [item.title for item in gate_signal_items(original, quality, 'sell')] == ['先收紧风控']
    advice = action_advice(quote, 90, '低风险', 16, 18, quality)
    assert advice.action == '控制风险'
    assert anomaly in advice.reason
    assert original[0].title == '原积极信号'


@pytest.mark.parametrize('cached', [False, True])
def test_fresh_fallback_blocks_public_actions_but_preserves_trend_math(cached):
    quote, rows, quality = _inputs(fallback=True, cached=cached)
    normal_quote, normal_rows, normal_quality = _inputs(cached=cached)
    normal = build_analysis(normal_quote, normal_rows, data_quality=normal_quality)
    analysis = build_analysis(quote, rows, data_quality=quality)
    assert quality.score == 88
    assert quality.anomalies == ['K线兜底缓存']
    assert analysis.trend_score == normal.trend_score == 90
    assert analysis.action_advice.action == '控制风险'
    assert [item.title for item in analysis.buy_points] == ['暂停新增买点']
    assert [item.title for item in analysis.t_plan] == ['暂停做T', '已有底仓才做T']
    assert normal.action_advice.action == '回踩关注'
    assert normal.buy_points[0].level == '积极'
    assert normal.action_advice.confidence == 90

    insights = build_stock_insight_bundle(analysis)
    assert [card.status for card in insights.strategy_cards] == ['暂停观察', '暂停观察', '暂停', '暂停做T', '暂停']
    assert all(card.level != '积极' for card in insights.strategy_cards)
    feature = build_feature_snapshot(analysis, insights)
    report = build_t_strategy_assistant_report(
        analysis, feature, SimpleNamespace(risk_multiplier=1.0), SimpleNamespace(overall_status='条件较好'),
    )
    assert report.suitability == '不适合主动做T'
    assert not any('低吸只看' in item or '高抛只看' in item for item in report.execution_steps)


@pytest.mark.parametrize('score', [70, 88, 100])
def test_nonblocking_anomaly_does_not_gain_a_new_hard_policy(score):
    quote, rows, quality = _inputs()
    quality = quality.model_copy(update={'score': score, 'anomalies': ['普通待核验提示']})
    analysis = build_analysis(quote, rows, data_quality=quality)
    assert not quality_blocks_active_signals(quality)
    assert analysis.action_advice.action == '回踩关注'
    assert analysis.buy_points[0].level == '积极'
