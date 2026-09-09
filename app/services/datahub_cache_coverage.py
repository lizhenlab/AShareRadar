from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import math

from pydantic import BaseModel

from app.utils.clock import monotonic_now


ProviderChain = tuple[tuple[str, object], ...]
MAX_SHORT_RESPONSE_RECORDS = 256


@dataclass(frozen=True)
class _ShortResponse:
    requested_limit: int
    row_count: int
    digest: str
    provider_chain: ProviderChain
    expires_at: float


class ShortResponseCoverage:
    """Reuse an observed short response only for the same fresh cache and provider chain."""

    def __init__(self, max_entries: int = MAX_SHORT_RESPONSE_RECORDS) -> None:
        if max_entries < 1:
            raise ValueError("short-response coverage capacity must be positive")
        self._max_entries = max_entries
        self._records: OrderedDict[Hashable, _ShortResponse] = OrderedDict()

    def covers(
        self, key: Hashable, rows: Sequence[BaseModel], requested_limit: int, chain: ProviderChain,
    ) -> bool:
        if len(rows) >= requested_limit:
            return True
        observed = self._records.get(key)
        if observed is None:
            return False
        if (
            monotonic_now() >= observed.expires_at
            or requested_limit > observed.requested_limit
            or len(rows) != observed.row_count
            or not _same_provider_chain(chain, observed.provider_chain)
            or _rows_digest(rows) != observed.digest
        ):
            self._records.pop(key, None)
            return False
        self._records.move_to_end(key)
        return True

    def remember(
        self, key: Hashable, rows: Sequence[BaseModel], requested_limit: int, chain: ProviderChain,
        *, ttl_seconds: float,
    ) -> None:
        self._records.pop(key, None)
        if not rows or len(rows) >= requested_limit or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            return
        self._records[key] = _ShortResponse(
            requested_limit, len(rows), _rows_digest(rows), chain, monotonic_now() + ttl_seconds,
        )
        while len(self._records) > self._max_entries:
            self._records.popitem(last=False)


def provider_cache_chain(priority_rows: Iterable[tuple[int, str]], providers: Mapping[str, object]) -> ProviderChain:
    return tuple((name, providers.get(name)) for _index, name in priority_rows)


def _same_provider_chain(left: ProviderChain, right: ProviderChain) -> bool:
    return len(left) == len(right) and all(
        left_name == right_name and left_provider is right_provider
        for (left_name, left_provider), (right_name, right_provider) in zip(left, right, strict=True)
    )


def _rows_digest(rows: Sequence[BaseModel]) -> str:
    # Cache transport flags and write timestamps do not change the observed data.
    # Retain source, business timestamps, interval, fallback status and all values.
    encoded = sorted(row.model_dump_json(exclude={"from_cache", "fetched_at"}) for row in rows)
    return hashlib.sha256("\n".join(encoded).encode("utf-8")).hexdigest()
