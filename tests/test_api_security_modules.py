from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.routes import local_data
from app.api.security import SameOriginMutationMiddleware
from app.services.cache import SQLiteCache


def _guarded_app() -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    calls: list[str] = []
    app.add_middleware(
        SameOriginMutationMiddleware,
        allowed_origins=(
            "http://testserver",
            "http://127.0.0.1:8010",
            "http://trusted.test",
            "http://default-port.test:80",
        ),
    )

    @app.post("/api/mutate")
    async def mutate() -> dict[str, bool]:
        calls.append("post")
        return {"ok": True}

    @app.get("/api/read")
    async def read(refresh: bool = False) -> dict[str, bool]:
        calls.append(f"get:{refresh}")
        return {"ok": True}

    return app, calls


def test_cross_origin_post_is_rejected_before_side_effect() -> None:
    app, calls = _guarded_app()

    response = TestClient(app).post("/api/mutate", headers={"Origin": "https://evil.test"})

    assert response.status_code == 403
    assert response.json() == {"detail": "拒绝跨站触发本地写操作"}
    assert calls == []


def test_same_or_explicitly_allowed_origin_can_mutate() -> None:
    app, calls = _guarded_app()
    client = TestClient(app)

    assert client.post("/api/mutate", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/mutate", headers={"Origin": "http://trusted.test"}).status_code == 200
    assert calls == ["post", "post"]


def test_cross_site_fetch_metadata_without_origin_is_rejected() -> None:
    app, calls = _guarded_app()

    response = TestClient(app).post("/api/mutate", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403
    assert calls == []


def test_cli_request_without_browser_origin_metadata_remains_supported() -> None:
    app, calls = _guarded_app()

    response = TestClient(app).post("/api/mutate")

    assert response.status_code == 200
    assert calls == ["post"]


def test_cross_site_read_and_refresh_get_are_both_guarded_for_api_routes() -> None:
    app, calls = _guarded_app()
    client = TestClient(app)

    read = client.get("/api/read", headers={"Origin": "https://evil.test"})
    refresh = client.get("/api/read?refresh=true", headers={"Origin": "https://evil.test"})

    assert read.status_code == 403
    assert refresh.status_code == 403
    assert calls == []


def test_same_origin_referer_is_accepted_when_origin_is_absent() -> None:
    app, calls = _guarded_app()

    response = TestClient(app).post("/api/mutate", headers={"Referer": "http://testserver/page"})

    assert response.status_code == 200
    assert calls == ["post"]


def test_default_port_and_implicit_default_port_are_same_origin() -> None:
    app, calls = _guarded_app()

    response = TestClient(app).post("/api/mutate", headers={"Origin": "http://default-port.test"})

    assert response.status_code == 200
    assert calls == ["post"]


def test_untrusted_host_and_matching_origin_cannot_extend_the_trust_set() -> None:
    app, calls = _guarded_app()

    response = TestClient(app).post(
        "/api/mutate",
        headers={"Host": "evil.test", "Origin": "http://evil.test", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 403
    assert calls == []


def test_browser_metadata_requires_a_configured_request_host() -> None:
    app, calls = _guarded_app()

    response = TestClient(app).post(
        "/api/mutate",
        headers={"Host": "evil.test", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 403
    assert calls == []


def test_dns_rebinding_host_is_rejected_for_read_only_api_request() -> None:
    app, calls = _guarded_app()

    response = TestClient(app).get(
        "/api/read",
        headers={"Host": "evil.test", "Origin": "http://evil.test", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "拒绝通过未受信任的主机或来源访问本地 API"}
    assert calls == []


def test_metadata_free_cli_request_to_configured_loopback_host_is_supported() -> None:
    app, calls = _guarded_app()
    client = TestClient(app, base_url="http://127.0.0.1:8010")

    response = client.get("/api/read")

    assert response.status_code == 200
    assert calls == ["get:False"]


def test_metadata_free_request_to_unconfigured_host_is_rejected() -> None:
    app, calls = _guarded_app()

    response = TestClient(app).get("/api/read", headers={"Host": "evil.test"})

    assert response.status_code == 403
    assert calls == []


def test_local_user_data_export_is_post_only_and_not_cacheable(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(cache)
    client = TestClient(app)

    get_response = client.get("/api/local-data/export")
    post_response = client.post("/api/local-data/export")

    assert get_response.status_code == 405
    assert post_response.status_code == 200
    assert post_response.headers["cache-control"] == "no-store"


class _DataHubStub:
    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache
