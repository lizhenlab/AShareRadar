"""Explicit, bounded REST access to Fuyao; construction never opens the network."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any
import weakref

import httpx

from app.config import Settings
from app.services.fuyao_contracts import (
    FUYAO_BASE_URL, FUYAO_MAX_RESPONSE_BYTES, FUYAO_MAX_RETRIES, FUYAO_MAX_RETRY_SECONDS,
    FuyaoError, FuyaoRequestBudget, resolve_api_key, retry_after_seconds, validated_params,
)
from app.utils.clock import market_now, monotonic_now
from app.services.fuyao_job_runtime import ACTIVE_JOB


@dataclass
class _AccountState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_request_at: float = 0.0
    date: str = ""
    requests: int = 0
    interval: float = 0.0
    daily_limit: int = 100000


_ACCOUNTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _AccountState]] = weakref.WeakKeyDictionary()
_RETRYABLE = frozenset({"rate_limited", "transport_error", "remote_unavailable"})


def _account_state(key: str) -> _AccountState:
    accounts = _ACCOUNTS.setdefault(asyncio.get_running_loop(), {})
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    return accounts.setdefault(fingerprint, _AccountState())


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON")


def _parse_envelope(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise FuyaoError("invalid_response") from None
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise FuyaoError("invalid_response")
    code = payload["code"]
    if code != 0:
        category = {2001: "invalid_key", 2002: "invalid_key", 2003: "permission_denied", 2004: "invalid_key",
                    4001: "rate_limited", 5001: "remote_unavailable", 5002: "remote_unavailable",
                    5003: "remote_unavailable"}.get(code, "business_error")
        raise FuyaoError(category, code=code)
    if "data" not in payload:
        raise FuyaoError("invalid_response")
    return payload


async def _bounded_response(response: httpx.Response) -> bytes:
    try:
        length = int(response.headers.get("content-length", "0"))
    except ValueError:
        raise FuyaoError("invalid_response") from None
    if length > FUYAO_MAX_RESPONSE_BYTES:
        raise FuyaoError("response_too_large")
    chunks = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=65536):
        if len(chunks) + len(chunk) > FUYAO_MAX_RESPONSE_BYTES:
            raise FuyaoError("response_too_large")
        chunks.extend(chunk)
    return bytes(chunks)


def _check_job_cancellation() -> None:
    execution = ACTIVE_JOB.get()
    if execution is not None:
        execution.control.checkpoint()


def _http_failure(response: httpx.Response) -> FuyaoError | None:
    code = response.status_code
    if 200 <= code < 300:
        return None
    category = {401: "invalid_key", 403: "permission_denied", 429: "rate_limited"}.get(code)
    category = category or ("remote_unavailable" if code >= 500 else "http_error")
    return FuyaoError(category, retry_after=retry_after_seconds(response.headers.get("retry-after")))


class FuyaoClient:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
        budget: FuyaoRequestBudget | None = None,
    ) -> None:
        self._settings = settings
        self._budget = budget
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._state: _AccountState | None = None
        self._closed = False
        self._counts = {"requests": 0, "successes": 0, "failures": 0, "retries": 0, "rate_limits": 0}
        self._permissions: dict[str, str] = {}
        self._last_error: str | None = None

    async def request(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        values = validated_params(path, params)
        key = self._request_key()
        state = self._state = _account_state(key)
        state.interval = max(state.interval, self._settings.fuyao_request_interval_seconds)
        state.daily_limit = min(state.daily_limit, self._settings.fuyao_daily_request_limit)
        async with state.lock:
            return await self._request_with_retries(path, values, key, state)

    def _request_key(self) -> str:
        if self._closed:
            raise FuyaoError("client_closed")
        if not self._settings.fuyao_enabled:
            raise FuyaoError("disabled")
        return resolve_api_key(self._settings.fuyao_api_key, self._settings.fuyao_api_key_file)

    async def _request_with_retries(self, path: str, params: dict[str, Any], key: str, state: _AccountState) -> dict[str, Any]:
        for attempt in range(FUYAO_MAX_RETRIES + 1):
            try:
                _check_job_cancellation()
                await self._reserve_attempt(state)
                _check_job_cancellation()
                result = await self._send(path, params, key)
            except FuyaoError as exc:
                self._record_failure(path, exc)
                delay = max(2.0 ** attempt, exc.retry_after or 0.0)
                if exc.category in _RETRYABLE:
                    self._defer_account(state, delay)
                if attempt >= FUYAO_MAX_RETRIES or exc.category not in _RETRYABLE:
                    raise
                if delay > FUYAO_MAX_RETRY_SECONDS:
                    raise
                self._counts["retries"] += 1
            else:
                self._counts["successes"] += 1
                self._last_error = None
                self._permissions[path] = "available"
                return result
        raise AssertionError("unreachable")

    async def _reserve_attempt(self, state: _AccountState) -> None:
        if self._closed:
            raise FuyaoError("client_closed")
        delay = max(0.0, state.next_request_at - monotonic_now())
        if delay > FUYAO_MAX_RETRY_SECONDS:
            raise FuyaoError("rate_limited", retry_after=delay)
        if delay:
            await asyncio.sleep(delay)
        _check_job_cancellation()
        today = market_now().date().isoformat()
        if state.date != today:
            state.date, state.requests = today, 0
        if state.requests >= state.daily_limit:
            raise FuyaoError("daily_request_limit")
        if self._budget is not None:
            await self._budget.reserve()
        state.requests += 1
        self._counts["requests"] += 1
        state.next_request_at = monotonic_now() + state.interval

    def _defer_account(self, state: _AccountState, delay: float) -> None:
        state.next_request_at = max(state.next_request_at, monotonic_now() + delay)

    async def _send(self, path: str, params: dict[str, Any], key: str) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=FUYAO_BASE_URL, timeout=self._settings.fuyao_timeout_seconds,
                                          transport=self._transport, follow_redirects=False, trust_env=False)
        try:
            async with asyncio.timeout(self._settings.fuyao_timeout_seconds):
                return await self._read_http(path, params, key)
        except (httpx.HTTPError, TimeoutError):
            raise FuyaoError("transport_error") from None

    async def _read_http(self, path: str, params: dict[str, Any], key: str) -> dict[str, Any]:
        assert self._http is not None
        async with self._http.stream("GET", path, params=params, headers={"X-api-key": key, "Accept": "application/json"}) as response:
            failure = _http_failure(response)
            if failure is not None:
                raise failure
            body = await _bounded_response(response)
            try:
                return _parse_envelope(body)
            except FuyaoError as exc:
                exc.retry_after = retry_after_seconds(response.headers.get("retry-after"))
                raise

    def _record_failure(self, path: str, exc: FuyaoError) -> None:
        self._counts["failures"] += 1
        self._counts["rate_limits"] += int(exc.category == "rate_limited")
        self._last_error = exc.category
        if exc.category in {"invalid_key", "permission_denied"}:
            self._permissions[path] = exc.category

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._settings.fuyao_enabled,
            "configured": bool(self._settings.fuyao_api_key or self._settings.fuyao_api_key_file),
            "closed": self._closed, **self._counts, "last_error": self._last_error,
            "permissions": dict(self._permissions),
            "daily_request_limit": self._state.daily_limit if self._state else self._settings.fuyao_daily_request_limit,
            "daily_requests": self._state.requests if self._state and self._state.date == market_now().date().isoformat() else 0,
            "budget_scope": "injected" if self._budget is not None else "process_event_loop",
        }

    async def aclose(self) -> None:
        self._closed = True
        if self._http is not None:
            await self._http.aclose()
