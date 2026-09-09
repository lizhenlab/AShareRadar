"""Real due-route JSON must be consumable by the browser's page validator."""

from datetime import datetime
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.routes.reviews import router
from app.services import advice_review, trading_calendar
from app.services.cache import SQLiteCache
from tests.test_advice_reviews import _insert_advice, _plan_input


def test_real_due_api_pages_pass_frontend_contract_and_preserve_continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", tmp_path / "absent-calendar.json")
    monkeypatch.setattr(advice_review, "market_now_naive", lambda: datetime(2026, 7, 21, 16))
    cache = SQLiteCache(tmp_path / "queue.sqlite3")
    plans = [cache.create_advice_review_plan(_plan_input(
        _insert_advice(Path(cache.path), market_time="2026-07-01 15:00:00"),
    )) for _ in range(2)]
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_datahub] = lambda: SimpleNamespace(cache=cache)
    filters = {"symbol": "519.SH", "from_date": "2026-07-01", "horizon_days": 3}
    with TestClient(app) as client:
        first_response = client.get("/api/reviews/due", params={
            "as_of": "2026-07-17T16:00:00", "page_size": 1, **filters,
        })
        assert first_response.status_code == 200
        first = first_response.json()
        continuation = {"as_of": first["as_of"], "snapshot_token": first["snapshot_token"]}
        second_response = client.get("/api/reviews/due", params={
            "page": 2, "page_size": 1, **filters, **continuation,
        })
        assert second_response.status_code == 200
        second = second_response.json()
        empty_response = client.get("/api/reviews/due", params={"symbol": "NO_MATCH", "page_size": 1})
        assert empty_response.status_code == 200
    script = r'''
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { assertDuePage } from "./static/js/advice-review-contracts.js";
const input = JSON.parse(readFileSync(0, "utf8"));
const request = { page: 1, page_size: 1, filters: input.filters };
const first = assertDuePage(input.first, request);
const second = assertDuePage(input.second, { ...request, page: 2, ...input.continuation });
assert.deepEqual([first.items[0].plan.id, second.items[0].plan.id], input.ids);
assert.equal(first.total, 2);
assert.equal(second.total, 2);
const empty = assertDuePage(input.empty, { page: 1, page_size: 1,
  filters: { symbol: "NO_MATCH", from_date: "", horizon_days: null } });
assert.equal(empty.total, 0);
assert.deepEqual(empty.items, []);
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=30,
        input=json.dumps({"first": first, "second": second, "empty": empty_response.json(),
                          "filters": filters, "continuation": continuation, "ids": [plan.id for plan in plans]}),
    )
    assert result.returncode == 0, result.stderr
