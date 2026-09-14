from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api.deps import get_datahub
from app.api.routes.fuyao import router
from app.config import Settings
from app.models.fuyao_research import FuyaoObservation
from app.models.fuyao_scoring import FuyaoValuationScore
from app.repositories.fuyao_research import FuyaoResearchRepository
from app.services.fuyao_scoring import build_fuyao_valuation_score
from app.services.fuyao_service import FuyaoService
from app.utils.audit_time import audit_datetime_to_text, parse_audit_time


CUTOFF = "2026-09-12T10:00:00+08:00"
FETCHED = "2026-09-11T15:00:00+08:00"


def observation(*, pe=10, pb=2, fetched=FETCHED, batch=None):
    payload = {"symbol": "600519.SH", "values": {"pe_ttm": pe, "pb_mrq": pb, "pe_mrq": 9999},
               "batch_timestamp": batch, "individual_timestamp": None}
    return rehash(FuyaoObservation(id=1, capability="valuations", symbol="600519.SH", fetched_at=fetched,
                                  digest="pending", payload=payload))


def rehash(record):
    encoded = json.dumps(record.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return record.model_copy(update={"digest": hashlib.sha256(encoded.encode()).hexdigest()})


@pytest.mark.parametrize("pe,pb,expected", [
    (10, 2, 69), (25, 3, 55), (60, 8, 55), (61, 9, 41), (-5, -1, 41),
    (None, 2, 61), (10, None, 63), (-5, None, 47), (None, -1, 49), (0, 2, 61), (10, 0, 63),
    (None, None, None), (0, 0, None), (0, None, None),
])
def test_explicit_basis_contributions_missingness_and_negative_multiples(pe, pb, expected):
    record = observation(pe=pe, pb=pb)
    result = build_fuyao_valuation_score("600519.sh", record, CUTOFF)
    assert result.score == expected and result.score_available is (expected is not None)
    assert result.pe_ttm == pe and result.pb_mrq == pb
    assert result.observation_id == record.id and result.observation_digest == record.digest
    assert result.fetched_at == FETCHED
    assert result.evaluated_at == "2026-09-12T02:00:00.000000Z"
    assert result.point_in_time is False and result.ranking_effect == "individual_research_only"
    assert {item.key for item in result.components} == {"pe_ttm", "pb_mrq"}
    assert result.score_semantics == "heuristic_valuation_pressure"
    if expected is not None:
        assert 55 + sum(item.points for item in result.components) == expected
    else:
        assert result.unavailable_reason and len(result.missing_data) == 2
    if pe is not None and pe < 0:
        assert result.components[0].points == -8 and "亏损" in result.components[0].reason


@pytest.mark.parametrize("offset,available", [(timedelta(days=7), True), (timedelta(days=7, microseconds=1), False),
                                           (timedelta(0), True), (timedelta(microseconds=-1), False)])
def test_observation_window_includes_boundary_but_rejects_future_and_stale(offset, available):
    fetched = audit_datetime_to_text(parse_audit_time(CUTOFF) - offset)
    result = build_fuyao_valuation_score("600519.SH", observation(fetched=fetched), CUTOFF)
    assert result.score_available is available
    if not available:
        assert result.score is None and not result.components and result.unavailable_reason


@pytest.mark.parametrize("fetched", [FETCHED, "2026-09-11T07:00:00Z", "2026-09-11 15:00:00"])
def test_equivalent_timezones_and_legacy_shanghai_timestamp_have_identical_scores(fetched):
    assert build_fuyao_valuation_score("600519.SH", observation(fetched=fetched), CUTOFF).score == 69


@pytest.mark.parametrize("fetched", ["2026-09-11", "2026-09-99T15:00:00+08:00", "not-a-time"])
def test_invalid_observation_time_cannot_enter_score(fetched):
    result = build_fuyao_valuation_score("600519.SH", observation(fetched=fetched), CUTOFF)
    assert not result.score_available and result.score is None


@pytest.mark.parametrize("batch,available", [
    (None, True), (1757660400000, False),
    (int(parse_audit_time(FETCHED).timestamp() * 1000), True),
    (int(parse_audit_time(FETCHED).timestamp() * 1000) + 1, False),
    (True, False), ("1789110000000", False), (0, False),
])
def test_batch_timestamp_cannot_rejuvenate_old_data_or_claim_future_availability(batch, available):
    result = build_fuyao_valuation_score("600519.SH", observation(batch=batch), CUTOFF)
    assert result.score_available is available
    if not available:
        assert result.score is None


@pytest.mark.parametrize("field,value", [("capability", "financials"), ("symbol", "000001.SZ"), ("id", 0), ("digest", "tampered")])
def test_observation_identity_and_digest_are_required(field, value):
    bad = observation().model_copy(update={field: value})
    result = build_fuyao_valuation_score("600519.SH", bad, CUTOFF)
    assert result.score is None and result.unavailable_reason


@pytest.mark.parametrize("payload", [
    {"symbol": "000001.SZ", "values": {"pe_ttm": 10, "pb_mrq": 2}},
    {"values": {"pe_ttm": 10, "pb_mrq": 2}},
    {"symbol": "600519.SH", "values": []},
    {"symbol": "600519.SH", "values": {"pe_ttm": True, "pb_mrq": 2}},
    {"symbol": "600519.SH", "values": {"pe_ttm": "10", "pb_mrq": 2}},
])
def test_consistent_digest_does_not_admit_wrong_identity_or_dirty_values(payload):
    bad = rehash(observation().model_copy(update={"payload": payload}))
    result = build_fuyao_valuation_score("600519.SH", bad, CUTOFF)
    assert not result.score_available and result.score is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 10**1000])
def test_nonfinite_or_overflowing_content_fails_closed(value):
    bad = observation()
    bad.payload["values"]["pe_ttm"] = value
    result = build_fuyao_valuation_score("600519.SH", bad, CUTOFF)
    assert not result.score_available and result.score is None


def test_no_cache_and_all_missing_are_not_neutral_scores():
    result = build_fuyao_valuation_score("600519.SH", None, CUTOFF)
    assert result.score is None and not result.score_available and result.observation_id is None
    assert result.unavailable_reason
    with pytest.raises(ValueError):
        build_fuyao_valuation_score("600519.SH", None, "2026-09-12")


@pytest.mark.parametrize("updates", [
    {"score_available": False}, {"score": None}, {"score": 100}, {"components": []},
    {"unavailable_reason": "rejected"}, {"pe_ttm": 9999.0}, {"observation_id": None},
    {"observation_digest": "invalid"}, {"fetched_at": None},
])
def test_score_model_rejects_claims_that_disagree_with_contributions(updates):
    result = build_fuyao_valuation_score("600519.SH", observation(), CUTOFF)
    with pytest.raises(ValidationError):
        FuyaoValuationScore.model_validate({**result.model_dump(), **updates})


def test_duplicate_components_cannot_double_count_one_input():
    result = build_fuyao_valuation_score("600519.SH", observation(), CUTOFF)
    duplicated = [result.components[0].model_dump(), result.components[0].model_dump()]
    with pytest.raises(ValidationError):
        FuyaoValuationScore.model_validate({**result.model_dump(), "components": duplicated, "score": 71})


def test_cached_service_and_http_score_agree_without_provider_or_extra_reads(tmp_path, monkeypatch):
    import app.config_settings as config_module
    import app.api.routes.fuyao as routes
    from tests.test_fuyao_service import Client, Runtime
    monkeypatch.setattr(config_module, "_SHELL_ENV_VALUES", {})
    monkeypatch.setattr(routes, "audit_now_text", lambda: CUTOFF)
    service = FuyaoService(Settings(cache_path=tmp_path / "cache.sqlite3", fuyao_enabled=False,
                                    fuyao_api_key=None, fuyao_api_key_file=None), Runtime(), client=Client())
    saved = service.repository.save_observation("valuations", "600519.SH", FETCHED, observation().payload)
    service.client.request = AsyncMock(side_effect=AssertionError("cache reads must not call provider"))
    reads, latest = [], service.repository.latest
    def counted_latest(capability, symbol):
        reads.append((capability, symbol))
        return latest(capability, symbol)
    monkeypatch.setattr(service.repository, "latest", counted_latest)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_datahub] = lambda: SimpleNamespace(fuyao=service)
    async def run():
        direct = await service.valuation_score("600519.sh", CUTOFF)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
            response = await client.get("/api/fuyao/stock?symbol=600519.SH")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["valuation_score"] == direct.model_dump(mode="json")
        assert direct.score == 69 and direct.observation_digest == saved.digest
        assert reads == [("valuations", "600519.SH"), ("valuations", "600519.SH")]
        assert service.repository.observations("valuations", "600519.SH") == [saved]
        assert service.repository.request_count("2026-09-12") == 0
        service.client.request.assert_not_awaited()
        await service.aclose()
    asyncio.run(run())


def test_source_independent_reads_do_not_create_missing_cache(tmp_path):
    repository = FuyaoResearchRepository(tmp_path / "absent.sqlite3")
    assert repository.latest("valuations", "600519.SH") is None
    assert not repository.path.exists()
