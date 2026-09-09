"""Keep SQL result filtering and Python screen explanations on one input contract."""

import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_market_scan_heavy_read_admission, get_market_scanner
from app.api.errors import sensitive_individual_http_exception_handler, validation_exception_handler
from app.api.market_scan_read_admission import MarketScanHeavyReadAdmission
from app.api.routes.market_scan import router
from app.market_scan_screening import screen_spec_from_market_scan_filters
from app.models.market_scan_screening import ScreenSpecV2
from app.repositories.market_scan_screening_sql import screen_spec_filter_sql
from app.services.market_scan_screening import MarketScanScreeningService
from tests.test_market_scan_api import _ScannerStub
from tests.test_market_scan_screening import _FrozenRepository, _rows, _run


_INVALID_KEYWORDS = ("\x00", "SH\x00missing", "芯片\x01", "\x08芯片", "芯片\x1b", "芯片\x7f")


def _compiled_spec(keyword):
    return screen_spec_from_market_scan_filters(
        status=None, market=None, industry=None, is_st=None, is_new=None,
        keyword=keyword, sort="symbol", order="asc",
    )


def _keyword_rows():
    rows = _rows()
    rows[0] = rows[0].model_copy(update={"name": "芯片%_\\龙头"})
    return rows


class _KeywordScanner(_ScannerStub):
    def __init__(self):
        super().__init__()
        self.screen_calls = []
        self.screening = MarketScanScreeningService(_FrozenRepository(_run(), _keyword_rows()))

    def evaluate_screen(self, run_id, request):
        self.screen_calls.append((run_id, request))
        return self.screening.evaluate(run_id, request)


@pytest.fixture
def keyword_client():
    scanner = _KeywordScanner()
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(HTTPException, sensitive_individual_http_exception_handler)
    app.dependency_overrides[get_market_scanner] = lambda: scanner
    admission = MarketScanHeavyReadAdmission()
    app.dependency_overrides[get_market_scan_heavy_read_admission] = lambda: admission
    with TestClient(app) as client:
        yield client, scanner


@pytest.mark.parametrize("keyword", _INVALID_KEYWORDS)
def test_screen_model_and_shared_filter_compiler_reject_control_characters(keyword):
    with pytest.raises(ValueError, match="控制字符"):
        ScreenSpecV2(keyword=keyword)
    with pytest.raises(ValueError, match="控制字符"):
        _compiled_spec(keyword)


@pytest.mark.parametrize("keyword", _INVALID_KEYWORDS)
@pytest.mark.parametrize("method", ("get", "post"))
def test_real_result_and_explanation_routes_reject_before_any_scan_read(keyword_client, keyword, method):
    client, scanner = keyword_client
    if method == "get":
        response = client.get("/api/market-scans/41/results", params={"keyword": keyword})
    else:
        response = client.post("/api/market-scans/41/screen/evaluate", json={"spec": {"keyword": keyword}})
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert "控制字符" in response.json()["detail"]
    assert scanner.result_calls == []
    assert scanner.screen_calls == []


@pytest.mark.parametrize(("keyword", "normalized", "expected"), [
    (" \t芯片\r\n ", "芯片", ["600001.SH"]),
    ("  sh  ", "sh", ["600001.SH", "600003.SH"]),
    ("芯片 \t 龙头", "芯片 龙头", []),
    ("%", "%", ["600001.SH"]),
    ("_", "_", ["600001.SH"]),
    ("\\", "\\", ["600001.SH"]),
    (" \t\r\n ", None, ["000002.SZ", "600001.SH", "600003.SH"]),
])
def test_ordinary_whitespace_ascii_case_and_literal_wildcards_keep_sql_python_parity(
    keyword_client, keyword, normalized, expected,
):
    client, scanner = keyword_client
    result = client.get("/api/market-scans/41/results", params={"keyword": keyword, "status": "all"})
    assert result.status_code == 200
    assert scanner.result_calls[-1][1]["keyword"] == normalized
    spec = _compiled_spec(keyword)
    assert spec.keyword == normalized
    response = client.post(
        "/api/market-scans/41/screen/evaluate",
        json={"spec": spec.model_dump(mode="json"), "near_miss_limit": 0},
    )
    assert response.status_code == 200
    assert [item["symbol"] for item in response.json()["matched"]["items"]] == expected
    where, parameters = screen_spec_filter_sql(spec)
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE result(symbol TEXT, code TEXT, name TEXT)")
        connection.executemany("INSERT INTO result VALUES (?, ?, ?)", [(row.symbol, row.code, row.name) for row in _keyword_rows()])
        actual = [row[0] for row in connection.execute(f"SELECT symbol FROM result WHERE {where} ORDER BY symbol", parameters)]
    assert actual == expected
