from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from app.models.market_scan import (
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanRun,
    MarketScanRunPage,
)
from app.services.market_scan_future_range_store import FutureRangeResearchUnavailable
from app.services.market_scan_probability_store import ProbabilityFilterUnavailable
from app.services.market_scan_query_service import MarketScanQueryService
from app.services.market_scan_research_stores import MarketScanResearchStores
from app.services.market_scan_universe import FULL_MARKET_SCOPE


class _Cache:
    def __init__(self, run: MarketScanRun) -> None:
        self.current_run = run
        self.result_queries: list[dict[str, object]] = []

    def market_scan_run(self, run_id: int) -> MarketScanRun:
        assert run_id == self.current_run.id
        return self.current_run

    def latest_market_scan_run(self, *, mode: str | None = None) -> MarketScanRun:
        assert mode in {None, "official"}
        return self.current_run

    def latest_published_market_scan_run(self, *, mode: str | None = None) -> MarketScanRun:
        assert mode == "official"
        return self.current_run

    def market_scan_runs(self, **query: object) -> MarketScanRunPage:
        assert query == {
            "page": 2,
            "page_size": 10,
            "mode": "official",
            "status": "published",
            "data_date": "2026-08-11",
        }
        return MarketScanRunPage(items=[self.current_run], total=1, page=2, page_size=10, page_count=1)

    def market_scan_results(self, run_id: int, **query: object) -> MarketScanResultPage:
        assert run_id == self.current_run.id
        self.result_queries.append(query)
        requested = cast(tuple[str, ...] | None, query["symbols"])
        items = [_result(run_id, symbol) for symbol in (requested or ("600519.SH",))]
        return MarketScanResultPage(
            run=self.current_run,
            items=items,
            total=len(items),
            page=cast(int, query["page"]),
            page_size=cast(int, query["page_size"]),
            page_count=1,
        )


class _ProbabilityStore:
    def __init__(
        self,
        research: dict[str, object],
        probabilities: dict[str, dict[str, object]],
    ) -> None:
        self.research = research
        self.probabilities = probabilities
        self.projection_symbols: list[tuple[str, ...] | None] = []

    def research_projection(self, run_id: int) -> dict[str, object]:
        assert run_id == 29
        return self.research

    def run_projection(
        self,
        run_id: int,
        *,
        symbols: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        assert run_id == 29
        self.projection_symbols.append(symbols)
        if symbols is None:
            return self.research, self.probabilities
        return self.research, {
            symbol: self.probabilities[symbol]
            for symbol in symbols
            if symbol in self.probabilities
        }


class _ResearchSource:
    def __init__(self, projection: dict[str, object]) -> None:
        self.projection = projection
        self.calls: list[int] = []

    def research_projection(self, run_id: int) -> dict[str, object]:
        self.calls.append(run_id)
        return self.projection


@dataclass
class _FutureRangeStore:
    calls: list[tuple[int, dict[str, object]]]

    def research_projection(self, run_id: int, **query: object) -> dict[str, object]:
        self.calls.append((run_id, query))
        return {"generation_status": "ready", "record_page": {"items": []}}


def test_query_service_delegates_read_models_and_returns_explicit_missing_artifacts() -> None:
    run = _run()
    cache = _Cache(run)
    service = _service(cache)

    assert service.run(29) == run
    assert service.latest_run() == run
    assert service.latest_published_run(mode="official") == run
    assert service.runs(
        page=2,
        page_size=10,
        mode="official",
        status="published",
        data_date="2026-08-11",
    ).items == [run]
    research, probabilities = service.probability_projection(29)
    assert research["status"] == "not_generated"
    assert probabilities == {}
    assert service.future_range_research(
        29,
        page=1,
        page_size=20,
        session_offset=None,
        symbol=None,
        include_research=False,
    )["generation_status"] == "not_generated"


def test_probability_projection_uses_source_only_for_not_generated_model() -> None:
    run = _run()
    source = _ResearchSource({"status": "insufficient_data", "origin": "source"})
    missing_store = _ProbabilityStore({"status": "not_generated"}, {})
    service = _service(_Cache(run), probability=missing_store, source=source)

    assert service.probability_research(29) == source.projection
    assert service.probability_projection(29) == (source.projection, {})
    assert source.calls == [29, 29]

    calibrated = {"status": "calibrated_shadow"}
    calibrated_store = _ProbabilityStore(calibrated, {})
    service = _service(_Cache(run), probability=calibrated_store, source=source)
    assert service.probability_projection(29) == (calibrated, {})
    assert source.calls == [29, 29]


def test_probability_filter_forwards_only_finite_calibrated_matches_and_attaches_page_values() -> None:
    research = _calibrated_research()
    probabilities: dict[str, dict[str, object]] = {
        "600519.SH": _probability_horizons(0.81),
        "000001.SZ": _probability_horizons(0.70),
        "300750.SZ": _probability_horizons(True),
        "688981.SH": _probability_horizons(float("nan")),
        "601398.SH": _probability_horizons(0.99, status="insufficient_data"),
        "002594.SZ": {"5": []},
        "600036.SH": {},
    }
    store = _ProbabilityStore(research, probabilities)
    cache = _Cache(_run())
    service = _service(cache, probability=store)

    page = service.results(
        29,
        page=1,
        page_size=100,
        status="success",
        market=["SH", "SZ"],
        industry=None,
        is_st=False,
        is_new=None,
        min_data_quality_score=80,
        keyword="银行",
        sort=["score", "symbol"],
        order=["desc", "asc"],
        probability_horizon=5,
        min_upside_probability=0.70,
    )

    assert cache.result_queries[0]["symbols"] == ("600519.SH", "000001.SZ")
    assert cache.result_queries[0]["market"] == ["SH", "SZ"]
    assert cache.result_queries[0]["min_data_quality_score"] == 80
    assert [item.symbol for item in page.items] == ["600519.SH", "000001.SZ"]
    assert page.items[0].upside_probabilities == probabilities["600519.SH"]
    assert page.probability_research == research
    assert store.projection_symbols == [None, ("600519.SH", "000001.SZ")]


@pytest.mark.parametrize("minimum", [float("nan"), -0.01, 1.01])
def test_probability_filter_rejects_non_finite_and_out_of_range_minimum(minimum: float) -> None:
    service = _service(
        _Cache(_run()),
        probability=_ProbabilityStore(_calibrated_research(), {}),
    )

    with pytest.raises(ValueError, match="最低上涨概率"):
        _results(service, minimum=minimum)


@pytest.mark.parametrize(
    "research",
    [
        {"status": "insufficient_data", "horizons": []},
        {"status": "insufficient_data", "horizons": {"5": []}},
        {"status": "insufficient_data", "horizons": {"5": {"net_excess_positive": None}}},
    ],
)
def test_probability_filter_requires_a_calibrated_primary_target(research: dict[str, object]) -> None:
    service = _service(_Cache(_run()), probability=_ProbabilityStore(research, {}))

    with pytest.raises(ProbabilityFilterUnavailable, match="已校准 Shadow 概率"):
        _results(service, minimum=0.5)


def test_probability_filter_rejects_calibrated_but_unqualified_evidence() -> None:
    research = _calibrated_research()
    summary = cast(dict[str, object], cast(dict[str, object], research["horizons"])["5"])
    primary = cast(dict[str, object], summary["net_excess_positive"])
    primary.update(selection_qualified=False, selection_qualification={"passed": False})
    service = _service(_Cache(_run()), probability=_ProbabilityStore(research, {}))

    with pytest.raises(ProbabilityFilterUnavailable, match="样本外效力门禁"):
        _results(service, minimum=0.5)


def test_future_range_query_delegates_full_filter_contract_and_checks_run_eligibility() -> None:
    calls: list[tuple[int, dict[str, object]]] = []
    cache = _Cache(_run())
    service = _service(cache, future_range=_FutureRangeStore(calls))

    result = service.future_range_research(
        29,
        page=3,
        page_size=40,
        session_offset=2,
        symbol="600519.SH",
        include_research=True,
    )
    assert result["generation_status"] == "ready"
    assert calls == [
        (
            29,
            {
                "page": 3,
                "page_size": 40,
                "session_offset": 2,
                "symbol": "600519.SH",
                "include_research": True,
            },
        )
    ]

    for mode in ("intraday", "preopen"):
        cache.current_run = _run(mode=mode)
        with pytest.raises(FutureRangeResearchUnavailable, match="盘后正式"):
            service.future_range_research(
                29,
                page=1,
                page_size=20,
                session_offset=None,
                symbol=None,
                include_research=True,
            )


def _results(service: MarketScanQueryService, *, minimum: float) -> MarketScanResultPage:
    return service.results(
        29,
        page=1,
        page_size=20,
        status=None,
        market=None,
        industry=None,
        is_st=None,
        is_new=None,
        min_data_quality_score=None,
        keyword=None,
        sort="rank",
        order="asc",
        min_upside_probability=minimum,
    )


def _service(
    cache: _Cache,
    *,
    probability: object | None = None,
    source: object | None = None,
    future_range: object | None = None,
) -> MarketScanQueryService:
    stores = MarketScanResearchStores(
        probability=cast(Any, probability),
        probability_source=cast(Any, source),
        future_range=cast(Any, future_range),
    )
    return MarketScanQueryService(cast(Any, cache), stores)


def _run(*, mode: str = "official") -> MarketScanRun:
    return MarketScanRun(
        id=29,
        status="success",
        trigger="manual",
        mode=mode,
        rule_version="scan-v1",
        as_of="2026-08-11 16:00:00",
        data_date="2026-08-11",
        quote_date="2026-08-11",
        scope=FULL_MARKET_SCOPE,
        total_count=2,
        excluded_count=0,
        processed_count=2,
        success_count=2,
        missing_count=0,
        skipped_count=0,
        retry_count=0,
        progress_pct=100,
        coverage_pct=100,
        created_at="2026-08-11 16:00:00",
        updated_at="2026-08-11 16:10:00",
    )


def _result(run_id: int, symbol: str) -> MarketScanResultItem:
    code, market = symbol.split(".")
    return MarketScanResultItem(
        run_id=run_id,
        symbol=symbol,
        code=code,
        market=market,
        name=f"测试{code}",
        status="success",
        updated_at="2026-08-11 16:10:00",
    )


def _calibrated_research() -> dict[str, object]:
    return {
        "status": "calibrated_shadow",
        "horizons": {
            "5": {
                "net_excess_positive": {
                    "status": "calibrated_shadow",
                    "selection_qualified": True,
                    "selection_qualification": {"passed": True},
                }
            }
        },
    }


def _probability_horizons(value: object, *, status: str = "calibrated_shadow") -> dict[str, object]:
    return {
        "5": {
            "net_excess_positive": {
                "status": status,
                "probability": value,
            }
        }
    }
