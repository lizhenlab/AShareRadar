from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import Settings
from app.models.market import StockInfo
from app.services.baostock_provider import BaoStockProvider
from app.services.cache import SQLiteCache
from app.services.datahub_metadata_coordinator import MetadataCoordinator
from app.services.datahub_runtime import ProviderRuntime
from app.utils.market_time import market_now_naive
from tests.factories import make_stock_info


class _BaoStockResult:
    def __init__(
        self,
        rows: list[list[str]],
        fields: list[str],
        *,
        error_code: str = "0",
        error_msg: str = "",
    ) -> None:
        self.rows = rows
        self.fields = fields
        self.error_code = error_code
        self.error_msg = error_msg
        self.index = -1

    def next(self) -> bool:
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self) -> list[str]:
        return self.rows[self.index]


class _BaoStockLogin:
    error_code = "0"
    error_msg = ""


class _BaoStockModule:
    def __init__(self, *, industry_error: bool = False) -> None:
        self.industry_error = industry_error
        self.industry_calls = 0
        self.login_calls = 0
        self.logout_calls = 0

    def login(self) -> _BaoStockLogin:
        self.login_calls += 1
        return _BaoStockLogin()

    def logout(self) -> None:
        self.logout_calls += 1

    @staticmethod
    def query_stock_basic() -> _BaoStockResult:
        return _BaoStockResult(
            [
                ["sh.600519", "贵州茅台", "2001-08-27"],
                ["sz.000001", "平安银行", "1991-04-03"],
            ],
            ["code", "code_name", "ipoDate"],
        )

    def query_stock_industry(self) -> _BaoStockResult:
        self.industry_calls += 1
        if self.industry_error:
            return _BaoStockResult([], [], error_code="1001", error_msg="industry unavailable")
        return _BaoStockResult(
            [
                ["2026-07-01", "sh.600519", "贵州茅台", "C15白酒", "申万一级行业"],
                ["2026-07-01", "sz.000001", "平安银行", "J66银行", "申万一级行业"],
                ["2026-07-01", "sh.600000", "浦发银行", "--", "申万一级行业"],
            ],
            ["updateDate", "code", "code_name", "industry", "industryClassification"],
        )


def _stock_rows(*, sh_count: int, sh_industry: str | None = None) -> list[StockInfo]:
    stamp = market_now_naive().strftime("%Y-%m-%d %H:%M:%S")
    sh_rows = [
        make_stock_info(code=f"{600000 + index:06d}", market="SH").model_copy(
            update={"industry": sh_industry, "updated_at": stamp, "source": "AKShare"}
        )
        for index in range(sh_count)
    ]
    return [
        *sh_rows,
        make_stock_info(code="000001", market="SZ").model_copy(update={"updated_at": stamp}),
        make_stock_info(code="920001", market="BJ").model_copy(update={"updated_at": stamp}),
    ]


def test_baostock_stock_pool_uses_one_bulk_industry_query() -> None:
    fake = _BaoStockModule()
    provider = BaoStockProvider()

    with patch("app.services.baostock_provider.is_installed", return_value=True), patch.dict(
        "sys.modules", {"baostock": fake}
    ):
        rows = asyncio.run(provider.stock_pool())

    assert fake.industry_calls == 1
    assert fake.login_calls == fake.logout_calls == 1
    assert {item.symbol: item.industry for item in rows} == {
        "600519.SH": "白酒",
        "000001.SZ": "银行",
    }


def test_baostock_stock_pool_keeps_complete_universe_when_industry_query_fails() -> None:
    fake = _BaoStockModule(industry_error=True)
    provider = BaoStockProvider()

    with patch("app.services.baostock_provider.is_installed", return_value=True), patch.dict(
        "sys.modules", {"baostock": fake}
    ):
        rows = asyncio.run(provider.stock_pool())

    assert [item.symbol for item in rows] == ["600519.SH", "000001.SZ"]
    assert all(item.industry is None for item in rows)
    assert fake.industry_calls == 1
    assert fake.login_calls == fake.logout_calls == 1


def test_stock_pool_bulk_enriches_systemic_market_gap_and_preserves_universe() -> None:
    primary_rows = _stock_rows(sh_count=100)
    primary_rows[0] = primary_rows[0].model_copy(update={"industry": "已有行业"})
    industry_calls = 0

    class PrimaryProvider:
        source_name = "AKShare"

        async def stock_pool(self):
            return primary_rows

    class IndustryProvider:
        source_name = "BaoStock"

        async def stock_industries(self):
            nonlocal industry_calls
            industry_calls += 1
            return {item.symbol: "批量行业" for item in primary_rows if item.market == "SH"}

        async def stock_pool(self):
            raise AssertionError("行业增强不得重新拉取备用股票池")

    async def run_check(path: Path):
        settings = Settings(stock_pool_authoritative_min_count=3, provider_failure_cooldown_seconds=0)
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"akshare": PrimaryProvider(), "baostock": IndustryProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "akshare"), (2, "baostock")],
        )
        first_rows = await coordinator.stock_pool(limit=None, refresh=True)
        rows = await coordinator.stock_pool(limit=None, refresh=True)
        cached = cache.get_stock_pool(60, limit=None)
        statuses = cache.provider_capability_statuses()
        return first_rows, rows, cached, statuses

    with TemporaryDirectory() as tmpdir:
        first_rows, rows, cached, statuses = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert industry_calls == 1
    assert [(item.industry, item.source) for item in rows] == [
        (item.industry, item.source) for item in first_rows
    ]
    assert [item.symbol for item in rows] == [item.symbol for item in primary_rows]
    assert rows[0].industry == "已有行业"
    assert rows[0].source == "AKShare"
    assert all(item.industry is not None for item in rows if item.market == "SH")
    assert all(item.source == "AKShare + BaoStock(行业)" for item in rows[1:100])
    assert {item.symbol: (item.industry, item.source) for item in cached} == {
        item.symbol: (item.industry, item.source) for item in rows
    }
    industry_status = next(item for item in statuses if item.name == "baostock" and item.kind == "stock_industry")
    assert industry_status.success_count == 1
    assert industry_status.failure_count == 0
    assert not any(item.name == "baostock" and item.kind == "stock" for item in statuses)


def test_stock_pool_industry_enrichment_failure_is_non_fatal() -> None:
    primary_rows = _stock_rows(sh_count=100)

    class PrimaryProvider:
        source_name = "AKShare"

        async def stock_pool(self):
            return primary_rows

    class FailingIndustryProvider:
        source_name = "BaoStock"

        async def stock_industries(self):
            raise RuntimeError("industry endpoint unavailable")

    async def run_check(path: Path):
        settings = Settings(stock_pool_authoritative_min_count=3, provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"akshare": PrimaryProvider(), "baostock": FailingIndustryProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "akshare"), (2, "baostock")],
        )
        rows = await coordinator.stock_pool(limit=None, refresh=True)
        statuses = cache.provider_capability_statuses()
        return (
            rows,
            statuses,
            runtime.is_cooling("baostock", "stock_industry"),
            runtime.is_cooling("baostock", "stock"),
        )

    with TemporaryDirectory() as tmpdir:
        rows, statuses, industry_cooling, stock_cooling = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.symbol for item in rows] == [item.symbol for item in primary_rows]
    assert {item.market for item in rows} == {"SH", "SZ", "BJ"}
    assert all(item.industry is None for item in rows if item.market == "SH")
    assert all(item.source == "AKShare" for item in rows if item.market == "SH")
    status = next(item for item in statuses if item.name == "baostock" and item.kind == "stock_industry")
    assert status.failure_count == 1
    assert status.last_error == "industry endpoint unavailable"
    assert industry_cooling is True
    assert stock_cooling is False
    assert not any(item.name == "baostock" and item.kind == "stock" for item in statuses)


def test_stock_pool_does_not_bulk_enrich_isolated_metadata_gaps() -> None:
    primary_rows = _stock_rows(sh_count=99)

    class PrimaryProvider:
        source_name = "AKShare"

        async def stock_pool(self):
            return primary_rows

    class UnexpectedIndustryProvider:
        source_name = "BaoStock"

        async def stock_industries(self):
            raise AssertionError("小样本缺失不应触发全量行业接口")

    async def run_check(path: Path):
        settings = Settings(stock_pool_authoritative_min_count=3)
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"primary": PrimaryProvider(), "industry": UnexpectedIndustryProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "primary"), (2, "industry")],
        )
        return await coordinator.stock_pool(limit=None, refresh=True)

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.symbol for item in rows] == [item.symbol for item in primary_rows]
