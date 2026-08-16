from __future__ import annotations

from typing import cast

import pytest

from app.models.market_scan import (
    MarketScanProductionScoreContract,
    MarketScanResultPage,
    MarketScanRun,
)
from app.models.market_scan_snapshot import MarketScanSnapshotIntegrityError
from app.services.market_scan_future_range_artifact import FutureRangeArtifactError
from tests.test_market_scan_query_service import (
    _Cache,
    _ProbabilityStore,
    _calibrated_research,
    _results,
    _run,
    _service,
)


class _RunSequenceCache(_Cache):
    def __init__(self, runs: list[MarketScanRun]) -> None:
        super().__init__(runs[0])
        self._runs = iter(runs)

    def market_scan_run(self, run_id: int) -> MarketScanRun:
        assert run_id == self.current_run.id
        return next(self._runs)


class _PageMutationCache(_Cache):
    def __init__(self, run: MarketScanRun, mutation: str) -> None:
        super().__init__(run)
        self._mutation = mutation

    def market_scan_results(self, run_id: int, **query: object) -> MarketScanResultPage:
        page = super().market_scan_results(run_id, **query)
        if self._mutation == "header":
            return page.model_copy(
                update={"run": page.run.model_copy(update={"success_count": 1})}
            )
        if self._mutation == "item_run":
            item = page.items[0].model_copy(update={"run_id": 999})
            return page.model_copy(update={"items": [item]})
        if self._mutation == "item_date":
            item = page.items[0].model_copy(update={"data_date": "2026-08-10"})
            return page.model_copy(update={"items": [item]})
        return page.model_copy(update={"total": page.run.total_count + 1})


class _ScoreContractSequenceCache(_Cache):
    def __init__(
        self,
        run: MarketScanRun,
        contracts: list[MarketScanProductionScoreContract],
    ) -> None:
        super().__init__(run)
        self._contracts = iter(contracts)

    def market_scan_success_score_contract(
        self,
        run_id: int,
    ) -> MarketScanProductionScoreContract:
        assert run_id == self.current_run.id
        return next(self._contracts)


class _BoundFutureRangeStore:
    def __init__(self, run: MarketScanRun, *, quote_date: str | None = None) -> None:
        self.run = run
        self.quote_date = quote_date or run.quote_date
        self.include_research_calls: list[bool] = []

    def research_projection(self, run_id: int, **query: object) -> dict[str, object]:
        self.include_research_calls.append(cast(bool, query["include_research"]))
        return self._projection(run_id)

    def export_projection(self, run_id: int) -> dict[str, object]:
        return self._projection(run_id)

    def _projection(self, run_id: int) -> dict[str, object]:
        return {
            "generation_status": "ready",
            "research": {
                "run": {
                    "run_id": run_id,
                    "mode": self.run.mode,
                    "scope": self.run.scope,
                    "rule_version": self.run.rule_version,
                    "as_of": self.run.as_of,
                    "quote_date": self.quote_date,
                    "data_date": self.run.data_date,
                }
            },
            "record_page": {"items": []},
        }


@pytest.mark.parametrize("mutation", ("header", "item_run", "item_date", "total"))
def test_result_page_rejects_every_cross_snapshot_cohort_mismatch(mutation: str) -> None:
    run = _run()
    service = _service(_PageMutationCache(run, mutation))

    with pytest.raises(MarketScanSnapshotIntegrityError):
        _results(service, minimum=None)


def test_probability_projection_uses_one_request_local_run_snapshot() -> None:
    run = _run()
    changed = run.model_copy(update={"success_count": 1})
    cache = _RunSequenceCache([run, changed])
    service = _service(
        cache,
        probability=_ProbabilityStore(_calibrated_research(), {}),
    )

    assert service.probability_research(run.id)["status"] == "calibrated_shadow"
    assert cache.verified_read_calls == [run.id]


def test_probability_projection_uses_score_contract_from_same_request_snapshot() -> None:
    run = _run()
    first = MarketScanProductionScoreContract(
        "full-market-score-v4",
        "b" * 64,
        run.success_count,
    )
    changed = MarketScanProductionScoreContract(
        "full-market-score-v4",
        "c" * 64,
        run.success_count,
    )
    cache = _ScoreContractSequenceCache(run, [first, changed])
    service = _service(
        cache,
        probability=_ProbabilityStore(_calibrated_research(), {}),
    )

    assert service.probability_research(run.id)["status"] == "calibrated_shadow"
    assert cache.verified_read_calls == [run.id]


def test_result_page_uses_one_request_local_run_snapshot() -> None:
    run = _run(mode="intraday")
    changed = run.model_copy(update={"data_date": "2026-08-10"})
    cache = _RunSequenceCache([run, changed])
    service = _service(cache)

    assert _results(service, minimum=None).run == run
    assert cache.verified_read_calls == [run.id]


def test_future_range_rejects_inner_binding_and_post_read_db_drift() -> None:
    run = _run()
    mismatched = _service(
        _Cache(run),
        future_range=_BoundFutureRangeStore(run, quote_date="2026-08-10"),
    )
    with pytest.raises(FutureRangeArtifactError, match="quote_date"):
        mismatched.future_range_research(
            run.id,
            page=1,
            page_size=20,
            session_offset=None,
            symbol=None,
            include_research=True,
        )

    changed = run.model_copy(update={"processed_count": 1})
    drifted = _service(
        _RunSequenceCache([run, changed]),
        future_range=_BoundFutureRangeStore(run),
    )
    with pytest.raises(FutureRangeArtifactError, match="读取期间"):
        drifted.future_range_research(
            run.id,
            page=1,
            page_size=20,
            session_offset=None,
            symbol=None,
            include_research=True,
        )


def test_future_range_hidden_research_is_still_validated_and_export_is_double_bound() -> None:
    run = _run()
    store = _BoundFutureRangeStore(run)
    service = _service(_Cache(run), future_range=store)

    projection = service.future_range_research(
        run.id,
        page=1,
        page_size=20,
        session_offset=None,
        symbol=None,
        include_research=False,
    )
    assert projection["research"] is None
    assert store.include_research_calls == [True]

    changed = run.model_copy(update={"quote_date": "2026-08-10"})
    with pytest.raises(FutureRangeArtifactError, match="读取期间"):
        service.future_range_export_projection(run.id, expected_run=changed)
