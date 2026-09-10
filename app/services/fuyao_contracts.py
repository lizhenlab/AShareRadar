"""Public Fuyao transport contract; no provider response text enters errors."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime
import math
from pathlib import Path
import os
import stat
from typing import Any, Protocol

from pydantic import SecretStr

from app.utils.provider_errors import ProviderError
from app.utils.clock import utc_now


FUYAO_BASE_URL = "https://fuyao.aicubes.cn"
FUYAO_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
FUYAO_MAX_RETRIES = 2
FUYAO_MAX_RETRY_SECONDS = 60.0
FUYAO_ALLOWED_PATHS = frozenset({
    "/api/a-share/financials/income-statements", "/api/a-share/financials/balance-sheets",
    "/api/a-share/financials/cash-flow-statements", "/api/a-share/financials/indicators",
    "/api/meta/tickers/search", "/api/meta/tickers/list", "/api/a-share/valuations/snapshot",
    "/api/a-share/prices/snapshot", "/api/a-share/prices/historical",
    "/api/a-share/corporate-actions/adjustment-factors", "/api/a-share/calendar/trading-days",
    "/api/a-share-index/catalog/ths-index-list", "/api/a-share-index/constituents/ths-stock-list",
    "/api/a-share-index/prices/snapshot", "/api/a-share-index/prices/historical",
    "/api/a-share/special-data/limit-up-pool", "/api/a-share/special-data/limit-down-pool",
    "/api/a-share/special-data/limit-break-pool", "/api/a-share/special-data/dragon-tiger-list",
    "/api/a-share/special-data/anomaly-analysis-stock", "/api/a-share/special-data/hot-stock-list",
    "/api/a-share/special-data/hot-stock-list-history", "/api/a-share/special-data/hot-stock-rank-trend",
    "/api/a-share/special-data/skyrocket-list", "/api/a-share/special-data/limit-up-ladder",
    "/api/dump/market-dumps/daily-k/download-url", "/api/dump/market-dumps/daily-k-10d/download-url",
    "/api/dump/market-dumps/adjustment-factors/download-url",
})


class FuyaoRequestBudget(Protocol):
    """Reserve one attempted HTTP call; implement durable accounting externally."""

    async def reserve(self) -> None: ...


class FuyaoError(ProviderError):
    def __init__(self, category: str, *, code: int | None = None, retry_after: float | None = None) -> None:
        self.category = category
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"扶摇数据请求失败：{category}")


def validated_params(path: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
    if path not in FUYAO_ALLOWED_PATHS:
        raise FuyaoError("unsupported_path")
    values = dict(params or {})
    if len(values) > 30:
        raise FuyaoError("invalid_parameters")
    for name, value in values.items():
        if not isinstance(name, str) or len(name) > 80:
            raise FuyaoError("invalid_parameters")
        if name.lower().replace("-", "_") in {"key", "api_key", "apikey", "x_api_key", "token", "access_token", "auth", "secret", "url"}:
            raise FuyaoError("invalid_parameters")
        if not isinstance(value, (str, int, float, bool, type(None))) or len(str(value)) > 32000:
            raise FuyaoError("invalid_parameters")
        if isinstance(value, float) and not math.isfinite(value):
            raise FuyaoError("invalid_parameters")
    return values


def resolve_api_key(key: SecretStr | None, key_file: Path | None) -> str:
    value = key.get_secret_value() if key is not None else _read_key_file(key_file)
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise FuyaoError("missing_or_invalid_key")
    return value


def _read_key_file(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        descriptor = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
                raise FuyaoError("invalid_key_file")
            if metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
                raise FuyaoError("insecure_key_file")
            return handle.read(4097).decode("utf-8")
    except (OSError, UnicodeError):
        raise FuyaoError("unreadable_key_file") from None


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            seconds = (target - utc_now()).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None
