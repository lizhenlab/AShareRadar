from __future__ import annotations

import asyncio
import socket
import time
from datetime import datetime
from typing import Iterable

from app.config import get_settings
from app.models.schemas import (
    DataQuality,
    DataSourcePlan,
    DataStatus,
    Kline,
    MinuteKline,
    OrderBook,
    PlateItem,
    ProviderCapability,
    ProviderCapabilityStatus,
    ProviderDecision,
    ProviderStatus,
    Quote,
    StockConceptItem,
    StockInfo,
)
from app.services.cache import SQLiteCache
from app.services.data_quality import build_data_quality, latest_expected_trade_date
from app.services.provider_registry import (
    all_provider_names,
    build_providers,
    provider_capabilities,
    provider_enabled_for,
    provider_capability,
    provider_index,
    provider_is_enabled,
    provider_priority,
)
from app.utils.symbols import normalize_symbol, standard_symbol
from app.utils.time import seconds_since_text


_STOCK_POOL_FALLBACK_SECONDS = 60 * 60 * 24 * 30


class DataHub:
    def __init__(self, cache: SQLiteCache | None = None) -> None:
        self.settings = get_settings()
        self.cache = cache or SQLiteCache()
        self.providers = build_providers(self.settings)
        self._provider_cooldowns: dict[tuple[str, str], float] = {}
        self._sync_provider_enabled_flags()

    async def quote(self, symbol: str, use_cache: bool = True) -> Quote:
        return (await self.quotes([symbol], use_cache=use_cache))[0]

    async def quotes(self, symbols: Iterable[str], use_cache: bool = True) -> list[Quote]:
        requested_symbols = self._normalize_symbols(symbols)
        if not requested_symbols:
            return []
        symbol_list = list(dict.fromkeys(requested_symbols))

        collected: dict[str, Quote] = {}
        if use_cache:
            cached = self.cache.get_quotes(symbol_list, self.settings.quote_cache_seconds)
            cached = _tag_cached_quotes(cached, "短时缓存")
            collected.update({standard_symbol(f"{quote.code}.{quote.market}"): quote for quote in cached})
            if len(collected) == len(symbol_list):
                by_symbol = collected
                return [by_symbol[symbol] for symbol in requested_symbols]

        errors: list[str] = []
        for index, name in self._priority("quote"):
            remaining = [symbol for symbol in symbol_list if symbol not in collected]
            if not remaining:
                break
            if self._provider_is_cooling(name, "quote"):
                errors.append(f"{name}: 最近失败，短暂冷却中")
                continue
            provider = self.providers[name]
            started = time.perf_counter()
            try:
                quotes = await self._call(provider.quotes(remaining))
                matched, missing = self._matched_quotes(quotes, remaining)
                if not matched:
                    raise RuntimeError(f"{provider.source_name} 行情缺失：{','.join(missing)}")
                latency_ms = (time.perf_counter() - started) * 1000
                self._record_provider_success(name, index, round(latency_ms, 2), "quote")
                self.cache.save_quotes(matched)
                collected.update({standard_symbol(f"{quote.code}.{quote.market}"): quote for quote in matched})
                if missing:
                    message = f"{provider.source_name} 批量行情部分缺失：{','.join(missing)}"
                    errors.append(f"{name}: {message}")
                    self.cache.log_event("fallback", message)
                if len(collected) == len(symbol_list):
                    return [collected[symbol] for symbol in requested_symbols]
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                self._record_provider_failure(name, index, exc, "quote")

        missing_symbols = [symbol for symbol in symbol_list if symbol not in collected]
        fallback = _tag_cached_quotes(self.cache.get_quotes(missing_symbols, max_age_seconds=60 * 60 * 24), "兜底缓存")
        for quote in fallback:
            collected[standard_symbol(f"{quote.code}.{quote.market}")] = quote
        if len(collected) == len(symbol_list):
            self.cache.log_event("fallback", "部分或全部实时数据源失败，缺失个股使用24小时内缓存报价")
            return [collected[symbol] for symbol in requested_symbols]
        unresolved = [symbol for symbol in symbol_list if symbol not in collected]
        raise RuntimeError("实时行情未完整返回，缺失：" + "、".join(unresolved) + "；" + "；".join(errors))

    async def quote_with_quality(
        self,
        symbol: str,
        use_cache: bool = True,
        check_consistency: bool = True,
    ) -> tuple[Quote, DataQuality]:
        quote = await self.quote(symbol, use_cache=use_cache)
        quality = await self.assess_quote_quality(quote, check_consistency=check_consistency)
        return quote, quality

    async def assess_quote_quality(
        self,
        quote: Quote,
        klines: list[Kline] | None = None,
        use_cache: bool = True,
        require_kline: bool = True,
        check_consistency: bool = True,
    ) -> DataQuality:
        quality_klines = klines
        if quality_klines is None:
            quality_klines = (
                self.cache.get_klines(f"{quote.code}.{quote.market}", 120, self.settings.kline_cache_seconds)
                if require_kline
                else []
            )
        consistency_level, notes, penalty = await self._quote_consistency(quote, check_consistency=check_consistency)
        return build_data_quality(
            quote,
            quality_klines,
            consistency_level=consistency_level,
            consistency_notes=notes,
            consistency_penalty=penalty,
            require_kline=require_kline,
        )

    async def kline(self, symbol: str, limit: int = 120, use_cache: bool = True) -> list[Kline]:
        normalize_symbol(symbol)
        if use_cache:
            cached = self.cache.get_klines(symbol, limit, self.settings.kline_cache_seconds)
            if len(cached) >= min(limit, 20) and _kline_cache_is_fresh(cached):
                return cached[-limit:]

        errors: list[str] = []
        for index, name in self._priority("kline"):
            if self._provider_is_cooling(name, "kline"):
                errors.append(f"{name}: 最近失败，短暂冷却中")
                continue
            provider = self.providers[name]
            started = time.perf_counter()
            try:
                klines = await self._call(provider.kline(symbol, limit))
                latency_ms = (time.perf_counter() - started) * 1000
                self._record_provider_success(name, index, round(latency_ms, 2), "kline")
                klines = _tag_klines(klines, provider.source_name, from_cache=False)
                self.cache.save_klines(symbol, klines, provider.source_name)
                return klines
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                self._record_provider_failure(name, index, exc, "kline")

        fallback = self.cache.get_klines(symbol, limit, max_age_seconds=60 * 60 * 24 * 30)
        if fallback:
            self.cache.log_event("fallback", f"所有K线数据源失败，使用缓存K线：{symbol}")
            return _tag_klines(fallback, None, from_cache=True, fallback_used=True)
        raise RuntimeError("所有K线数据源均不可用：" + "；".join(errors))

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120, use_cache: bool = True) -> list[MinuteKline]:
        normalize_symbol(symbol)
        normalized_interval = _normalize_minute_interval(interval)
        if use_cache:
            cached = self.cache.get_minute_klines(symbol, normalized_interval, limit, self.settings.minute_kline_cache_seconds)
            if len(cached) >= min(limit, 12):
                return cached[-limit:]

        errors: list[str] = []
        for index, name in self._priority("minute"):
            if self._provider_is_cooling(name, "minute"):
                errors.append(f"{name}: 最近失败，短暂冷却中")
                continue
            provider = self.providers[name]
            if not hasattr(provider, "minute_kline"):
                continue
            started = time.perf_counter()
            try:
                rows = await self._call(provider.minute_kline(symbol, normalized_interval, limit))  # type: ignore[attr-defined]
                if not rows:
                    raise RuntimeError("分钟K线返回为空")
                latency_ms = (time.perf_counter() - started) * 1000
                self._record_provider_success(name, index, round(latency_ms, 2), "minute")
                rows = _tag_minute_klines(rows[-limit:], provider.source_name, normalized_interval, from_cache=False)
                self.cache.save_minute_klines(symbol, normalized_interval, rows, provider.source_name)
                return rows
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                self._record_provider_failure(name, index, exc, "minute")

        fallback = self.cache.get_minute_klines(symbol, normalized_interval, limit, max_age_seconds=60 * 60 * 6)
        if fallback:
            self.cache.log_event("fallback", f"所有分钟K线数据源失败，使用缓存分钟K线：{symbol}")
            return _tag_minute_klines(fallback, None, normalized_interval, from_cache=True, fallback_used=True)
        raise RuntimeError("所有分钟K线数据源均不可用：" + "；".join(errors))

    async def stock_pool(self, keyword: str | None = None, limit: int = 5000, refresh: bool = False) -> list[StockInfo]:
        if not refresh:
            cache_stats = self.cache.stats()
            cached = self.cache.get_stock_pool(self.settings.stock_pool_cache_seconds, limit=limit, keyword=keyword)
            if cached:
                return cached
            fallback = self.cache.get_stock_pool(_STOCK_POOL_FALLBACK_SECONDS, limit=limit, keyword=keyword) if keyword else []
            if fallback:
                self.cache.log_event("fallback", f"股票池新缓存未命中，使用30天内本地股票主数据：{keyword}")
                return fallback
            if keyword and _stock_pool_cache_is_authoritative(
                cache_stats,
                self.settings.stock_pool_cache_seconds,
                self.settings.stock_pool_authoritative_min_count,
                fresh_count=self.cache.stock_pool_count(self.settings.stock_pool_cache_seconds),
            ):
                return cached

        errors: list[str] = []
        for index, name in self._priority("stock"):
            if self._provider_is_cooling(name, "stock"):
                errors.append(f"{name}: 最近失败，短暂冷却中")
                continue
            provider = self.providers[name]
            if not hasattr(provider, "stock_pool"):
                continue
            started = time.perf_counter()
            try:
                rows = await self._call(provider.stock_pool())  # type: ignore[attr-defined]
                if not rows:
                    raise RuntimeError(f"{provider.source_name} 股票池返回为空")
                latency_ms = (time.perf_counter() - started) * 1000
                self._record_provider_success(name, index, round(latency_ms, 2), "stock")
                self.cache.save_stock_pool(rows)
                if keyword:
                    keyword_lower = keyword.lower()
                    matched = [
                        item
                        for item in rows
                        if keyword_lower in item.code.lower()
                        or keyword_lower in item.name.lower()
                        or keyword_lower in item.symbol.lower()
                    ]
                    if matched or _stock_pool_rows_are_authoritative(rows, self.settings.stock_pool_authoritative_min_count):
                        return matched[:limit]
                    errors.append(f"{name}: 股票池覆盖不足，无法确认 {keyword}")
                    continue
                return rows[:limit]
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                self._record_provider_failure(name, index, exc, "stock")

        fallback = self.cache.get_stock_pool(max_age_seconds=_STOCK_POOL_FALLBACK_SECONDS, limit=limit, keyword=keyword)
        if fallback:
            self.cache.log_event("fallback", "股票池数据源失败，使用本地缓存股票池")
            return fallback
        if keyword and _stock_pool_cache_is_authoritative(
            self.cache.stats(),
            _STOCK_POOL_FALLBACK_SECONDS,
            self.settings.stock_pool_authoritative_min_count,
            fresh_count=self.cache.stock_pool_count(_STOCK_POOL_FALLBACK_SECONDS),
        ):
            return []
        raise RuntimeError("所有股票池数据源均不可用：" + "；".join(errors))

    async def stock_profile(self, symbol: str) -> StockInfo | None:
        code, market = normalize_symbol(symbol)
        target = f"{code}.{market.upper()}"
        rows = await self.stock_pool(keyword=code, limit=10, refresh=False)
        profile = next((item for item in rows if item.symbol == target), None)

        local_provider = self.providers.get("local")
        local_rows = []
        if local_provider and hasattr(local_provider, "stock_pool"):
            try:
                local_rows = await self._call(local_provider.stock_pool())  # type: ignore[attr-defined]
            except Exception:
                local_rows = []
        local_profile = next((item for item in local_rows if item.symbol == target), None)
        if profile and local_profile and not profile.industry:
            profile.industry = local_profile.industry
        return profile or local_profile

    async def plate_rank(self, limit: int = 20, refresh: bool = False) -> list[PlateItem]:
        if not refresh:
            cached = self.cache.get_plate_rank(self.settings.plate_rank_cache_seconds, limit=limit)
            if cached:
                return cached

        errors: list[str] = []
        for index, name in self._priority("plate"):
            if self._provider_is_cooling(name, "plate"):
                errors.append(f"{name}: 最近失败，短暂冷却中")
                continue
            provider = self.providers[name]
            if not hasattr(provider, "plate_rank"):
                continue
            started = time.perf_counter()
            try:
                rows = await self._call(provider.plate_rank(limit=limit))  # type: ignore[attr-defined]
                latency_ms = (time.perf_counter() - started) * 1000
                self._record_provider_success(name, index, round(latency_ms, 2), "plate")
                self.cache.save_plate_rank(rows)
                return rows[:limit]
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                if name == "akshare":
                    self.cache.log_event("fallback", f"AKShare板块排行不可用，继续尝试本地板块：{_provider_error_text(exc)}")
                else:
                    self._record_provider_failure(name, index, exc, "plate")

        fallback = self.cache.get_plate_rank(max_age_seconds=60 * 60 * 24, limit=limit)
        if fallback:
            self.cache.log_event("fallback", "板块数据源失败，使用本地缓存板块排行")
            return fallback
        raise RuntimeError("所有板块数据源均不可用：" + "；".join(errors))

    async def stock_concepts(self, symbol: str, limit: int = 8, refresh: bool = False) -> list[StockConceptItem]:
        normalized = standard_symbol(symbol)
        if not refresh:
            cached = self.cache.get_stock_concepts(normalized, self.settings.stock_concept_cache_seconds, limit=limit)
            if cached:
                return cached

        errors: list[str] = []
        for index, name in self._priority("concept"):
            if self._provider_is_cooling(name, "concept"):
                errors.append(f"{name}: 最近失败，短暂冷却中")
                continue
            provider = self.providers[name]
            if not hasattr(provider, "stock_concepts"):
                continue
            started = time.perf_counter()
            try:
                rows = await self._call(provider.stock_concepts(normalized, limit=limit))  # type: ignore[attr-defined]
                rows = _normalize_stock_concepts(normalized, rows, limit)
                if not rows:
                    raise RuntimeError("概念归属返回为空")
                latency_ms = (time.perf_counter() - started) * 1000
                self._record_provider_success(name, index, round(latency_ms, 2), "concept")
                self.cache.save_stock_concepts(normalized, rows)
                return rows[:limit]
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                self._record_provider_failure(name, index, exc, "concept")

        fallback = self.cache.get_stock_concepts(normalized, max_age_seconds=60 * 60 * 24 * 30, limit=limit)
        if fallback:
            self.cache.log_event("fallback", f"概念归属数据源失败，使用本地缓存概念：{normalized}")
            return fallback
        self.cache.log_event("fallback", f"概念归属不可用：{normalized}；" + "；".join(errors))
        return []

    async def order_book(self, symbol: str) -> OrderBook:
        provider = self.providers["futu"]
        if not hasattr(provider, "order_book"):
            raise RuntimeError("当前没有可用盘口数据源")
        if self._provider_is_cooling("futu", "order_book"):
            raise RuntimeError("Futu 盘口最近失败，短暂冷却中")
        started = time.perf_counter()
        try:
            result = await self._call(provider.order_book(symbol))  # type: ignore[attr-defined]
            latency_ms = (time.perf_counter() - started) * 1000
            self._record_provider_success("futu", self._provider_index("futu"), round(latency_ms, 2), "order_book")
            return result
        except Exception as exc:
            self._record_provider_failure("futu", self._provider_index("futu"), exc, "order_book")
            raise

    async def futu_ping(self) -> dict[str, object]:
        provider = self.providers["futu"]
        started = time.perf_counter()
        try:
            message = await self._call(provider.ping())  # type: ignore[attr-defined]
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            self._record_provider_success("futu", self._provider_index("futu"), latency_ms, "order_book")
            return {"ok": True, "message": message, "latency_ms": latency_ms}
        except Exception as exc:
            self._record_provider_failure("futu", self._provider_index("futu"), exc, "order_book")
            return {"ok": False, "message": str(exc), "latency_ms": None}

    async def warmup(self, symbols: list[str]) -> None:
        await asyncio.gather(
            self.quotes(symbols, use_cache=False),
            *(self.kline(symbol, 120, use_cache=False) for symbol in symbols),
        )

    def status(self) -> DataStatus:
        self._sync_provider_enabled_flags()
        providers = self.cache.provider_statuses()
        capability_statuses = self.cache.provider_capability_statuses()
        capabilities = self.capabilities()
        return DataStatus(
            providers=providers,
            cache=self.cache.stats(),
            capabilities=capabilities,
            capability_statuses=capability_statuses,
            source_plan=self._source_plan(providers, capabilities, capability_statuses),
        )

    def capabilities(self) -> list[ProviderCapability]:
        return provider_capabilities(self.providers)

    def _source_plan(
        self,
        providers: list[ProviderStatus],
        capabilities: list[ProviderCapability],
        capability_statuses: list[ProviderCapabilityStatus] | None = None,
    ) -> DataSourcePlan:
        by_name = {item.name: item for item in providers}
        by_capability = _capability_status_map(capability_statuses or [])
        caps_by_name = {item.name: item for item in capabilities}
        quote_names = [name for _, name in self._priority("quote")]
        kline_names = [name for _, name in self._priority("kline")]
        minute_names = [name for _, name in self._priority("minute")]
        primary_quote = _first_healthy_provider(quote_names, by_name, by_capability, "quote")
        primary_kline = _first_healthy_provider(kline_names, by_name, by_capability, "kline")
        primary_minute = _first_healthy_provider(minute_names, by_name, by_capability, "minute")
        decisions = [
            self._provider_decision(name, by_name.get(name), caps_by_name.get(name), quote_names, kline_names, minute_names, by_capability)
            for name in self._all_provider_names()
        ]
        warnings: list[str] = []
        suggestions: list[str] = []
        unhealthy_capabilities = _unhealthy_capability_labels(capability_statuses or [])
        unhealthy_enabled = [item.name for item in providers if item.enabled and not item.healthy]
        if unhealthy_capabilities:
            warnings.append("最近失败能力：" + "、".join(unhealthy_capabilities[:6]))
            suggestions.append("优先检查失败能力对应的网络代理、源站连通性或本地授权。")
        elif unhealthy_enabled:
            warnings.append("最近失败源：" + "、".join(unhealthy_enabled[:5]))
            suggestions.append("优先检查网络代理、源站连通性，或临时依赖当前正常主源。")
        if not primary_quote:
            warnings.append("没有健康的实时报价主源，实时分析会依赖缓存。")
            suggestions.append("修复 Tencent/AKShare/Futu 任一实时报价源。")
        if not primary_minute:
            warnings.append("分钟线主源不可用，盘中做T判断会降级。")
            suggestions.append("启用 Futu OpenD 或修复 AKShare 分钟线接口。")
        enabled_realtime = [
            item.name
            for item in capabilities
            if item.enabled and item.realtime_quote and item.reliability_level != "演示"
        ]
        if len(enabled_realtime) < 2:
            suggestions.append("建议至少保留两个实时报价源，用于价格一致性校验。")
        health_level = "健康"
        if warnings:
            health_level = "降级可用" if primary_quote or primary_kline else "高风险"
        summary = _source_plan_summary(health_level, primary_quote, primary_kline, primary_minute)
        return DataSourcePlan(
            primary_quote_source=primary_quote,
            primary_kline_source=primary_kline,
            primary_minute_source=primary_minute,
            health_level=health_level,
            summary=summary,
            decisions=decisions,
            warnings=warnings,
            suggestions=suggestions,
        )

    def _provider_decision(
        self,
        name: str,
        status: ProviderStatus | None,
        capability: ProviderCapability | None,
        quote_names: list[str],
        kline_names: list[str],
        minute_names: list[str],
        capability_statuses: dict[tuple[str, str], ProviderCapabilityStatus],
    ) -> ProviderDecision:
        capabilities = _capability_labels(capability)
        role = _provider_role(name, quote_names, kline_names, minute_names)
        state = _provider_capability_state(name, quote_names, kline_names, minute_names, capability_statuses)
        action = "无需处理，当前不参与分析。"
        if status and status.enabled:
            cooling = _provider_cooling_kinds(name, quote_names, kline_names, minute_names, self._provider_is_cooling)
            if cooling:
                state = state or "冷却中"
                action = "刚发生失败的能力会短暂跳过：" + "、".join(cooling) + "。其他能力可继续使用。"
            elif status.healthy:
                if not state:
                    state = "正常"
                action = "继续作为当前可用数据源。"
            else:
                if not state or "最近失败" not in state:
                    state = "最近失败"
                action = _provider_recovery_action(name, status.last_error)
        elif not state:
            state = "未启用"
        return ProviderDecision(
            name=name,
            role=role,
            state=state,
            priority=status.priority if status else self._provider_index(name),
            capabilities=capabilities,
            success_rate=_provider_success_rate(status),
            last_success=status.last_success if status else None,
            last_error=status.last_error if status else None,
            action=action,
        )

    def _priority(self, kind: str) -> list[tuple[int, str]]:
        return provider_priority(self.settings, self.providers, kind)

    async def _call(self, awaitable):
        return await asyncio.wait_for(awaitable, timeout=self.settings.provider_call_timeout_seconds)

    def _provider_is_cooling(self, name: str, kind: str = "general") -> bool:
        until = self._provider_cooldowns.get((name, kind))
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        self._provider_cooldowns.pop((name, kind), None)
        return False

    def _record_provider_success(self, name: str, index: int, latency_ms: float, kind: str) -> None:
        self.cache.update_provider_capability_success(name, kind, index, latency_ms)
        self._clear_provider_cooldown(name, kind)

    def _record_provider_failure(self, name: str, index: int, exc: Exception, kind: str) -> None:
        self.cache.update_provider_capability_failure(name, kind, index, _provider_error_text(exc))
        cooldown_seconds = max(0, self.settings.provider_failure_cooldown_seconds)
        if cooldown_seconds:
            self._provider_cooldowns[(name, kind)] = time.monotonic() + cooldown_seconds

    def _clear_provider_cooldown(self, name: str, kind: str = "general") -> None:
        self._provider_cooldowns.pop((name, kind), None)

    def _all_provider_names(self) -> list[str]:
        return all_provider_names(self.settings, self.providers)

    def _provider_index(self, name: str) -> int:
        return provider_index(self.settings, self.providers, name)

    def _normalize_symbols(self, symbols: Iterable[str]) -> list[str]:
        normalized = []
        for symbol in symbols:
            if not symbol or not symbol.strip():
                continue
            normalized.append(standard_symbol(symbol.strip()))
        return normalized

    def _ordered_complete_quotes(self, quotes: list[Quote], requested_symbols: list[str], source_name: str) -> list[Quote]:
        by_symbol = {standard_symbol(f"{quote.code}.{quote.market}"): quote for quote in quotes}
        missing = [symbol for symbol in requested_symbols if symbol not in by_symbol]
        if missing:
            raise RuntimeError(f"{source_name} 行情缺失：{','.join(missing)}")
        return [by_symbol[symbol] for symbol in requested_symbols]

    def _matched_quotes(self, quotes: list[Quote], requested_symbols: list[str]) -> tuple[list[Quote], list[str]]:
        by_symbol = {standard_symbol(f"{quote.code}.{quote.market}"): quote for quote in quotes}
        matched = [by_symbol[symbol] for symbol in requested_symbols if symbol in by_symbol]
        missing = [symbol for symbol in requested_symbols if symbol not in by_symbol]
        return matched, missing

    def _sync_provider_enabled_flags(self) -> None:
        for name in self._all_provider_names():
            provider = self.providers[name]
            priority = self._provider_index(name)
            self.cache.ensure_provider(name, priority, enabled=provider_is_enabled(provider))
            for kind in _supported_provider_kinds(provider):
                self.cache.ensure_provider_capability(name, kind, priority, enabled=provider_enabled_for(provider, kind))

    async def _quote_consistency(self, quote: Quote, check_consistency: bool = True) -> tuple[str, list[str], int]:
        if not check_consistency:
            return "未校验", ["当前报价未做多源一致性抽检。"], 4
        if "缓存" in quote.source and "短时缓存" not in quote.source:
            return "未校验", ["当前报价来自较旧兜底缓存，暂不做多源一致性抽检。"], 4
        notes: list[str] = []
        target_symbol = f"{quote.code}.{quote.market}"
        current_source = _provider_source_key(quote.source)
        tasks = []
        for index, name in self._priority("quote"):
            if name in {"demo"}:
                continue
            if self._provider_is_cooling(name, "quote"):
                continue
            provider = self.providers[name]
            if _provider_source_key(provider.source_name) == current_source:
                continue
            tasks.append(self._quote_consistency_probe(index, name, provider, target_symbol))

        if not tasks:
            return "单源可用", ["当前只有主行情源可用，多源一致性暂无法确认。"], 8

        results = await asyncio.gather(*tasks)
        compared = 0
        gaps: list[float] = []
        failed = 0
        for result in results:
            name = result["name"]
            index = int(result["index"])
            exc = result.get("error")
            if isinstance(exc, Exception):
                failed += 1
                self._record_provider_failure(str(name), index, exc, "quote")
                continue
            self._record_provider_success(str(name), index, float(result["latency_ms"]), "quote")
            other = result["quote"]
            compared += 1
            if quote.price > 0 and other.price > 0:
                gap_pct = abs(other.price - quote.price) / quote.price * 100
                gaps.append(gap_pct)
        if compared == 0:
            return "单源可用", ["备用行情源均不可用，多源一致性暂无法确认。"], 8
        if not gaps:
            return "字段异常", [f"已连接 {compared + 1} 个行情源，但备用源价格字段无效，需人工复核。"], 12
        max_price_gap_pct = max(gaps)
        threshold = self.settings.quote_consistency_warning_pct
        source_note = f"参与校验 {compared + 1} 个行情源，备用失败 {failed} 个。"
        if max_price_gap_pct > threshold:
            notes.append(f"{source_note}多源最大价格差异 {max_price_gap_pct:.2f}%，超过 {threshold:.2f}% 阈值。")
            self.cache.save_monitor_event("warning", "quote", notes[-1], symbol=target_symbol)
            return "存在差异", notes, 18
        notes.append(f"{source_note}多源最大价格差异 {max_price_gap_pct:.2f}%，处于可接受范围。")
        return "一致", notes, 0

    async def _quote_consistency_probe(self, index: int, name: str, provider, target_symbol: str) -> dict[str, object]:
        started = time.perf_counter()
        try:
            rows = await self._call(provider.quotes([target_symbol]))
            ordered = self._ordered_complete_quotes(rows, [target_symbol], provider.source_name)
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            return {"name": name, "index": index, "quote": ordered[0], "latency_ms": latency_ms}
        except Exception as exc:
            return {"name": name, "index": index, "error": exc}


def _provider_source_key(source: str) -> str:
    lowered = source.lower()
    if "腾讯" in source or "tencent" in lowered:
        return "tencent"
    if "akshare" in lowered:
        return "akshare"
    if "tushare" in lowered:
        return "tushare"
    if "baostock" in lowered:
        return "baostock"
    if "futu" in lowered or "富途" in source:
        return "futu"
    if "演示" in source or "demo" in lowered:
        return "demo"
    return lowered.split("·", 1)[0]


def _capability_status_map(items: list[ProviderCapabilityStatus]) -> dict[tuple[str, str], ProviderCapabilityStatus]:
    return {(item.name, item.kind): item for item in items}


def _first_healthy_provider(
    names: list[str],
    providers: dict[str, ProviderStatus],
    capabilities: dict[tuple[str, str], ProviderCapabilityStatus],
    kind: str,
) -> str | None:
    for name in names:
        capability = capabilities.get((name, kind))
        if capability is not None:
            if _capability_has_activity(capability):
                if capability.enabled and capability.healthy:
                    return name
                continue
            status = providers.get(name)
            if status and status.enabled and status.healthy:
                return name
            continue
        status = providers.get(name)
        if status and status.enabled and status.healthy:
            return name
    return None


_KIND_LABELS = {
    "quote": "报价",
    "kline": "日K",
    "minute": "分钟",
    "stock": "股票池",
    "plate": "板块",
    "concept": "概念",
    "order_book": "盘口",
}


def _provider_capability_state(
    name: str,
    quote_names: list[str],
    kline_names: list[str],
    minute_names: list[str],
    statuses: dict[tuple[str, str], ProviderCapabilityStatus],
) -> str:
    kinds = _decision_kinds(name, quote_names, kline_names, minute_names)
    pieces = []
    for kind in kinds:
        status = statuses.get((name, kind))
        if status is None or not status.enabled:
            continue
        label = _KIND_LABELS.get(kind, kind)
        if not _capability_has_activity(status):
            pieces.append(f"{label}未探测")
        elif status.healthy:
            pieces.append(f"{label}正常")
        elif status.last_error:
            pieces.append(f"{label}最近失败")
    return " / ".join(pieces)


def _provider_cooling_kinds(name: str, quote_names: list[str], kline_names: list[str], minute_names: list[str], checker) -> list[str]:
    return [
        _KIND_LABELS.get(kind, kind)
        for kind in _decision_kinds(name, quote_names, kline_names, minute_names)
        if checker(name, kind)
    ]


def _decision_kinds(name: str, quote_names: list[str], kline_names: list[str], minute_names: list[str]) -> list[str]:
    kinds = []
    if name in quote_names:
        kinds.append("quote")
    if name in kline_names:
        kinds.append("kline")
    if name in minute_names:
        kinds.append("minute")
    if name == "futu":
        kinds.append("order_book")
    return kinds


def _unhealthy_capability_labels(items: list[ProviderCapabilityStatus]) -> list[str]:
    labels = []
    for item in items:
        if item.enabled and not item.healthy and _capability_has_activity(item):
            labels.append(f"{item.name} {_KIND_LABELS.get(item.kind, item.kind)}")
    return labels


def _capability_has_activity(item: ProviderCapabilityStatus) -> bool:
    return bool(item.last_success or item.last_error or item.success_count or item.failure_count)


def _supported_provider_kinds(provider) -> list[str]:
    capability = provider_capability(provider)
    if capability is None:
        return ["quote", "kline"]
    pairs = [
        ("quote", capability.realtime_quote),
        ("kline", capability.daily_kline),
        ("minute", capability.minute_kline),
        ("stock", capability.stock_pool),
        ("plate", capability.plate_rank),
        ("concept", capability.concept_board),
        ("order_book", capability.order_book),
    ]
    return [kind for kind, supported in pairs if supported]


def _source_plan_summary(health_level: str, quote: str | None, kline: str | None, minute: str | None) -> str:
    quote_text = quote or "缺失"
    kline_text = kline or "缺失"
    minute_text = minute or "缺失"
    if health_level == "健康":
        return f"数据链路健康：报价主源 {quote_text}，日K主源 {kline_text}，分钟线主源 {minute_text}。"
    if health_level == "降级可用":
        return f"数据链路降级但仍可分析：报价主源 {quote_text}，日K主源 {kline_text}，分钟线 {minute_text}。"
    return f"数据链路高风险：报价 {quote_text}，日K {kline_text}，分钟线 {minute_text}，结论需要谨慎。"


def _capability_labels(capability: ProviderCapability | None) -> list[str]:
    if capability is None:
        return []
    labels: list[str] = []
    if capability.realtime_quote:
        labels.append("实时报价")
    if capability.daily_kline:
        labels.append("日K")
    if capability.minute_kline:
        labels.append("分钟线")
    if capability.stock_pool:
        labels.append("股票池")
    if capability.plate_rank:
        labels.append("行业板块")
    if capability.concept_board:
        labels.append("概念")
    if capability.order_book:
        labels.append("盘口")
    return labels


def _provider_role(name: str, quote_names: list[str], kline_names: list[str], minute_names: list[str]) -> str:
    roles: list[str] = []
    if name in quote_names:
        roles.append("报价")
    if name in kline_names:
        roles.append("日K")
    if name in minute_names:
        roles.append("分钟")
    return " / ".join(roles) if roles else "辅助"


def _provider_success_rate(status: ProviderStatus | None) -> float | None:
    if status is None:
        return None
    total = status.success_count + status.failure_count
    if total <= 0:
        return None
    return round(status.success_count / total * 100, 1)


def _provider_recovery_action(name: str, last_error: str | None) -> str:
    text = str(last_error or "")
    if "ProxyError" in text or "Unable to connect to proxy" in text or "HTTPSConnectionPool" in text:
        return "检查网络代理或源站连通性；冷却期内系统会先使用其他源或缓存。"
    if "RemoteDisconnected" in text or "Connection aborted" in text:
        return "东方财富源站主动断开连接；系统会先使用 Tencent、BaoStock 或缓存，稍后自动重试。"
    if "TimeoutError" in text or "超时" in text:
        return "源站响应超时；系统会先使用其他源或缓存，稍后自动重试。"
    if name == "baostock":
        return "BaoStock 偏历史备份，失败时先依赖 Tencent/AKShare 日K缓存。"
    if name == "futu":
        return "确认 Futu OpenD 已启动，并设置 FUTU_ENABLED=1。"
    if name == "tushare":
        return "配置 TUSHARE_TOKEN 后再启用。"
    return "稍后自动重试；若持续失败，建议检查依赖安装和网络。"


def _provider_error_text(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return text
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return "TimeoutError: 数据源响应超时"
    if isinstance(exc, socket.timeout):
        return "TimeoutError: 网络请求超时"
    return exc.__class__.__name__


def _tag_cached_quotes(quotes: list[Quote], label: str) -> list[Quote]:
    return [_quote_with_cache_label(quote, label) for quote in quotes]


def _quote_with_cache_label(quote: Quote, label: str) -> Quote:
    base_source = quote.source.split("·", 1)[0].strip() or quote.source
    return quote.model_copy(update={"source": f"{base_source}·{label}"})


def _stock_pool_rows_are_authoritative(rows: list[StockInfo], min_count: int) -> bool:
    return len(rows) >= max(1, min_count)


def _stock_pool_cache_is_authoritative(
    cache,
    max_age_seconds: int,
    min_count: int,
    fresh_count: int | None = None,
) -> bool:
    stock_count = cache.stock_count if fresh_count is None else fresh_count
    return _stock_pool_cache_is_fresh(cache, max_age_seconds) and stock_count >= max(1, min_count)


def _stock_pool_cache_is_fresh(cache, max_age_seconds: int) -> bool:
    if not cache.latest_stock_at or cache.stock_count <= 0:
        return False
    age = seconds_since_text(cache.latest_stock_at)
    return age is not None and age <= max_age_seconds


def _kline_cache_is_fresh(klines: list[Kline]) -> bool:
    if not klines:
        return False
    last_date = _parse_kline_date(klines[-1].date)
    if last_date is None:
        return False
    latest_expected = latest_expected_trade_date(datetime.now())
    return last_date.date() >= latest_expected


def _parse_kline_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value[:10])
    except ValueError:
        return None


def _tag_klines(
    klines: list[Kline],
    source: str | None,
    *,
    from_cache: bool,
    fallback_used: bool = False,
) -> list[Kline]:
    tagged: list[Kline] = []
    for item in klines:
        tagged.append(
            item.model_copy(
                update={
                    "source": item.source or source,
                    "from_cache": from_cache,
                    "fallback_used": fallback_used or item.fallback_used,
                }
            )
        )
    return tagged


def _tag_minute_klines(
    rows: list[MinuteKline],
    source: str | None,
    interval: str,
    *,
    from_cache: bool,
    fallback_used: bool = False,
) -> list[MinuteKline]:
    tagged: list[MinuteKline] = []
    for item in rows:
        tagged.append(
            item.model_copy(
                update={
                    "source": item.source or source,
                    "interval": interval,
                    "from_cache": from_cache,
                    "fallback_used": fallback_used or item.fallback_used,
                }
            )
        )
    return tagged


def _normalize_stock_concepts(symbol: str, rows: list[StockConceptItem], limit: int) -> list[StockConceptItem]:
    normalized = standard_symbol(symbol)
    deduped: dict[str, StockConceptItem] = {}
    for item in rows:
        name = item.name.strip()
        if not name or name in deduped:
            continue
        deduped[name] = item.model_copy(update={"symbol": normalized, "rank": len(deduped) + 1})
        if len(deduped) >= limit:
            break
    return list(deduped.values())


def _normalize_minute_interval(interval: str) -> str:
    normalized = str(interval or "5m").lower().strip()
    if normalized in {"1", "1min", "1m"}:
        return "1m"
    if normalized in {"3", "3min", "3m"}:
        return "3m"
    if normalized in {"5", "5min", "5m"}:
        return "5m"
    if normalized in {"10", "10min", "10m"}:
        return "10m"
    if normalized in {"15", "15min", "15m"}:
        return "15m"
    if normalized in {"30", "30min", "30m"}:
        return "30m"
    if normalized in {"60", "60min", "60m", "1h"}:
        return "60m"
    raise ValueError("分钟周期只支持 1m、3m、5m、10m、15m、30m、60m")
