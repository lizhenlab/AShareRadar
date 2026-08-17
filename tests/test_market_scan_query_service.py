from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
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
from app.services.market_scan_future_range_store import FutureRangeResearchUnavailable
from app.services.market_scan_probability import (
    PROBABILITY_FILTER_AUTHORIZATION_VERSION,
    stable_probability_hash,
)
from app.services.market_scan_probability_artifact import ProbabilityArtifactError
from app.services.market_scan_probability_store import (
    ProbabilityFilterUnavailable,
    ProbabilityResearchUnavailable,
)
from app.services.market_scan_query_service import MarketScanQueryService
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
            "full-market-score-v4", "b" * 64, run.success_count,
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
        return self.research, {
            symbol: self.probabilities[symbol]
            for symbol in symbols
            if symbol in self.probabilities
        }


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

    def research_projection(self, run_id: int) -> dict[str, object]:
        self.calls.append(run_id)
        return self.projection

    def preload(self) -> int:
        self.preload_calls += 1
        self.projection = self.after
        return 1


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
    ).items == []
    assert service.run_identities(
        page=1,
        page_size=100,
        mode="intraday",
        status="published",
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
    run = _run(action_eligible=True)
    source = _ResearchSource({
        "status": "insufficient_data",
        "origin": "source",
        "run_binding": _run_binding(run),
    })
    missing_store = _ProbabilityStore({"status": "not_generated"}, {})
    service = _service(_Cache(run, capture_status="succeeded"), probability=missing_store, source=source)

    assert service.probability_research(29) == source.projection
    assert service.probability_projection(29) == (source.projection, {})
    assert source.calls == [29, 29]

    calibrated = {"status": "calibrated_shadow", "run_binding": _run_binding(run)}
    calibrated_store = _ProbabilityStore(calibrated, {})
    service = _service(_Cache(run, capture_status="succeeded"), probability=calibrated_store, source=source)
    assert service.probability_projection(29) == (calibrated, {})
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
    store = _ProbabilityStore(_calibrated_research(), {
        "600519.SH": _probability_horizons(0.81),
    })
    source = _ResearchSource({
        "status": "insufficient_data",
        "run_binding": _run_binding(run),
    })
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
    store = _ProbabilityStore(_calibrated_research(), {
        "600519.SH": _probability_horizons(0.81),
    })
    source = _ResearchSource({
        "status": "insufficient_data",
        "run_binding": _run_binding(run),
    })
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
    assert cache.capture_status_calls == (
        [] if action_source_digest is None else [run.id] * 4
    )


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

    assert service.probability_research(run.id) == archived
    assert source.preload_calls == 1
    assert source.calls == [run.id, run.id]


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
    source = _ResearchSource({
        "status": "insufficient_data",
        "origin": "source",
        "run_binding": _run_binding(run),
    })
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
    source = _ResearchSource({
        "status": "insufficient_data",
        "run_binding": _run_binding(run),
    })
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
    service = _service(_Cache(_run(mode="intraday")), probability=store)

    with pytest.raises(ProbabilityResearchUnavailable, match="盘后正式"):
        service.probability_research(29)
    page = _results(service, minimum=None)
    assert page.probability_research["availability"] == "ineligible_run_contract"
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
) -> MarketScanQueryService:
    if probability is not None and source is None:
        # Existing probability artifacts in these fixtures represent a source
        # capture that completed and is still present in the read-only index.
        cache.capture_status = "succeeded"
        source = _ResearchSource({
            "status": "insufficient_data",
            "run_binding": _run_binding(cache.current_run),
        })
    stores = MarketScanResearchStores(
        probability=cast(Any, probability),
        probability_source=cast(Any, source),
        future_range=cast(Any, future_range),
    )
    return MarketScanQueryService(cast(Any, cache), stores)


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


def _calibrated_research() -> dict[str, object]:
    current = _current_selection_summary()
    current["filter_qualification"] = _filter_authorization(current)
    return {
        "status": "calibrated_shadow",
        "run_binding": _run_binding(),
        "horizons": {
            "5": {
                "net_excess_positive": current
            }
        },
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
            "version": "gates-v1", "passed": True, "evidence_digest": digest,
            "gates": {name: True for name in (
                "calibrated_shadow", "selection_qualified", "label_coverage_at_least_95pct",
                "point_in_time_evidence_at_least_95pct", "deterministic_replay_verified",
            )},
        },
        "multiple_testing": {
            "version": "fdr-v1", "passed": True, "evidence_digest": digest,
            "method": "benjamini_hochberg_fdr", "alpha": 0.05,
            "adjusted_p_value": 0.01, "family_size": 6,
            "checks": {name: True for name in (
                "family_registered", "all_horizon_target_candidates_included",
                "adjusted_significance_passed",
            )},
        },
        "calibration": {
            "version": "cal-v1", "passed": True, "evidence_digest": digest,
            "metrics_digest": metrics_digest, "independent_session_count": 60,
            "checks": {name: True for name in (
                "proper_score_ci_passed", "ece_threshold_passed",
                "calibration_slope_ci_contains_one", "calibration_intercept_ci_contains_zero",
            )},
        },
        "drift": {
            "version": "drift-v1", "passed": True, "evidence_digest": digest,
            "independent_session_count": 60,
            "checks": {name: True for name in (
                "feature_drift_passed", "probability_drift_passed", "performance_drift_passed",
            )},
        },
        "execution": {
            "version": "exec-v1", "passed": True, "evidence_digest": digest,
            "independent_session_count": 60,
            "checks": {name: True for name in (
                "net_excess_return_positive", "turnover_within_limit",
                "drawdown_within_limit", "capacity_coverage_passed",
            )},
        },
    }
    sections = {
        name: {**value, "integrity_digest": stable_probability_hash(value)}
        for name, value in raw_sections.items()
    }
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
