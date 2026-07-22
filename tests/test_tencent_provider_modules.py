from __future__ import annotations

import asyncio
import random
from datetime import datetime

import pytest

from app.services.providers import (
    DemoMarketDataProvider,
    MarketDataError,
    MarketDataProtocolError,
    TENCENT_KLINE_URL,
    TencentMarketDataProvider,
    _format_timestamp,
    _parse_tencent_quote_payload,
    _tencent_kline_response_is_coverage_miss,
    _tencent_kline_rows,
    _tencent_klines_from_rows,
    _tencent_quote_payloads,
    _tencent_quote_url,
    _tencent_quotes_from_text,
)


def test_tencent_quote_payloads_extract_multiple_rows() -> None:
    text = 'v_sh600519="1~贵州茅台~600519";\nv_sz000001="0~平安银行~000001";'

    payloads = _tencent_quote_payloads(text)

    assert payloads == ["1~贵州茅台~600519", "0~平安银行~000001"]


def test_tencent_quote_payloads_strip_trailing_whitespace_without_semicolon() -> None:
    text = 'v_sh600519="1~贵州茅台~600519"\n'

    payloads = _tencent_quote_payloads(text)

    assert payloads == ["1~贵州茅台~600519"]


def test_tencent_quote_payloads_extract_closed_quotes_without_semicolon_between_rows() -> None:
    text = 'v_sh600519="1~贵州茅台~600519"\n v_sz000001="0~平安银行~000001"   '

    payloads = _tencent_quote_payloads(text)

    assert payloads == ["1~贵州茅台~600519", "0~平安银行~000001"]


def test_tencent_quote_payloads_ignore_unclosed_assignments() -> None:
    assert _tencent_quote_payloads('v_sh600519="1~贵州茅台~600519') == []


def test_tencent_quote_url_normalizes_symbols_and_keeps_request_order() -> None:
    assert _tencent_quote_url(["600519.SH", "000001.SZ", "920066.BJ"]) == "https://qt.gtimg.cn/q=sh600519,sz000001,bj920066"
    assert _tencent_quote_url([]) == ""


def test_tencent_quotes_from_text_filters_invalid_payloads() -> None:
    valid = "~".join(_quote_parts())
    unknown_market = "~".join(_quote_parts(flag="9"))
    short = "1~贵州茅台"
    text = f'v_sh600519="{valid}";v_bad="{unknown_market}";v_short="{short}";'

    quotes = _tencent_quotes_from_text(text, "腾讯行情")

    assert [quote.code for quote in quotes] == ["600519"]


def test_tencent_provider_quotes_raises_when_payloads_are_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(_: str, __: float) -> str:
        return 'v_sh600519="1~贵州茅台";'

    monkeypatch.setattr("app.services.providers._fetch_tencent_quote_text", fake_fetch)

    with pytest.raises(MarketDataError, match="实时行情返回为空"):
        asyncio.run(TencentMarketDataProvider(timeout=8.0).quotes(["600519.SH"]))


def test_demo_provider_does_not_mutate_global_random_state() -> None:
    provider = DemoMarketDataProvider(enabled=True)
    random.seed(20260701)
    before = random.getstate()

    asyncio.run(provider.quotes(["600519.SH", "000001.SZ"]))
    asyncio.run(provider.kline("600519.SH", limit=8))

    assert random.getstate() == before


def test_demo_provider_quotes_keep_open_inside_price_range() -> None:
    provider = DemoMarketDataProvider(enabled=True)

    quotes = asyncio.run(provider.quotes(["600519.SH", "000001.SZ", "300750.SZ", "002182.SZ"]))

    assert quotes
    assert all(item.low <= item.open <= item.high for item in quotes)
    assert all(item.low <= item.price <= item.high for item in quotes)


def test_demo_provider_quotes_repeat_with_fixed_local_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls) -> datetime:
            return cls(2026, 7, 3, 10, 17, 5)

    monkeypatch.setattr("app.services.providers.datetime", FixedDatetime)
    monkeypatch.setattr("app.services.providers.now_text", lambda: "2026-07-03 10:17:05")
    provider = DemoMarketDataProvider(enabled=True)

    first = asyncio.run(provider.quotes(["600519.SH", "000001.SZ", "002182.SZ"]))
    second = asyncio.run(provider.quotes(["600519.SH", "000001.SZ", "002182.SZ"]))

    assert first == second


def test_demo_provider_quotes_preserve_order_and_unknown_symbol_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls) -> datetime:
            return cls(2026, 7, 3, 10, 17, 5)

    monkeypatch.setattr("app.services.providers.datetime", FixedDatetime)
    monkeypatch.setattr("app.services.providers.now_text", lambda: "2026-07-03 10:17:05")
    provider = DemoMarketDataProvider(enabled=True)

    quotes = asyncio.run(provider.quotes(["688001.SH", "000001.SZ", "002594.SZ"]))

    assert [item.code for item in quotes] == ["688001", "000001", "002594"]
    assert [item.market for item in quotes] == ["SH", "SZ", "SZ"]
    assert quotes[0].name == "演示股票001"
    assert quotes[1].name == "平安银行"
    assert all(item.source == "本地演示数据" for item in quotes)


def test_demo_provider_klines_keep_open_and_close_inside_price_range() -> None:
    provider = DemoMarketDataProvider(enabled=True)

    klines = asyncio.run(provider.kline("600519.SH", limit=30))

    assert klines
    assert all(item.low <= item.open <= item.high for item in klines)
    assert all(item.low <= item.close <= item.high for item in klines)


def test_demo_provider_kline_small_limit_on_monday_returns_previous_weekday(monkeypatch: pytest.MonkeyPatch) -> None:
    class MondayDatetime(datetime):
        @classmethod
        def now(cls) -> datetime:
            return cls(2026, 7, 6, 10, 0, 0)

    monkeypatch.setattr("app.services.providers.datetime", MondayDatetime)
    provider = DemoMarketDataProvider(enabled=True)

    klines = asyncio.run(provider.kline("600519.SH", limit=1))

    assert len(klines) == 1
    assert klines[0].date == "2026-07-03"
    assert klines[0].low <= klines[0].open <= klines[0].high
    assert klines[0].low <= klines[0].close <= klines[0].high


def test_tencent_klines_from_rows_skips_malformed_ohlc_rows() -> None:
    rows = [
        ["2026-05-26", "100", "101", "99", "98", "1000"],
        ["2026-05-26", "100", "101", "102", "99", "-1"],
        ["2026-05-26", "100", "101", "102", "99", "nan"],
        ["2026-05-26", "100", "101", "102", "99", "inf"],
        ["2026-05-26", "100", "101", "102", "99", "bad"],
        "bad-row",
        ["2026-05-27", "100", "101", "102", "99", "2000"],
        ["2026-05-28", "bad", "101", "102", "99", "3000"],
        ["", "100", "101", "102", "99", "4000"],
    ]

    klines = _tencent_klines_from_rows(rows)

    assert [item.date for item in klines] == ["2026-05-27"]
    assert klines[0].close == 101.0


def test_tencent_kline_rows_handles_malformed_payload_shapes() -> None:
    assert list(_tencent_kline_rows([], "sh600519")) == []
    assert list(_tencent_kline_rows({"data": []}, "sh600519")) == []
    assert list(_tencent_kline_rows({"data": {"sh600519": []}}, "sh600519")) == []
    assert list(_tencent_kline_rows({"data": {"sh600519": {"qfqday": "bad"}}}, "sh600519")) == []


def test_tencent_kline_rows_accepts_day_key_when_qfq_has_no_adjustment_event() -> None:
    rows = [["2026-07-22", "10", "10.5", "10.8", "9.9", "12345"]]

    assert list(_tencent_kline_rows({"data": {"bj920011": {"day": rows}}}, "bj920011")) == rows


def test_tencent_kline_rows_uses_day_when_qfq_series_is_empty() -> None:
    rows = [["2026-07-22", "10", "10.5", "10.8", "9.9", "12345"]]
    payload = {"data": {"bj920011": {"qfqday": [], "day": rows}}}

    assert list(_tencent_kline_rows(payload, "bj920011")) == rows
    assert _tencent_kline_response_is_coverage_miss(payload, "bj920011") is False


def test_tencent_kline_empty_coverage_is_distinct_from_malformed_protocol() -> None:
    assert _tencent_kline_response_is_coverage_miss(
        {"data": {"sh600519": {"qfqday": []}}},
        "sh600519",
    )
    assert _tencent_kline_response_is_coverage_miss({"data": {}}, "sh600519")
    assert not _tencent_kline_response_is_coverage_miss([], "sh600519")
    assert not _tencent_kline_response_is_coverage_miss(
        {"data": {"sh600519": {"qfqday": "bad"}}},
        "sh600519",
    )


def test_tencent_provider_kline_raises_when_all_rows_are_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    requested_urls: list[str] = []

    class FakeResponse:
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"sh600519": {"qfqday": [["2026-05-26", "100", "101", "99", "98", "1000"]]}}}

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            requested_urls.append(url)
            return FakeResponse()

    monkeypatch.setattr("app.services.providers.httpx.AsyncClient", FakeClient)

    with pytest.raises(MarketDataError, match="K线有效数据为空"):
        asyncio.run(TencentMarketDataProvider(timeout=8.0).kline("600519.SH", limit=1))

    assert requested_urls == [f"{TENCENT_KLINE_URL}?param=sh600519,day,,,1,qfq"]


def test_tencent_provider_rejects_nonzero_business_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"code": 429, "msg": "upstream throttled", "data": {}}

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            del url
            return FakeResponse()

    monkeypatch.setattr("app.services.providers.httpx.AsyncClient", FakeClient)

    with pytest.raises(MarketDataProtocolError, match="code=429"):
        asyncio.run(TencentMarketDataProvider(timeout=8.0).kline("600519.SH", limit=1))


@pytest.mark.parametrize("symbol, provider_symbol", [("920000.BJ", "bj920000"), ("920066.BJ", "bj920066")])
def test_tencent_provider_uses_new_qfq_endpoint_for_beijing_stocks(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    provider_symbol: str,
) -> None:
    requested_urls: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    provider_symbol: {
                        "qfqday": [["2026-07-22", "10", "10.5", "10.8", "9.9", "12345"]]
                    }
                }
            }

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            requested_urls.append(url)
            return FakeResponse()

    monkeypatch.setattr("app.services.providers.httpx.AsyncClient", FakeClient)

    rows = asyncio.run(TencentMarketDataProvider(timeout=8.0).kline(symbol, limit=260))

    assert requested_urls == [f"{TENCENT_KLINE_URL}?param={provider_symbol},day,,,260,qfq"]
    assert len(rows) == 1
    assert rows[0].adjustment_mode == "qfq"
    assert rows[0].date == "2026-07-22"


def test_parse_tencent_quote_uses_backup_high_low_fields_and_optional_market_cap() -> None:
    parts = _quote_parts()
    parts[33] = ""
    parts[34] = ""
    parts[41] = "1310.50"
    parts[42] = "1278.20"
    parts[44] = ""

    quote = _parse_tencent_quote_payload("~".join(parts), "腾讯行情")

    assert quote is not None
    assert quote.code == "600519"
    assert quote.market == "SH"
    assert quote.high == 1310.5
    assert quote.low == 1278.2
    assert quote.market_cap is None
    assert quote.timestamp == "2026-05-13 10:11:12"


def test_parse_tencent_quote_accepts_exact_minimum_fields_and_strips_field_whitespace() -> None:
    parts = _quote_parts()
    parts[0] = " 1 "
    parts[1] = " 贵州茅台 "
    parts[2] = " 600519 "
    parts[33] = "   "
    parts[34] = " -- "
    parts[41] = " 1310.50 "
    parts[42] = " 1278.20 "
    payload = (" ~ ".join(parts[:45])).join((" \n", "\t"))

    quote = _parse_tencent_quote_payload(payload, "腾讯行情")

    assert quote is not None
    assert quote.code == "600519"
    assert quote.name == "贵州茅台"
    assert quote.market == "SH"
    assert quote.high == 1310.5
    assert quote.low == 1278.2
    assert quote.pb is None


@pytest.mark.parametrize("value", ["20261399101112", "20260230101112", "bad", ""])
def test_format_timestamp_rejects_missing_or_impossible_timestamp(value: str) -> None:
    with pytest.raises(MarketDataProtocolError, match="事件时间"):
        _format_timestamp(value)


def test_parse_tencent_quote_propagates_invalid_event_time_as_protocol_failure() -> None:
    parts = _quote_parts()
    parts[30] = "bad-time"

    with pytest.raises(MarketDataProtocolError, match="事件时间"):
        _parse_tencent_quote_payload("~".join(parts), "腾讯行情")


def test_parse_tencent_quote_computes_missing_change_pct() -> None:
    parts = _quote_parts()
    parts[32] = ""

    quote = _parse_tencent_quote_payload("~".join(parts), "腾讯行情")

    assert quote is not None
    assert quote.change_pct == pytest.approx((1300.0 - 1273.38) / 1273.38 * 100)


def test_parse_tencent_quote_rejects_negative_core_amounts() -> None:
    negative_volume = _quote_parts()
    negative_volume[36] = "-1"
    negative_amount = _quote_parts()
    negative_amount[37] = "-1"

    assert _parse_tencent_quote_payload("~".join(negative_volume), "腾讯行情") is None
    assert _parse_tencent_quote_payload("~".join(negative_amount), "腾讯行情") is None


@pytest.mark.parametrize(
    ("index", "value"),
    [(36, "nan"), (36, "inf"), (36, "bad"), (37, "nan"), (37, "inf"), (37, "bad")],
)
def test_parse_tencent_quote_rejects_non_finite_or_malformed_core_amounts(index: int, value: str) -> None:
    parts = _quote_parts()
    parts[index] = value

    assert _parse_tencent_quote_payload("~".join(parts), "腾讯行情") is None


def test_parse_tencent_quote_ignores_negative_optional_non_price_fields() -> None:
    parts = _quote_parts()
    parts[38] = "-0.1"
    parts[39] = "-28.5"
    parts[44] = "-18000"
    parts[46] = "-6.20"

    quote = _parse_tencent_quote_payload("~".join(parts), "腾讯行情")

    assert quote is not None
    assert quote.turnover_rate is None
    assert quote.pe == -28.5
    assert quote.market_cap is None
    assert quote.pb is None


def test_parse_tencent_quote_accepts_index_market_flags() -> None:
    sh_index = _quote_parts(flag="1", code="000001", name="上证指数")
    sz_index = _quote_parts(flag="51", code="399001", name="深证成指")

    sh_quote = _parse_tencent_quote_payload("~".join(sh_index), "腾讯行情")
    sz_quote = _parse_tencent_quote_payload("~".join(sz_index), "腾讯行情")

    assert sh_quote is not None
    assert sz_quote is not None
    assert sh_quote.market == "SH"
    assert sz_quote.market == "SZ"


def test_parse_tencent_quote_maps_current_shenzhen_stock_flag() -> None:
    sz_stock = _quote_parts(flag="51", code="002182", name="宝武镁业")

    quote = _parse_tencent_quote_payload("~".join(sz_stock), "腾讯行情")

    assert quote is not None
    assert quote.market == "SZ"


def test_parse_tencent_quote_maps_beijing_stock_flag() -> None:
    bj_stock = _quote_parts(flag="62", code="920066", name="科拜尔")

    quote = _parse_tencent_quote_payload("~".join(bj_stock), "腾讯行情")

    assert quote is not None
    assert quote.market == "BJ"


def test_parse_tencent_quote_rejects_market_flag_code_mismatch() -> None:
    bj_as_sz = _quote_parts(flag="51", code="920066", name="科拜尔")
    sh_as_bj = _quote_parts(flag="62", code="600519", name="贵州茅台")

    assert _parse_tencent_quote_payload("~".join(bj_as_sz), "腾讯行情") is None
    assert _parse_tencent_quote_payload("~".join(sh_as_bj), "腾讯行情") is None


def test_parse_tencent_quote_rejects_unknown_market_and_short_rows() -> None:
    unknown_market = _quote_parts(flag="9")

    assert _parse_tencent_quote_payload("~".join(unknown_market), "腾讯行情") is None
    assert _parse_tencent_quote_payload("1~贵州茅台", "腾讯行情") is None


def test_parse_tencent_quote_rejects_invalid_critical_prices() -> None:
    invalid_price = _quote_parts()
    invalid_price[3] = "bad"

    price_above_high = _quote_parts()
    price_above_high[3] = "1310.00"

    price_below_low = _quote_parts()
    price_below_low[3] = "1260.00"

    open_above_high = _quote_parts()
    open_above_high[5] = "1310.00"

    open_below_low = _quote_parts()
    open_below_low[5] = "1260.00"

    missing_low = _quote_parts()
    missing_low[34] = ""
    missing_low[42] = ""

    inverted_range = _quote_parts()
    inverted_range[33] = "1260.00"
    inverted_range[34] = "1270.00"

    assert _parse_tencent_quote_payload("~".join(invalid_price), "腾讯行情") is None
    assert _parse_tencent_quote_payload("~".join(price_above_high), "腾讯行情") is None
    assert _parse_tencent_quote_payload("~".join(price_below_low), "腾讯行情") is None
    assert _parse_tencent_quote_payload("~".join(open_above_high), "腾讯行情") is None
    assert _parse_tencent_quote_payload("~".join(open_below_low), "腾讯行情") is None
    assert _parse_tencent_quote_payload("~".join(missing_low), "腾讯行情") is None
    assert _parse_tencent_quote_payload("~".join(inverted_range), "腾讯行情") is None


def test_parse_tencent_quote_rejects_blank_name_and_invalid_code() -> None:
    blank_name = _quote_parts(name=" ")
    bad_code = _quote_parts(code="ABC123")
    zero_code = _quote_parts(code="000000")

    assert _parse_tencent_quote_payload("~".join(blank_name), "腾讯行情") is None
    assert _parse_tencent_quote_payload("~".join(bad_code), "腾讯行情") is None
    assert _parse_tencent_quote_payload("~".join(zero_code), "腾讯行情") is None


def _quote_parts(*, flag: str = "1", code: str = "600519", name: str = "贵州茅台") -> list[str]:
    parts = [""] * 47
    parts[0] = flag
    parts[1] = name
    parts[2] = code
    parts[3] = "1300.00"
    parts[4] = "1273.38"
    parts[5] = "1280.00"
    parts[30] = "20260513101112"
    parts[31] = "26.62"
    parts[32] = "2.09"
    parts[33] = "1305.00"
    parts[34] = "1270.00"
    parts[36] = "123456"
    parts[37] = "16.20"
    parts[38] = "0.42"
    parts[39] = "28.50"
    parts[44] = "18000"
    parts[46] = "6.20"
    return parts
