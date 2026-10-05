from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.models.analysis import DataQuality
from app.models.market import PlateItem
from app.services.eastmoney_client import EASTMONEY_BRIDGE_SOURCE_NAME, EASTMONEY_INDUSTRY_PLATE_PAGE_SIZE
from app.workflows.stock_analysis import _optional_plate_rank, analyze_individual_stock
from app.workflows.stock_lookup import match_industry
from tests.factories import make_plate_item, make_quote, make_stock_info


DECISION_TIME = "2026-05-13T02:00:01Z"


def _plate(**updates: object) -> PlateItem:
    return make_plate_item(change_pct=-2.5).model_copy(update={
        "symbol": "BK0477",
        "source": EASTMONEY_BRIDGE_SOURCE_NAME,
        "quote_timestamp": "2026-05-13 10:00:00",
        "updated_at": DECISION_TIME,
        **updates,
    })


def _match(rows: list[PlateItem], *, quote_time: str = "2026-05-13 10:00:00", decision: str = DECISION_TIME):
    return match_industry(make_stock_info(), rows, quote=make_quote(timestamp=quote_time), evaluated_at=decision)


def test_negative_industry_beyond_top_twenty_reaches_actual_analysis(monkeypatch) -> None:
    rows = [_plate(rank=index, name=f"热门行业{index}") for index in range(1, 30)]
    industry = _plate(rank=30)
    hub = _IndustryHub([*rows, industry])
    monkeypatch.setattr("app.workflows.stock_analysis.audit_now_text", lambda: DECISION_TIME)

    result = asyncio.run(analyze_individual_stock(hub, "600519.SH", mode="official"))  # type: ignore[arg-type]

    assert hub.plate_limits == [EASTMONEY_INDUSTRY_PLATE_PAGE_SIZE]
    assert result.industry_context == industry
    assert result.industry_context.change_pct == -2.5


@pytest.mark.parametrize("field, value", [
    ("quote_timestamp", None),
    ("quote_timestamp", "2026-05-13"),
    ("quote_timestamp", "2026-05-13 10:00"),
    ("quote_timestamp", "2026-05-12 15:00:00"),
    ("quote_timestamp", "2026-05-13 10:00:00.000001"),
    ("quote_timestamp", "2026-05-14 10:00:00"),
    ("quote_timestamp", "2026-02-30 10:00:00"),
    ("quote_timestamp", "0001-01-01 00:00:00"),
    ("quote_timestamp", "9999-12-31 23:59:59-08:00"),
    ("updated_at", "2026-05-13"),
    ("updated_at", "2026-05-13 09:59:59"),
    ("updated_at", "2026-05-13 10:00:01.000001"),
    ("fallback_used", True),
    ("rank", 0),
    ("source", " "),
    ("source", "其他分类行情"),
    ("symbol", None),
    ("symbol", ""),
    ("symbol", "BK04770"),
    ("symbol", "000001.SH"),
    ("symbol", "885001.TI"),
    ("change_pct", float("nan")),
    ("change_pct", float("inf")),
])
def test_invalid_or_unavailable_industry_observation_is_not_scoring_context(field: str, value: object) -> None:
    assert _match([_plate(**{field: value})]) is None


def test_local_fetch_timestamp_cannot_substitute_for_missing_quote_event(monkeypatch) -> None:
    hub = _IndustryHub([_plate(quote_timestamp=None)])
    monkeypatch.setattr("app.workflows.stock_analysis.audit_now_text", lambda: DECISION_TIME)

    result = asyncio.run(analyze_individual_stock(hub, "600519.SH", mode="official"))  # type: ignore[arg-type]

    assert result.industry_context is None
    assert result.stock_profile is not None
    assert result.stock_profile.industry == "测试行业"


@pytest.mark.parametrize("other", [_plate(), _plate(quote_timestamp="2026-05-12 15:00:00"), _plate(symbol="BK0999")])
def test_duplicate_exact_industry_is_ambiguous_even_when_one_observation_is_stale(other: PlateItem) -> None:
    assert _match([_plate(), other]) is None


@pytest.mark.parametrize("name", ["热门测试行业", "测试行业概念", "测试行业 ", "测试"])
def test_similar_or_hot_industry_names_never_replace_confirmed_industry(name: str) -> None:
    assert _match([_plate(name=name)]) is None


@pytest.mark.parametrize("industry", [None, "", " "])
def test_unknown_stock_industry_does_not_select_a_market_leader(industry: str | None) -> None:
    profile = make_stock_info().model_copy(update={"industry": industry})
    assert match_industry(profile, [_plate()], quote=make_quote(), evaluated_at=DECISION_TIME) is None
    assert match_industry(None, [_plate()], quote=make_quote(), evaluated_at=DECISION_TIME) is None


@pytest.mark.parametrize("quote_time, decision", [
    ("2026-05-13", DECISION_TIME),
    ("2026-05-13 10:00:00", "2026-05-13"),
    ("2026-05-13 10:00:00", "2026-05-13 09:59:59.999999"),
])
def test_primary_quote_and_actual_decision_both_bound_industry(quote_time: str, decision: str) -> None:
    assert _match([_plate()], quote_time=quote_time, decision=decision) is None


def test_different_timezones_compare_actual_instants_and_shanghai_trading_day() -> None:
    industry = _plate(quote_timestamp="2026-05-12T18:00:00-08:00")
    assert _match([industry]) is industry


def test_microsecond_cutoffs_preserve_valid_boundary_without_truncation() -> None:
    industry = _plate(quote_timestamp="2026-05-13 10:00:00.500000", updated_at="2026-05-13T02:00:00.500000Z")
    assert _match([industry], quote_time="2026-05-13 10:00:00.500000", decision="2026-05-13T02:00:00.500000Z") is industry


@pytest.mark.parametrize("event_time, available", [
    ("2026-05-13 09:55:00", True),
    ("2026-05-13 09:54:59.999999", False),
    ("2026-05-13 09:30:00", False),
])
def test_same_day_industry_event_has_five_minute_alignment_tolerance(event_time: str, available: bool) -> None:
    industry = _plate(quote_timestamp=event_time)
    assert (_match([industry]) is industry) is available


def test_full_industry_request_cancellation_propagates() -> None:
    hub = _IndustryHub([], error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_optional_plate_rank(hub, "600519.SH"))  # type: ignore[arg-type]
    assert hub.cache.events == []


def test_full_industry_provider_failure_preserves_optional_fallback() -> None:
    hub = _IndustryHub([], error=RuntimeError("industry unavailable"))
    assert asyncio.run(_optional_plate_rank(hub, "600519.SH")) == []  # type: ignore[arg-type]
    assert len(hub.cache.events) == 1
    assert "行业背景暂不可用" in hub.cache.events[0][1]


class _IndustryCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log_event(self, category: str, message: str) -> None:
        self.events.append((category, message))

    def quote_history(self, symbol: str, *, limit: int) -> list:
        return []


class _IndustryHub:
    def __init__(self, rows: list[PlateItem], *, error: BaseException | None = None) -> None:
        self.rows = rows
        self.error = error
        self.plate_limits: list[int] = []
        self.cache = _IndustryCache()
        self.settings = SimpleNamespace(seed_symbols=(), workbench_optional_timeout_seconds=0.5)

    async def stock_profile(self, symbol: str):
        return make_stock_info()

    async def stock_pool(self, **kwargs):
        return []

    async def quote(self, symbol: str):
        return make_quote()

    async def kline(self, symbol: str, limit: int):
        return []

    async def plate_rank(self, limit: int):
        self.plate_limits.append(limit)
        if self.error is not None:
            raise self.error
        return self.rows[:limit]

    async def assess_quote_quality(self, quote, klines):
        return DataQuality(level="健康", source=quote.source, quote_time=quote.timestamp, kline_count=len(klines))
