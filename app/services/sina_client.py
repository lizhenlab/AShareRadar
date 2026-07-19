from __future__ import annotations

from math import ceil
from typing import Any

import requests  # type: ignore[import-untyped]

from app.services.provider_errors import ProviderError, ProviderProtocolError, ProviderTransportError, sanitize_provider_error


SINA_BJ_STOCK_POOL_SOURCE_NAME = "AKShare·新浪财经"
SINA_MARKET_NODE = "hs_bjs"
SINA_STOCK_COUNT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount"
)
SINA_STOCK_DATA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
SINA_PAGE_SIZE = 100
SINA_MAX_STOCK_COUNT = 2_000
SINA_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://vip.stock.finance.sina.com.cn/mkt/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}


def sina_bj_stock_pool_rows(*, timeout: float = 8) -> list[dict[str, Any]]:
    """Load the complete BSE equity node without relying on BSE or Eastmoney."""
    raw_count = _sina_get_json(SINA_STOCK_COUNT_URL, {"node": SINA_MARKET_NODE}, timeout=timeout)
    count = _sina_stock_count(raw_count)
    pages = ceil(count / SINA_PAGE_SIZE)
    rows: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        payload = _sina_get_json(
            SINA_STOCK_DATA_URL,
            {
                "page": str(page),
                "num": str(SINA_PAGE_SIZE),
                "sort": "symbol",
                "asc": "1",
                "node": SINA_MARKET_NODE,
                "symbol": "",
                "_s_r_a": "page",
            },
            timeout=timeout,
        )
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ProviderProtocolError("新浪北交所股票列表返回结构异常")
        rows.extend(payload)
    return _validated_sina_stock_rows(rows, expected_count=count)


def _sina_stock_count(value: Any) -> int:
    if isinstance(value, bool):
        raise ProviderProtocolError("新浪北交所股票数量异常")
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ProviderProtocolError("新浪北交所股票数量异常") from None
    if count <= 0 or count > SINA_MAX_STOCK_COUNT:
        raise ProviderProtocolError("新浪北交所股票数量超出合理范围")
    return count


def _validated_sina_stock_rows(rows: list[dict[str, Any]], *, expected_count: int) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("code") or row.get("代码") or "").strip()
        name = str(row.get("name") or row.get("名称") or "").strip()
        if len(code) != 6 or not code.isdigit() or not name:
            raise ProviderProtocolError("新浪北交所股票列表包含无效记录")
        if code in by_code:
            raise ProviderProtocolError(f"新浪北交所股票列表包含重复代码：{code}")
        by_code[code] = row
    if len(by_code) != expected_count:
        raise ProviderProtocolError(
            f"新浪北交所股票列表不完整：期望 {expected_count} 条，实际 {len(by_code)} 条"
        )
    return list(by_code.values())


def _sina_get_json(url: str, params: dict[str, str], *, timeout: float) -> Any:
    if not url.startswith("https://"):
        raise ProviderProtocolError("新浪行情接口仅允许 HTTPS")
    try:
        with requests.Session() as session:
            response = session.get(url, params=params, headers=SINA_HEADERS, timeout=timeout)
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                raise ProviderProtocolError("新浪行情接口返回非 JSON 响应") from None
    except ProviderError:
        raise
    except requests.RequestException as exc:
        raise ProviderTransportError(sanitize_provider_error(exc)) from exc
    except Exception as exc:
        raise ProviderTransportError(sanitize_provider_error(exc)) from exc


__all__ = ["SINA_BJ_STOCK_POOL_SOURCE_NAME", "sina_bj_stock_pool_rows"]
