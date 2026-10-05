from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json

import pytest
import requests

from app.config import Settings
from app.services.akshare_provider import AKShareProvider
from app.services.cache import SQLiteCache
from app.services.datahub_metadata_coordinator import MetadataCoordinator
from app.services.datahub_runtime import ProviderRuntime
from app.services import eastmoney_client as client
from app.services.market_context_scoring import build_market_context_score
from app.utils.audit_time import audit_datetime_to_text
from app.utils.clock import ASHARE_TIMEZONE, utc_now
from app.utils.provider_errors import ProviderProtocolError, ProviderTransportError
from app.workflows.stock_lookup import match_industry
from tests.factories import make_plate_item, make_quote, make_stock_info


EVENT = "2026-05-13 10:00:00"
OBSERVED = "2026-05-13T02:00:01.000000Z"
EVENT_SECONDS = int(datetime(2026, 5, 13, 10, tzinfo=ASHARE_TIMEZONE).timestamp())


def _payload(**updates):
    row = {"f3": 1.2, "f6": 1000, "f8": 0.5, "f12": "BK0477", "f14": "测试行业", "f124": EVENT_SECONDS}
    row.update(updates)
    return {"rc": 0, "data": {"total": 1, "diff": [row]}}


class _Response:
    def __init__(self, payload=None, *, status=200):
        self.status_code = status
        self.body = json.dumps(payload or _payload()).encode()
        self.headers = {"Content-Length": str(len(self.body))}

    def iter_content(self, *, chunk_size):
        assert chunk_size == 64 * 1024
        yield self.body


def _install_session(monkeypatch, responses, *, on_request=None):
    remaining = iter(responses)
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if on_request:
                on_request(len(calls))
            result = next(remaining)
            if isinstance(result, BaseException):
                raise result
            return result

    monkeypatch.setattr(client, "_eastmoney_session", Session)
    monkeypatch.setattr(client, "audit_now_text", lambda: OBSERVED)
    return calls


def test_primary_success_uses_one_request_and_keeps_original_contract(monkeypatch):
    calls = _install_session(monkeypatch, [_Response()])

    [row] = client.eastmoney_industry_plate_rank()

    assert len(calls) == 1
    assert calls[0][0] == client.EASTMONEY_INDUSTRY_PLATE_URL
    assert row.source == client.EASTMONEY_BRIDGE_SOURCE_NAME
    assert row.symbol == "BK0477" and row.quote_timestamp == EVENT
    assert row.updated_at == OBSERVED and not row.fallback_used


@pytest.mark.parametrize("total", [100, 101, 500])
def test_industry_universe_can_exceed_requested_complete_first_ranking_page(monkeypatch, total):
    rows = [dict(_payload()["data"]["diff"][0], f12=f"BK{index:04d}", f14=f"行业{index}", f3=10 - index / 10) for index in range(100)]
    calls = _install_session(monkeypatch, [_Response({"rc": 0, "data": {"total": total, "diff": rows}})])

    selected = client.eastmoney_industry_plate_rank(limit=20)

    assert len(calls) == 1
    assert calls[0][1]["params"]["pn"] == "1" and calls[0][1]["params"]["pz"] == "100"
    assert [row.symbol for row in selected] == [f"BK{index:04d}" for index in range(20)]
    assert all(row.quote_timestamp == EVENT and row.updated_at == OBSERVED for row in selected)
    assert all(row.source == client.EASTMONEY_BRIDGE_SOURCE_NAME for row in selected)


@pytest.mark.parametrize("row_count", [0, 20, 99, 101])
def test_large_industry_universe_rejects_incomplete_or_oversized_first_page(monkeypatch, row_count):
    rows = [dict(_payload()["data"]["diff"][0], f12=f"BK{index:04d}", f3=100 - index) for index in range(row_count)]
    calls = _install_session(monkeypatch, [_Response({"rc": 0, "data": {"total": 150, "diff": rows}})])

    with pytest.raises(ProviderProtocolError, match="diff 与 total 不一致"):
        client.eastmoney_industry_plate_rank(limit=20)
    assert len(calls) == 1


def test_transport_fallback_preserves_identity_event_time_and_marks_delayed_source(monkeypatch):
    calls = _install_session(monkeypatch, [requests.ConnectionError("remote closed"), _Response()])

    [row] = client.eastmoney_industry_plate_rank()

    assert [url for url, _ in calls] == [client.EASTMONEY_INDUSTRY_PLATE_URL, client.EASTMONEY_DELAYED_INDUSTRY_PLATE_URL]
    assert calls[0][1]["params"] == calls[1][1]["params"]
    assert calls[1][1]["params"]["fs"] == "m:90 t:2 f:!50"
    assert row.source == client.EASTMONEY_DELAYED_PLATE_SOURCE_NAME
    assert row.symbol == "BK0477" and row.quote_timestamp == EVENT and row.updated_at == OBSERVED
    assert row.amount == 1000 and row.turnover_rate == 0.5
    assert not row.fallback_used  # A fresh delayed response is not stale local cache.


def test_delayed_endpoint_cannot_enter_industry_scoring_even_with_aligned_time(monkeypatch):
    _install_session(monkeypatch, [requests.Timeout("timeout"), _Response()])
    [row] = client.eastmoney_industry_plate_rank()
    stock = make_quote(timestamp=EVENT)

    assert match_industry(make_stock_info(), [row], quote=stock, evaluated_at=OBSERVED) is None
    result = build_market_context_score(80, stock, market=None, industry=row,
                                        industry_name="测试行业", evaluated_at=OBSERVED)
    assert result.industry is None
    assert result.relative_weight == 0


@pytest.mark.parametrize("event", [None, "-", "", "invalid"])
def test_delayed_endpoint_never_invents_quote_time(monkeypatch, event):
    _install_session(monkeypatch, [requests.ConnectionError("down"), _Response(_payload(f124=event))])

    [row] = client.eastmoney_industry_plate_rank()

    assert row.quote_timestamp is None
    assert row.updated_at == OBSERVED


@pytest.mark.parametrize("response", [
    _Response(status=302),
    _Response({"rc": 1, "data": None}),
    _Response({"rc": 0, "data": {"total": 101, "diff": []}}),
    _Response({"rc": 0, "data": {"total": 2, "diff": []}}),
    _Response(_payload(f3="invalid")),
    _Response(_payload(f12="600519")),
])
def test_protocol_failure_does_not_hide_bad_payload_with_delayed_fallback(monkeypatch, response):
    calls = _install_session(monkeypatch, [response, _Response()])

    with pytest.raises(ProviderProtocolError):
        client.eastmoney_industry_plate_rank()

    assert len(calls) == 1


def test_all_endpoints_down_raise_instead_of_returning_empty_or_fabricated_rows(monkeypatch):
    calls = _install_session(monkeypatch, [requests.ConnectionError("primary down"), requests.Timeout("backup timeout")])

    with pytest.raises(ProviderTransportError, match="入口均不可用") as error:
        client.eastmoney_industry_plate_rank()

    assert len(calls) == 2
    assert "primary down" in str(error.value) and "backup timeout" in str(error.value)


def test_one_shared_deadline_prevents_new_http_request_after_budget_expiry(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    calls = _install_session(monkeypatch, [requests.Timeout("slow primary"), _Response()],
                             on_request=lambda _count: clock.__setitem__(0, 107.0))

    with pytest.raises(ProviderTransportError, match="内部截止时间"):
        client.eastmoney_industry_plate_rank()

    assert len(calls) == 1


def test_backup_socket_timeouts_share_remaining_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    calls = _install_session(monkeypatch, [requests.Timeout("slow primary"), _Response()],
                             on_request=lambda count: clock.__setitem__(0, 104.0) if count == 1 else None)

    client.eastmoney_industry_plate_rank()

    assert calls[0][1]["timeout"] == (2.0, 2.0)
    assert calls[1][1]["timeout"] == (1.0, 1.0)
    assert all(kwargs["allow_redirects"] is False for _, kwargs in calls)


def test_plate_direct_request_does_not_enter_sdk_global_environment_lock(monkeypatch):
    def forbidden():
        raise AssertionError("direct request must not acquire the SDK environment lock")

    expected = make_plate_item()
    monkeypatch.setattr("app.services.akshare_provider._eastmoney_no_proxy", forbidden)
    monkeypatch.setattr("app.services.akshare_provider._eastmoney_industry_plate_rank", lambda _limit: [expected])
    monkeypatch.setattr(AKShareProvider, "_ensure_installed", staticmethod(lambda: None))

    assert asyncio.run(AKShareProvider().plate_rank()) == [expected]


@pytest.mark.parametrize("age_days", [0, 40])
def test_upstream_outage_preserves_cache_and_never_refreshes_its_timestamp(tmp_path, age_days):
    class FailingProvider:
        async def plate_rank(self, limit):
            raise ProviderTransportError("all industry endpoints down")

    old = make_plate_item().model_copy(update={"updated_at": audit_datetime_to_text(utc_now() - timedelta(days=age_days))})
    settings = Settings(cache_path=tmp_path / "cache.sqlite3")
    cache = SQLiteCache(settings=settings)
    cache.save_plate_rank([old])
    coordinator = MetadataCoordinator(settings=settings, cache=cache, providers={"akshare": FailingProvider()},
                                      runtime=ProviderRuntime(cache, settings), priority=lambda _kind: [(1, "akshare")])
    if age_days:
        with pytest.raises(RuntimeError, match="所有板块数据源均不可用"):
            asyncio.run(coordinator.plate_rank_result(refresh=True))
    else:
        result = asyncio.run(coordinator.plate_rank_result(refresh=True))
        assert result.used_fallback_cache is True
        assert result.rows[0].fallback_used is True
        assert result.rows[0].updated_at == old.updated_at
    assert cache.get_plate_rank(max_age_seconds=10**9) == [old]
