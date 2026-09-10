from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging

import httpx
import pytest

from app.services.fuyao_dumps_download import download_dump, require_public_dns, signed_url, validate_download_url
from app.services.fuyao_dumps_validation import FuyaoDumpError, dump_date, normalize_row, require_schema, validate_trading_dates
from app.utils.clock import ASHARE_TIMEZONE


def daily_row(day="2026-09-01", **updates):
    row = {"thscode": "600519.SH", "currency": "CNY", "interval": "1d", "adjusted": "none",
           "date_ms": int(datetime.fromisoformat(day).replace(tzinfo=ASHARE_TIMEZONE).timestamp() * 1000),
           "open_price": 10, "high_price": 12, "low_price": 9, "close_price": 11, "volume": 100, "turnover": 1000}
    return row | updates


def action_row(**updates):
    return {"thscode": "600519.SH", "ticker": "600519", "ex_date_ms": daily_row()["date_ms"],
            "currency": "CNY", "dividend_per_share": 1, "per_share_bonus": 0,
            "allotment_ratio": 0, "allotment_price": 0} | updates


@pytest.mark.parametrize("updates", [{"adjusted": "qfq"}, {"interval": "1m"}, {"currency": "USD"},
                                    {"thscode": "600519"}, {"date_ms": 1788192000000.0}, {"date_ms": True},
                                    {"open_price": "10"}, {"high_price": float("nan")}, {"volume": -1},
                                    {"close_price": 15}, {"low_price": 0}, {"date_ms": 1}])
def test_daily_rejects_ambiguous_or_invalid_data(updates):
    with pytest.raises(FuyaoDumpError):
        normalize_row(daily_row(**updates), "daily")


def test_decimal_normalization_and_daily_future_rejection():
    result = normalize_row(daily_row(volume=Decimal("10")), "daily")
    assert result["volume"] == 10.0
    with pytest.raises(FuyaoDumpError):
        normalize_row(daily_row(), "daily", today=date(2026, 8, 31))


@pytest.mark.parametrize("updates", [{"ticker": "000001"}, {"dividend_per_share": -1}, {"per_share_bonus": True}, {"ex_date_ms": 1}])
def test_action_contract(updates):
    assert normalize_row(action_row(), "actions")["allotment_ratio"] == 0.0
    with pytest.raises(FuyaoDumpError):
        normalize_row(action_row(**updates), "actions")


@pytest.mark.parametrize("symbol,day,ratio", [
    # Company disclosures: cninfo 37922833.PDF p22; 1200011113.pdf and 1200011114.PDF.
    ("000887.SZ", "2007-03-30", -0.67335), ("600381.SH", "2014-06-27", -0.87581),
])
def test_confirmed_capital_reductions_preserve_signed_vendor_ratio(symbol, day, ratio):
    raw = action_row(thscode=symbol, ticker=symbol[:6], ex_date_ms=daily_row(day)["date_ms"],
                     dividend_per_share=0, per_share_bonus=ratio)
    normalized = normalize_row(raw, "actions")
    assert normalized["per_share_bonus"] == ratio
    assert 1 + normalized["per_share_bonus"] + normalized["allotment_ratio"] > 0
    assert normalized["dividend_per_share"] == 0


@pytest.mark.parametrize("updates", [
    {"per_share_bonus": -1}, {"per_share_bonus": -1.1, "allotment_ratio": 2},
    {"per_share_bonus": float("nan")}, {"per_share_bonus": float("-inf")},
    {"per_share_bonus": -0.8, "dividend_per_share": -0.1},
    {"per_share_bonus": -0.8, "allotment_ratio": -0.1},
    {"per_share_bonus": -0.8, "allotment_price": -0.1},
    {"per_share_bonus": 1e308, "allotment_ratio": 1e308},
])
def test_signed_share_ratio_does_not_relax_other_action_invariants(updates):
    with pytest.raises(FuyaoDumpError):
        normalize_row(action_row(**updates), "actions")


def test_schema_and_calendar_do_not_silently_fallback_to_weekdays():
    with pytest.raises(FuyaoDumpError):
        require_schema([*daily_row(), "volume"], "daily")
    calendar = ("2026-09-01", "2026-09-02", "2026-09-03")
    assert validate_trading_dates(calendar, calendar) == "explicit_calendar"
    for observed, reference in [((), calendar), ((calendar[0], calendar[2]), calendar), (("2026-09-04",), calendar), (calendar, ())]:
        with pytest.raises(FuyaoDumpError):
            validate_trading_dates(observed, reference)
    assert dump_date(daily_row()["date_ms"]) == date(2026, 9, 1)
    with pytest.raises(FuyaoDumpError):
        dump_date(10**100)


@pytest.mark.parametrize("url,hosts", [("http://files.example.com/file", ("files.example.com",)),
    ("https://user:secret@files.example.com/file", ("files.example.com",)),
    ("https://files.example.com:8443/file", ("files.example.com",)),
    ("https://files.example.com/file#fragment", ("files.example.com",)),
    ("https://localhost/file", ("localhost",)), ("https://127.0.0.1/file", ("127.0.0.1",)),
    ("https://files.internal/file", ("files.internal",)), ("https://evil.example.com/file", ("files.example.com",))])
def test_download_url_fails_closed(url, hosts):
    with pytest.raises(FuyaoDumpError):
        validate_download_url(url, hosts)


def test_signing_contract_and_expiry():
    response = {"code": 0, "data": {"presigned_url": "https://files.example.com/file?secret=private",
                                   "presigned_url_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat()}}
    assert signed_url(response).endswith("secret=private")
    for invalid in [{"code": True}, {"code": 4040}, {"code": 0, "data": {}},
                    {"code": 0, "data": response["data"] | {"presigned_url_expires_at": "2020-01-01T00:00:00"}}]:
        with pytest.raises(FuyaoDumpError):
            signed_url(invalid)


def test_public_dns_rejects_private_and_mixed_answers(monkeypatch):
    for addresses in [("127.0.0.1",), ("8.8.8.8", "10.0.0.1")]:
        monkeypatch.setattr("socket.getaddrinfo", lambda *args, addresses=addresses, **kwargs: [(2, 1, 6, "", (address, 443)) for address in addresses])
        with pytest.raises(FuyaoDumpError, match="非公网"):
            asyncio.run(require_public_dns("files.example.com"))


def test_stream_download_never_forwards_key_or_logs_signed_url(tmp_path, monkeypatch, caplog):
    async def public(_host):
        return None
    monkeypatch.setattr("app.services.fuyao_dumps_download.require_public_dns", public)
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=b"parquet")
    caplog.set_level(logging.DEBUG)
    target = tmp_path / "download.parquet"
    digest = asyncio.run(download_dump("https://files.example.com/file?token=secret-signature", target,
                                      allowed_hosts=("files.example.com",), max_bytes=100, transport=httpx.MockTransport(respond)))
    assert target.read_bytes() == b"parquet" and len(digest) == 64
    assert not {"x-api-key", "authorization", "cookie"}.intersection(seen[0].headers)
    assert "secret-signature" not in caplog.text


@pytest.mark.parametrize("response", [httpx.Response(302, headers={"location": "http://localhost/"}),
    httpx.Response(403, content=b"signed secret"), httpx.Response(200, content=b"too large"),
    httpx.Response(200, content=b""), httpx.Response(200, headers={"content-length": "nonsense"}, content=b"x")])
def test_download_failure_is_sanitized_and_bounded(tmp_path, monkeypatch, response):
    async def public(_host):
        return None
    monkeypatch.setattr("app.services.fuyao_dumps_download.require_public_dns", public)
    with pytest.raises(FuyaoDumpError) as failure:
        asyncio.run(download_dump("https://files.example.com/file?secret=private", tmp_path / "partial",
                                 allowed_hosts=("files.example.com",), max_bytes=4, transport=httpx.MockTransport(lambda request: response)))
    assert "private" not in str(failure.value) and "signed secret" not in str(failure.value)


def test_chunked_download_enforces_actual_capacity(tmp_path, monkeypatch):
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123"
            yield b"456"
    async def public(_host):
        return None
    monkeypatch.setattr("app.services.fuyao_dumps_download.require_public_dns", public)
    response = httpx.Response(200, stream=Chunks())
    with pytest.raises(FuyaoDumpError, match="实际容量"):
        asyncio.run(download_dump("https://files.example.com/object", tmp_path / "partial", allowed_hosts=("files.example.com",),
                                 max_bytes=5, transport=httpx.MockTransport(lambda request: response)))


def test_transport_exception_does_not_reveal_url(tmp_path, monkeypatch):
    async def public(_host):
        return None
    def fail(request):
        raise httpx.ConnectError(f"secret URL: {request.url}")
    monkeypatch.setattr("app.services.fuyao_dumps_download.require_public_dns", public)
    with pytest.raises(FuyaoDumpError) as failure:
        asyncio.run(download_dump("https://files.example.com/object?token=confidential", tmp_path / "partial", allowed_hosts=("files.example.com",),
                                 max_bytes=5, transport=httpx.MockTransport(fail)))
    assert "confidential" not in str(failure.value)


def test_missing_pyarrow_has_explicit_actionable_error(monkeypatch):
    from app.services.fuyao_dumps_validation import parquet_modules
    def missing(_name):
        raise ImportError("missing")
    monkeypatch.setattr("app.services.fuyao_dumps_validation.importlib.import_module", missing)
    with pytest.raises(FuyaoDumpError, match="pyarrow"):
        parquet_modules()
