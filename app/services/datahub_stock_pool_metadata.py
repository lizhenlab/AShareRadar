from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial

from app.models.market import StockInfo
from app.services.datahub_metadata_provider import _safe_log_metadata_event_async
from app.services.datahub_runtime import ProviderRuntime, TimedProviderCall, provider_source_name
from app.utils.provider_errors import ProviderProtocolError
from app.utils.stock_pool import (
    STOCK_POOL_MIN_INDUSTRY_COVERAGE,
    diagnose_stock_pool_metadata,
    normalize_stock_industry_text,
    normalize_stock_pool_rows,
)
from app.utils.symbols import normalize_symbol


STOCK_POOL_INDUSTRY_ENRICH_MIN_MARKET_COUNT = 100


def merge_cached_stock_fields(rows: list[StockInfo], cached: list[StockInfo]) -> list[StockInfo]:
    normalized_rows = normalize_stock_pool_rows(rows)
    cached_by_symbol = {item.symbol: item for item in normalize_stock_pool_rows(cached)}
    merged: list[StockInfo] = []
    for item in normalized_rows:
        previous = cached_by_symbol.get(item.symbol)
        if previous is None:
            merged.append(item)
            continue
        merged.append(
            item.model_copy(
                update={
                    "industry": item.industry or previous.industry,
                    "list_date": item.list_date or previous.list_date,
                    "source": _cached_metadata_source(item, previous),
                }
            )
        )
    return normalize_stock_pool_rows(merged)


def _cached_metadata_source(item: StockInfo, previous: StockInfo) -> str:
    cached_fields = [
        label
        for value, cached_value, label in (
            (item.industry, previous.industry, "行业"),
            (item.list_date, previous.list_date, "上市日期"),
        )
        if not value and cached_value
    ]
    if not cached_fields:
        return item.source
    primary = " ".join(str(item.source or "").split()).strip()
    cached_source = " ".join(str(previous.source or "").split()).strip()
    if not cached_source or cached_source == primary:
        return primary
    if cached_source.startswith(f"{primary} + "):
        return cached_source
    return f"{primary} + {cached_source}({'/'.join(cached_fields)}缓存)"


class StockIndustryEnricher:
    def __init__(
        self,
        *,
        settings,
        cache,
        providers: dict,
        runtime: ProviderRuntime,
        priority: Callable[[str], list[tuple[int, str]]],
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.providers = providers
        self.runtime = runtime
        self.priority = priority

    async def enrich(
        self,
        rows: list[StockInfo],
        *,
        primary_provider_name: str | None = None,
    ) -> tuple[list[StockInfo], int]:
        target_markets = _industry_gap_markets(rows)
        if not target_markets:
            return rows, 0

        errors: list[str] = []
        total_filled = 0
        priority_rows = list(self.priority("stock"))
        for attempt in self.runtime.attempts(priority_rows, self.providers, "stock_industry", errors):
            if attempt.name == primary_provider_name or not callable(getattr(attempt.provider, "stock_industries", None)):
                continue
            try:
                result: TimedProviderCall[Mapping[object, object]] = await self.runtime.timed_provider_call(
                    attempt.name,
                    "stock_industry",
                    partial(_required_stock_industry_call, attempt.provider),
                    request_key=("stock_industries",),
                    timeout_seconds=self.settings.stock_pool_provider_timeout_seconds,
                )
                rows, filled_count = _merge_industry_snapshot(
                    rows,
                    result.value,
                    target_markets=target_markets,
                    source_name=provider_source_name(attempt.provider, attempt.name),
                )
                if not filled_count:
                    raise ProviderProtocolError("批量行业快照未覆盖缺失市场")
            except Exception as exc:
                await self.runtime.record_attempt_failure_async(attempt, "stock_industry", exc, errors)
                continue

            total_filled += filled_count
            await self.runtime.record_attempt_success_async(attempt, "stock_industry", result.latency_ms)
            target_markets = _industry_gap_markets(rows)
            if not target_markets:
                break

        await self._log_result(total_filled, target_markets, errors)
        return rows, total_filled

    async def _log_result(
        self,
        total_filled: int,
        target_markets: frozenset[str],
        errors: list[str],
    ) -> None:
        markets = ",".join(sorted(target_markets))
        if total_filled:
            await _safe_log_metadata_event_async(
                self.cache,
                "provider",
                f"股票池批量行业增强完成：补齐 {total_filled} 条" + (f"，仍缺失 {markets}" if markets else ""),
            )
        elif errors:
            await _safe_log_metadata_event_async(
                self.cache,
                "fallback",
                f"股票池批量行业增强失败，保留原股票池：{'；'.join(errors)}",
            )


def _industry_gap_markets(rows: list[StockInfo]) -> frozenset[str]:
    diagnostic = diagnose_stock_pool_metadata(rows)
    return frozenset(
        coverage.scope
        for coverage in diagnostic.markets
        if coverage.total_count >= STOCK_POOL_INDUSTRY_ENRICH_MIN_MARKET_COUNT
        and coverage.industry_ratio < STOCK_POOL_MIN_INDUSTRY_COVERAGE
    )


def _normalize_industry_snapshot(snapshot: Mapping[object, object]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_symbol, raw_industry in snapshot.items():
        try:
            code, market = normalize_symbol(str(raw_symbol or ""))
        except (AttributeError, TypeError, ValueError):
            continue
        industry = normalize_stock_industry_text(raw_industry)
        if industry is not None:
            normalized[f"{code}.{market.upper()}"] = industry
    return normalized


def _merge_industry_snapshot(
    rows: list[StockInfo],
    snapshot: Mapping[object, object],
    *,
    target_markets: frozenset[str],
    source_name: str,
) -> tuple[list[StockInfo], int]:
    industries = _normalize_industry_snapshot(snapshot)
    filled_count = 0
    merged: list[StockInfo] = []
    for item in rows:
        industry = industries.get(item.symbol) if item.market in target_markets and not item.industry else None
        if industry is None:
            merged.append(item)
            continue
        merged.append(
            item.model_copy(
                update={
                    "industry": industry,
                    "source": _combined_metadata_source(item.source, source_name),
                }
            )
        )
        filled_count += 1
    return merged, filled_count


def _combined_metadata_source(primary: str, industry_source: str) -> str:
    primary_text = " ".join(str(primary or "").split()).strip()
    industry_text = " ".join(str(industry_source or "").split()).strip()
    supplement = f"{industry_text}(行业)"
    if not primary_text:
        return supplement
    if supplement in primary_text:
        return primary_text
    return f"{primary_text} + {supplement}"


async def _required_stock_industry_call(provider: object) -> Mapping[object, object]:
    method = getattr(provider, "stock_industries", None)
    if not callable(method):
        raise RuntimeError("数据源不支持批量行业快照能力")
    snapshot = await method()
    if not isinstance(snapshot, Mapping):
        raise ProviderProtocolError("批量行业快照返回格式无效")
    return snapshot


__all__ = ["StockIndustryEnricher", "merge_cached_stock_fields"]
