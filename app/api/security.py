from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import parse_qs, urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TRUE_QUERY_VALUES = frozenset({"1", "true", "yes", "on"})
_API_PATH_PREFIX = "/api/"


class SameOriginMutationMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(
            origin for value in allowed_origins if (origin := _origin_from_url(value)) is not None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = _headers(scope)
            is_api_request = _is_api_request(scope)
            trusted_host = _request_origin(scope, headers) in self.allowed_origins
            if (is_api_request and not trusted_host) or (
                (is_api_request or _requires_origin_check(scope))
                and not _trusted_browser_request(scope, headers, self.allowed_origins)
            ):
                response = JSONResponse(status_code=403, content={"detail": _rejection_detail(scope)})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _is_api_request(scope: Scope) -> bool:
    path = str(scope.get("path") or "")
    return path == "/api" or path.startswith(_API_PATH_PREFIX)


def _rejection_detail(scope: Scope) -> str:
    if _requires_origin_check(scope):
        return "拒绝跨站触发本地写操作"
    return "拒绝通过未受信任的主机或来源访问本地 API"


def _requires_origin_check(scope: Scope) -> bool:
    method = str(scope.get("method") or "").upper()
    if method in _UNSAFE_METHODS:
        return True
    if method not in {"GET", "HEAD"}:
        return False
    query = parse_qs(bytes(scope.get("query_string") or b"").decode("utf-8", errors="ignore"))
    return any(value.strip().casefold() in _TRUE_QUERY_VALUES for value in query.get("refresh", ()))


def _trusted_browser_request(
    scope: Scope,
    headers: dict[str, str],
    allowed_origins: frozenset[str],
) -> bool:
    raw_origin = headers.get("origin")
    raw_referer = headers.get("referer")
    fetch_site = headers.get("sec-fetch-site", "").strip().casefold()
    if raw_origin is None and not raw_referer and not fetch_site:
        return True
    request_origin = _request_origin(scope, headers)
    if request_origin is None or request_origin not in allowed_origins:
        return False
    if raw_origin is not None:
        origin = _origin_from_url(raw_origin)
        return origin is not None and origin in allowed_origins
    if raw_referer:
        referer_origin = _origin_from_url(raw_referer)
        return referer_origin is not None and referer_origin in allowed_origins
    return fetch_site != "cross-site"


def _request_origin(scope: Scope, headers: dict[str, str]) -> str | None:
    scheme = str(scope.get("scheme") or "http").strip().casefold()
    host = headers.get("host", "").strip()
    if not host or any(character in host for character in "/\\?#") or any(character.isspace() for character in host):
        return None
    return _origin_from_url(f"{scheme}://{host}") if host else None


def _headers(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin-1").casefold(): value.decode("latin-1")
        for key, value in scope.get("headers", ())
    }


def _origin_from_url(value: object) -> str | None:
    try:
        parsed = urlsplit(str(value).strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    if port == (80 if parsed.scheme.casefold() == "http" else 443):
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme.casefold()}://{netloc}"


__all__ = ["SameOriginMutationMiddleware"]
