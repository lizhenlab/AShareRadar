from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from typing import Any

import pytest
import requests

import app.services.sina_client as sina
from app.services.provider_errors import (
    ProviderCoverageMiss,
    ProviderProtocolError,
    ProviderTransportError,
)


def _raw_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "day": "2024-01-02",
        "open": "40",
        "high": "48",
        "low": "36",
        "close": "44",
        "volume": "1000",
    }
    row.update(overrides)
    return row


def _qfq_document(symbol: str, factors: list[tuple[str, object]]) -> str:
    payload = {
        "total": len(factors),
        "data": [{"d": day, "f": factor} for day, factor in factors],
    }
    return f"var {symbol}qfq={json.dumps(payload)};\n/* signed metadata */"


def _stub_sina_text(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_payload: object,
    qfq_text: str,
) -> list[tuple[str, dict[str, str] | None, float]]:
    calls: list[tuple[str, dict[str, str] | None, float]] = []
    responses: Iterator[str] = iter((json.dumps(raw_payload), qfq_text))

    def fake_get_text(
        url: str,
        *,
        params: dict[str, str] | None,
        timeout: float,
    ) -> str:
        calls.append((url, params, timeout))
        return next(responses)

    monkeypatch.setattr(sina, "_sina_get_text", fake_get_text)
    return calls


def test_qfq_daily_klines_forward_fill_effective_factors_and_stamp_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_payload = [
        _raw_row(day="2024-01-02"),
        _raw_row(day="2024-01-03", volume="1200"),
        _raw_row(day="2024-01-04", volume="1400"),
    ]
    calls = _stub_sina_text(
        monkeypatch,
        raw_payload=raw_payload,
        qfq_text=_qfq_document(
            "sh600519",
            [
                ("2024-01-05", "1"),
                ("2024-01-03", "2"),
                ("1900-01-01", "4"),
            ],
        ),
    )

    rows = sina.sina_qfq_daily_klines("600519.SH", limit=3, timeout=2.5)

    assert [row.open for row in rows] == pytest.approx([10, 20, 20])
    assert [row.close for row in rows] == pytest.approx([11, 22, 22])
    assert [row.high for row in rows] == pytest.approx([12, 24, 24])
    assert [row.low for row in rows] == pytest.approx([9, 18, 18])
    assert [row.volume for row in rows] == pytest.approx([1000, 1200, 1400])
    assert {row.source for row in rows} == {sina.SINA_QFQ_DAILY_KLINE_SOURCE_NAME}
    assert {row.adjustment_mode for row in rows} == {"qfq"}
    assert {row.as_of for row in rows} == {"2024-01-04"}
    assert {row.data_version for row in rows} == {"daily-kline.v1|qfq|新浪财经·前复权日K|2024-01-04"}
    assert calls == [
        (
            sina.SINA_DAILY_KLINE_URL,
            {"symbol": "sh600519", "scale": "240", "ma": "no", "datalen": "3"},
            2.5,
        ),
        (sina.SINA_QFQ_URL_TEMPLATE.format(symbol="sh600519"), None, 2.5),
    ]


@pytest.mark.parametrize(
    ("symbol", "provider_symbol"),
    [
        ("sh600519", "sh600519"),
        ("000001.SZ", "sz000001"),
        ("920000.bj", "bj920000"),
    ],
)
def test_qfq_daily_klines_support_sh_sz_and_bj(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    provider_symbol: str,
) -> None:
    calls = _stub_sina_text(
        monkeypatch,
        raw_payload=[_raw_row()],
        qfq_text=_qfq_document(provider_symbol, [("1900-01-01", "1")]),
    )

    rows = sina.sina_qfq_daily_klines(symbol, limit=1)

    assert len(rows) == 1
    assert calls[0][1] == {
        "symbol": provider_symbol,
        "scale": "240",
        "ma": "no",
        "datalen": "1",
    }
    assert calls[1][0].endswith(f"/{provider_symbol}/qfq.js")


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, "10", sina.SINA_MAX_DAILY_KLINE_LIMIT + 1])
def test_qfq_daily_klines_reject_invalid_limit_before_request(
    monkeypatch: pytest.MonkeyPatch,
    limit: object,
) -> None:
    get_text = pytest.fail
    monkeypatch.setattr(sina, "_sina_get_text", get_text)

    with pytest.raises(ValueError, match="limit"):
        sina.sina_qfq_daily_klines("600519", limit=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), "8", "bad"])
def test_qfq_daily_klines_reject_invalid_timeout_before_request(
    monkeypatch: pytest.MonkeyPatch,
    timeout: object,
) -> None:
    get_text = pytest.fail
    monkeypatch.setattr(sina, "_sina_get_text", get_text)

    with pytest.raises(ValueError, match="timeout"):
        sina.sina_qfq_daily_klines("600519", timeout=timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("symbol", [None, "", "600519.SZ", "920000.SH", "not-a-symbol"])
def test_qfq_daily_klines_reject_invalid_or_conflicting_symbol(symbol: object) -> None:
    with pytest.raises(ValueError):
        sina.sina_qfq_daily_klines(symbol)  # type: ignore[arg-type]


def test_qfq_daily_klines_classifies_empty_market_data_as_coverage_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_sina_text(
        monkeypatch,
        raw_payload=[],
        qfq_text=_qfq_document("sh600519", [("1900-01-01", "1")]),
    )

    with pytest.raises(ProviderCoverageMiss, match="空序列"):
        sina.sina_qfq_daily_klines("600519")

    assert len(calls) == 1


def test_qfq_daily_klines_classifies_missing_factors_as_coverage_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_sina_text(
        monkeypatch,
        raw_payload=[_raw_row()],
        qfq_text=_qfq_document("sh600519", []),
    )

    with pytest.raises(ProviderCoverageMiss, match="前复权因子"):
        sina.sina_qfq_daily_klines("600519")


def test_qfq_daily_klines_requires_factor_coverage_for_every_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_sina_text(
        monkeypatch,
        raw_payload=[_raw_row(day="2024-01-02")],
        qfq_text=_qfq_document("sh600519", [("2024-01-03", "1")]),
    )

    with pytest.raises(ProviderCoverageMiss, match="2024-01-02"):
        sina.sina_qfq_daily_klines("600519")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"day": "2024/01/02"}, "YYYY-MM-DD"),
        ({"day": "2024-02-30"}, "有效"),
        ({"open": "0"}, "大于 0"),
        ({"close": "NaN"}, "有限数"),
        ({"high": "39"}, "OHLC"),
        ({"low": "45"}, "OHLC"),
        ({"volume": "-1"}, "负数"),
    ],
)
def test_raw_daily_bar_validation_rejects_bad_dates_prices_ohlc_and_volume(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ProviderProtocolError, match=message):
        sina._validated_raw_daily_bars([_raw_row(**overrides)], limit=1)


@pytest.mark.parametrize("payload", [None, []])
def test_raw_daily_bar_empty_payload_is_coverage_miss(payload: object) -> None:
    with pytest.raises(ProviderCoverageMiss):
        sina._validated_raw_daily_bars(payload, limit=1)


@pytest.mark.parametrize("payload", [{}, ["bad-row"]])
def test_raw_daily_bar_bad_structure_is_protocol_error(payload: object) -> None:
    with pytest.raises(ProviderProtocolError):
        sina._validated_raw_daily_bars(payload, limit=1)


def test_raw_daily_bar_rejects_duplicates_out_of_order_and_excess_rows() -> None:
    duplicate = [_raw_row(day="2024-01-02"), _raw_row(day="2024-01-02")]
    with pytest.raises(ProviderProtocolError, match="严格递增"):
        sina._validated_raw_daily_bars(duplicate, limit=2)

    out_of_order = [_raw_row(day="2024-01-03"), _raw_row(day="2024-01-02")]
    with pytest.raises(ProviderProtocolError, match="严格递增"):
        sina._validated_raw_daily_bars(out_of_order, limit=2)

    with pytest.raises(ProviderProtocolError, match="超过请求上限"):
        sina._validated_raw_daily_bars([_raw_row(), _raw_row(day="2024-01-03")], limit=1)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "空响应"),
        ("alert(1)", "var"),
        ('var sh600519qfq={"total":0,"data":[]}; alert(1)', "未知内容"),
        ('var sz000001qfq={"total":0,"data":[]}', "不一致"),
        ('var sh600519qfq={"total":1,"data":[]}', "不完整"),
        ('var sh600519qfq={"total":"0","data":[]}', "total"),
        ('var sh600519qfq={"total":0,"data":{}}', "data"),
    ],
)
def test_qfq_parser_rejects_executable_trailing_text_and_bad_structure(
    text: str,
    message: str,
) -> None:
    with pytest.raises(ProviderProtocolError, match=message):
        sina._validated_qfq_factors(text, expected_symbol="sh600519")


@pytest.mark.parametrize("factor", ["0", "-1", "NaN", "Infinity", True, None])
def test_qfq_parser_requires_positive_finite_factor(factor: object) -> None:
    text = _qfq_document("sh600519", [("1900-01-01", factor)])

    with pytest.raises(ProviderProtocolError):
        sina._validated_qfq_factors(text, expected_symbol="sh600519")


def test_qfq_parser_rejects_duplicate_or_invalid_dates() -> None:
    duplicate = _qfq_document(
        "sh600519",
        [("1900-01-01", "2"), ("1900-01-01", "1")],
    )
    with pytest.raises(ProviderProtocolError, match="重复日期"):
        sina._validated_qfq_factors(duplicate, expected_symbol="sh600519")

    invalid = _qfq_document("sh600519", [("2024-02-30", "1")])
    with pytest.raises(ProviderProtocolError, match="有效"):
        sina._validated_qfq_factors(invalid, expected_symbol="sh600519")


@pytest.mark.parametrize("text", ["not json", "[] trailing"])
def test_json_document_parser_rejects_malformed_or_trailing_content(text: str) -> None:
    with pytest.raises(ProviderProtocolError):
        sina._decode_json_document(text)


class _FakeResponse:
    def __init__(self, text: str = "[]", *, http_error: bool = False) -> None:
        self.text = text
        self.http_error = http_error

    def raise_for_status(self) -> None:
        if self.http_error:
            raise requests.HTTPError("503 from upstream")


class _FakeSession:
    def __init__(self, response: _FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response or _FakeResponse()
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    "session",
    [
        _FakeSession(error=requests.Timeout("timed out")),
        _FakeSession(response=_FakeResponse(http_error=True)),
    ],
)
def test_request_classifies_network_and_http_failures_as_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeSession,
) -> None:
    monkeypatch.setattr(sina, "_SINA_LAST_REQUEST_STARTED_AT", None)

    with pytest.raises(ProviderTransportError):
        sina._sina_request_text(
            sina.SINA_DAILY_KLINE_URL,
            params=None,
            timeout=1,
            min_interval=0,
            session_factory=lambda: session,
        )


def test_request_rejects_non_https_before_opening_session() -> None:
    with pytest.raises(ProviderProtocolError, match="HTTPS"):
        sina._sina_request_text(
            "http://quotes.sina.cn/data",
            params=None,
            timeout=1,
            min_interval=0,
            session_factory=pytest.fail,
        )


def test_request_throttle_is_serial_and_dependencies_are_injectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sina, "_SINA_LAST_REQUEST_STARTED_AT", None)
    clock_values = iter((10.0, 10.05, 10.10))
    sleeps: list[float] = []
    sessions: list[_FakeSession] = []

    def make_session() -> _FakeSession:
        session = _FakeSession(_FakeResponse("[]"))
        sessions.append(session)
        return session

    sina._sina_request_text(
        sina.SINA_DAILY_KLINE_URL,
        params={"symbol": "sh600519"},
        timeout=1,
        min_interval=0.1,
        clock=lambda: next(clock_values),
        sleeper=sleeps.append,
        session_factory=make_session,
    )
    sina._sina_request_text(
        sina.SINA_DAILY_KLINE_URL,
        params={"symbol": "sz000001"},
        timeout=1,
        min_interval=0.1,
        clock=lambda: next(clock_values),
        sleeper=sleeps.append,
        session_factory=make_session,
    )

    assert sleeps == pytest.approx([0.05])
    assert len(sessions) == 2
    assert sessions[0].calls[0][1]["params"] == {"symbol": "sh600519"}
    assert sessions[1].calls[0][1]["params"] == {"symbol": "sz000001"}


def test_request_throttle_does_not_hold_global_lock_during_network_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sina, "_SINA_LAST_REQUEST_STARTED_AT", None)
    release = threading.Event()
    both_started = threading.Event()
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    class BlockingSession(_FakeSession):
        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    both_started.set()
            try:
                release.wait(timeout=1)
                return super().get(url, **kwargs)
            finally:
                with state_lock:
                    active -= 1

    def request(symbol: str) -> str:
        return sina._sina_request_text(
            sina.SINA_DAILY_KLINE_URL,
            params={"symbol": symbol},
            timeout=1,
            min_interval=0,
            session_factory=BlockingSession,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(request, symbol) for symbol in ("sh600519", "sz000001")]
        try:
            assert both_started.wait(timeout=0.5)
        finally:
            release.set()
        assert [future.result(timeout=1) for future in futures] == ["[]", "[]"]

    assert max_active == 2
