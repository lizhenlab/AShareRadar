"""Current sector observations never claim historical constituent membership."""

from __future__ import annotations

import re
from typing import Any

from app.services.fuyao_fetch import FuyaoRequester
from app.services.fuyao_observations import batch_timestamp, canonical_stock, envelope_data, finite_or_none, item_rows, safe_text


def canonical_index(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("板块代码格式无效")
    symbol = value.strip().upper()
    if not re.fullmatch(r"\d{6}\.(TI|SH|SZ)", symbol):
        raise ValueError("板块代码须为六位代码及 .TI/.SH/.SZ 后缀")
    return symbol


async def fetch_sectors(client: FuyaoRequester, index_symbols: list[str]) -> dict[str, Any]:
    catalog: dict[str, dict[str, Any]] = {}
    for tag in ("industry", "cn_concept"):
        data = envelope_data(await client.request("/api/a-share-index/catalog/ths-index-list", {"tag": tag}))
        for item in item_rows(data, limit=5000):
            code = canonical_index(item.get("thscode"))
            if code in catalog:
                raise ValueError("扶摇板块目录存在重复代码")
            catalog[code] = {"symbol": code, "name": safe_text(item.get("name")), "category": tag}
    if not catalog or len(catalog) > 5000:
        raise ValueError("扶摇板块目录为空或超出范围")
    quotes = await _sector_quotes(client, list(catalog))
    members = {}
    for symbol in dict.fromkeys(canonical_index(value) for value in index_symbols):
        data = envelope_data(await client.request("/api/a-share-index/constituents/ths-stock-list", {"thscode": symbol}))
        members[symbol] = list(dict.fromkeys(canonical_stock(item.get("thscode")) for item in item_rows(data)))
    rows = [{**item, **quotes.get(symbol, {})} for symbol, item in catalog.items()]
    rows.sort(key=lambda item: (item.get("change_pct") is not None, item.get("change_pct") or 0), reverse=True)
    return {"rows": rows, "members": members, "membership_basis": "当前成员，非历史名单", "point_in_time": False,
            "catalog_count": len(catalog), "quote_count": len(quotes)}


async def _sector_quotes(client: FuyaoRequester, symbols: list[str]) -> dict[str, dict[str, Any]]:
    result = {}
    for offset in range(0, len(symbols), 100):
        selected = symbols[offset:offset + 100]
        data = envelope_data(await client.request("/api/a-share-index/prices/snapshot", {"thscodes": ",".join(selected)}))
        timestamp = batch_timestamp(data.get("timestamp"))
        for item in item_rows(data, limit=100):
            symbol = canonical_index(item.get("thscode"))
            if symbol not in selected or symbol in result:
                raise ValueError("扶摇板块行情包含重复或非请求标的")
            price = finite_or_none(item.get("last_price"))
            if price is not None and price <= 0:
                raise ValueError("扶摇板块价格无效")
            result[symbol] = {"change_pct": finite_or_none(item.get("price_change_ratio_pct")),
                              "last_price": price, "batch_timestamp": timestamp,
                              "individual_timestamp": None}
    return result
