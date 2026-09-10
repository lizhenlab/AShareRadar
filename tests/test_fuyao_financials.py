from __future__ import annotations

from copy import deepcopy
from datetime import datetime

import pytest

from app.models.fuyao import FinancialReportBundle
from app.services.fuyao_financials import financial_fact_answer, financial_health_from_bundle, normalize_financials
from app.utils.clock import ASHARE_TIMEZONE

FETCHED = "2026-09-10T17:30:00+08:00"
SYMBOL = "600519.SH"


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=ASHARE_TIMEZONE).timestamp() * 1000)


def _row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "thscode": SYMBOL, "ticker": "600519", "period": "annual", "period_end_ms": _ms("2025-12-31"),
        "report_date_ms": _ms("2026-04-01"), "fiscal_year": 2025, "fiscal_period": "FY", "currency": "CNY",
    }
    row.update(updates)
    return row


def _envelope(*rows: dict[str, object]) -> dict[str, object]:
    return {"code": 0, "data": {"timestamp": _ms("2025-12-31"), "item": list(rows)}}


def _payloads() -> dict[str, dict[str, object]]:
    return {
        "income": _envelope(_row(operating_income=123456789.5, net_profit=-42.5, parent_holder_net_profit=0)),
        "balance": _envelope(_row(assets_total=100, total_debt=45)),
        "cashflow": _envelope(_row(act_cash_flow_net=-10)),
        "indicators": {"code": 0, "data": {"thscode": SYMBOL, "report": "2025-4", "abilities": [
            {"ability": "growth", "indicators": [{"index_id": "calculate_operating_income_yoy_growth_ratio", "value": "-3.50%"}]},
            {"ability": "profitability", "indicators": [{"index_id": "unverified_roe", "value": "0.157"}]},
        ]}},
    }


def _bundle() -> FinancialReportBundle:
    return normalize_financials(SYMBOL, _payloads(), FETCHED)


def test_aligns_period_and_preserves_negative_missing_and_unknown_units() -> None:
    bundle = _bundle()
    period = bundle.periods[0]
    facts = {item.key: item for item in period.metrics}
    assert bundle.symbol == SYMBOL and bundle.point_in_time is False and bundle.score is None
    assert period.alignment == "complete" and period.currency == "CNY"
    assert facts["net_profit"].value == -42.5
    assert facts["parent_holder_net_profit"].value == 0
    assert facts["interest_expenses"].value is None
    assert facts["total_debt"].label == "负债合计"
    assert facts["operating_income"].unit is None
    assert facts["calculate_operating_income_yoy_growth_ratio"].value == -3.5
    assert facts["calculate_operating_income_yoy_growth_ratio"].unit == "%"
    assert facts["unverified_roe"].value == 0.157 and facts["unverified_roe"].unit is None
    assert period.supplier_report_dates == ["2026-04-01T00:00:00+08:00"]
    assert "published_at" not in bundle.model_dump_json()
    assert FinancialReportBundle.model_validate_json(bundle.model_dump_json()) == bundle


def test_three_statements_from_different_periods_never_join() -> None:
    payloads = _payloads()
    payloads["cashflow"] = _envelope(_row(period_end_ms=_ms("2024-12-31"), fiscal_year=2024, act_cash_flow_net=30))
    bundle = normalize_financials(SYMBOL, payloads, FETCHED)
    assert len(bundle.periods) == 2
    assert all(period.alignment == "partial" for period in bundle.periods)
    assert all(item.key != "act_cash_flow_net" for item in bundle.periods[0].metrics)


def test_quarterly_cashflow_is_not_ttm_or_annual() -> None:
    payload = _envelope(_row(period="quarterly", period_end_ms=_ms("2026-06-30"), report_date_ms=_ms("2026-08-20"),
                             fiscal_year=2026, fiscal_period="H1", act_cash_flow_net=123))
    bundle = normalize_financials(SYMBOL, {"cashflow": payload}, FETCHED)
    assert bundle.periods[0].period_type == "quarterly"
    assert "单季或累计" in " ".join(bundle.warnings)
    answer = financial_fact_answer("2026年中报经营现金流是多少", bundle)
    assert answer is not None and answer.answerability == "answerable"
    assert "单季/累计待核实" in answer.answer
    missing = financial_fact_answer("2026年全年经营现金流是多少", bundle)
    assert missing is not None and missing.answerability == "insufficient_evidence"


@pytest.mark.parametrize("field,value", [
    ("thscode", "000001.SZ"), ("thscode", "600519"), ("ticker", "000001"),
    ("period_end_ms", True), ("period_end_ms", float("nan")), ("period_end_ms", "1767110400000"),
    ("period_end_ms", _ms("2027-12-31")), ("period_end_ms", _ms("2025-12-30")),
    ("report_date_ms", _ms("2027-01-01")), ("report_date_ms", _ms("2024-12-31")),
    ("fiscal_year", True), ("fiscal_year", 2024), ("currency", "USD"), ("period", []),
    ("net_profit", True), ("net_profit", "100"), ("net_profit", float("inf")), ("net_profit", float("nan")),
])
def test_rejects_contaminated_financial_record(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        normalize_financials(SYMBOL, {"income": _envelope(_row(**{field: value}))}, FETCHED)


@pytest.mark.parametrize("timestamp", ["2026-09-10", "invalid", "2026-02-30 15:00:00", "NaN"])
def test_fetched_at_requires_valid_full_time(timestamp: str) -> None:
    with pytest.raises(ValueError):
        normalize_financials(SYMBOL, _payloads(), timestamp)


def test_equivalent_fetched_time_keeps_timezone_semantics() -> None:
    assert normalize_financials(SYMBOL, _payloads(), "2026-09-10T09:30:00Z").fetched_at == FETCHED


@pytest.mark.parametrize("code", [2003, 4001, 5002, True, "0", None])
def test_http_success_does_not_hide_business_failure(code: object) -> None:
    with pytest.raises(ValueError):
        normalize_financials(SYMBOL, {"income": {"code": code, "data": {"item": []}}}, FETCHED)


def test_same_period_duplicate_is_idempotent_but_conflict_rejected() -> None:
    row = _row(net_profit=5)
    assert len(normalize_financials(SYMBOL, {"income": _envelope(row, deepcopy(row))}, FETCHED).periods) == 1
    with pytest.raises(ValueError, match="conflicting"):
        normalize_financials(SYMBOL, {"income": _envelope(row, _row(net_profit=6))}, FETCHED)


@pytest.mark.parametrize("value", [True, 1, "NaN", "NaN%", "Infinity", "1e999%"])
def test_indicators_reject_invalid_values(value: object) -> None:
    payload = {"code": 0, "data": {"thscode": SYMBOL, "report": "2025-4", "abilities": [
        {"ability": "growth", "indicators": [{"index_id": "growth", "value": value}]},
    ]}}
    with pytest.raises(ValueError):
        normalize_financials(SYMBOL, {"indicators": payload}, FETCHED)


def test_indicators_without_statements_stay_separate_and_units_are_not_guessed() -> None:
    bundle = normalize_financials(SYMBOL, {"indicators": _payloads()["indicators"]}, FETCHED)
    assert bundle.periods[0].alignment == "indicators_only" and bundle.periods[0].currency is None
    health = financial_health_from_bundle(bundle)
    assert health.score is None and health.formal_minimum_complete is False


def test_display_keeps_financial_score_unavailable_and_source_visible() -> None:
    report = financial_health_from_bundle(_bundle())
    assert report.report_period == "2025-12-31"
    assert report.score is None and not report.score_available and not report.formal_minimum_complete
    assert report.metric_scope == "formal_financial_health"
    assert "单位待核实" in next(item.value for item in report.metrics if item.name == "营业收入")
    assert "不生成财务体检分" in report.summary


def test_empty_success_is_not_converted_into_zero_financials() -> None:
    bundle = normalize_financials(SYMBOL, {"income": _envelope()}, FETCHED)
    report = financial_health_from_bundle(bundle)
    assert report.metrics == [] and report.score is None
    assert report.report_period is None
    answer = financial_fact_answer("净利润是多少", bundle)
    assert answer is not None and answer.answerability == "insufficient_evidence"


def test_fact_answer_includes_field_period_source_and_observation_time() -> None:
    answer = financial_fact_answer("2025年净利润是多少", _bundle())
    assert answer is not None and answer.answerability == "answerable"
    assert "-42.5" in answer.answer and "2025-12-31" in answer.answer
    assert FETCHED in answer.answer and "同花顺扶摇" in answer.answer
    assert "不等同于" in answer.answer and answer.llm_used is False
    zero = financial_fact_answer("2025年归母净利润是多少", _bundle())
    assert zero is not None and zero.answerability == "answerable" and "原值为 0" in zero.answer


@pytest.mark.parametrize("question", [
    "2024年净利润是多少", "2024-02-30营收是多少", "2024和2025年营收比较", "去年和2024年营收",
    "第一季度净利润", "最近三年营收", "2025年营收和净利润", "2025年第四季度营收",
    "2025年净利润增长率", "2025年TTM营收", "2025年有息负债", "2025年ROE",
])
def test_missing_or_ambiguous_question_does_not_substitute_other_evidence(question: str) -> None:
    answer = financial_fact_answer(question, _bundle())
    assert answer is not None and answer.answerability == "insufficient_evidence"


@pytest.mark.parametrize("question", ["预测明年净利润", "公告原文中的营业收入", "董事长怎么说财报", "净利润对下周股价有何影响"])
def test_financial_data_does_not_unlock_unsupported_questions(question: str) -> None:
    answer = financial_fact_answer(question, _bundle())
    assert answer is not None and answer.answerability == "out_of_scope"


def test_nonfinancial_question_is_left_to_existing_question_pipeline() -> None:
    assert financial_fact_answer("当前趋势怎么样", _bundle()) is None


def test_relative_year_follows_question_time_instead_of_old_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.fuyao_financials_qa.market_now", lambda: datetime(2027, 1, 1, tzinfo=ASHARE_TIMEZONE))
    bundle = _bundle()
    previous = financial_fact_answer("去年营业收入是多少", bundle)
    current = financial_fact_answer("今年营业收入是多少", bundle)
    explicit = financial_fact_answer("2025年营业收入是多少", bundle)
    assert previous is not None and previous.answerability == "insufficient_evidence" and "2026-12-31" in previous.answer
    assert current is not None and current.answerability == "insufficient_evidence" and "2027-12-31" in current.answer
    assert explicit is not None and explicit.answerability == "answerable"


def test_explicit_percentage_only_is_answered_as_growth_rate() -> None:
    answer = financial_fact_answer("2025年营业收入同比增长率是多少", _bundle())
    assert answer is not None and answer.answerability == "answerable" and "-3.5%" in answer.answer
    payloads = _payloads()
    payloads["indicators"] = {"code": 0, "data": {"thscode": SYMBOL, "report": "2025-4", "abilities": [
        {"ability": "growth", "indicators": [{"index_id": "calculate_operating_income_yoy_growth_ratio", "value": "0.15"}]},
    ]}}
    unavailable = financial_fact_answer("2025年营收同比增长率是多少", normalize_financials(SYMBOL, payloads, FETCHED))
    assert unavailable is not None and unavailable.answerability == "insufficient_evidence"


@pytest.mark.parametrize("question", ["000001.SZ净利润是多少", "今年营业收入是多少", "净利润是不是造假", "净利润高于同行吗", "本季度营收"])
def test_fact_lookup_does_not_ignore_stock_relative_period_or_judgment(question: str) -> None:
    answer = financial_fact_answer(question, _bundle())
    assert answer is not None and answer.answerability == "insufficient_evidence"


def test_latest_annual_question_uses_an_annual_record() -> None:
    answer = financial_fact_answer("最新年报营业收入是多少", _bundle())
    assert answer is not None and answer.answerability == "answerable" and "2025-12-31" in answer.answer
