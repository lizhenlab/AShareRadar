from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import cmp_to_key
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, Iterator, cast

import pytest

from app.models.market_scan import (
    MarketScanPublicationDiagnostic,
    MarketScanPublicationDiagnostics,
    MarketScanProductionScoreContract,
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanRun,
    MarketScanRunPage,
)
from app.models.market_scan_screening import ScreenSortV2, ScreenSpecV2
from app.repositories.market_scan_screening_sql import screen_spec_order_sql
from app.services.market_scan_export import MarketScanExportFilters
from app.services.market_scan_future_range_store import FutureRangeResearchUnavailable
from app.services.market_scan_probability import (
    PROBABILITY_FILTER_AUTHORIZATION_VERSION,
    stable_probability_hash,
)
from app.services.market_scan_probability_artifact import ProbabilityArtifactError
from app.services.market_scan_probability_historical_context import (
    HistoricalProbabilityContextError,
)
from app.services.market_scan_official_execution_store import OfficialExecutionStoreStatus
from app.services.market_scan_joint_execution_maintenance import (
    MarketScanJointExecutionMaintenanceService,
)
from app.services.market_scan_probability_store import (
    ProbabilityFilterUnavailable,
    ProbabilityResearchUnavailable,
)
from app.services.market_scan_probability_ranking import (
    PROBABILITY_RANKING_SCORE_RULE_VERSION,
    probability_ranking_score_spec_hash,
)
from app.services.market_scan_scoring import FULL_MARKET_SCORE_RULE_VERSION
from app.services.market_scan_query_service import MarketScanQueryService
from app.services import market_scan_query_service as query_service_module
from app.services.market_scan_research_stores import MarketScanResearchStores
from app.services.market_scan_universe import FULL_MARKET_SCOPE


class _Cache:
    def __init__(
        self,
        run: MarketScanRun,
        *,
        score_contract: MarketScanProductionScoreContract | None = None,
        capture_status: str | None = None,
        action_source_digest: str | None = "a" * 64,
    ) -> None:
        self.current_run = run
        self.result_queries: list[dict[str, object]] = []
        self.score_contract = score_contract or MarketScanProductionScoreContract(
            "full-market-score-v4",
            "b" * 64,
            run.success_count,
        )
        self.capture_status = capture_status
        self.capture_status_calls: list[int] = []
        self.action_source_digest = action_source_digest
        self.action_source_calls: list[int] = []
        self.verified_read_calls: list[int] = []

    @contextmanager
    def verified_market_scan_read(self, run_id: int) -> Iterator[_VerifiedRead]:
        self.verified_read_calls.append(run_id)
        run = self.market_scan_run(run_id)
        action_digest = self.market_scan_action_source_digest(run_id)
        capture = None
        score_contract = None
        if action_digest is not None and action_digest == run.snapshot_digest:
            capture = self.probability_source_capture_status(run_id)
            score_contract = self.market_scan_success_score_contract(run_id)
        verified = _VerifiedRead(
            self,
            run,
            action_digest=action_digest,
            capture=capture,
            score_contract=score_contract,
        )
        try:
            yield verified
        finally:
            verified.close()

    def market_scan_action_source_digest(self, run_id: int) -> str | None:
        assert run_id == self.current_run.id
        self.action_source_calls.append(run_id)
        return self.action_source_digest

    def market_scan_run(self, run_id: int) -> MarketScanRun:
        assert run_id == self.current_run.id
        return self.current_run

    def market_scan_success_score_contract(
        self,
        run_id: int,
    ) -> MarketScanProductionScoreContract | None:
        assert run_id == self.current_run.id
        return self.score_contract

    def probability_source_capture_status(self, run_id: int) -> dict[str, object] | None:
        assert run_id == self.current_run.id
        self.capture_status_calls.append(run_id)
        if self.capture_status is None:
            return None
        return {
            "status": self.capture_status,
            "archive_digest": "c" * 64 if self.capture_status == "succeeded" else None,
            "last_error": "fixture skipped" if self.capture_status == "skipped" else None,
        }

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
        return MarketScanRunPage(items=[], total=1, page=2, page_size=10, page_count=1)

    def market_scan_run_identities(self, **query: object) -> MarketScanRunPage:
        assert query == {
            "page": 1,
            "page_size": 100,
            "mode": "intraday",
            "status": "published",
            "data_date": None,
        }
        return MarketScanRunPage(items=[self.current_run], total=1, page=1, page_size=100, page_count=1)

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


class _VerifiedRead:
    def __init__(
        self,
        cache: _Cache,
        run: MarketScanRun,
        *,
        action_digest: str | None,
        capture: dict[str, object] | None,
        score_contract: MarketScanProductionScoreContract | None,
    ) -> None:
        self._cache = cache
        self._run = run
        self._action_digest = action_digest
        self._capture = capture
        self._score_contract = score_contract
        self._active = True

    @property
    def run(self) -> MarketScanRun:
        self._require_active()
        return self._run

    @property
    def snapshot_digest(self) -> str | None:
        self._require_active()
        return self._run.snapshot_digest

    @property
    def action_source_digest(self) -> str | None:
        self._require_active()
        return self._action_digest

    @property
    def probability_source_capture_state(self) -> dict[str, object] | None:
        self._require_active()
        return self._capture

    @property
    def success_score_contract(self) -> MarketScanProductionScoreContract | None:
        self._require_active()
        return self._score_contract

    def results_page(self, **query: object) -> MarketScanResultPage:
        self._require_active()
        return self._cache.market_scan_results(self._run.id, **query)

    def close(self) -> None:
        self._active = False

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("verified read closed")


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
        return self.research, {symbol: self.probabilities[symbol] for symbol in symbols if symbol in self.probabilities}


class _ForbiddenProbabilityStore:
    def research_projection(self, _run_id: int) -> dict[str, object]:
        pytest.fail("probability artifact must not be read before capture authorization")

    def run_projection(
        self,
        _run_id: int,
        *,
        symbols: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        del symbols
        pytest.fail("probability artifact must not be read before capture authorization")


class _ForbiddenResearchSource:
    def research_projection(self, _run_id: int) -> dict[str, object]:
        pytest.fail("source artifact must not be read before capture authorization")

    def preload(self) -> int:
        pytest.fail("source index must not preload before capture authorization")


class _ResearchSource:
    def __init__(self, projection: dict[str, object]) -> None:
        self.projection = projection
        self.calls: list[int] = []

    def research_projection(self, run_id: int) -> dict[str, object]:
        self.calls.append(run_id)
        return self.projection

    def preload(self) -> int:
        return 1


class _RacingResearchSource:
    def __init__(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        self.projection = before
        self.after = after
        self.calls: list[int] = []
        self.preload_calls = 0
        self.pending = False

    def research_projection(self, run_id: int) -> dict[str, object]:
        self.calls.append(run_id)
        return self.projection

    def preload(self) -> int:
        self.preload_calls += 1
        self.projection = self.after
        return 1

    def refresh_pending(self) -> bool:
        return self.pending


class _HistoricalProbabilityStore:
    def __init__(
        self,
        projection: dict[str, object] | None = None,
        *,
        broken: bool = False,
    ) -> None:
        self.projection = projection or {
            "schema_version": "market-scan-probability-historical-context-v1",
            "status": "ready",
            "availability": "historical_replay_no_verified_predictive_skill",
            "production_ranking_effect": "none",
            "selection_qualified": False,
            "filter_qualified": False,
        }
        self.broken = broken
        self.calls = 0

    def research_projection(self) -> dict[str, object]:
        self.calls += 1
        if self.broken:
            raise HistoricalProbabilityContextError("fixture integrity failure")
        return self.projection


class _CountingEvidenceCatalog:
    def __init__(self) -> None:
        self.official_calls = 0
        self.joint_calls = 0

    def status(self) -> OfficialExecutionStoreStatus:
        self.official_calls += 1
        return OfficialExecutionStoreStatus(False, "store_unavailable", None, 0, None, None)

    def status_projection(self) -> dict[str, object]:
        self.joint_calls += 1
        return {"status": "store_unavailable", "filter_ready": False}

    def has_current_projection(self, _run_id: int) -> bool:
        return False


def _run_binding(run: MarketScanRun | None = None, *, legacy: bool = False) -> dict[str, object]:
    current = run
    run_id = current.id if current is not None else 29
    mode = current.mode if current is not None else "official"
    scope = current.scope if current is not None else FULL_MARKET_SCOPE
    rule_version = current.rule_version if current is not None else f"full-market-scan-v6:{'a' * 64}"
    quote_date = current.quote_date if current is not None else "2026-08-11"
    data_date = current.data_date if current is not None else "2026-08-11"
    return {
        "schema_version": "market-scan-probability-run-binding-v1",
        "binding_status": "legacy_unbound" if legacy else "verified",
        "legacy": legacy,
        "run_id": run_id,
        "mode": mode,
        "scope": scope,
        "rule_version": rule_version,
        "quote_date": quote_date,
        "data_date": data_date,
        "scan_rule_hash": rule_version.rsplit(":", 1)[-1],
        "production_score_rule_version": "full-market-score-v4",
        "production_score_spec_hash": "b" * 64,
        "source_integrity_digest": "c" * 64,
        "cohort_contract": {
            "mode": mode,
            "scope": scope,
            "rule_version": rule_version,
        },
    }


@dataclass
class _FutureRangeStore:
    calls: list[tuple[int, dict[str, object]]]

    def research_projection(self, run_id: int, **query: object) -> dict[str, object]:
        self.calls.append((run_id, query))
        return {
            "generation_status": "ready",
            "research": {
                "run": {
                    "run_id": run_id,
                    "mode": "official",
                    "scope": FULL_MARKET_SCOPE,
                    "rule_version": f"full-market-scan-v6:{'a' * 64}",
                    "as_of": "2026-08-11 16:00:00",
                    "quote_date": "2026-08-11",
                    "data_date": "2026-08-11",
                }
            },
            "record_page": {"items": []},
        }

    def export_projection(self, run_id: int) -> dict[str, object]:
        self.calls.append((run_id, {"export": True}))
        return {
            "generation_status": "ready",
            "research": {
                "run": {
                    "run_id": run_id,
                    "mode": "official",
                    "scope": FULL_MARKET_SCOPE,
                    "rule_version": f"full-market-scan-v6:{'a' * 64}",
                    "as_of": "2026-08-11 16:00:00",
                    "quote_date": "2026-08-11",
                    "data_date": "2026-08-11",
                }
            },
            "record_page": {"items": []},
        }


def test_query_service_delegates_read_models_and_returns_explicit_missing_artifacts() -> None:
    run = _run()
    cache = _Cache(run)
    service = _service(cache)

    assert service.run(29) == run
    assert service.latest_run() == run
    assert service.latest_published_run(mode="official") == run
    assert (
        service.runs(
            page=2,
            page_size=10,
            mode="official",
            status="published",
            data_date="2026-08-11",
        ).items
        == []
    )
    assert service.run_identities(
        page=1,
        page_size=100,
        mode="intraday",
        status="published",
    ).items == [run]
    research, probabilities = service.probability_projection(29)
    assert research["status"] == "not_generated"
    assert probabilities == {}
    assert (
        service.future_range_research(
            29,
            page=1,
            page_size=20,
            session_offset=None,
            symbol=None,
            include_research=False,
        )["generation_status"]
        == "not_generated"
    )


def test_export_projection_normalizes_every_filter_in_one_verified_snapshot() -> None:
    run = _run()
    cache = _Cache(run)
    service = _service(cache)
    filters = MarketScanExportFilters(
        status=None,
        market=("SZ", "SH"),
        industry=("  银行   服务 ", "电力"),
        is_st=False,
        min_score=60,
        max_score=98,
        min_trend_score=50,
        max_trend_score=95,
        min_change_pct=-3,
        max_change_pct=10,
        min_turnover_rate=1,
        max_turnover_rate=25,
        min_amount=1_000_000,
        max_amount=900_000_000,
        min_data_quality_score=70,
        max_data_quality_score=100,
        min_confidence=75,
        max_risk=35,
        min_tradability=65,
        keyword=" 000001   平安 ",
        sort=("score", "amount", "symbol"),
        order=("desc", "desc", "asc"),
    )

    page, future_range = service.export_projection(run.id, filters=filters)

    assert page.run == run
    assert future_range["generation_status"] == "not_generated"
    assert cache.verified_read_calls == [run.id]
    assert cache.result_queries == [
        {
            "page": 1,
            "page_size": run.total_count,
            "status": None,
            "market": ("SZ", "SH"),
            "industry": ("银行 服务", "电力"),
            "is_st": False,
            "is_new": None,
            "min_score": 60,
            "max_score": 98,
            "min_trend_score": 50,
            "max_trend_score": 95,
            "min_change_pct": -3,
            "max_change_pct": 10,
            "min_turnover_rate": 1,
            "max_turnover_rate": 25,
            "min_amount": 1_000_000,
            "max_amount": 900_000_000,
            "min_data_quality_score": 70,
            "max_data_quality_score": 100,
            "min_confidence": 75,
            "max_risk": 35,
            "min_tradability": 65,
            "keyword": "000001 平安",
            "sort": ("score", "amount", "symbol"),
            "order": ("desc", "desc", "asc"),
            "symbols": None,
        }
    ]


def test_export_projection_reads_future_artifact_only_after_run_eligibility() -> None:
    run = _run()
    calls: list[tuple[int, dict[str, object]]] = []
    cache = _Cache(run)
    service = _service(cache, future_range=_FutureRangeStore(calls))

    _page, future_range = service.export_projection(
        run.id,
        filters=MarketScanExportFilters(),
    )

    assert future_range["generation_status"] == "ready"
    assert calls == [(run.id, {"export": True})]
    assert cache.verified_read_calls == [run.id]

    calls.clear()
    top100 = run.model_copy(update={"scope": "top100-refresh"})
    with pytest.raises(ProbabilityResearchUnavailable, match="全市场"):
        _service(
            _Cache(top100),
            future_range=_FutureRangeStore(calls),
        ).export_projection(top100.id, filters=MarketScanExportFilters())
    assert calls == []


def test_probability_projection_uses_source_only_for_not_generated_model() -> None:
    run = _run(action_eligible=True)
    source = _ResearchSource(
        {
            "status": "insufficient_data",
            "origin": "source",
            "run_binding": _run_binding(run),
        }
    )
    missing_store = _ProbabilityStore({"status": "not_generated"}, {})
    service = _service(_Cache(run, capture_status="succeeded"), probability=missing_store, source=source)

    research = service.probability_research(29)
    assert _without_historical_context(research) == source.projection
    assert research["historical_context"]["status"] == "not_generated"  # type: ignore[index]
    projected, probabilities = service.probability_projection(29)
    assert _without_historical_context(projected) == source.projection
    assert probabilities == {}
    assert source.calls == [29, 29]

    calibrated = {"status": "calibrated_shadow", "run_binding": _run_binding(run)}
    calibrated_store = _ProbabilityStore(calibrated, {})
    service = _service(_Cache(run, capture_status="succeeded"), probability=calibrated_store, source=source)
    projected, probabilities = service.probability_projection(29)
    assert _without_historical_context(projected) == calibrated
    assert probabilities == {}
    assert source.calls == [29, 29, 29]


@pytest.mark.parametrize("capture_status", ("pending", "processing"))
def test_probability_projection_exposes_only_active_capture_as_pending(
    capture_status: str,
) -> None:
    run = _run(action_eligible=True)
    cache = _Cache(run, capture_status=capture_status)
    service = _service(cache)

    research = service.probability_research(run.id)

    assert research["status"] == "not_generated"
    assert research["availability"] == "source_capture_pending"
    assert research["pipeline_stage"] == "source_capture_pending"
    primary = cast(dict[str, object], cast(dict[str, object], research["horizons"])["5"])
    summary = cast(dict[str, object], primary["net_excess_positive"])
    assert summary["probability"] is None
    assert summary["filter_qualified"] is False
    assert summary["pipeline_stage"] == "source_capture_pending"
    assert cache.capture_status_calls == [run.id]


@pytest.mark.parametrize("capture_status", ("pending", "succeeded"))
@pytest.mark.parametrize("failure", ("missing_receipt", "invalid_skip"))
def test_unified_action_source_failure_never_looks_pending(
    capture_status: str,
    failure: str,
) -> None:
    # The cache is the sole read-only projection of the DB verifier. Both a
    # missing canonical replay receipt and invalid persisted skip evidence are
    # represented by no eligible action-source digest.
    assert failure in {"missing_receipt", "invalid_skip"}
    run = _run(action_eligible=True)
    cache = _Cache(
        run,
        capture_status=capture_status,
        action_source_digest=None,
    )
    service = _service(cache)

    research = service.probability_research(run.id)

    assert research["availability"] == "source_scan_action_ineligible"
    assert "pipeline_stage" not in research
    assert research["limitations"] == ["source_scan_action_ineligible"]
    assert cache.action_source_calls == [run.id]
    assert cache.capture_status_calls == []


@pytest.mark.parametrize("capture_status", ("pending", "succeeded"))
def test_missing_action_source_receipt_cannot_consume_existing_calibrated_artifact(
    capture_status: str,
) -> None:
    run = _run(action_eligible=True)
    cache = _Cache(
        run,
        capture_status=capture_status,
        action_source_digest=None,
    )
    store = _ProbabilityStore(
        _calibrated_research(),
        {
            "600519.SH": _probability_horizons(0.81),
        },
    )
    source = _ResearchSource(
        {
            "status": "insufficient_data",
            "run_binding": _run_binding(run),
        }
    )
    service = _service(cache, probability=store, source=source)

    research, probabilities = service.probability_projection(run.id)

    assert research["availability"] == "source_scan_action_ineligible"
    assert probabilities == {}
    assert cache.action_source_calls == [run.id]
    assert cache.capture_status_calls == []
    assert source.calls == []
    assert store.projection_symbols == []


@pytest.mark.parametrize(
    ("capture_status", "availability"),
    (
        ("pending", "source_capture_pending"),
        ("skipped", "source_capture_skipped"),
    ),
)
def test_terminal_capture_state_precedes_existing_calibrated_artifact(
    capture_status: str,
    availability: str,
) -> None:
    run = _run(action_eligible=True)
    cache = _Cache(run, capture_status=capture_status)
    store = _ProbabilityStore(
        _calibrated_research(),
        {
            "600519.SH": _probability_horizons(0.81),
        },
    )
    source = _ResearchSource(
        {
            "status": "insufficient_data",
            "run_binding": _run_binding(run),
        }
    )
    service = _service(cache, probability=store, source=source)

    research, probabilities = service.probability_projection(run.id)

    assert research["availability"] == availability
    assert probabilities == {}
    assert cache.capture_status_calls == [run.id]
    assert source.calls == []
    assert store.projection_symbols == []


@pytest.mark.parametrize(
    ("capture_status", "availability"),
    (
        (None, "source_capture_outbox_missing"),
        ("skipped", "source_capture_skipped"),
    ),
)
def test_probability_projection_distinguishes_non_pending_capture_states(
    capture_status: str | None,
    availability: str,
) -> None:
    run = _run(action_eligible=True)
    service = _service(_Cache(run, capture_status=capture_status))

    research = service.probability_research(run.id)

    assert research["availability"] == availability
    assert "pipeline_stage" not in research


@pytest.mark.parametrize(
    ("action_source_digest", "capture_status", "availability"),
    (
        (None, "succeeded", "source_scan_action_ineligible"),
        ("a" * 64, "pending", "source_capture_pending"),
    ),
)
def test_action_and_capture_gate_precede_every_probability_artifact_read(
    action_source_digest: str | None,
    capture_status: str,
    availability: str,
) -> None:
    run = _run(action_eligible=True)
    cache = _Cache(
        run,
        capture_status=capture_status,
        action_source_digest=action_source_digest,
    )
    service = _service(
        cache,
        probability=_ForbiddenProbabilityStore(),
        source=_ForbiddenResearchSource(),
    )

    assert service.probability_research(run.id)["availability"] == availability
    projected, probabilities = service.probability_projection(run.id)
    assert projected["availability"] == availability
    assert probabilities == {}
    assert _results(service, minimum=None).probability_research["availability"] == availability
    with pytest.raises(ProbabilityFilterUnavailable, match="尚无已校准"):
        _results(service, minimum=0.5)
    assert cache.verified_read_calls == [run.id] * 4
    assert cache.action_source_calls == [run.id] * 4
    assert cache.capture_status_calls == ([] if action_source_digest is None else [run.id] * 4)


def test_succeeded_capture_forces_one_blocking_preload_before_source_reread() -> None:
    run = _run(action_eligible=True)
    archived = {
        "status": "insufficient_data",
        "origin": "source",
        "run_binding": _run_binding(run),
    }
    source = _RacingResearchSource(
        {"status": "not_generated"},
        archived,
    )
    service = _service(_Cache(run, capture_status="succeeded"), source=source)

    assert _without_historical_context(service.probability_research(run.id)) == archived
    assert source.preload_calls == 1
    assert source.calls == [run.id, run.id]


def test_succeeded_capture_never_blocks_on_a_scheduled_source_preload() -> None:
    run = _run(action_eligible=True)
    archived = {
        "status": "insufficient_data",
        "origin": "source",
        "run_binding": _run_binding(run),
    }
    source = _RacingResearchSource(
        {"status": "not_generated"},
        archived,
    )
    source.pending = True
    service = _service(_Cache(run, capture_status="succeeded"), source=source)

    research = service.probability_research(run.id)

    assert research["status"] == "not_generated"
    assert research["availability"] == "source_index_verification_pending"
    assert research["pipeline_stage"] == "source_index_verification_pending"
    summary = cast(
        dict[str, object],
        cast(dict[str, object], research["horizons"])["5"],
    )
    primary = cast(dict[str, object], summary["net_excess_positive"])
    assert primary["probability"] is None
    assert primary["filter_qualified"] is False
    assert primary["pipeline_stage"] == "source_index_verification_pending"
    assert source.preload_calls == 0
    assert source.calls == [run.id]


@pytest.mark.parametrize("broken", (False, True))
def test_probability_research_attaches_non_authorizing_historical_context(
    broken: bool,
) -> None:
    run = _run(action_eligible=True)
    source = _ResearchSource(
        {
            "status": "insufficient_data",
            "run_binding": _run_binding(run),
        }
    )
    historical = _HistoricalProbabilityStore(broken=broken)
    service = _service(
        _Cache(run, capture_status="succeeded"),
        source=source,
        historical=historical,
    )

    context = service.probability_research(run.id)["historical_context"]

    assert context["status"] == ("unavailable" if broken else "ready")  # type: ignore[index]
    assert context["production_ranking_effect"] == "none"  # type: ignore[index]
    assert context["selection_qualified"] is False  # type: ignore[index]
    assert context["filter_qualified"] is False  # type: ignore[index]


@pytest.mark.parametrize("mode,capture_status", (("official", None), ("official", "succeeded"), ("intraday", None)))
@pytest.mark.parametrize("exported", (False, True))
@pytest.mark.parametrize("broken", (False, True))
def test_results_and_export_read_optional_context_catalogs_once(
    mode: str, capture_status: str | None, exported: bool, broken: bool,
) -> None:
    historical = _HistoricalProbabilityStore(broken=broken)
    catalogs = _CountingEvidenceCatalog()
    probability = _ProbabilityStore(_calibrated_research(), {}) if capture_status == "succeeded" else None
    service = _service(
        _Cache(_run(mode=mode), capture_status=capture_status), probability=probability,
        historical=historical, official=catalogs, joint=catalogs,
    )
    if exported:
        page, _future = service.export_projection(29, filters=MarketScanExportFilters())
    else:
        page = _results(service, minimum=None)
    assert historical.calls == catalogs.official_calls == catalogs.joint_calls == 1
    context = cast(dict[str, object], page.probability_research["historical_context"])
    assert context["status"] == ("unavailable" if broken else "ready")
    assert context["filter_qualified"] is False
    assert context["production_ranking_effect"] == "none"
    assert cast(dict[str, object], page.probability_research["official_execution_evidence"])["formal_evidence_available"] is False
    assert cast(dict[str, object], page.probability_research["joint_execution_evidence"])["filter_ready"] is False


@pytest.mark.parametrize("minimum", (None, 0.70))
def test_results_reject_invalid_probability_binding_before_reading_optional_catalogs(minimum: float | None) -> None:
    research = _calibrated_research()
    cast(dict[str, object], research["run_binding"])["quote_date"] = "2026-08-10"
    historical = _HistoricalProbabilityStore()
    catalogs = _CountingEvidenceCatalog()
    cache = _Cache(_run())
    service = _service(
        cache, probability=_ProbabilityStore(research, {}),
        historical=historical, official=catalogs, joint=catalogs,
    )
    with pytest.raises(ProbabilityArtifactError, match="quote_date"):
        _results(service, minimum=minimum)
    assert historical.calls == catalogs.official_calls == catalogs.joint_calls == 0
    assert len(cache.result_queries) == (1 if minimum is None else 0)


def test_succeeded_capture_without_source_artifact_fails_closed() -> None:
    run = _run(action_eligible=True)
    source = _RacingResearchSource(
        {"status": "not_generated"},
        {"status": "not_generated"},
    )
    service = _service(_Cache(run, capture_status="succeeded"), source=source)

    with pytest.raises(ProbabilityArtifactError, match="artifact 缺失"):
        service.probability_research(run.id)


def test_succeeded_capture_rejects_source_archive_digest_mismatch() -> None:
    run = _run(action_eligible=True)
    mismatched = {
        "status": "insufficient_data",
        "run_binding": {
            **_run_binding(run),
            "source_integrity_digest": "d" * 64,
        },
    }
    source = _RacingResearchSource(mismatched, mismatched)
    service = _service(_Cache(run, capture_status="succeeded"), source=source)

    with pytest.raises(ProbabilityArtifactError, match="artifact 缺失"):
        service.probability_research(run.id)

    assert source.preload_calls == 1


def test_oversized_legacy_projection_never_falls_back_or_enters_probability_filter() -> None:
    run = _run(action_eligible=True)
    unavailable = {
        "status": "not_generated",
        "availability": "legacy_artifact_exceeds_interactive_budget",
        "horizons": {},
    }
    source = _ResearchSource(
        {
            "status": "insufficient_data",
            "origin": "source",
            "run_binding": _run_binding(run),
        }
    )
    store = _ProbabilityStore(unavailable, {})
    service = _service(
        _Cache(run, capture_status="succeeded"),
        probability=store,
        source=source,
    )

    projected, probabilities = service.probability_projection(29)
    assert projected["availability"] == "probability_artifact_source_unbound"
    assert probabilities == {}
    assert source.calls == [run.id]
    with pytest.raises(ProbabilityFilterUnavailable, match="尚无已校准 Shadow 概率"):
        _results(service, minimum=0.5)
    assert source.calls == [run.id, run.id]


def test_probability_filter_rejects_self_attested_mapping_even_when_all_checks_are_true() -> None:
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

    with pytest.raises(ProbabilityFilterUnavailable, match="完整统计、校准、漂移与执行门禁"):
        service.results(
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

    assert cache.result_queries == []
    assert store.projection_symbols == [None]


def test_joint_opaque_store_projection_is_preferred_and_really_filters_symbols() -> None:
    run = _run()
    historical = _HistoricalProbabilityStore()
    context_calls: list[int] = []
    summary = {
        "status": "calibrated_shadow",
        "selection_qualified": True,
        "selection_qualification": {"passed": True},
        "filter_qualified": True,
        "probability": None,
        "horizon": 5,
    }
    research = {
        "schema_version": "market-scan-joint-execution-current-projection-v1",
        "status": "calibrated_shadow",
        "authority_backend": "joint_execution_opaque_v1",
        "run_binding": _run_binding(run),
        "horizons": {"1": {}, "5": {"net_excess_positive": summary}, "20": {}},
    }
    probabilities = {
        "600519.SH": _probability_horizons(0.81),
        "000001.SZ": _probability_horizons(0.69),
    }

    class _JointStore:
        @staticmethod
        def has_current_projection(run_id: int) -> bool:
            return run_id == run.id

        @staticmethod
        def research_projection(run_id: int) -> dict[str, object]:
            assert run_id == run.id
            return research

        @staticmethod
        def run_projection(
            run_id: int,
            *,
            symbols: tuple[str, ...] | None = None,
        ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
            assert run_id == run.id
            selected = set(symbols) if symbols is not None else None
            return research, {
                symbol: value
                for symbol, value in probabilities.items()
                if selected is None or symbol in selected
            }

        @staticmethod
        def filter_qualified(run_id: int) -> bool:
            return run_id == run.id

        @staticmethod
        def status_projection() -> dict[str, object]:
            context_calls.append(run.id)
            return {"status": "current_prediction_ready", "filter_ready": True}

    cache = _Cache(run, capture_status="succeeded")
    service = _service(cache, joint=_JointStore(), historical=historical)
    page = _results(service, minimum=0.70)

    assert [item.symbol for item in page.items] == ["600519.SH"]
    assert cache.result_queries[-1]["symbols"] == ("600519.SH",)
    assert page.probability_research["authority_backend"] == "joint_execution_opaque_v1"
    assert historical.calls == 1
    assert context_calls == [run.id]


class _ForbiddenAuthorityCatalog:
    registry_digest = "a" * 64

    def status(self) -> OfficialExecutionStoreStatus:
        pytest.fail("maintenance pending/failed must not deep-read the official store")

    def research_projection(self) -> dict[str, object]:
        pytest.fail("maintenance pending/failed must not reuse the old historical context")


def _maintenance_store(
    tmp_path: Path,
) -> MarketScanJointExecutionMaintenanceService:
    return MarketScanJointExecutionMaintenanceService(
        cast(Any, SimpleNamespace(path=tmp_path / "runtime.sqlite3")),
        cast(Any, _ForbiddenAuthorityCatalog()),
        ranking_store=cast(Any, object()),
    )


@pytest.mark.parametrize("status", ["maintenance_pending", "maintenance_failed"])
@pytest.mark.parametrize("capture_status", ["succeeded", "pending"])
def test_maintenance_unavailable_queries_never_fall_back_to_old_authority_or_official_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, capture_status: str,
) -> None:
    joint = _maintenance_store(tmp_path)
    if status == "maintenance_pending":
        joint._maintenance_running = True
    else:
        def fail(_now: object) -> object:
            raise RuntimeError("maintenance failed")
        monkeypatch.setattr(joint, "_run_locked", fail)
        with pytest.raises(RuntimeError, match="maintenance failed"):
            joint.run()
    cache = _Cache(_run(), capture_status=capture_status)
    catalog = _ForbiddenAuthorityCatalog()
    service = _service(
        cache, joint=joint, probability=_ForbiddenProbabilityStore(),
        source=_ForbiddenResearchSource(), official=catalog, historical=catalog,
    )

    research = service.probability_research(29)
    projected, probabilities = service.probability_projection(29)
    page = _results(service, minimum=None)
    exported, _future = service.export_projection(29, filters=MarketScanExportFilters())
    for evidence in (research, projected, page.probability_research, exported.probability_research):
        assert evidence["status"] == "not_generated"
        assert evidence["availability"] == evidence["pipeline_stage"] == status
        assert evidence["filter_qualified"] is False
        assert evidence["run_binding"] is None
        assert evidence["official_execution_evidence"]["formal_evidence_available"] is False
        assert evidence["official_execution_evidence"]["verified_session_count"] == 0
        assert evidence["joint_execution_evidence"]["filter_ready"] is False
        assert evidence["historical_context"]["availability"] == status
        for targets in evidence["horizons"].values():
            for summary in targets.values():
                assert summary["status"] == "not_generated"
                assert summary["filter_qualified"] is False
                assert summary["probability"] is None
    assert probabilities == {}
    assert all(item.upside_probabilities == {} for item in page.items + exported.items)
    assert page.production_ranking["status"] == exported.production_ranking["status"] == "inactive"
    assert all(item.base_production_rank is None for item in page.items + exported.items)
    cache.result_queries.clear()
    with pytest.raises(ProbabilityFilterUnavailable, match="尚无已校准 Shadow 概率"):
        _results(service, minimum=0.5)
    with pytest.raises(ProbabilityFilterUnavailable, match="尚无已校准 Shadow 概率"):
        service.export_projection(29, filters=MarketScanExportFilters(min_upside_probability=0.5))
    assert cache.result_queries == []


def test_maintenance_starting_after_probability_projection_discards_old_records(
    tmp_path: Path,
) -> None:
    joint = _maintenance_store(tmp_path)
    pending = joint._maintenance_summary("maintenance_pending").payload()

    class _RacingJoint:
        @staticmethod
        def run_projection(_run_id: int, **_query: object) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
            return _calibrated_research(), {"600519.SH": _probability_horizons(0.9)}

        @staticmethod
        def status_projection() -> dict[str, object]:
            return pending

    service = _service(
        _Cache(_run(), capture_status="succeeded"), joint=_RacingJoint(),
        probability=_ForbiddenProbabilityStore(), source=_ForbiddenResearchSource(),
        official=_ForbiddenAuthorityCatalog(), historical=_ForbiddenAuthorityCatalog(),
    )
    research, records = service.probability_projection(29)
    assert research["availability"] == "maintenance_pending"
    assert research["status"] == "not_generated" and research["run_binding"] is None
    assert records == {}


def test_maintenance_pending_after_old_ranking_capture_restores_base_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    joint = _maintenance_store(tmp_path)
    joint._maintenance_running = True
    cache = _Cache(_run(), capture_status="succeeded")
    service = _service(
        cache, joint=joint, probability=_ForbiddenProbabilityStore(),
        source=_ForbiddenResearchSource(), official=_ForbiddenAuthorityCatalog(),
    )
    monkeypatch.setattr(service, "_production_ranking_projection", lambda _run_id: ({"status": "active"}, {}))

    def old_ranking(verified: Any, *, query: dict[str, object], symbols: object, **_extra: object) -> MarketScanResultPage:
        page = verified.results_page(**query, symbols=symbols)
        return page.model_copy(update={
            "items": [item.model_copy(update={"score": 99, "base_production_rank": 1}) for item in page.items],
            "production_ranking": {"status": "active"},
        })

    monkeypatch.setattr(query_service_module, "_results_page_with_production_ranking", old_ranking)
    page = _results(service, minimum=None)
    assert page.probability_research["availability"] == "maintenance_pending"
    assert page.production_ranking["status"] == "inactive"
    assert page.production_ranking["reason"] == "maintenance_pending"
    assert [(item.score, item.base_production_rank) for item in page.items] == [(80, None)]
    assert all(item.upside_probabilities == {} for item in page.items)
    assert len(cache.result_queries) == 2 and cache.result_queries[0] == cache.result_queries[1]
    assert cache.verified_read_calls == [29]


def test_active_v6_ranking_reorders_filters_and_preserves_v5_fields() -> None:
    run = _run()

    class _RankingCache(_Cache):
        def market_scan_results(
            self,
            run_id: int,
            **query: object,
        ) -> MarketScanResultPage:
            assert run_id == run.id
            self.result_queries.append(query)
            items = [
                _result(run_id, "600519.SH").model_copy(
                    update={"rank": 1, "score": 80, "raw_score": 80.0}
                ),
                _result(run_id, "000001.SZ").model_copy(
                    update={"rank": 2, "score": 79, "raw_score": 79.0}
                ),
            ]
            requested = cast(tuple[str, ...] | None, query["symbols"])
            if requested is not None:
                selected = set(requested)
                items = [item for item in items if item.symbol in selected]
            return MarketScanResultPage(
                run=run,
                items=items,
                total=len(items),
                page=cast(int, query["page"]),
                page_size=cast(int, query["page_size"]),
                page_count=1,
            )

    records = {
        "600519.SH": _ranking_record(
            "600519.SH",
            base_rank=1,
            base_score=80,
            base_raw_score=80.0,
            rank=2,
            score=74,
            raw_score=74.0,
            adjustment=-6.0,
        ),
        "000001.SZ": _ranking_record(
            "000001.SZ",
            base_rank=2,
            base_score=79,
            base_raw_score=79.0,
            rank=1,
            score=85,
            raw_score=85.0,
            adjustment=6.0,
        ),
    }

    class _RankingStore:
        @staticmethod
        def has_current_projection(_run_id: int) -> bool:
            return False

        @staticmethod
        def status_projection() -> dict[str, object]:
            return {"status": "ranking_active", "filter_ready": False}

        @staticmethod
        def production_ranking_projection(
            run_id: int,
        ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
            assert run_id == run.id
            return {
                "contract_version": "market-scan-probability-ranking-projection-v1",
                "status": "active",
                "run_id": run_id,
                "score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
                "score_spec_hash": probability_ranking_score_spec_hash(),
                "artifact_digest": "d" * 64,
                "promotion_digest": "e" * 64,
                "generated_at": "2026-08-11T16:11:00+08:00",
                "record_count": 2,
                "base_snapshot_digest": run.snapshot_digest,
                "base_v5_mutated": False,
                "historical_ranks_mutated": False,
                "rollback_available": True,
            }, records

    cache = _RankingCache(
        run,
        score_contract=MarketScanProductionScoreContract(
            FULL_MARKET_SCORE_RULE_VERSION,
            "f" * 64,
            run.success_count,
        ),
    )
    page = _service(cache, joint=_RankingStore()).results(
        run.id,
        page=1,
        page_size=1,
        status="success",
        market=None,
        industry=None,
        is_st=None,
        is_new=None,
        min_score=80,
        min_data_quality_score=None,
        keyword=None,
        sort="rank",
        order="asc",
    )

    assert [item.symbol for item in page.items] == ["000001.SZ"]
    assert page.total == 1
    assert page.production_ranking is not None
    assert page.production_ranking["status"] == "active"
    result = page.items[0]
    assert (result.rank, result.score, result.raw_score) == (1, 85, 85.0)
    assert (
        result.base_production_rank,
        result.base_production_score,
        result.base_production_raw_score,
    ) == (2, 79, 79.0)
    assert result.production_score_rule_version == PROBABILITY_RANKING_SCORE_RULE_VERSION
    assert result.probability_ranking_adjustment == 6.0
    assert result.probability_ranking_artifact_digest == "d" * 64
    assert cache.result_queries == [
        {
            "page": 1,
            "page_size": run.total_count,
            "status": "success",
            "market": None,
            "industry": None,
            "is_st": None,
            "is_new": None,
            "min_score": None,
            "max_score": None,
            "min_trend_score": None,
            "max_trend_score": None,
            "min_change_pct": None,
            "max_change_pct": None,
            "min_turnover_rate": None,
            "max_turnover_rate": None,
            "min_amount": None,
            "max_amount": None,
            "min_data_quality_score": None,
            "max_data_quality_score": None,
            "min_confidence": None,
            "max_risk": None,
            "min_tradability": None,
            "keyword": None,
            "sort": "rank",
            "order": "asc",
            "symbols": None,
        }
    ]


@pytest.mark.parametrize("direction", ("asc", "desc"))
@pytest.mark.parametrize(
    "field",
    (
        "rank", "score", "raw_score", "trend_score", "change_pct", "amount",
        "turnover_rate", "data_quality_score", "alpha_5d", "confidence", "risk", "tradability",
    ),
)
def test_v6_sort_matches_frozen_sql_with_missing_values_and_ties(field: str, direction: str) -> None:
    rows = _production_sort_rows(field)
    _assert_production_sort_matches_sql(rows, [(field, direction)])


@pytest.mark.parametrize("directions", (("asc", "desc"), ("desc", "asc"), ("desc", "desc")))
def test_v6_multilevel_sort_keeps_missing_values_last_before_paging(directions: tuple[str, str]) -> None:
    rows = _production_sort_rows("amount")
    _assert_production_sort_matches_sql(rows, [("amount", directions[0]), ("raw_score", directions[1])])


def _production_sort_rows(field: str) -> list[MarketScanResultItem]:
    rows: list[MarketScanResultItem] = []
    for index, (value, raw_score) in enumerate(((10, 80), (None, 90), (20, 70), (None, None), (10, None), (10, 80))):
        updates: dict[str, object] = {"raw_score": raw_score}
        if field in {"alpha_5d", "confidence", "risk", "tradability"}:
            updates["score_details"] = {"components": {"score_dimensions": {"scores": {field: value}}}}
        else:
            updates[field] = value
        rows.append(_result(29, f"60000{index}.SH").model_copy(update=updates))
    return rows


def _assert_production_sort_matches_sql(rows: list[MarketScanResultItem], sorts: list[tuple[str, str]]) -> None:
    spec = ScreenSpecV2(sort=[ScreenSortV2.model_validate({"field": field, "order": order}) for field, order in sorts])
    columns = ("symbol", "rank", "score", "raw_score", "trend_score", "change_pct", "amount", "turnover_rate", "data_quality_score")
    with sqlite3.connect(":memory:") as connection:
        connection.execute(f"CREATE TABLE results ({','.join(columns)}, metrics_json)")
        connection.executemany(
            f"INSERT INTO results VALUES ({','.join('?' for _ in range(len(columns) + 1))})",
            [tuple(getattr(item, column) for column in columns) + (json.dumps({"score_details": item.score_details}),) for item in rows],
        )
        expected = [row[0] for row in connection.execute(f"SELECT symbol FROM results ORDER BY {screen_spec_order_sql(spec)}")]
    def compare(left: MarketScanResultItem, right: MarketScanResultItem) -> int:
        return query_service_module._compare_production_ranking_items(  # noqa: SLF001
            left, right, sort=tuple(field for field, _ in sorts), order=tuple(order for _, order in sorts),
        )
    actual = sorted(rows, key=cmp_to_key(compare))
    assert [item.symbol for item in actual] == expected
    # Paginated results and the full export share this ordering; page boundaries must not move missing data to the front.
    assert [[item.symbol for item in actual[start : start + 2]] for start in range(0, len(actual), 2)] == [
        expected[start : start + 2] for start in range(0, len(expected), 2)
    ]


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
        {"status": "insufficient_data", "horizons": [], "run_binding": _run_binding()},
        {"status": "insufficient_data", "horizons": {"5": []}, "run_binding": _run_binding()},
        {"status": "insufficient_data", "horizons": {"5": {"net_excess_positive": None}}, "run_binding": _run_binding()},
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

    with pytest.raises(ProbabilityFilterUnavailable, match="完整统计、校准、漂移与执行门禁"):
        _results(service, minimum=0.5)


def test_probability_projection_rejects_run_binding_mismatch() -> None:
    research = _calibrated_research()
    binding = cast(dict[str, object], research["run_binding"])
    binding["quote_date"] = "2026-08-10"
    service = _service(_Cache(_run()), probability=_ProbabilityStore(research, {}))

    with pytest.raises(ProbabilityArtifactError, match="quote_date"):
        service.probability_projection(29)


def test_probability_projection_rejects_missing_or_mismatched_db_score_contract() -> None:
    run = _run()
    research = _calibrated_research()
    missing = _service(_Cache(run, score_contract=None), probability=_ProbabilityStore(research, {}))
    missing._cache.score_contract = None  # type: ignore[attr-defined]  # noqa: SLF001
    with pytest.raises(ProbabilityArtifactError, match="生产评分合同"):
        missing.probability_projection(29)

    wrong = MarketScanProductionScoreContract("full-market-score-v4", "c" * 64, run.success_count)
    mismatched = _service(_Cache(run, score_contract=wrong), probability=_ProbabilityStore(research, {}))
    with pytest.raises(ProbabilityArtifactError, match="production_score_spec_hash"):
        mismatched.probability_projection(29)


def test_legacy_bound_probability_is_replaced_by_source_and_never_filtered() -> None:
    research = _calibrated_research()
    research["run_binding"] = _run_binding(legacy=True)
    service = _service(_Cache(_run()), probability=_ProbabilityStore(research, {}))

    projected, _records = service.probability_projection(29)
    assert projected["availability"] == "probability_artifact_source_unbound"
    assert cast(dict[str, object], projected["run_binding"])["legacy"] is False
    with pytest.raises(ProbabilityFilterUnavailable, match="尚无已校准 Shadow 概率"):
        _results(service, minimum=0.5)


@pytest.mark.parametrize("mutation", ("missing", "mismatched"))
def test_probability_artifact_source_digest_is_authoritative_before_projection(
    mutation: str,
) -> None:
    research = _calibrated_research()
    binding = cast(dict[str, object], research["run_binding"])
    if mutation == "missing":
        binding.pop("source_integrity_digest")
    else:
        binding["source_integrity_digest"] = "d" * 64
    service = _service(
        _Cache(_run(), capture_status="succeeded"),
        probability=_ProbabilityStore(
            research,
            {"600519.SH": _probability_horizons(0.81)},
        ),
    )

    projected, probabilities = service.probability_projection(29)

    assert projected["availability"] == "probability_artifact_source_unbound"
    assert probabilities == {}


def test_built_artifact_store_and_query_preserve_source_archive_commit(
    tmp_path,
) -> None:
    from app.services.market_scan_probability_store import MarketScanProbabilityStore
    from tests.test_market_scan_probability import (
        _STORE_RULE_VERSION,
        _write_store_artifact,
    )

    directory, _database, _target = _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-run-29-builder.json",
        generated_at="2026-08-11T10:00:00+00:00",
        status="calibrated_shadow",
        probability=0.66,
    )
    run = _run().model_copy(
        update={
            "quote_date": "2026-07-31",
            "data_date": "2026-07-31",
            "rule_version": _STORE_RULE_VERSION,
        },
    )
    source = _ResearchSource(
        {
            "status": "insufficient_data",
            "run_binding": _run_binding(run),
        }
    )
    service = _service(
        _Cache(run, capture_status="succeeded"),
        probability=MarketScanProbabilityStore(directory),
        source=source,
    )

    research, probabilities = service.probability_projection(29)

    binding = cast(dict[str, object], research["run_binding"])
    assert binding["source_integrity_digest"] == "c" * 64
    assert binding["binding_status"] == "verified"
    assert probabilities["600519.SH"]["5"]["net_excess_positive"]["probability"] == 0.66


def test_ineligible_run_is_resolved_before_any_legacy_artifact_read() -> None:
    store = _ProbabilityStore(_calibrated_research(), {})
    historical = _HistoricalProbabilityStore()
    service = _service(
        _Cache(_run(mode="intraday")),
        probability=store,
        historical=historical,
    )

    with pytest.raises(ProbabilityResearchUnavailable, match="盘后正式"):
        service.probability_research(29)
    page = _results(service, minimum=None)
    assert page.probability_research["availability"] == "ineligible_run_contract"
    context = cast(dict[str, object], page.probability_research["historical_context"])
    assert context["status"] == "ready"
    assert context["filter_qualified"] is False
    assert store.projection_symbols == []


def test_ineligible_run_cannot_enter_probability_filter_before_artifact_read() -> None:
    run = _run().model_copy(update={"scope": "top100-refresh"})
    store = _ProbabilityStore(_calibrated_research(), {})
    service = _service(_Cache(run), probability=store)

    with pytest.raises(ProbabilityFilterUnavailable, match="已发布的盘后正式全市场"):
        _results(service, minimum=0.5)

    assert store.projection_symbols == []


def test_legacy_backfill_run_cannot_authorize_probability_or_future_range_artifacts() -> None:
    run = _run().model_copy(update={"snapshot_seal_origin": "legacy_backfill"})
    probability = _ProbabilityStore(_calibrated_research(), {})
    future_calls: list[tuple[int, dict[str, object]]] = []
    service = _service(
        _Cache(run),
        probability=probability,
        future_range=_FutureRangeStore(future_calls),
    )

    with pytest.raises(ProbabilityResearchUnavailable, match="原发布时快照封印"):
        service.probability_projection(29)
    with pytest.raises(ProbabilityFilterUnavailable, match="已发布的盘后正式全市场"):
        _results(service, minimum=0.5)
    with pytest.raises(FutureRangeResearchUnavailable, match="原发布时快照封印"):
        service.future_range_research(
            29,
            page=1,
            page_size=20,
            session_offset=None,
            symbol=None,
            include_research=True,
        )

    assert probability.projection_symbols == []
    assert future_calls == []


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"scope": "top100-refresh"}, "正式全市场"),
        ({"status": "running"}, "已发布批次"),
        ({"quote_date": "2026-08-10"}, "行情日期与完整日K截止日一致"),
    ),
)
def test_probability_research_rejects_each_ineligible_run_contract_dimension(
    updates: dict[str, object],
    message: str,
) -> None:
    run = _run().model_copy(update=updates)
    store = _ProbabilityStore(_calibrated_research(), {})
    service = _service(_Cache(run), probability=store)

    with pytest.raises(ProbabilityResearchUnavailable, match=message):
        service.probability_research(29)

    assert store.projection_symbols == []


@pytest.mark.parametrize("case", ("missing", "invalid_status"))
def test_probability_projection_replaces_unbound_artifact_fail_closed(case: str) -> None:
    research = _calibrated_research()
    if case == "missing":
        research.pop("run_binding")
    else:
        cast(dict[str, object], research["run_binding"])["binding_status"] = "unknown"
    service = _service(_Cache(_run()), probability=_ProbabilityStore(research, {}))

    projected, probabilities = service.probability_projection(29)

    assert projected["availability"] == "probability_artifact_source_unbound"
    assert probabilities == {}


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


def _results(service: MarketScanQueryService, *, minimum: float | None) -> MarketScanResultPage:
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
    historical: object | None = None,
    official: object | None = None,
    joint: object | None = None,
) -> MarketScanQueryService:
    if probability is not None and source is None:
        # Existing probability artifacts in these fixtures represent a source
        # capture that completed and is still present in the read-only index.
        cache.capture_status = "succeeded"
        source = _ResearchSource(
            {
                "status": "insufficient_data",
                "run_binding": _run_binding(cache.current_run),
            }
        )
    stores = MarketScanResearchStores(
        probability=cast(Any, probability),
        probability_source=cast(Any, source),
        future_range=cast(Any, future_range),
        historical_probability=cast(Any, historical),
        official_execution=cast(Any, official),
        joint_probability=cast(Any, joint),
    )
    return MarketScanQueryService(cast(Any, cache), stores)


def _without_historical_context(value: dict[str, object]) -> dict[str, object]:
    return {
        key: item
        for key, item in value.items()
        if key
        not in {
            "historical_context",
            "official_execution_evidence",
            "joint_execution_evidence",
        }
    }


def _run(*, mode: str = "official", action_eligible: bool = True) -> MarketScanRun:
    return MarketScanRun(
        id=29,
        status="success",
        trigger="manual",
        mode=mode,
        rule_version=f"full-market-scan-v6:{'a' * 64}",
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
        market_progress=[
            {"market": "SH", "total_count": 1, "processed_count": 1, "success_count": 1, "coverage_pct": 100},
            {"market": "SZ", "total_count": 1, "processed_count": 1, "success_count": 1, "coverage_pct": 100},
            {"market": "BJ", "total_count": 0, "processed_count": 0, "success_count": 0, "coverage_pct": 0},
        ],
        finished_at="2026-08-11 16:10:00",
        duration_ms=600_000,
        snapshot_digest="a" * 64,
        snapshot_seal_origin="publication",
        snapshot_sealed_at="2026-08-11 16:10:00",
        created_at="2026-08-11 16:00:00",
        updated_at="2026-08-11 16:10:00",
        publication_diagnostics=(
            MarketScanPublicationDiagnostics(
                headline="评分分布通过",
                passed_gates=[
                    MarketScanPublicationDiagnostic(
                        code="score_distribution.pass",
                        label="评分分布",
                        detail="测试评分分布通过",
                        severity="info",
                    )
                ],
            )
            if action_eligible
            else MarketScanPublicationDiagnostics(
                headline="评分分布未通过",
                source_warnings=[
                    MarketScanPublicationDiagnostic(
                        code="score_distribution.degraded",
                        label="评分分布",
                        detail="测试评分分布未通过",
                        severity="warning",
                    )
                ],
            )
        ),
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
        rank=1,
        score=80,
        raw_score=80,
        trend_score=80,
        leader_score=80,
        data_quality_score=100,
        price=100,
        data_date="2026-08-11",
        quote_timestamp="2026-08-11T15:00:00+08:00",
        quote_observed_at="2026-08-11T15:00:01+08:00",
        quote_source="test",
        kline_source="test",
        adjustment_mode="qfq",
        updated_at="2026-08-11 16:10:00",
    )


def _ranking_record(
    symbol: str,
    *,
    base_rank: int,
    base_score: int,
    base_raw_score: float,
    rank: int,
    score: int,
    raw_score: float,
    adjustment: float,
) -> dict[str, object]:
    return {
        "run_id": 29,
        "symbol": symbol,
        "base_rank": base_rank,
        "base_score": base_score,
        "base_raw_score": base_raw_score,
        "probability": 0.8 if adjustment > 0 else 0.2,
        "reference_base_rate": 0.5,
        "probability_adjustment": adjustment,
        "rank": rank,
        "score": score,
        "raw_score": raw_score,
        "score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
        "score_spec_hash": probability_ranking_score_spec_hash(),
        "source_record_digest": "1" * 64,
        "prediction_record_digest": "2" * 64,
        "record_digest": ("3" if adjustment > 0 else "4") * 64,
    }


def _calibrated_research() -> dict[str, object]:
    current = _current_selection_summary()
    current["filter_qualification"] = _filter_authorization(current)
    return {
        "status": "calibrated_shadow",
        "run_binding": _run_binding(),
        "horizons": {"5": {"net_excess_positive": current}},
    }


def _current_selection_summary() -> dict[str, object]:
    from app.services.market_scan_probability import (
        PROBABILITY_FEATURE_VERSION,
        PROBABILITY_LABEL_VERSION,
        PROBABILITY_MODEL_VERSION,
        PROBABILITY_SCHEMA_VERSION,
        PROBABILITY_SPLIT_VERSION,
    )

    metrics = {
        "calibrated": {
            "brier_improvement_vs_reference_ci_95": [0.01, 0.03],
            "log_loss_improvement_vs_reference_ci_95": [0.01, 0.04],
            "ece": 0.03,
        }
    }
    evidence = {
        "schema_version": PROBABILITY_SCHEMA_VERSION,
        "model_version": PROBABILITY_MODEL_VERSION,
        "feature_version": PROBABILITY_FEATURE_VERSION,
        "label_version": PROBABILITY_LABEL_VERSION,
        "horizon": 5,
        "status": "calibrated_shadow",
        "selection_qualified": True,
        "selection_qualification": {"passed": True},
        "target_definition": "future_5d_net_excess_return_gt_0_after_costs",
        "calibration_metrics": metrics,
        "contract": {
            "split": {"version": PROBABILITY_SPLIT_VERSION},
            "label": {"target_session_offset": 6},
        },
    }
    unsigned = {key: value for key, value in evidence.items() if key != "evidence_digest"}
    evidence["evidence_digest"] = stable_probability_hash(unsigned)
    return evidence


def _filter_authorization(evidence: dict[str, object]) -> dict[str, object]:
    digest = str(evidence["evidence_digest"])
    metrics_digest = stable_probability_hash(evidence["calibration_metrics"])
    raw_sections = {
        "promotion_gates": {
            "version": "gates-v1",
            "passed": True,
            "evidence_digest": digest,
            "gates": {
                name: True
                for name in (
                    "calibrated_shadow",
                    "selection_qualified",
                    "label_coverage_at_least_95pct",
                    "point_in_time_evidence_at_least_95pct",
                    "deterministic_replay_verified",
                )
            },
        },
        "multiple_testing": {
            "version": "fdr-v1",
            "passed": True,
            "evidence_digest": digest,
            "method": "benjamini_hochberg_fdr",
            "alpha": 0.05,
            "adjusted_p_value": 0.01,
            "family_size": 6,
            "checks": {
                name: True
                for name in (
                    "family_registered",
                    "all_horizon_target_candidates_included",
                    "adjusted_significance_passed",
                )
            },
        },
        "calibration": {
            "version": "cal-v1",
            "passed": True,
            "evidence_digest": digest,
            "metrics_digest": metrics_digest,
            "independent_session_count": 60,
            "checks": {
                name: True
                for name in (
                    "proper_score_ci_passed",
                    "ece_threshold_passed",
                    "calibration_slope_ci_contains_one",
                    "calibration_intercept_ci_contains_zero",
                )
            },
        },
        "drift": {
            "version": "drift-v1",
            "passed": True,
            "evidence_digest": digest,
            "independent_session_count": 60,
            "checks": {
                name: True
                for name in (
                    "feature_drift_passed",
                    "probability_drift_passed",
                    "performance_drift_passed",
                )
            },
        },
        "execution": {
            "version": "exec-v1",
            "passed": True,
            "evidence_digest": digest,
            "independent_session_count": 60,
            "checks": {
                name: True
                for name in (
                    "net_excess_return_positive",
                    "turnover_within_limit",
                    "drawdown_within_limit",
                    "capacity_coverage_passed",
                )
            },
        },
    }
    sections = {name: {**value, "integrity_digest": stable_probability_hash(value)} for name, value in raw_sections.items()}
    authorization: dict[str, object] = {
        "version": PROBABILITY_FILTER_AUTHORIZATION_VERSION,
        "evidence_digest": digest,
        "metrics_digest": metrics_digest,
        "horizon": evidence["horizon"],
        "target_definition": evidence["target_definition"],
        **sections,
    }
    authorization["integrity_digest"] = stable_probability_hash(authorization)
    return authorization


def _probability_horizons(value: object, *, status: str = "calibrated_shadow") -> dict[str, object]:
    return {
        "5": {
            "net_excess_positive": {
                "status": status,
                "probability": value,
            }
        }
    }
