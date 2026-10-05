"""Bounded public exchange notices; absence never establishes executability."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
import math
import re
from typing import cast

import httpx

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, decode_json_bytes
from app.utils.clock import monotonic_now, utc_now


SCHEMA_VERSION = "public-exchange-execution-notices-v1"
MAX_HTTP_REQUESTS = 20
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SSE_ENDPOINT = "https://query.sse.com.cn/commonSoaQuery.do"
SZSE_ENDPOINT = "https://www.szse.cn/api/report/ShowReport/data"
SSE_LEGAL_URL = "https://www.sse.com.cn/home/legal/"
SZSE_LEGAL_URL = "https://www.szse.cn/application/laws/index.html"
_SOURCES = {
    "SH": ("sse_public_notice", SSE_ENDPOINT, "https://www.sse.com.cn/", 25),
    "SZ": ("szse_public_notice", SZSE_ENDPOINT, "https://www.szse.cn/disclosure/memo/index.html", 10),
}
_SSE_FIELDS = (
    "productCode", "productName", "controlType", "startStopDate", "endStopDate",
    "stopTime", "type", "stopReason", "endStopReason",
)
_SZSE_FIELDS = ("zqdm", "zqjc", "tpkssj", "fpkssj", "tpsj", "tpyy")
_STOCK_CODES = {"SH": re.compile(r"(?:600|601|603|605|688|689)[0-9]{3}"),
                "SZ": re.compile(r"(?:000|001|002|003|300|301)[0-9]{3}")}


class _NoticeError(ValueError):
    """A public-safe, fixed error code, never an upstream response message."""


@dataclass(frozen=True)
class _Page:
    number: int
    size: int
    count: int
    total: int
    rows: list[dict[str, object]]

    @property
    def contract(self) -> tuple[int, int, int]:
        return self.size, self.count, self.total


@dataclass
class _Budget:
    deadline: float
    timeout: float
    requests: int = 0
    byte_count: int = 0

    def remaining(self) -> float:
        remaining = self.deadline - monotonic_now()
        if remaining <= 0:
            raise _NoticeError("total_timeout")
        return min(self.timeout, remaining)

    def consume(self, chunk: bytes) -> None:
        self.remaining()
        self.byte_count += len(chunk)
        if self.byte_count > MAX_RESPONSE_BYTES:
            raise _NoticeError("response_too_large")


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _NoticeError("schema_invalid")
    return cast(dict[str, object], value)


def _sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise _NoticeError("schema_invalid")
    return cast(list[object], value)


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise _NoticeError("schema_invalid")
    return value


def _iso_date(value: object) -> str:
    if not isinstance(value, str):
        raise _NoticeError("input_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _NoticeError("input_invalid") from exc
    if parsed.isoformat() != value:
        raise _NoticeError("input_invalid")
    return value


def _timeout(value: object, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _NoticeError("input_invalid")
    if not 0 < value <= maximum or not math.isfinite(value):
        raise _NoticeError("input_invalid")
    return float(value)


def _params(market: str, session_date: str, page: int) -> dict[str, str | int]:
    if market == "SH":
        day = session_date.replace("-", "")
        return {"isPagination": "true", "sqlId": "GW_PL_JYTS_TFPXX",
                "pageHelp.pageSize": 25, "pageHelp.pageNo": page,
                "productCode": "", "keyWords": "", "startStopDate": day, "endStopDate": day}
    return {"SHOWTYPE": "JSON", "CATALOGID": "1798", "TABKEY": "tab1",
            "txtKsrq": session_date, "txtZzrq": session_date, "PAGENO": page}


def _sse_payload(payload: object) -> tuple[dict[str, object], list[object]]:
    source = _mapping(payload)
    if source.get("sqlId") != "GW_PL_JYTS_TFPXX" or source.get("isPagination") != "true":
        raise _NoticeError("schema_invalid")
    if source.get("actionErrors") or source.get("fieldErrors"):
        raise _NoticeError("upstream_error")
    meta = _mapping(source.get("pageHelp"))
    rows = _sequence(source.get("result"))
    if meta.get("data") != rows:
        raise _NoticeError("pagination_inconsistent")
    return {"size": meta.get("pageSize"), "number": meta.get("pageNo"),
            "count": meta.get("pageCount"), "total": meta.get("total")}, rows


def _szse_payload(payload: object, session_date: str) -> tuple[dict[str, object], list[object]]:
    reports = _sequence(payload)
    if len(reports) != 1:
        raise _NoticeError("schema_invalid")
    report = _mapping(reports[0])
    meta = _mapping(report.get("metadata"))
    if meta.get("catalogid") != "1798" or meta.get("tabkey") != "tab1":
        raise _NoticeError("schema_invalid")
    total = _integer(meta.get("recordcount"))
    allowed_dates = {session_date} if total else {session_date, ""}
    subname = meta.get("subname")
    if not isinstance(subname, str) or subname not in allowed_dates:
        raise _NoticeError("session_date_mismatch")
    return {"size": meta.get("pagesize"), "number": meta.get("pageno"),
            "count": meta.get("pagecount"), "total": total}, _sequence(report.get("data"))


def _raw_dates(row: dict[str, object], market: str) -> tuple[str, str]:
    if market == "SH":
        start, end = str(row["startStopDate"]), str(row["endStopDate"])
        if not re.fullmatch(r"[0-9]{8}", start) or (end and not re.fullmatch(r"[0-9]{8}", end)):
            raise _NoticeError("schema_invalid")
        start = f"{start[:4]}-{start[4:6]}-{start[6:]}"
        end = f"{end[:4]}-{end[4:6]}-{end[6:]}" if end else ""
    else:
        start, raw_end = str(row["tpkssj"]), str(row["fpkssj"])
        start = start[:10]
        end = raw_end[:10] if re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw_end) else ""
    return start, end


def _row_dates(row: dict[str, object], market: str, session_date: str) -> None:
    start, end = _raw_dates(row, market)
    if market == "SZ" and not start:
        if row["tpsj"] != "取消停牌" or end != session_date:
            raise _NoticeError("schema_invalid")
        return
    try:
        _iso_date(start)
        if end:
            _iso_date(end)
    except _NoticeError as exc:
        raise _NoticeError("schema_invalid") from exc
    if start > session_date or (end and end < session_date):
        raise _NoticeError("session_date_mismatch")


def _rows(raw_rows: list[object], market: str, session_date: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    fields = _SSE_FIELDS if market == "SH" else _SZSE_FIELDS
    code_field = "productCode" if market == "SH" else "zqdm"
    for value in raw_rows:
        row = _mapping(value)
        if any(not isinstance(row.get(key), str) for key in fields):
            raise _NoticeError("schema_invalid")
        if not re.fullmatch(r"[0-9]{6}", str(row[code_field])):
            raise _NoticeError("schema_invalid")
        _row_dates(row, market, session_date)
        rows.append(row)
    return rows


def _page(body: str, market: str, session_date: str, number: int) -> _Page:
    try:
        payload = decode_json_bytes(body.encode("utf-8"))
    except (ArtifactIOError, UnicodeError) as exc:
        raise _NoticeError("json_invalid") from exc
    meta, values = _sse_payload(payload) if market == "SH" else _szse_payload(payload, session_date)
    size = _integer(meta["size"], minimum=1)
    count, total = _integer(meta["count"]), _integer(meta["total"])
    if _integer(meta["number"], minimum=1) != number or size != _SOURCES[market][3]:
        raise _NoticeError("pagination_inconsistent")
    expected_count = (total + size - 1) // size
    if count not in ({0, 1} if total == 0 else {expected_count}):
        raise _NoticeError("pagination_inconsistent")
    if number > max(1, count) or len(values) != min(size, max(0, total - (number - 1) * size)):
        raise _NoticeError("pagination_inconsistent")
    if max(1, count) + (2 if count > 1 else 1) > MAX_HTTP_REQUESTS:
        raise _NoticeError("page_limit_exceeded")
    return _Page(number, size, count, total, _rows(values, market, session_date))


def _read_http(client: httpx.Client, endpoint: str, params: dict[str, str | int], budget: _Budget) -> str:
    budget.requests += 1
    if budget.requests > MAX_HTTP_REQUESTS:
        raise _NoticeError("page_limit_exceeded")
    chunks: list[bytes] = []
    with client.stream("GET", endpoint, params=params, timeout=budget.remaining()) as response:
        if response.status_code != 200:
            raise _NoticeError("http_status_error")
        for chunk in response.iter_bytes():
            budget.consume(chunk)
            chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeError as exc:
        raise _NoticeError("json_invalid") from exc


def _fetch_page(client: httpx.Client, envelope: dict[str, object], budget: _Budget,
                number: int, *, verification: bool = False) -> _Page:
    market, session_date = str(envelope["market"]), str(envelope["session_date"])
    params = _params(market, session_date, number)
    request = _mapping(envelope["request"])
    _sequence(request["page_params"]).append(params)
    body = _read_http(client, str(envelope["endpoint"]), params, budget)
    response = _mapping(envelope["response"])
    target = "verification_pages" if verification else "pages"
    _sequence(response[target]).append({"page_no": number, "body": body})
    response["total_bytes"] = budget.byte_count
    return _page(body, market, session_date, number)


def _collect(client: httpx.Client, envelope: dict[str, object], budget: _Budget) -> None:
    first = _fetch_page(client, envelope, budget, 1)
    pages = [first]
    for number in range(2, first.count + 1):
        page = _fetch_page(client, envelope, budget, number)
        if page.contract != first.contract:
            raise _NoticeError("pagination_changed")
        pages.append(page)
    for original in ([first, pages[-1]] if len(pages) > 1 else [first]):
        repeated = _fetch_page(client, envelope, budget, original.number, verification=True)
        if repeated != original:
            raise _NoticeError("pagination_changed")
    response = _mapping(envelope["response"])
    response.update(total_records=first.total, page_count=first.count, page_size=first.size,
                    fetched_page_count=len(pages), pagination_verified=True)
    _unique_rows(pages)
    budget.remaining()


def _unique_rows(pages: list[_Page]) -> list[dict[str, object]]:
    seen: set[bytes] = set()
    rows: list[dict[str, object]] = []
    for page in pages:
        for row in page.rows:
            identity = canonical_json_bytes(row)
            if identity in seen:
                raise _NoticeError("duplicate_notice")
            seen.add(identity)
            rows.append(row)
    return rows


def _envelope(market: str, session_date: str) -> dict[str, object]:
    source = _SOURCES.get(market) if isinstance(market, str) else None
    return {"schema_version": SCHEMA_VERSION, "provider": source[0] if source else None,
            "market": market, "endpoint": source[1] if source else None, "session_date": session_date,
            "requested_at": utc_now().isoformat(), "observed_at": None, "status": "error",
            "representation": "http_json_pages", "error_type": None,
            "request": {"method": "GET", "page_params": []},
            "response": {"pages": [], "verification_pages": [], "total_records": None,
                         "page_count": None, "total_bytes": 0, "pagination_verified": False}}


def fetch_exchange_execution_notices(market: str, session_date: str, *, timeout_seconds: float = 10,
                                     total_timeout_seconds: float = 30) -> dict[str, object]:
    """Collect a date-bound notice set, preserving raw completed pages even on failure.

    Only the two fixed public endpoints are contacted. The cumulative deadline is
    checked between requests and streamed chunks; a currently waiting socket read
    is additionally bounded by the remaining budget at the request's start.
    """
    envelope = _envelope(market, session_date)
    try:
        if not isinstance(market, str) or market not in _SOURCES:
            raise _NoticeError("input_invalid")
        _iso_date(session_date)
        timeout, total = _timeout(timeout_seconds, 60), _timeout(total_timeout_seconds, 120)
        _mapping(envelope["request"]).update(timeout_seconds=timeout, total_timeout_seconds=total)
        budget = _Budget(monotonic_now() + total, timeout)
        headers = {"User-Agent": "AShareRadar-PublicNotice/1.0", "Referer": _SOURCES[market][2]}
        with httpx.Client(trust_env=False, follow_redirects=False, headers=headers) as client:
            _collect(client, envelope, budget)
        envelope["status"] = "success"
    except _NoticeError as exc:
        envelope["error_type"] = str(exc)
    except httpx.TimeoutException:
        envelope["error_type"] = "network_timeout"
    except httpx.HTTPError:
        envelope["error_type"] = "network_error"
    finally:
        envelope["observed_at"] = utc_now().isoformat()
    if envelope["status"] != "success":
        _mapping(envelope["response"])["pagination_verified"] = False
    return envelope


def _timestamps(envelope: Mapping[str, object]) -> None:
    parsed: list[datetime] = []
    for key in ("requested_at", "observed_at"):
        value = envelope.get(key)
        if not isinstance(value, str):
            raise _NoticeError("envelope_invalid")
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError as exc:
            raise _NoticeError("envelope_invalid") from exc
        if stamp.tzinfo is None or stamp.utcoffset() != UTC.utcoffset(stamp):
            raise _NoticeError("envelope_invalid")
        parsed.append(stamp)
    if parsed[1] < parsed[0]:
        raise _NoticeError("envelope_invalid")


def _raw_pages(value: object, market: str, session_date: str) -> tuple[list[_Page], int]:
    entries = _sequence(value)
    if len(entries) > MAX_HTTP_REQUESTS:
        raise _NoticeError("page_limit_exceeded")
    pages: list[_Page] = []
    total_bytes = 0
    for entry in entries:
        raw = _mapping(entry)
        number, body = _integer(raw.get("page_no"), minimum=1), raw.get("body")
        if not isinstance(body, str):
            raise _NoticeError("envelope_invalid")
        try:
            total_bytes += len(body.encode("utf-8"))
        except UnicodeError as exc:
            raise _NoticeError("json_invalid") from exc
        if total_bytes > MAX_RESPONSE_BYTES:
            raise _NoticeError("response_too_large")
        pages.append(_page(body, market, session_date, number))
    return pages, total_bytes


def _revalidate_pages(response: dict[str, object], market: str,
                      session_date: str) -> tuple[list[dict[str, object]], list[int]]:
    pages, size = _raw_pages(response.get("pages"), market, session_date)
    verification, verify_size = _raw_pages(response.get("verification_pages"), market, session_date)
    if not pages or len(pages) + len(verification) > MAX_HTTP_REQUESTS:
        raise _NoticeError("pagination_inconsistent")
    first = pages[0]
    expected_numbers = list(range(1, max(1, first.count) + 1))
    if [page.number for page in pages] != expected_numbers:
        raise _NoticeError("pagination_inconsistent")
    if any(page.contract != first.contract for page in pages):
        raise _NoticeError("pagination_changed")
    expected_verification = [first, pages[-1]] if len(pages) > 1 else [first]
    if verification != expected_verification:
        raise _NoticeError("pagination_changed")
    if size + verify_size > MAX_RESPONSE_BYTES:
        raise _NoticeError("response_too_large")
    _response_contract(response, first, len(pages), size + verify_size)
    return _unique_rows(pages), expected_numbers + [page.number for page in expected_verification]


def _response_contract(response: dict[str, object], first: _Page, page_count: int, byte_count: int) -> None:
    expected = {"total_bytes": byte_count, "total_records": first.total,
                "page_count": first.count, "page_size": first.size, "fetched_page_count": page_count}
    if any(type(response.get(key)) is not int or response[key] != value for key, value in expected.items()):
        raise _NoticeError("envelope_invalid")
    if response.get("pagination_verified") is not True:
        raise _NoticeError("envelope_invalid")


def _notice(row: dict[str, object], market: str, session_date: str) -> dict[str, object]:
    is_sse = market == "SH"
    code = str(row["productCode" if is_sse else "zqdm"])
    control = str(row.get("controlType", ""))
    stock = bool(_STOCK_CODES[market].fullmatch(code)) and (not is_sse or control == "TR")
    notice_type = {"CB": "conversion_suspension_notice", "GB": "conversion_suspension_notice",
                   "CP": "bond_suspension_notice", "GP": "bond_suspension_notice",
                   "CR": "bond_putback_suspension_notice", "GR": "bond_putback_suspension_notice"}
    kind = notice_type.get(control, "suspension_notice")
    if not is_sse and row["tpsj"] == "取消停牌":
        kind = "resumption_notice"
    return {"provider": _SOURCES[market][0], "market": market, "session_date": session_date,
            "product_code": code, "symbol": f"{code}.{market}" if stock else None,
            "instrument_type": "stock" if stock else "other_or_unknown",
            "notice_type": kind,
            "raw_start_time": row["startStopDate" if is_sse else "tpkssj"],
            "raw_end_time": row["endStopDate" if is_sse else "fpkssj"],
            "raw_period": row["stopTime" if is_sse else "tpsj"], "raw_row": dict(row)}


def normalize_exchange_execution_notices(envelope: Mapping[str, object]) -> list[dict[str, object]]:
    """Revalidate raw pages and return notice facts, never full-day trading states.

    Raises ``ValueError`` with a safe code for failed or inconsistent envelopes.
    Empty output means only that this query returned no notices. Collection-time
    timestamps establish observation time, not historical publication time.
    """
    if not isinstance(envelope, Mapping):
        raise _NoticeError("envelope_invalid")
    market, session_date = envelope.get("market"), _iso_date(envelope.get("session_date"))
    if not isinstance(market, str) or market not in _SOURCES:
        raise _NoticeError("envelope_invalid")
    expected = {"schema_version": SCHEMA_VERSION, "provider": _SOURCES[market][0],
                "endpoint": _SOURCES[market][1], "status": "success", "representation": "http_json_pages"}
    if any(envelope.get(key) != value for key, value in expected.items()) or envelope.get("error_type") is not None:
        raise _NoticeError("envelope_invalid")
    _timestamps(envelope)
    rows, page_numbers = _revalidate_pages(_mapping(envelope.get("response")), market, session_date)
    request = _mapping(envelope.get("request"))
    _timeout(request.get("timeout_seconds"), 60)
    _timeout(request.get("total_timeout_seconds"), 120)
    if request.get("method") != "GET" or request.get("page_params") != [_params(market, session_date, n) for n in page_numbers]:
        raise _NoticeError("envelope_invalid")
    return [_notice(row, market, session_date) for row in rows]
