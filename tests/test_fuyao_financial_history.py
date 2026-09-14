from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models.fuyao import FINANCIAL_PARTIAL_WARNING, FinancialFact, FinancialPeriodRecord, FinancialReportBundle, FinancialSourceRecord
from app.repositories.fuyao_research import FuyaoResearchRepository
from app.services.fuyao_financials import financial_fact_answer
from app.services.fuyao_service import FuyaoService


def period(end="2025-12-31", kind="annual", *, value=100.0, statements=("income", "balance", "cashflow")):
    return FinancialPeriodRecord(
        period_end=end, period_type=kind, currency="CNY",
        statements=[FinancialSourceRecord(source_kind=source) for source in statements],
        metrics=[FinancialFact(key="operating_income", label="营业收入", value=value,
                               raw_value=str(value) if value is not None else None,
                               source_kind="income" if statements else "indicators")],
        alignment="complete" if len(statements) == 3 else "partial" if statements else "indicators_only",
    )


def save(repository, periods, *, fetched_at="2026-04-01T09:00:00+08:00", source="年报来源", warnings=()):
    bundle = FinancialReportBundle(symbol="600519.SH", fetched_at=fetched_at, source=source,
                                   periods=periods, warnings=list(warnings))
    return repository.save_observation("financials", bundle.symbol, fetched_at,
                                       {"report": bundle.model_dump(mode="json"), "raw": {"opaque": "provider envelope"}})


@pytest.fixture
def repository(tmp_path):
    return FuyaoResearchRepository(tmp_path / "research.sqlite3")


def test_empty_history_does_not_create_database(repository):
    assert repository.financials("600519.SH") is None
    assert not repository.path.exists()
    repository.initialize()
    assert repository.financials("600519.SH") is None


def test_latest_periods_survive_over_one_hundred_narrow_refreshes(repository):
    annual = save(repository, [period()], warnings=["年度提示", "共同提示"])
    for number in range(105):
        save(repository, [period("2026-06-30", "quarterly", value=float(number))],
             fetched_at="2026-09-10T09:00:00+08:00", source="中报来源", warnings=["中期提示", "共同提示"])
    report = repository.financials("600519.SH")
    assert [(row.period_end, row.metrics[0].value) for row in report.periods] == [("2026-06-30", 104), ("2025-12-31", 100)]
    assert report.periods[1].fetched_at == annual.fetched_at
    assert report.periods[1].source == "年报来源"
    assert report.source == "中报来源" and report.fetched_at == "2026-09-10T09:00:00+08:00"
    assert report.warnings == ["中期提示", "共同提示", "年度提示"]
    assert report.score is None and report.point_in_time is False
    assert repository.observations("financials", "600519.SH", 100)[-1].id > annual.id


def test_q4_indicators_cannot_hide_annual_statements_from_another_observation(repository):
    save(repository, [period(value=100)])
    save(repository, [period(kind="quarterly", value=25), period(value=999, statements=())],
         fetched_at="2026-05-01T09:00:00+08:00", source="季度来源")
    report = repository.financials("600519.SH")
    assert [(row.period_type, row.metrics[0].value) for row in report.periods] == [("annual", 100), ("quarterly", 25)]
    assert [row.source for row in report.periods] == ["年报来源", "季度来源"]


def test_new_partial_replaces_old_complete_without_cross_observation_statement_fill(repository):
    old = save(repository, [period(value=100)])
    save(repository, [period(value=110, statements=("income",))])
    report = repository.financials("600519.SH")
    assert len(report.periods) == 1
    current = report.periods[0]
    assert current.alignment == "partial" and current.metrics[0].value == 110
    assert [item.source_kind for item in current.statements] == ["income"]
    assert FINANCIAL_PARTIAL_WARNING in report.warnings
    assert repository.observations("financials", "600519.SH")[-1] == old


def test_replaced_partial_period_does_not_leave_stale_warning_on_complete_reports(repository):
    save(repository, [period("2024-12-31"), period(statements=("income",))], warnings=[FINANCIAL_PARTIAL_WARNING])
    save(repository, [period()])
    report = repository.financials("600519.SH")
    assert len(report.periods) == 2 and all(row.alignment == "complete" for row in report.periods)
    assert FINANCIAL_PARTIAL_WARNING not in report.warnings


@pytest.mark.parametrize("raw", [None, "", " \t\r\n "])
def test_legacy_empty_indicators_do_not_hide_facts_or_create_a_period(repository, raw):
    save(repository, [period(value=0, statements=())])
    empty = period(value=None, statements=()).model_copy(update={"metrics": [FinancialFact(
        key="placeholder", label="缺失值", source_kind="indicators", raw_value=raw)]})
    save(repository, [empty, empty.model_copy(update={"period_end": "2024-12-31"})])
    report = repository.financials("600519.SH")
    assert len(report.periods) == 1 and report.periods[0].metrics[0].value == 0
    assert len(repository.observations("financials", "600519.SH")) == 2


def test_missing_default_statements_does_not_change_latest_indicator_priority(repository):
    save(repository, [period(value=100, statements=())])
    saved = save(repository, [period(value=0, statements=())])
    del saved.payload["report"]["periods"][0]["statements"]
    repository.save_observation("financials", "600519.SH", saved.fetched_at, saved.payload)
    assert repository.financials("600519.SH").periods[0].metrics[0].value == 0


def test_empty_legacy_records_and_other_capabilities_or_stocks_are_excluded(repository):
    save(repository, [period(value=None, statements=())])
    repository.save_observation("valuations", "600519.SH", "2026-09-10T00:00:00Z", {"values": {"pe_ttm": 1}})
    repository.save_observation("financials", "000001.SZ", "2026-09-10T00:00:00Z", {"report": {"periods": []}})
    assert repository.financials("600519.SH") is None
    save(repository, [period(value=None, statements=("income",))])
    assert repository.financials("600519.SH").periods[0].alignment == "partial"


@pytest.mark.parametrize("field,value,error", [("symbol", "000001.SZ", ValueError), ("point_in_time", True, ValidationError)])
def test_selected_report_retains_full_model_and_stock_identity_validation(repository, field, value, error):
    observation = save(repository, [period()])
    observation.payload["report"][field] = value
    repository.save_observation("financials", "600519.SH", observation.fetched_at, observation.payload)
    with pytest.raises(error):
        repository.financials("600519.SH")


def test_service_and_old_annual_qa_read_merged_cache_without_provider_calls(tmp_path, monkeypatch):
    import app.config_settings as config_module
    from tests.test_fuyao_service import Client, Runtime
    monkeypatch.setattr(config_module, "_SHELL_ENV_VALUES", {})
    service = FuyaoService(Settings(cache_path=tmp_path / "cache.sqlite3", fuyao_enabled=False,
                                    fuyao_api_key=None, fuyao_api_key_file=None), Runtime(), client=Client())
    save(service.repository, [period()])
    save(service.repository, [period("2026-06-30", "quarterly")], fetched_at="2026-09-10T09:00:00+08:00", source="中报来源")
    async def run():
        report = await service.financials("600519.sh")
        answer = financial_fact_answer("2025年年报营业收入是多少", report)
        assert answer.answerability == "answerable" and "100" in answer.answer
        assert answer.updated_at == "2026-04-01T09:00:00+08:00"
        assert answer.answer_source == "年报来源结构化财务记录"
        assert "2026-09-10" not in answer.answer
        assert service.repository.request_count("2026-09-11") == 0
        await service.aclose()
    asyncio.run(run())
