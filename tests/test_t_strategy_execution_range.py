from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_features import build_feature_snapshot
from app.services.research_qa_answer_actions import _t_strategy_actions
from app.services.research_qa_answer_report import answer_stock_question
from app.services.research_t_strategy import build_t_strategy_assistant_report
from app.services.research_validation import build_signal_validation_report
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_quote
from tests.test_indicator_atr_window import _rows
from tests.test_research_t_strategy_modules import _analysis, _feature, _regime, _validation
from tests.test_research_qa_answer_modules import _qa_args


def _public_report(price=100, *, narrow=False):
    width = .1 if narrow else 6
    rows = [r.model_copy(update={
        'open': 100., 'close': 100., 'high': 100 + width / 2, 'low': 100 - width / 2,
    }) for r in _rows([0.] * 80)]
    if narrow:
        rows[-20:-14] = [r.model_copy(update={'high': 110., 'low': 90.}) for r in rows[-20:-14]]
    quote = make_quote(
        price=price, prev_close=100, high=max(price, 100) + 1, low=min(price, 100) - 1,
        change_pct=price - 100, timestamp='2026-09-08 10:00:00',
    ).model_copy(update={'open': 100.})
    quality = build_data_quality(quote, rows, now=datetime(2026, 9, 8, 10, 1))
    analysis = build_analysis(quote, rows, data_quality=quality, mode='intraday')
    feature = build_feature_snapshot(analysis, build_stock_insight_bundle(analysis))
    regime = SimpleNamespace(risk_multiplier=1.0, confidence_adjustment=0)
    validation = build_signal_validation_report(analysis, feature, SimpleNamespace(factors=[]), regime)
    report = build_t_strategy_assistant_report(analysis, feature, regime, validation)
    assert quality.score == 100
    return feature, validation, report


@pytest.mark.parametrize('price', [90, 97, 103, 110])
def test_public_price_outside_or_at_historical_range_never_publishes_inverted_t_actions(price):
    feature, _validation_report, report = _public_report(price)
    assert (feature.support, feature.resistance) == (97, 103)
    assert report.suitability != '仅底仓可做T'
    assert report.low_zone == report.high_zone == '待确认'
    assert not any('低吸只看' in step or '高抛只看' in step for step in report.execution_steps)


def test_public_breakout_cannot_borrow_overall_confirmation_for_invalid_t_range():
    _, validation, report = _public_report(110)
    assert validation.overall_status == '条件较好'
    assert next(item for item in validation.items if item.category == '做T').status == '等待确认'
    assert report.suitability == '等待更大区间'


def test_public_wide_history_cannot_admit_an_atr_capped_range_below_original_minimum():
    feature, validation, report = _public_report(narrow=True)
    assert (feature.support, feature.resistance, feature.atr14) == (90, 110, .1)
    assert validation.overall_status == '条件较好'
    assert (report.low_zone, report.high_zone) == ('99.90 附近', '100.10 附近')
    assert report.suitability == '等待更大区间'
    assert not any('低吸只看' in step or '高抛只看' in step for step in report.execution_steps)


def test_public_normal_range_keeps_original_prices_and_actions():
    _, _, report = _public_report()
    assert report.suitability == '仅底仓可做T'
    assert (report.low_zone, report.high_zone) == ('97.00 附近', '103.00 附近')
    assert any('低吸只看 97.00' in step for step in report.execution_steps)
    assert any('高抛只看 103.00' in step for step in report.execution_steps)


@pytest.mark.parametrize(('atr', 'allowed'), [(.59, False), (.60, True), (.61, True)])
def test_published_range_width_respects_original_minimum_at_boundary(atr, allowed):
    report = build_t_strategy_assistant_report(
        _analysis(90), _feature(atr14=atr, atr_pct=atr), _regime(1), _validation('条件较好'),
    )
    assert (report.suitability == '仅底仓可做T') is allowed


@pytest.mark.parametrize(('price', 'support', 'resistance', 'atr'), [
    (1, .95, 1.05, .004), (1, .999, 1.001, 0),
    (0, .5, 1.5, .2), (100, 107, 95, 2),
])
def test_unordered_or_tick_collapsed_ranges_have_no_published_execution_levels(price, support, resistance, atr):
    report = build_t_strategy_assistant_report(
        _analysis(90), _feature(price=price, support=support, resistance=resistance, atr14=atr),
        _regime(1), _validation('条件较好'),
    )
    assert report.suitability != '仅底仓可做T'
    assert report.low_zone == report.high_zone == '待确认'


@pytest.mark.parametrize(('quality', 'risk', 'validation', 'atr'), [
    (65, 1., '条件较好', 2.), (90, 1.28, '条件较好', 2.),
    (90, 1., '风险优先', 2.), (90, 1., '条件较好', .1),
])
def test_qa_actions_preserve_blocked_t_strategy_instead_of_reintroducing_execution(quality, risk, validation, atr):
    report = build_t_strategy_assistant_report(
        _analysis(quality), _feature(atr14=atr, atr_pct=atr), _regime(risk), _validation(validation),
    )
    actions = _t_strategy_actions(SimpleNamespace(t_strategy=report))
    assert report.suitability != '仅底仓可做T'
    assert actions == report.execution_steps
    assert not any('低吸参考' in action or '高抛参考' in action for action in actions)


def test_qa_actions_preserve_valid_t_strategy_reference():
    _, _, report = _public_report()
    actions = _t_strategy_actions(SimpleNamespace(t_strategy=report))
    assert any('低吸参考 97.00' in action and '高抛参考 103.00' in action for action in actions)


def test_public_stock_question_keeps_quality_block_in_actions_and_answer():
    args = list(_qa_args())
    args[0] = args[0].model_copy(update={'data_quality': args[0].data_quality.model_copy(update={'score': 65})})
    feature = build_feature_snapshot(args[0], build_stock_insight_bundle(args[0]))
    args[6] = build_t_strategy_assistant_report(args[0], feature, args[7], args[9])
    result = answer_stock_question('适合做T吗', *args)
    assert args[6].suitability == '不适合主动做T'
    assert result.topic == '做T'
    assert result.actions == args[6].execution_steps
    assert '低吸参考' not in result.answer and '高抛参考' not in result.answer
