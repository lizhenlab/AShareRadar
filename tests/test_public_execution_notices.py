from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from typing import Any

import httpx
import pytest

from app.services import public_execution_notices as notices


DAY = "2026-09-18"


def _sse_row(index: int = 0, **changes: object) -> dict[str, object]:
    return {"productCode": f"{600000 + index:06d}", "productName": f"股票{index}",
            "controlType": "TR", "startStopDate": "20260915", "endStopDate": "",
            "stopTime": "", "type": "LXTP", "stopReason": "重要公告", "endStopReason": "",
            **changes}


def _szse_row(index: int = 0, **changes: object) -> dict[str, object]:
    return {"zqdm": f"{index + 1:06d}", "zqjc": f"股票{index}",
            "tpkssj": f"{DAY} 开市", "fpkssj": f"{DAY} 10:30:00", "tpsj": "1小时",
            "tpyy": "重大事项", **changes}


def _payload(market: str, number: int = 1, total: int = 1) -> Any:
    size = 25 if market == "SH" else 10
    count = (total + size - 1) // size
    indexes = range((number - 1) * size, min(number * size, total))
    if market == "SH":
        rows = [_sse_row(index) for index in indexes]
        return {"sqlId": "GW_PL_JYTS_TFPXX", "isPagination": "true", "actionErrors": [],
                "fieldErrors": {}, "pageHelp": {"pageNo": number, "pageSize": size,
                "pageCount": count, "total": total, "data": rows}, "result": rows}
    return [{"metadata": {"catalogid": "1798", "tabkey": "tab1", "pageno": number,
             "pagesize": size, "pagecount": count, "recordcount": total, "subname": DAY},
             "data": [_szse_row(index) for index in indexes]}]


def _body(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> tuple[list[httpx.Request], list[dict[str, Any]]]:
    requests: list[httpx.Request] = []
    options: list[dict[str, Any]] = []
    client_type = httpx.Client

    def dispatch(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request, len(requests))

    def client(**kwargs: Any) -> httpx.Client:
        options.append(kwargs)
        return client_type(transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(notices.httpx, "Client", client)
    return requests, options


def _success(monkeypatch: pytest.MonkeyPatch, market: str = "SH", total: int = 1) -> dict[str, Any]:
    key = "pageHelp.pageNo" if market == "SH" else "PAGENO"
    _install(monkeypatch, lambda request, _: httpx.Response(
        200, text=_body(_payload(market, int(request.url.params[key]), total))))
    result = notices.fetch_exchange_execution_notices(market, DAY)
    assert result["status"] == "success", result
    return result


def _replace_raw(envelope: dict[str, Any], payload: Any, *, verification: bool = True) -> None:
    envelope["response"]["pages"][0]["body"] = _body(payload)
    if verification:
        envelope["response"]["verification_pages"][0]["body"] = _body(payload)
    envelope["response"]["total_bytes"] = sum(
        len(page["body"].encode()) for key in ("pages", "verification_pages")
        for page in envelope["response"][key])


@pytest.mark.parametrize("market,total", [("SH", 0), ("SH", 1), ("SH", 26), ("SZ", 0), ("SZ", 1), ("SZ", 21)])
def test_fixed_endpoints_paginated_raw_stability_and_transport(monkeypatch: pytest.MonkeyPatch, market: str, total: int) -> None:
    key = "pageHelp.pageNo" if market == "SH" else "PAGENO"
    requests, options = _install(monkeypatch, lambda request, _: httpx.Response(
        200, text=_body(_payload(market, int(request.url.params[key]), total))))
    before = datetime.now(UTC)
    envelope = notices.fetch_exchange_execution_notices(market, DAY, timeout_seconds=3, total_timeout_seconds=20)
    after = datetime.now(UTC)
    assert envelope["status"] == "success"
    response = envelope["response"]
    assert isinstance(response, dict)
    size = 25 if market == "SH" else 10
    count = (total + size - 1) // size
    expected_pages = list(range(1, max(1, count) + 1)) + ([1, count] if count > 1 else [1])
    assert [int(request.url.params[key]) for request in requests] == expected_pages
    assert all(request.method == "GET" and request.url.scheme == "https" for request in requests)
    assert all(str(request.url).startswith(str(envelope["endpoint"]) + "?") for request in requests)
    assert all("authorization" not in request.headers for request in requests)
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert all(0 < request.extensions["timeout"]["read"] <= 3 for request in requests)
    assert before <= datetime.fromisoformat(str(envelope["requested_at"])) <= after
    assert before <= datetime.fromisoformat(str(envelope["observed_at"])) <= after
    assert response["total_records"] == total and response["page_count"] == count
    normalized = notices.normalize_exchange_execution_notices(envelope)
    assert len(normalized) == total
    assert all(row["session_date"] == DAY for row in normalized)
    assert all("executable" not in row and "tradable" not in row for row in normalized)


@pytest.mark.parametrize("market,field,value,expected_type", [
    ("SH", "controlType", "GB", "conversion_suspension_notice"),
    ("SH", "controlType", "CB", "conversion_suspension_notice"),
    ("SH", "controlType", "GP", "bond_suspension_notice"),
    ("SH", "controlType", "CP", "bond_suspension_notice"),
    ("SH", "controlType", "GR", "bond_putback_suspension_notice"),
    ("SH", "controlType", "CR", "bond_putback_suspension_notice"),
    ("SH", "productCode", "118060", "suspension_notice"),
    ("SZ", "zqdm", "159501", "suspension_notice"),
])
def test_nonstock_and_conversion_notices_never_match_stock(monkeypatch: pytest.MonkeyPatch, market: str,
                                                          field: str, value: str, expected_type: str) -> None:
    payload = _payload(market)
    row = payload["result"][0] if market == "SH" else payload[0]["data"][0]
    row[field] = value
    row["extra_raw"] = {"保留": [1, None]}
    _install(monkeypatch, lambda *_: httpx.Response(200, text=_body(payload)))
    normalized = notices.normalize_exchange_execution_notices(notices.fetch_exchange_execution_notices(market, DAY))
    assert normalized[0]["symbol"] is None
    assert normalized[0]["instrument_type"] == "other_or_unknown"
    assert normalized[0]["notice_type"] == expected_type
    assert normalized[0]["raw_row"] == row


@pytest.mark.parametrize("market", ["SH", "SZ"])
def test_partial_halts_keep_raw_times_without_full_day_inference(monkeypatch: pytest.MonkeyPatch, market: str) -> None:
    envelope = _success(monkeypatch, market)
    rows = notices.normalize_exchange_execution_notices(envelope)
    row = rows[0]
    assert row["symbol"] == ("600000.SH" if market == "SH" else "000001.SZ")
    assert row["raw_period"] == ("" if market == "SH" else "1小时")
    assert row["raw_start_time"] == ("20260915" if market == "SH" else f"{DAY} 开市")
    assert row["raw_end_time"] == ("" if market == "SH" else f"{DAY} 10:30:00")
    assert "status" not in row and "is_suspended" not in row


@pytest.mark.parametrize("market,day,kwargs", [
    ("BJ", DAY, {}), ("sh", DAY, {}), ([], DAY, {}),
    ("SH", "20260918", {}), ("SH", "2026-02-30", {}), ("SH", None, {}),
    ("SH", DAY, {"timeout_seconds": float("nan")}),
    ("SH", DAY, {"timeout_seconds": float("inf")}),
    ("SH", DAY, {"timeout_seconds": 0}), ("SH", DAY, {"timeout_seconds": True}),
    ("SH", DAY, {"timeout_seconds": "10"}), ("SH", DAY, {"timeout_seconds": 10**500}),
    ("SH", DAY, {"total_timeout_seconds": 121}),
])
def test_invalid_input_never_requests(monkeypatch: pytest.MonkeyPatch, market: Any, day: Any, kwargs: Any) -> None:
    requests, _ = _install(monkeypatch, lambda *_: pytest.fail("must not request"))
    result = notices.fetch_exchange_execution_notices(market, day, **kwargs)
    assert result["status"] == "error" and result["error_type"] == "input_invalid"
    assert requests == [] and result["observed_at"] is not None


@pytest.mark.parametrize("body", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', "not JSON", "[]", "null", '"a"'])
def test_invalid_json_or_shape_is_never_partial_success(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    _install(monkeypatch, lambda *_: httpx.Response(200, text=body))
    result = notices.fetch_exchange_execution_notices("SH", DAY)
    assert result["status"] == "error"
    assert result["error_type"] in {"json_invalid", "schema_invalid"}
    assert result["response"]["pages"] == [{"page_no": 1, "body": body}]
    with pytest.raises(ValueError):
        notices.normalize_exchange_execution_notices(result)


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
def test_http_status_and_redirect_never_follow(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    requests, _ = _install(monkeypatch, lambda *_: httpx.Response(status, headers={"location": "https://evil.invalid/"}))
    result = notices.fetch_exchange_execution_notices("SZ", DAY)
    assert result["error_type"] == "http_status_error"
    assert len(requests) == 1


@pytest.mark.parametrize("error,code", [(httpx.ReadTimeout, "network_timeout"), (httpx.ConnectError, "network_error")])
def test_network_errors_are_safe_and_keep_previous_pages(monkeypatch: pytest.MonkeyPatch, error: Any, code: str) -> None:
    def handler(request: httpx.Request, call: int) -> httpx.Response:
        if call == 2:
            raise error("private diagnostic must not leak", request=request)
        return httpx.Response(200, text=_body(_payload("SH", total=26)))
    _install(monkeypatch, handler)
    result = notices.fetch_exchange_execution_notices("SH", DAY)
    assert result["error_type"] == code
    assert len(result["response"]["pages"]) == 1
    assert result["response"]["pagination_verified"] is False
    assert "private diagnostic" not in json.dumps(result)


def _mutate_payload(case: str, payload: Any) -> None:
    if isinstance(payload, list):
        meta, rows = payload[0]["metadata"], payload[0]["data"]
    else:
        meta, rows = payload["pageHelp"], payload["result"]
    actions = {
        "total_bool": lambda: meta.update(total=True),
        "size_wrong": lambda: meta.update(pageSize=20),
        "number_wrong": lambda: meta.update(pageNo=2),
        "count_wrong": lambda: meta.update(pageCount=3),
        "total_wrong": lambda: meta.update(total=2),
        "row_missing": lambda: rows[0].pop("stopTime"),
        "row_type": lambda: rows[0].update(stopTime=None),
        "code_wrong": lambda: rows[0].update(productCode="600000.SH"),
        "start_bad": lambda: rows[0].update(startStopDate="20260230"),
        "start_format": lambda: rows[0].update(startStopDate="2026-09-18"),
        "start_future": lambda: rows[0].update(startStopDate="20260919"),
        "end_past": lambda: rows[0].update(endStopDate="20260917"),
        "end_format": lambda: rows[0].update(endStopDate="bad"),
        "sse_source": lambda: payload.update(sqlId="other"),
        "sse_error": lambda: payload.update(actionErrors=["private upstream message"]),
        "sse_data": lambda: meta.update(data=[]),
        "sz_source": lambda: meta.update(catalogid="other"),
        "sz_date": lambda: meta.update(subname="2026-09-17"),
        "sz_date_type": lambda: meta.update(subname=[]),
        "sz_start": lambda: rows[0].update(tpkssj="invalid"),
        "sz_data": lambda: payload[0].update(data={}),
        "sz_reports": lambda: payload.append(deepcopy(payload[0])),
        "sz_metadata": lambda: payload[0].update(metadata=[]),
        "sz_row": lambda: rows.__setitem__(0, []),
    }
    actions[case]()


@pytest.mark.parametrize("case", [
    "total_bool", "size_wrong", "number_wrong", "count_wrong", "total_wrong", "row_missing",
    "row_type", "code_wrong", "start_bad", "start_format", "start_future", "end_past",
    "end_format", "sse_source", "sse_error", "sse_data", "sz_source", "sz_date", "sz_date_type",
    "sz_start", "sz_data", "sz_reports", "sz_metadata", "sz_row",
])
def test_strict_metadata_rows_and_date_binding(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    market = "SZ" if case.startswith("sz_") else "SH"
    payload = _payload(market)
    _mutate_payload(case, payload)
    _install(monkeypatch, lambda *_: httpx.Response(200, text=_body(payload)))
    result = notices.fetch_exchange_execution_notices(market, DAY)
    assert result["status"] == "error"
    assert result["error_type"] in {"schema_invalid", "pagination_inconsistent", "session_date_mismatch", "upstream_error"}
    assert result["response"]["pagination_verified"] is False


@pytest.mark.parametrize("changed_call", [2, 3, 4])
def test_middle_or_rechecked_first_last_change_fails(monkeypatch: pytest.MonkeyPatch, changed_call: int) -> None:
    def handler(request: httpx.Request, call: int) -> httpx.Response:
        number = int(request.url.params["pageHelp.pageNo"])
        payload = _payload("SH", number, 26)
        if call == changed_call:
            if call == 2:
                payload = _payload("SH", number, 27)
            else:
                payload["result"][0]["stopReason"] = "changed"
        return httpx.Response(200, text=_body(payload))
    _install(monkeypatch, handler)
    result = notices.fetch_exchange_execution_notices("SH", DAY)
    assert result["status"] == "error" and result["error_type"] == "pagination_changed"


def test_duplicate_across_pages_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request, _: int) -> httpx.Response:
        number = int(request.url.params["pageHelp.pageNo"])
        payload = _payload("SH", number, 26)
        if number == 2:
            payload["result"][0].update(_sse_row())
        return httpx.Response(200, text=_body(payload))
    _install(monkeypatch, handler)
    result = notices.fetch_exchange_execution_notices("SH", DAY)
    assert result["error_type"] == "duplicate_notice"
    assert result["response"]["pagination_verified"] is False


def test_distinct_notices_for_same_stock_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload("SH", total=2)
    payload["result"][1]["productCode"] = payload["result"][0]["productCode"]
    _install(monkeypatch, lambda *_: httpx.Response(200, text=_body(payload)))
    rows = notices.normalize_exchange_execution_notices(notices.fetch_exchange_execution_notices("SH", DAY))
    assert len(rows) == 2 and rows[0]["symbol"] == rows[1]["symbol"]


@pytest.mark.parametrize("market,total", [("SH", 451), ("SZ", 181)])
def test_page_budget_includes_first_last_verification(monkeypatch: pytest.MonkeyPatch, market: str, total: int) -> None:
    requests, _ = _install(monkeypatch, lambda *_: httpx.Response(200, text=_body(_payload(market, total=total))))
    result = notices.fetch_exchange_execution_notices(market, DAY)
    assert result["error_type"] == "page_limit_exceeded" and len(requests) == 1


def test_exact_twenty_request_limit_can_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _success(monkeypatch, "SZ", 171)
    assert len(envelope["request"]["page_params"]) == 20
    assert len(notices.normalize_exchange_execution_notices(envelope)) == 171


def test_byte_budget_counts_repeat_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _body(_payload("SH"))
    monkeypatch.setattr(notices, "MAX_RESPONSE_BYTES", len(body.encode()) + 10)
    _install(monkeypatch, lambda *_: httpx.Response(200, text=body))
    result = notices.fetch_exchange_execution_notices("SH", DAY)
    assert result["error_type"] == "response_too_large"
    assert len(result["response"]["pages"]) == 1


def test_total_deadline_stops_and_preserves_completed_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(notices, "monotonic_now", lambda: clock[0])
    def handler(_: httpx.Request, call: int) -> httpx.Response:
        if call == 2:
            clock[0] = 31.0
        return httpx.Response(200, text=_body(_payload("SH")))
    _install(monkeypatch, handler)
    result = notices.fetch_exchange_execution_notices("SH", DAY)
    assert result["error_type"] == "total_timeout"
    assert len(result["response"]["pages"]) == 1


def test_utf8_failure_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda *_: httpx.Response(200, content=b"\xff"))
    result = notices.fetch_exchange_execution_notices("SH", DAY)
    assert result["error_type"] == "json_invalid"


@pytest.mark.parametrize("case", ["provider", "endpoint", "status", "error", "market", "representation",
                                  "timestamp_type", "timestamp_invalid", "timestamp_naive", "timestamp_order",
                                  "request_method", "request_date", "request_timeout", "response_verified",
                                  "response_bytes", "response_bool_count", "response_pages", "missing_repeat",
                                  "raw_body_type", "raw_body_surrogate", "raw_page_zero", "empty_pages"])
def test_normalization_revalidates_envelope_and_raw_pages(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    envelope = _success(monkeypatch)
    mutations = {
        "provider": lambda: envelope.update(provider="other"),
        "endpoint": lambda: envelope.update(endpoint="https://evil.invalid/"),
        "status": lambda: envelope.update(status="error"),
        "error": lambda: envelope.update(error_type="partial"),
        "market": lambda: envelope.update(market=[]),
        "representation": lambda: envelope.update(representation="normalized"),
        "timestamp_type": lambda: envelope.update(requested_at=0),
        "timestamp_invalid": lambda: envelope.update(requested_at="bad"),
        "timestamp_naive": lambda: envelope.update(requested_at="2026-09-18T01:00:00"),
        "timestamp_order": lambda: envelope.update(requested_at="2099-09-18T01:00:00+00:00"),
        "request_method": lambda: envelope["request"].update(method="POST"),
        "request_date": lambda: envelope["request"]["page_params"][0].update(startStopDate="20260917"),
        "request_timeout": lambda: envelope["request"].update(timeout_seconds=False),
        "response_verified": lambda: envelope["response"].update(pagination_verified=False),
        "response_bytes": lambda: envelope["response"].update(total_bytes=1),
        "response_bool_count": lambda: envelope["response"].update(total_records=True),
        "response_pages": lambda: envelope["response"]["pages"].append(deepcopy(envelope["response"]["pages"][0])),
        "missing_repeat": lambda: envelope["response"].update(verification_pages=[]),
        "raw_body_type": lambda: envelope["response"]["pages"][0].update(body={}),
        "raw_body_surrogate": lambda: envelope["response"]["pages"][0].update(body="\ud800"),
        "raw_page_zero": lambda: envelope["response"]["pages"][0].update(page_no=0),
        "empty_pages": lambda: envelope["response"].update(pages=[]),
    }
    mutations[case]()
    with pytest.raises(ValueError):
        notices.normalize_exchange_execution_notices(envelope)


def test_normalization_does_not_trust_success_when_page_contents_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _success(monkeypatch)
    payload = _payload("SH")
    payload["result"][0]["stopReason"] = "new"
    _replace_raw(envelope, payload, verification=False)
    with pytest.raises(ValueError, match="pagination_changed"):
        notices.normalize_exchange_execution_notices(envelope)


def test_normalization_rejects_duplicate_and_oversized_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _success(monkeypatch, total=2)
    payload = _payload("SH", total=2)
    payload["result"][1].update(payload["result"][0])
    _replace_raw(envelope, payload)
    with pytest.raises(ValueError, match="duplicate_notice"):
        notices.normalize_exchange_execution_notices(envelope)
    monkeypatch.setattr(notices, "MAX_RESPONSE_BYTES", 1)
    with pytest.raises(ValueError, match="response_too_large"):
        notices.normalize_exchange_execution_notices(envelope)


def test_normalization_combined_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _success(monkeypatch)
    monkeypatch.setattr(notices, "MAX_RESPONSE_BYTES", envelope["response"]["total_bytes"] - 1)
    with pytest.raises(ValueError, match="response_too_large"):
        notices.normalize_exchange_execution_notices(envelope)


def test_normalization_requires_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="envelope_invalid"):
        notices.normalize_exchange_execution_notices([])


def test_empty_szse_date_label_and_zero_or_one_pages_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload("SZ", total=0)
    payload[0]["metadata"].update(subname="", pagecount=1)
    _install(monkeypatch, lambda *_: httpx.Response(200, text=_body(payload)))
    envelope = notices.fetch_exchange_execution_notices("SZ", DAY)
    assert envelope["status"] == "success"
    assert notices.normalize_exchange_execution_notices(envelope) == []


def test_unknown_szse_resume_time_is_preserved_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload("SZ")
    payload[0]["data"][0]["fpkssj"] = "待公告"
    _install(monkeypatch, lambda *_: httpx.Response(200, text=_body(payload)))
    rows = notices.normalize_exchange_execution_notices(notices.fetch_exchange_execution_notices("SZ", DAY))
    assert rows[0]["raw_end_time"] == "待公告"


@pytest.mark.parametrize("code,symbol", [("301266", "301266.SZ"), ("123224", None)])
def test_official_szse_resumption_notice_may_have_no_suspension_start(monkeypatch: pytest.MonkeyPatch,
                                                                 code: str, symbol: str | None) -> None:
    # The official 2026-09-07 response uses these fields for a stock and a bond.
    payload = _payload("SZ")
    payload[0]["data"][0].update(zqdm=code, tpkssj="", fpkssj=f"{DAY} 开市", tpsj="取消停牌")
    _install(monkeypatch, lambda *_: httpx.Response(200, text=_body(payload)))
    envelope = notices.fetch_exchange_execution_notices("SZ", DAY)
    assert envelope["status"] == "success"
    row = notices.normalize_exchange_execution_notices(envelope)[0]
    assert row["symbol"] == symbol and row["notice_type"] == "resumption_notice"
    assert row["raw_start_time"] == "" and row["raw_end_time"] == f"{DAY} 开市"
    assert "executable" not in row


@pytest.mark.parametrize("period,end", [("1小时", f"{DAY} 开市"), ("取消停牌", ""),
                                       ("取消停牌", "2026-09-17 开市")])
def test_empty_start_requires_explicit_same_day_resumption(monkeypatch: pytest.MonkeyPatch,
                                                         period: str, end: str) -> None:
    payload = _payload("SZ")
    payload[0]["data"][0].update(tpkssj="", fpkssj=end, tpsj=period)
    _install(monkeypatch, lambda *_: httpx.Response(200, text=_body(payload)))
    assert notices.fetch_exchange_execution_notices("SZ", DAY)["error_type"] == "schema_invalid"


def test_non_ascii_stock_digits_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload("SH")
    payload["result"][0]["productCode"] = "600١٢٣"
    _install(monkeypatch, lambda *_: httpx.Response(200, text=_body(payload)))
    assert notices.fetch_exchange_execution_notices("SH", DAY)["error_type"] == "schema_invalid"
