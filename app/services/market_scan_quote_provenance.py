from __future__ import annotations

from collections.abc import Iterable

from app.models.market import Quote
from app.utils.symbols import standard_symbol


def provider_response_quotes(quotes: Iterable[Quote]) -> tuple[list[Quote], int]:
    """Keep provider responses while rejecting short/fallback-cache materializations."""
    available = list(quotes)
    provider_quotes = [quote for quote in available if not quote.from_cache]
    return provider_quotes, len(available) - len(provider_quotes)


def normalized_quote_batch(
    quotes: Iterable[Quote],
    provider_errors: tuple[str, ...],
    *,
    require_provider_response: bool,
) -> tuple[dict[str, Quote], tuple[str, ...], int]:
    available = list(quotes)
    cached_count = 0
    if require_provider_response:
        available, cached_count = provider_response_quotes(available)
    if cached_count:
        provider_errors = (
            *provider_errors,
            f"严格快照拒绝 {cached_count} 条缓存报价（必须由实时数据源返回）",
        )
    by_symbol: dict[str, Quote] = {}
    for quote in available:
        try:
            by_symbol[standard_symbol(f"{quote.code}.{quote.market}")] = quote
        except ValueError:
            continue
    return by_symbol, provider_errors, cached_count


__all__ = ["normalized_quote_batch", "provider_response_quotes"]
