from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
import re
import unicodedata

from app.models.market import (
    StockInfo,
)
from app.utils.symbols import is_a_share_stock_code, standard_symbol


MISSING_STOCK_METADATA_TEXT = frozenset(
    {"", "-", "--", "—", "<na>", "n/a", "na", "nan", "nat", "none", "null", "不详", "不适用", "暂无", "未知", "未分类"}
)
STOCK_POOL_MIN_INDUSTRY_COVERAGE = 0.80
STOCK_POOL_MIN_LIST_DATE_COVERAGE = 0.95
STOCK_INDUSTRY_CLASSIFICATION_PREFIX = re.compile(r"^[A-Z](?:\d{1,3})?(?=[\u3400-\u9fff])", re.IGNORECASE)


@dataclass(frozen=True)
class StockPoolMetadataCoverage:
    scope: str
    total_count: int
    industry_count: int
    list_date_count: int

    @property
    def industry_ratio(self) -> float:
        return self.industry_count / self.total_count if self.total_count else 0.0

    @property
    def list_date_ratio(self) -> float:
        return self.list_date_count / self.total_count if self.total_count else 0.0


@dataclass(frozen=True)
class StockPoolMetadataDiagnostic:
    overall: StockPoolMetadataCoverage
    markets: tuple[StockPoolMetadataCoverage, ...]
    issues: tuple[str, ...]

    @property
    def degraded(self) -> bool:
        return bool(self.issues)

    def summary(self) -> str:
        return "；".join(self.issues) if self.issues else "股票池元数据完整"


def normalize_stock_pool_rows(rows: Iterable[StockInfo]) -> list[StockInfo]:
    normalized: list[StockInfo] = []
    seen_symbols: set[str] = set()
    for item in rows:
        row = normalize_stock_pool_row(item)
        if row is None or row.symbol in seen_symbols:
            continue
        seen_symbols.add(row.symbol)
        normalized.append(row)
    return normalized


def normalize_stock_pool_row(item: StockInfo) -> StockInfo | None:
    raw_symbol = _required_text(item.symbol)
    code = _required_text(item.code)
    market = _required_text(item.market).upper()
    name = _required_text(item.name)
    source = _required_text(item.source)
    updated_at = _required_text(item.updated_at)
    if not all((raw_symbol, code, market, name, source, updated_at)):
        return None
    try:
        symbol = standard_symbol(raw_symbol)
    except (AttributeError, TypeError, ValueError):
        return None
    if symbol != f"{code}.{market}" or not is_a_share_stock_code(code, market):
        return None
    return item.model_copy(
        update={
            "symbol": symbol,
            "code": code,
            "market": market,
            "name": name,
            "industry": normalize_stock_industry_text(item.industry),
            "list_date": normalize_stock_metadata_text(item.list_date),
            "source": source,
            "updated_at": updated_at,
        }
    )


def _required_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def normalize_stock_metadata_text(value: object) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).replace("\u200b", "")
    text = "".join(text.split()).strip()
    return None if text.casefold() in MISSING_STOCK_METADATA_TEXT else text


def normalize_stock_industry_text(value: object) -> str | None:
    text = normalize_stock_metadata_text(value)
    if text is None:
        return None
    normalized = STOCK_INDUSTRY_CLASSIFICATION_PREFIX.sub("", text).strip()
    return normalize_stock_metadata_text(normalized)


def diagnose_stock_pool_metadata(
    rows: Iterable[StockInfo],
    *,
    min_industry_ratio: float = STOCK_POOL_MIN_INDUSTRY_COVERAGE,
    min_list_date_ratio: float = STOCK_POOL_MIN_LIST_DATE_COVERAGE,
) -> StockPoolMetadataDiagnostic:
    _validate_coverage_ratio(min_industry_ratio, "行业完整率")
    _validate_coverage_ratio(min_list_date_ratio, "上市日期完整率")
    normalized = normalize_stock_pool_rows(rows)
    overall = _metadata_coverage("ALL", normalized)
    by_market: dict[str, list[StockInfo]] = {}
    for row in normalized:
        by_market.setdefault(row.market, []).append(row)
    markets = tuple(_metadata_coverage(market, by_market[market]) for market in sorted(by_market))
    issues = _metadata_issues((overall, *markets), min_industry_ratio, min_list_date_ratio)
    return StockPoolMetadataDiagnostic(overall=overall, markets=markets, issues=issues)


def _metadata_coverage(scope: str, rows: list[StockInfo]) -> StockPoolMetadataCoverage:
    return StockPoolMetadataCoverage(
        scope=scope,
        total_count=len(rows),
        industry_count=sum(normalize_stock_metadata_text(row.industry) is not None for row in rows),
        list_date_count=sum(_is_valid_stock_list_date(row.list_date) for row in rows),
    )


def _metadata_issues(
    coverages: tuple[StockPoolMetadataCoverage, ...],
    min_industry_ratio: float,
    min_list_date_ratio: float,
) -> tuple[str, ...]:
    if not coverages[0].total_count:
        return ("股票池为空，无法诊断元数据完整性",)
    issues: list[str] = []
    for coverage in coverages:
        if coverage.industry_ratio < min_industry_ratio:
            issues.append(_coverage_issue(coverage, "行业", coverage.industry_count, coverage.industry_ratio, min_industry_ratio))
        if coverage.list_date_ratio < min_list_date_ratio:
            issues.append(_coverage_issue(coverage, "上市日期", coverage.list_date_count, coverage.list_date_ratio, min_list_date_ratio))
    return tuple(issues)


def _coverage_issue(coverage: StockPoolMetadataCoverage, field: str, count: int, ratio: float, minimum: float) -> str:
    return f"{coverage.scope} {field}完整率 {count}/{coverage.total_count} ({ratio:.2%})，低于 {minimum:.0%}"


def _is_valid_stock_list_date(value: object) -> bool:
    text = normalize_stock_metadata_text(value)
    if text is None:
        return False
    compact = text.replace("-", "").replace("/", "")
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError:
        return False
    return True


def _validate_coverage_ratio(value: float, label: str) -> None:
    if not 0 <= value <= 1:
        raise ValueError(f"{label}必须在 0 到 1 之间")


__all__ = [
    "MISSING_STOCK_METADATA_TEXT",
    "STOCK_POOL_MIN_INDUSTRY_COVERAGE",
    "STOCK_POOL_MIN_LIST_DATE_COVERAGE",
    "StockPoolMetadataCoverage",
    "StockPoolMetadataDiagnostic",
    "diagnose_stock_pool_metadata",
    "normalize_stock_industry_text",
    "normalize_stock_metadata_text",
    "normalize_stock_pool_row",
    "normalize_stock_pool_rows",
]
