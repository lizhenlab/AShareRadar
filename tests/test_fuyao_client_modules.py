from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
import json

import httpx
import pytest

from app.config import Settings
import app.config_settings as config_module
import app.services.fuyao_client as client_module
from app.services.fuyao_client import FuyaoClient
from app.services.fuyao_contracts import FuyaoError, retry_after_seconds


PATH = "/api/a-share/financials/income-statements"
OTHER_PATH = "/api/a-share/financials/balance-sheets"
KEY = "synthetic-fixture-secret"


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    monkeypatch.setattr(config_module, "_SHELL_ENV_VALUES", {})
    monkeypatch.delenv("ASHARE_RADAR_FUYAO_API_KEY_FILE", raising=False)


def settings(**overrides):
    return Settings(**{"fuyao_enabled": True, "fuyao_api_key": KEY, "fuyao_api_key_file": None,
                       "fuyao_request_interval_seconds": 0, **overrides})


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"now": 0.0, "sleeps": []}

    async def sleep(seconds):
        state["sleeps"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(client_module, "monotonic_now", lambda: state["now"])
    monkeypatch.setattr(client_module.asyncio, "sleep", sleep)
    return state


def test_client_is_lazy_and_returns_complete_success_envelope():
    seen = []
    envelope = {"code": 0, "data": {"item": {"thscode": "600519.SH"}}, "message": "ok"}

    def handle(request):
        seen.append(request)
        assert request.headers["X-api-key"] == KEY
        assert request.url.host == "fuyao.aicubes.cn" and request.url.scheme == "https"
        assert KEY not in str(request.url)
        assert request.url.params["thscode"] == "600519.SH"
        return httpx.Response(200, json=envelope)

    client = FuyaoClient(settings(), transport=httpx.MockTransport(handle))
    assert not seen and client.status()["requests"] == 0

    async def run():
        assert await client.request(PATH, {"thscode": "600519.SH"}) == envelope
        assert client.status()["permissions"][PATH] == "available"
        assert KEY not in json.dumps(client.status())
        await client.aclose()
        await client.aclose()
        with pytest.raises(FuyaoError, match="client_closed"):
            await client.request(PATH)

    asyncio.run(run())
    assert len(seen) == 1


@pytest.mark.parametrize("overrides,category", [({"fuyao_enabled": False}, "disabled"), ({"fuyao_api_key": None}, "missing_or_invalid_key")])
def test_disabled_or_missing_key_never_sends(overrides, category):
    def unexpected(_request):
        raise AssertionError("network request must not occur")

    async def run():
        client = FuyaoClient(settings(**overrides), transport=httpx.MockTransport(unexpected))
        with pytest.raises(FuyaoError, match=category):
            await client.request(PATH)
        assert client.status()["requests"] == 0
        await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("path", ["https://evil.test/api/a-share/financials/income-statements", "//evil.test/x", PATH + "?token=x",
    PATH + "#x", "/api/../admin", "/api/a-share/auction/snapshot"])
def test_path_allowlist_prevents_key_exfiltration(path):
    async def run():
        client = FuyaoClient(settings())
        with pytest.raises(FuyaoError, match="unsupported_path"):
            await client.request(path)
        assert client.status()["requests"] == 0
        await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("status,payload,category", [(200, {"code": 2003, "message": KEY}, "permission_denied"),
    (200, {"code": 2001}, "invalid_key"), (200, {"code": 1001}, "business_error"),
    (403, {"code": 0, "data": {}}, "permission_denied"), (302, {}, "http_error"),
    (200, {"code": False, "data": {}}, "invalid_response"), (200, {"code": "0", "data": {}}, "invalid_response"),
    (200, {"code": 0}, "invalid_response"), (200, [], "invalid_response")])
def test_http_and_business_codes_are_both_required_and_errors_are_sanitized(status, payload, category):
    async def run():
        client = FuyaoClient(settings(), transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=payload,
            headers={"location": "https://evil.test/?signed-secret=value"})))
        with pytest.raises(FuyaoError) as caught:
            await client.request(PATH)
        assert caught.value.category == category
        assert KEY not in str(caught.value) and "signed-secret" not in str(caught.value)
        assert client.status()["requests"] == 1
        assert client.status()["last_error"] == category
        await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("limited_http", [200, 429])
def test_retries_honor_retry_after_and_consume_budget_for_every_attempt(fake_clock, limited_http):
    calls = []

    class Budget:
        count = 0

        async def reserve(self):
            self.count += 1

    budget = Budget()

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(limited_http, json={"code": 4001}, headers={"retry-after": "3"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    async def run():
        client = FuyaoClient(settings(), transport=httpx.MockTransport(handle), budget=budget)
        await client.request(PATH)
        assert client.status()["rate_limits"] == 1 and client.status()["retries"] == 1
        assert client.status()["budget_scope"] == "injected"
        await client.aclose()

    asyncio.run(run())
    assert budget.count == 2 and len(calls) == 2
    assert fake_clock["sleeps"] == [3]


def test_retry_attempts_are_bounded_and_local_daily_limit_is_enforced(fake_clock):
    async def run():
        client = FuyaoClient(settings(fuyao_daily_request_limit=2), transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"code": 5003})))
        with pytest.raises(FuyaoError, match="daily_request_limit"):
            await client.request(PATH)
        assert client.status()["requests"] == 2
        assert client.status()["daily_requests"] == 2
        await client.aclose()

    asyncio.run(run())


def test_same_key_clients_share_interval_concurrency_and_budget(fake_clock):
    async def run():
        maximum = active = 0

        async def handle(_request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            active -= 1
            return httpx.Response(200, json={"code": 0, "data": {}})

        first = FuyaoClient(settings(fuyao_request_interval_seconds=2, fuyao_daily_request_limit=2), transport=httpx.MockTransport(handle))
        second = FuyaoClient(settings(fuyao_request_interval_seconds=1, fuyao_daily_request_limit=3), transport=httpx.MockTransport(handle))
        await asyncio.gather(first.request(PATH), second.request(OTHER_PATH))
        with pytest.raises(FuyaoError, match="daily_request_limit"):
            await second.request(PATH)
        assert first._state is second._state
        assert maximum == 1 and second.status()["daily_request_limit"] == 2
        await first.aclose()
        await second.aclose()

    asyncio.run(run())
    assert fake_clock["sleeps"] == [2, 2]


def test_long_retry_after_is_preserved_without_early_retry(fake_clock):
    async def run():
        client = FuyaoClient(settings(), transport=httpx.MockTransport(lambda _request: httpx.Response(
            429, headers={"retry-after": "3600"}, json={"code": 4001})))
        with pytest.raises(FuyaoError, match="rate_limited"):
            await client.request(PATH)
        with pytest.raises(FuyaoError, match="rate_limited"):
            await client.request(PATH)
        assert client.status()["requests"] == 1
        await client.aclose()

    asyncio.run(run())
    assert not fake_clock["sleeps"]


@pytest.mark.parametrize("body", [b"<html>signed-secret</html>", b'{"code":0,"data":NaN}', b'{"code":0,"data":'])
def test_malformed_json_never_enters_error_messages(body):
    async def run():
        client = FuyaoClient(settings(), transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)))
        with pytest.raises(FuyaoError, match="invalid_response") as caught:
            await client.request(PATH)
        assert "signed-secret" not in str(caught.value)
        await client.aclose()

    asyncio.run(run())


def test_response_size_limit_checks_headers_and_actual_bytes(monkeypatch):
    monkeypatch.setattr(client_module, "FUYAO_MAX_RESPONSE_BYTES", 32)

    async def run(headers):
        client = FuyaoClient(settings(), transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, content=b"x" * 33, headers=headers)))
        with pytest.raises(FuyaoError, match="response_too_large"):
            await client.request(PATH)
        await client.aclose()

    asyncio.run(run({"content-length": "100"}))
    asyncio.run(run({"content-length": "1"}))


def test_transport_errors_are_bounded_and_sanitized(fake_clock):
    def handle(request):
        raise httpx.ConnectError(f"{KEY} https://evil.test/?signed-secret=x", request=request)

    async def run():
        client = FuyaoClient(settings(), transport=httpx.MockTransport(handle))
        with pytest.raises(FuyaoError, match="transport_error") as caught:
            await client.request(PATH)
        assert KEY not in str(caught.value) and "signed-secret" not in str(caught.value)
        assert client.status()["requests"] == 3
        await client.aclose()

    asyncio.run(run())
    assert fake_clock["sleeps"] == [1, 2]


def test_retry_after_supports_http_dates_and_ignores_invalid_values():
    target = datetime.now(UTC) + timedelta(seconds=30)
    assert 28 <= retry_after_seconds(format_datetime(target)) <= 30
    assert retry_after_seconds("NaN") is None
    assert retry_after_seconds("bad date") is None
    assert retry_after_seconds("-2") == 0


def test_disabled_and_closed_clients_do_not_read_key_file(monkeypatch, tmp_path):
    def unexpected(*_args):
        raise AssertionError("key file must not be accessed")

    monkeypatch.setattr(client_module, "resolve_api_key", unexpected)

    async def run():
        disabled = FuyaoClient(settings(fuyao_enabled=False, fuyao_api_key=None, fuyao_api_key_file=tmp_path / "absent.key"))
        closed = FuyaoClient(settings(fuyao_api_key=None, fuyao_api_key_file=tmp_path / "absent.key"))
        await closed.aclose()
        for client, reason in ((disabled, "disabled"), (closed, "client_closed")):
            with pytest.raises(FuyaoError, match=reason):
                await client.request(PATH)
        await disabled.aclose()

    asyncio.run(run())


def test_same_account_cross_capability_calls_really_wait_for_active_request():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def handle(request):
            calls.append(request.url.path)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"code": 0, "data": {}})

        first = FuyaoClient(settings(), transport=httpx.MockTransport(handle))
        second = FuyaoClient(settings(), transport=httpx.MockTransport(handle))
        task = asyncio.create_task(first.request(PATH))
        await entered.wait()
        following = asyncio.create_task(second.request(OTHER_PATH))
        await asyncio.sleep(0)
        assert calls == [PATH]
        release.set()
        await asyncio.gather(task, following)
        assert calls == [PATH, OTHER_PATH]
        await first.aclose()
        await second.aclose()

    asyncio.run(run())


def test_cancelled_request_releases_account_lock_without_retrying():
    async def run():
        entered = asyncio.Event()

        async def blocked(_request):
            entered.set()
            await asyncio.Event().wait()

        first = FuyaoClient(settings(), transport=httpx.MockTransport(blocked))
        second = FuyaoClient(settings(), transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0, "data": {}})))
        task = asyncio.create_task(first.request(PATH))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert first.status()["requests"] == 1 and first.status()["retries"] == 0
        assert (await second.request(PATH))["code"] == 0
        await first.aclose()
        await second.aclose()

    asyncio.run(run())


def test_injected_budget_rejection_never_opens_http_transport():
    class ExhaustedBudget:
        async def reserve(self):
            raise FuyaoError("daily_request_limit")

    async def run():
        client = FuyaoClient(settings(), budget=ExhaustedBudget())
        with pytest.raises(FuyaoError, match="daily_request_limit"):
            await client.request(PATH)
        assert client.status()["requests"] == 0
        assert client.status()["last_error"] == "daily_request_limit"
        await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("params", [{"api_key": KEY}, {"X-api-key": KEY}, {"access_token": KEY},
    {"limit": float("inf")}, {"limit": {"nested": 1}}, {"thscode": "x" * 32001}])
def test_invalid_query_parameters_do_not_send(params):
    async def run():
        client = FuyaoClient(settings())
        with pytest.raises(FuyaoError, match="invalid_parameters"):
            await client.request(PATH, params)
        assert client.status()["requests"] == 0
        await client.aclose()

    asyncio.run(run())


def test_metadata_keyword_is_not_mistaken_for_a_secret():
    async def run():
        client = FuyaoClient(settings(), transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"code": 0, "data": {"query": request.url.params["keyword"]}})))
        result = await client.request("/api/meta/tickers/search", {"keyword": "茅台"})
        assert result["data"]["query"] == "茅台"
        await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("code", [5001, 5002, 5003])
def test_each_documented_server_error_has_bounded_counted_retries(fake_clock, code):
    async def run():
        client = FuyaoClient(settings(), transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"code": code, "message": KEY, "data": None})))
        with pytest.raises(FuyaoError) as caught:
            await client.request(PATH)
        assert caught.value.category == "remote_unavailable" and caught.value.code == code
        assert KEY not in str(caught.value)
        assert client.status()["requests"] == 3 and client.status()["retries"] == 2
        await client.aclose()
    asyncio.run(run())
    assert fake_clock["sleeps"] == [1, 2]


@pytest.mark.parametrize("code", [2002, 2004])
def test_dump_authentication_failures_are_classified_without_retry(code):
    path = "/api/dump/market-dumps/daily-k/download-url"
    async def run():
        client = FuyaoClient(settings(), transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"code": code, "message": KEY, "data": None})))
        with pytest.raises(FuyaoError) as caught:
            await client.request(path)
        assert caught.value.category == "invalid_key" and caught.value.code == code
        assert client.status()["requests"] == 1 and client.status()["permissions"][path] == "invalid_key"
        await client.aclose()
    asyncio.run(run())
