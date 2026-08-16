from __future__ import annotations

from collections.abc import Iterable

from app.models.market import Quote
from app.utils.provider_errors import ProviderChainUnavailable
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
    duplicate_symbols: set[str] = set()
    invalid_identity_count = 0
    for quote in available:
        try:
            symbol = standard_symbol(f"{quote.code}.{quote.market}")
        except ValueError:
            invalid_identity_count += 1
            continue
        if symbol in duplicate_symbols:
            continue
        if symbol in by_symbol:
            duplicate_symbols.add(symbol)
            by_symbol.pop(symbol, None)
            continue
        by_symbol[symbol] = quote
    if duplicate_symbols:
        examples = "、".join(sorted(duplicate_symbols)[:5])
        provider_errors = (
            *provider_errors,
            f"报价响应包含重复股票 {len(duplicate_symbols)} 只：{examples}",
        )
    if invalid_identity_count:
        provider_errors = (
            *provider_errors,
            f"报价响应包含无法识别股票代码 {invalid_identity_count} 条",
        )
    return by_symbol, provider_errors, cached_count


def requested_quote_response(
    symbols: list[str],
    quotes: dict[str, Quote],
    provider_errors: tuple[str, ...],
) -> tuple[dict[str, Quote], tuple[str, ...]]:
    requested = set(symbols)
    unexpected = set(quotes) - requested
    if not unexpected:
        return quotes, provider_errors
    filtered = {symbol: quote for symbol, quote in quotes.items() if symbol in requested}
    return filtered, (*provider_errors, f"报价响应包含未请求股票 {len(unexpected)} 只")


def require_available_quote_chain(missing_count: int, chain_state: object) -> None:
    status = getattr(chain_state, "status", None)
    if missing_count and status in {"temporary_unavailable", "permanent_unavailable"}:
        raise ProviderChainUnavailable(
            "实时报价数据源当前不可用或仍有调用未结束",
            retry_after_seconds=getattr(chain_state, "retry_after_seconds", None),
        )


def require_quote_batch_coverage(
    coverage_error: str | None,
    *,
    cached_count: int,
    require_provider_response: bool,
    retry_after_seconds: float,
) -> None:
    if not coverage_error:
        return
    if require_provider_response and cached_count:
        coverage_error += f"；其中 {cached_count} 条缓存报价未计入实时快照"
    raise ProviderChainUnavailable(
        coverage_error,
        retry_after_seconds=retry_after_seconds,
    )


__all__ = [
    "normalized_quote_batch",
    "provider_response_quotes",
    "requested_quote_response",
    "require_available_quote_chain",
    "require_quote_batch_coverage",
]
