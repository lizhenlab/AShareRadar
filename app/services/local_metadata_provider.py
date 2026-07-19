from __future__ import annotations

from app.models.schemas import PlateItem, ProviderCapability, StockConceptItem, StockInfo
from app.services.provider_utils import ensure_positive_limit
from app.utils.symbols import normalize_symbol, standard_symbol
from app.utils.time import now_text


class LocalIndividualStockProvider:
    source_name = "本地个股基础数据"

    _stocks = [
        ("600519.SH", "贵州茅台", "白酒"),
        ("000001.SZ", "平安银行", "银行"),
        ("300750.SZ", "宁德时代", "电池"),
        ("601318.SH", "中国平安", "保险"),
        ("000858.SZ", "五粮液", "白酒"),
        ("002594.SZ", "比亚迪", "汽车整车"),
        ("600036.SH", "招商银行", "银行"),
        ("600900.SH", "长江电力", "电力"),
        ("000333.SZ", "美的集团", "家电"),
        ("002475.SZ", "立讯精密", "消费电子"),
        ("002182.SZ", "宝武镁业", "小金属"),
    ]

    _plates = [
        ("电池", 2.8, "宁德时代"),
        ("汽车整车", 2.2, "比亚迪"),
        ("消费电子", 1.9, "立讯精密"),
        ("电力", 1.1, "长江电力"),
        ("银行", 0.6, "招商银行"),
        ("白酒", -0.4, "五粮液"),
        ("保险", -0.8, "中国平安"),
        ("家电", 0.3, "美的集团"),
        ("小金属", 0.2, "宝武镁业"),
    ]

    _concepts = {
        "600519.SH": [("白酒概念", -0.4, "贵州茅台"), ("MSCI中国", 0.2, "贵州茅台"), ("消费龙头", 0.1, "五粮液")],
        "000001.SZ": [("互联金融", 0.8, "平安银行"), ("破净股", 0.3, "招商银行")],
        "300750.SZ": [("动力电池", 2.8, "宁德时代"), ("储能", 1.6, "阳光电源"), ("新能源汽车", 2.2, "比亚迪")],
        "601318.SH": [("保险", -0.8, "中国平安"), ("大金融", 0.4, "东方财富")],
        "000858.SZ": [("白酒概念", -0.4, "五粮液"), ("消费龙头", 0.1, "贵州茅台")],
        "002594.SZ": [("新能源汽车", 2.2, "比亚迪"), ("刀片电池", 2.1, "比亚迪")],
        "600036.SH": [("银行", 0.6, "招商银行"), ("破净股", 0.3, "招商银行")],
        "600900.SH": [("水电", 1.1, "长江电力"), ("高股息", 0.7, "中国神华")],
        "000333.SZ": [("家电", 0.3, "美的集团"), ("机器人概念", 1.2, "汇川技术")],
        "002475.SZ": [("消费电子", 1.9, "立讯精密"), ("苹果概念", 1.4, "立讯精密")],
        "002182.SZ": [("镁金属", 0.2, "宝武镁业"), ("小金属概念", 0.4, "宝武镁业")],
    }

    async def stock_pool(self) -> list[StockInfo]:
        stamp = now_text()
        rows = []
        for symbol, name, industry in self._stocks:
            code, market = normalize_symbol(symbol)
            rows.append(
                StockInfo(
                    symbol=standard_symbol(symbol),
                    code=code,
                    market=market.upper(),
                    name=name,
                    industry=industry,
                    source=self.source_name,
                    updated_at=stamp,
                )
            )
        return rows

    async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
        ensure_positive_limit(limit)
        stamp = now_text()
        return [
            PlateItem(
                rank=index,
                name=name,
                change_pct=change_pct,
                amount=None,
                turnover_rate=None,
                leading_stock=leading_stock,
                leading_stock_change_pct=None,
                source=self.source_name,
                updated_at=stamp,
            )
            for index, (name, change_pct, leading_stock) in enumerate(self._plates[:limit], start=1)
        ]

    async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
        ensure_positive_limit(limit)
        normalized = standard_symbol(symbol)
        stamp = now_text()
        return [
            StockConceptItem(
                symbol=normalized,
                rank=index,
                name=name,
                change_pct=change_pct,
                amount=None,
                turnover_rate=None,
                leading_stock=leading_stock,
                leading_stock_change_pct=None,
                match_reason="本地兜底概念归属",
                source=self.source_name,
                updated_at=stamp,
            )
            for index, (name, change_pct, leading_stock) in enumerate(self._concepts.get(normalized, [])[:limit], start=1)
        ]

    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            name="local",
            installed=True,
            enabled=True,
            reliability_level="本地基础数据",
            stock_pool=True,
            plate_rank=False,
            concept_board=False,
            note="本地静态资料只补充股票名称和行业归属，不提供实时板块或概念涨跌幅。",
        )
