from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.routes import reviews


def test_delete_review_plan_returns_mutation_result() -> None:
    cache = _DeleteCache(removed=True)
    response = _client(_DataHubStub(cache)).delete("/api/reviews/plans/9")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "removed": True}
    assert cache.deleted_ids == [9]


def test_delete_review_plan_returns_404_when_missing() -> None:
    response = _client(_DataHubStub(_DeleteCache(removed=False))).delete("/api/reviews/plans/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "研究计划不存在"}


def test_delete_review_plan_uses_mutation_response_model() -> None:
    app = FastAPI()
    app.include_router(reviews.router)

    schema = app.openapi()["paths"]["/api/reviews/plans/{plan_id}"]["delete"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]

    assert schema == {"$ref": "#/components/schemas/MutationResult"}


def _client(datahub: _DataHubStub) -> TestClient:
    app = FastAPI()
    app.include_router(reviews.router)
    app.dependency_overrides[get_datahub] = lambda: datahub
    return TestClient(app)


@dataclass
class _DataHubStub:
    cache: object


class _DeleteCache:
    def __init__(self, *, removed: bool) -> None:
        self.removed = removed
        self.deleted_ids: list[int] = []

    def delete_advice_review_plan(self, plan_id: int) -> bool:
        self.deleted_ids.append(plan_id)
        return self.removed
