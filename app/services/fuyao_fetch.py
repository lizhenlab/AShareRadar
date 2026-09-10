"""Bounded collection flows; only normalized observations reach application storage."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.models.fuyao_research import FuyaoJobRequest
from app.services.fuyao_financials import normalize_financials
from app.services.fuyao_observations import (
    SENTIMENT_POOLS, batch_timestamp, canonical_stock, envelope_data, finite_or_none,
    item_rows, normalized_pool_rows, pool_pagination, safe_text,
)
from app.services.trading_calendar import expected_quote_date
from app.utils.clock import ASHARE_TIMEZONE, market_now


class FuyaoRequester(Protocol):
    async def request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


async def fetch_financials(client: FuyaoRequester, symbol: str, request: FuyaoJobRequest, fetched_at: str) -> dict[str, Any]:
    symbol = canonical_stock(symbol)
    payloads = {}
    params = {"thscode": symbol, "period": request.period, "limit": request.limit}
    endpoints = {"income": "income-statements", "balance": "balance-sheets", "cashflow": "cash-flow-statements"}
    for kind, endpoint in endpoints.items():
        payloads[kind] = await client.request(f"/api/a-share/financials/{endpoint}", params)
    report = request.report or _latest_report(payloads["income"])
    if report is not None:
        payloads["indicators"] = await client.request("/api/a-share/financials/indicators", {"thscode": symbol, "report": report})
    bundle = normalize_financials(symbol, payloads, fetched_at)
    if not bundle.periods:
        raise ValueError("本次未返回财报记录，不能覆盖既有记录")
    return {"report": bundle.model_dump(mode="json"), "raw": payloads}


def _latest_report(payload: dict[str, Any]) -> str | None:
    rows = item_rows(envelope_data(payload), limit=80)
    valid = [value for row in rows if (value := batch_timestamp(row.get("period_end_ms"))) is not None]
    if not valid:
        return None
    day = datetime.fromtimestamp(max(valid) / 1000, ASHARE_TIMEZONE)
    return f"{day.year}-{(day.month - 1) // 3 + 1}"


async def fetch_sentiment(client: FuyaoRequester, symbols: list[str]) -> dict[str, Any]:
    started = market_now()
    day = expected_quote_date(started)
    symbols = list(dict.fromkeys(canonical_stock(symbol) for symbol in symbols))
    date_ms = int(datetime.combine(day, datetime.min.time(), ASHARE_TIMEZONE).timestamp() * 1000)
    pools = {}
    for kind in SENTIMENT_POOLS:
        pools[kind] = await _fetch_pool(client, kind, date_ms)
    lhb = envelope_data(await client.request("/api/a-share/special-data/dragon-tiger-list", {"date": day.isoformat()}))
    if lhb.get("trade_date") != day.isoformat():
        raise ValueError("龙虎榜响应交易日与请求不一致")
    anomalies: list[dict[str, Any]] = []
    for offset in range(0, len(symbols) if day == started.date() else 0, 50):
        if market_now().date() != started.date():
            raise ValueError("当日异动采集跨越上海日期，拒绝混合日期发布")
        selected = symbols[offset:offset + 50]
        data = envelope_data(await client.request("/api/a-share/special-data/anomaly-analysis-stock",
                                                 {"thscodes": ",".join(selected)}))
        if market_now().date() != started.date():
            raise ValueError("当日异动采集跨越上海日期，拒绝混合日期发布")
        anomalies.extend(_anomalies(data, selected))
    return {"trade_date": day.isoformat(), "pools": pools, "dragon_tiger": _lhb_rows(lhb),
            "anomalies": anomalies, "anomaly_observation_date": started.date().isoformat() if day == started.date() else None,
            "scope": "指定交易日股票池；异动解释仅采集当日请求标的", "point_in_time": False}


async def _fetch_pool(client: FuyaoRequester, kind: str, date_ms: int) -> list[dict[str, Any]]:
    rows, pages, page, seen = [], 1, 1, set()
    contract: tuple[int, int, int] | None = None
    while page <= pages:
        payload = await client.request(f"/api/a-share/special-data/{kind}", {"date_ms": date_ms, "page": page, "size": 200})
        batch, observed_pages = normalized_pool_rows(payload)
        pagination = pool_pagination(envelope_data(payload), len(batch), page)
        observed_contract = (pagination["total"], observed_pages, pagination["size"])
        if contract is not None and contract != observed_contract:
            raise ValueError("股票池分页在采集中变化，请重新同步")
        contract = observed_contract
        pages = observed_pages
        for row in batch:
            if row["symbol"] in seen:
                raise ValueError("股票池分页重复，拒绝发布不完整统计")
            seen.add(row["symbol"])
        rows.extend(batch)
        page += 1
    if contract is None or len(rows) != contract[0]:
        raise ValueError("股票池总量与完整分页结果不一致")
    return rows


def _anomalies(data: dict[str, Any], symbols: list[str]) -> list[dict[str, Any]]:
    result = []
    timestamp = batch_timestamp(data.get("timestamp"))
    for item in item_rows(data):
        symbol = canonical_stock(item.get("thscode"))
        if symbol not in symbols:
            raise ValueError("异动解释包含非请求股票")
        result.append({"symbol": symbol, "tag": safe_text(item.get("tag_name")),
                       "text": safe_text(item.get("analysis_content"), 2000), "kind": "provider_interpretation",
                       "batch_timestamp": timestamp, "individual_timestamp": None})
    return result


def _lhb_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = item_rows(data, "stock_items")
    result = [{"symbol": canonical_stock(item.get("thscode")), "name": safe_text(item.get("name")),
               "net_value": finite_or_none(item.get("net_value")), "org_net_value": finite_or_none(item.get("org_net_value")),
               "range_days": finite_or_none(item.get("range_days"))} for item in rows]
    if type(data.get("stock_count")) is not int or data["stock_count"] != len({row["symbol"] for row in result}):
        raise ValueError("龙虎榜股票数量与记录不一致")
    return result
