from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import json
import re
from typing import Final

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from app.models.market_scan import (
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanSort,
    MarketScanSortOrder,
)
from app.utils.time import datetime_to_text


XLSX_MEDIA_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PUBLISHED_MARKET_SCAN_STATUSES: Final = frozenset({"success", "degraded"})
_DANGEROUS_FORMULA_PREFIXES: Final = frozenset({"=", "+", "-", "@"})
_ILLEGAL_CELL_CHARACTERS: Final = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_MAX_CELL_TEXT_LENGTH: Final = 4_096
_HEADER_FILL: Final = PatternFill(fill_type="solid", fgColor="1F4E78")
_HEADER_FONT: Final = Font(color="FFFFFF", bold=True)
_STATUS_LABELS: Final = {
    "pending": "待处理",
    "success": "有效排名",
    "missing": "数据缺失",
    "skipped": "已跳过",
}
_MODE_LABELS: Final = {"official": "盘后正式", "intraday": "盘中临时"}
_SORT_LABELS: Final = {
    "rank": "短线强势排名",
    "score": "短线强势分",
    "raw_score": "原始得分",
    "trend_score": "趋势分",
    "change_pct": "涨跌幅",
    "amount": "成交额",
    "turnover_rate": "换手率",
    "data_quality_score": "数据质量",
    "symbol": "股票代码",
}
_RESULT_COLUMNS: Final = (
    ("排名", 9), ("股票代码", 12), ("交易所代码", 15), ("股票名称", 18),
    ("市场", 9), ("行业", 18), ("上市日期", 13), ("ST", 8), ("新股", 8),
    ("结果状态", 12), ("短线强势分", 14), ("原始得分", 14), ("趋势分", 11),
    ("龙头分", 11), ("最新价", 12), ("涨跌幅(%)", 13), ("换手率(%)", 13),
    ("量比", 10), ("成交额(元)", 18), ("数据质量", 12), ("标签", 28),
    ("说明", 36), ("错误", 36), ("行情日期", 13), ("行情时间", 22),
    ("行情来源", 15), ("日K来源", 15), ("复权方式", 12), ("降级原因", 28),
    ("更新时间", 22),
)
_RESULT_LAST_COLUMN: Final = get_column_letter(len(_RESULT_COLUMNS))
_DETAIL_COLUMNS: Final = (
    ("批次 ID", 10), ("股票代码", 12), ("交易所代码", 15), ("最终分", 10),
    ("原始排名分", 15), ("趋势分", 10), ("龙头分", 10), ("数据质量", 12),
    ("龙头基础分", 14), ("趋势增减分", 14), ("规则增减分", 42), ("质量扣分", 12),
    ("扣分前基础分", 16), ("精排综合值", 14), ("精排组成", 48), ("精排扣分", 12),
    ("Tie-break 规则", 34), ("Tie-break 值", 34), ("规则版本", 34), ("规则哈希", 66),
)
_DETAIL_LAST_COLUMN: Final = get_column_letter(len(_DETAIL_COLUMNS))


@dataclass(frozen=True)
class MarketScanExportFilters:
    status: MarketScanResultStatus | None = "success"
    market: str | Sequence[str] | None = None
    industry: str | Sequence[str] | None = None
    is_st: bool | None = None
    is_new: bool | None = None
    min_score: int | None = None
    max_score: int | None = None
    min_trend_score: int | None = None
    max_trend_score: int | None = None
    min_change_pct: float | None = None
    max_change_pct: float | None = None
    min_turnover_rate: float | None = None
    max_turnover_rate: float | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    min_data_quality_score: int | None = None
    max_data_quality_score: int | None = None
    keyword: str | None = None
    sort: MarketScanSort | Sequence[MarketScanSort] = "rank"
    order: MarketScanSortOrder | Sequence[MarketScanSortOrder] = "asc"

    def normalized(self) -> "MarketScanExportFilters":
        return MarketScanExportFilters(
            status=self.status,
            market=_normalized_filter_values(self.market, maximum=3),
            industry=_normalized_filter_values(self.industry, maximum=20),
            is_st=self.is_st,
            is_new=self.is_new,
            min_score=self.min_score,
            max_score=self.max_score,
            min_trend_score=self.min_trend_score,
            max_trend_score=self.max_trend_score,
            min_change_pct=self.min_change_pct,
            max_change_pct=self.max_change_pct,
            min_turnover_rate=self.min_turnover_rate,
            max_turnover_rate=self.max_turnover_rate,
            min_amount=self.min_amount,
            max_amount=self.max_amount,
            min_data_quality_score=self.min_data_quality_score,
            max_data_quality_score=self.max_data_quality_score,
            keyword=_normalized_filter_text(self.keyword),
            sort=_normalized_sort_values(self.sort, maximum=3),
            order=_normalized_order_values(self.order, maximum=3),
        )


@dataclass(frozen=True)
class MarketScanWorkbookExport:
    content: bytes
    filename: str
    row_count: int


def build_market_scan_workbook(
    page: MarketScanResultPage,
    filters: MarketScanExportFilters,
    *,
    exported_at: datetime,
) -> MarketScanWorkbookExport:
    filters = filters.normalized()
    if len(page.items) != page.total:
        raise ValueError("Excel 导出结果读取不完整，请稍后重试")
    workbook = Workbook()
    results_sheet = workbook.active
    if results_sheet is None:
        raise RuntimeError("Excel 工作簿初始化失败")
    results_sheet.title = "榜单"
    _populate_results_sheet(results_sheet, page.items)
    _populate_score_details_sheet(workbook.create_sheet("评分明细"), page.items)
    _populate_info_sheet(workbook.create_sheet("导出信息"), page, filters, exported_at)
    stream = BytesIO()
    workbook.save(stream)
    return MarketScanWorkbookExport(
        content=stream.getvalue(),
        filename=_export_filename(page, exported_at),
        row_count=page.total,
    )


def _populate_results_sheet(sheet, items: list[MarketScanResultItem]) -> None:
    sheet.append([header for header, _width in _RESULT_COLUMNS])
    for item in items:
        sheet.append(_result_row(item))
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{_RESULT_LAST_COLUMN}{max(1, len(items) + 1)}"
    sheet.row_dimensions[1].height = 24
    for cell in sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for index, (_header, width) in enumerate(_RESULT_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    _format_result_columns(sheet, len(items))
    if items:
        table = Table(displayName="MarketScanResults", ref=f"A1:{_RESULT_LAST_COLUMN}{len(items) + 1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        sheet.add_table(table)


def _result_row(item: MarketScanResultItem) -> list[object]:
    return [
        item.rank, _safe_text(item.code.zfill(6)), _safe_text(item.symbol), _safe_text(item.name),
        _safe_text(item.market), _safe_text(item.industry), _safe_text(item.list_date),
        "是" if item.is_st else "否", "是" if item.is_new else "否", _STATUS_LABELS[item.status],
        item.score, item.raw_score, item.trend_score, item.leader_score, item.price,
        item.change_pct, item.turnover_rate, item.volume_ratio, item.amount,
        item.data_quality_score, _safe_text("、".join(item.tags)), _safe_text(item.reason),
        _safe_text(item.error), _safe_text(item.data_date), _safe_text(item.quote_timestamp),
        _safe_text(item.quote_source), _safe_text(item.kline_source), _safe_text(item.adjustment_mode),
        _safe_text("、".join(item.degradation_reasons)), _safe_text(item.updated_at),
    ]


def _format_result_columns(sheet, item_count: int) -> None:
    for row in range(2, item_count + 2):
        sheet.cell(row, 2).number_format = "@"
        for column in (11, 13, 14, 20):
            sheet.cell(row, column).number_format = "0"
        sheet.cell(row, 12).number_format = "0.000000"
        for column in (15, 16, 17, 18):
            sheet.cell(row, column).number_format = "0.00"
        sheet.cell(row, 19).number_format = "#,##0.00"
        for column in (21, 22, 23, 29):
            sheet.cell(row, column).alignment = Alignment(wrap_text=True, vertical="top")


def _populate_score_details_sheet(sheet, items: list[MarketScanResultItem]) -> None:
    sheet.append([header for header, _width in _DETAIL_COLUMNS])
    for item in items:
        sheet.append(_score_detail_row(item))
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{_DETAIL_LAST_COLUMN}{max(1, len(items) + 1)}"
    sheet.row_dimensions[1].height = 24
    for cell in sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for index, (_header, width) in enumerate(_DETAIL_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    _format_score_detail_columns(sheet, len(items))
    if items:
        table = Table(displayName="MarketScanScoreDetails", ref=f"A1:{_DETAIL_LAST_COLUMN}{len(items) + 1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        sheet.add_table(table)


def _score_detail_row(item: MarketScanResultItem) -> list[object]:
    details = _mapping(item.score_details)
    components = _mapping(details.get("components"))
    leader = _mapping(components.get("leader_score"))
    final_score = _mapping(components.get("final_score"))
    refinement = _mapping(components.get("rank_refinement"))
    ranking = _mapping(details.get("ranking"))
    return [
        item.run_id, _safe_text(item.code.zfill(6)), _safe_text(item.symbol), item.score,
        item.raw_score, item.trend_score, item.leader_score, item.data_quality_score,
        _finite_number(leader.get("base")), _finite_number(leader.get("trend_delta")),
        _safe_json(leader.get("rule_deltas")), _finite_number(final_score.get("quality_penalty")),
        _finite_number(final_score.get("base")), _finite_number(refinement.get("score")),
        _safe_json(refinement.get("weighted_terms")), _finite_number(final_score.get("rank_discount")),
        _safe_json(ranking.get("tie_break")), _safe_json(ranking.get("tie_break_values")),
        _safe_text(details.get("run_rule_version")), _safe_text(details.get("score_spec_hash")),
    ]


def _format_score_detail_columns(sheet, item_count: int) -> None:
    for row in range(2, item_count + 2):
        sheet.cell(row, 2).number_format = "@"
        for column in (4, 6, 7, 8):
            sheet.cell(row, column).number_format = "0"
        for column in (5, 9, 10, 12, 13, 14, 16):
            sheet.cell(row, column).number_format = "0.000000"
        for column in (11, 15, 17, 18, 19, 20):
            sheet.cell(row, column).alignment = Alignment(wrap_text=True, vertical="top")


def _populate_info_sheet(sheet, page: MarketScanResultPage, filters: MarketScanExportFilters, exported_at: datetime) -> None:
    run = page.run
    rows = (
        ("项目", "AShareRadar"), ("导出类型", "全市场A股榜单"), ("批次 ID", run.id),
        ("榜单类型", _MODE_LABELS[run.mode]), ("批次状态", run.status), ("触发方式", run.trigger),
        ("批次基准时间", run.as_of), ("行情日期", run.quote_date),
        ("日K截止日", run.data_date), ("扫描完成时间", _text_or(run.finished_at, "--")), ("规则版本", run.rule_version),
        ("股票池范围", run.scope), ("股票池来源", _text_or(run.stock_pool_source, "--")),
        ("有效覆盖率", f"{run.coverage_pct:.2f}%"),
        ("筛选状态", _status_filter_label(filters.status)),
        ("筛选市场", _filter_values_label(filters.market, "全部市场")),
        ("筛选行业", _filter_values_label(filters.industry, "不限")),
        ("ST 条件", _boolean_filter_label(filters.is_st, "仅 ST", "排除 ST")),
        ("新股条件", _boolean_filter_label(filters.is_new, "仅新股", "排除新股")),
        ("强势分范围", _range_filter_label(filters.min_score, filters.max_score)),
        ("趋势分范围", _range_filter_label(filters.min_trend_score, filters.max_trend_score)),
        ("涨跌幅范围", _range_filter_label(filters.min_change_pct, filters.max_change_pct)),
        ("换手率范围", _range_filter_label(filters.min_turnover_rate, filters.max_turnover_rate)),
        ("成交额范围", _range_filter_label(filters.min_amount, filters.max_amount)),
        ("数据质量范围", _range_filter_label(filters.min_data_quality_score, filters.max_data_quality_score)),
        ("搜索关键词", _text_or(filters.keyword, "无")),
        ("排序", _sort_filter_label(filters.sort, filters.order)),
        ("导出条数", page.total), ("导出时间", datetime_to_text(exported_at)),
        ("数据说明", "仅导出已持久化榜单快照，不会重新获取行情或重新计算。"),
    )
    sheet.append(["字段", "内容"])
    for key, value in rows:
        sheet.append([key, _safe_text(value) if isinstance(value, str) else value])
    sheet.freeze_panes = "A2"
    sheet.column_dimensions["A"].width = 20
    sheet.column_dimensions["B"].width = 72
    for cell in sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    for row in sheet.iter_rows(min_row=2, min_col=2, max_col=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
    for cell in sheet["A"]:
        cell.font = Font(bold=True)


def _safe_text(value: object | None) -> str:
    text = _ILLEGAL_CELL_CHARACTERS.sub("", str("" if value is None else value))
    if len(text) > _MAX_CELL_TEXT_LENGTH:
        text = f"{text[:_MAX_CELL_TEXT_LENGTH - 3]}..."
    if text.lstrip()[:1] in _DANGEROUS_FORMULA_PREFIXES:
        return f"'{text}"
    return text


def _safe_json(value: object) -> str:
    if value is None:
        return ""
    try:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return _safe_text(rendered)


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _finite_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _normalized_filter_text(value: str | None) -> str | None:
    normalized = " ".join((value or "").split()).strip()
    return normalized or None


def _normalized_filter_values(value: str | Sequence[str] | None, *, maximum: int) -> tuple[str, ...]:
    candidates = [value] if isinstance(value, str) else list(value or ())
    normalized = tuple(dict.fromkeys(" ".join(str(item).split()).strip() for item in candidates))
    return tuple(item for item in normalized if item)[:maximum]


def _normalized_sort_values(
    value: MarketScanSort | Sequence[MarketScanSort],
    *,
    maximum: int,
) -> tuple[MarketScanSort, ...]:
    return (value,) if isinstance(value, str) else tuple(value)[:maximum]


def _normalized_order_values(
    value: MarketScanSortOrder | Sequence[MarketScanSortOrder],
    *,
    maximum: int,
) -> tuple[MarketScanSortOrder, ...]:
    return (value,) if isinstance(value, str) else tuple(value)[:maximum]


def _boolean_filter_label(value: bool | None, true_label: str, false_label: str) -> str:
    if value is None:
        return "不限"
    return true_label if value else false_label


def _status_filter_label(value: MarketScanResultStatus | None) -> str:
    return "全部状态" if value is None else _STATUS_LABELS[value]


def _quality_filter_label(value: int | None) -> int | str:
    return value if value is not None else "不限"


def _filter_values_label(value: str | Sequence[str] | None, fallback: str) -> str:
    normalized = _normalized_filter_values(value, maximum=20)
    return "、".join(normalized) if normalized else fallback


def _range_filter_label(minimum: int | float | None, maximum: int | float | None) -> str:
    if minimum is None and maximum is None:
        return "不限"
    return f"{minimum if minimum is not None else '--'} ～ {maximum if maximum is not None else '--'}"


def _sort_filter_label(
    sort: MarketScanSort | Sequence[MarketScanSort],
    order: MarketScanSortOrder | Sequence[MarketScanSortOrder],
) -> str:
    sorts = (sort,) if isinstance(sort, str) else tuple(sort)
    orders = (order,) if isinstance(order, str) else tuple(order)
    return " → ".join(
        f"{_SORT_LABELS[field]}（{_sort_order_label(direction)}）"
        for field, direction in zip(sorts, orders, strict=False)
    )


def _sort_order_label(value: MarketScanSortOrder) -> str:
    return "升序" if value == "asc" else "降序"


def _text_or(value: str | None, fallback: str) -> str:
    return value if value else fallback


def _export_filename(page: MarketScanResultPage, exported_at: datetime) -> str:
    date = page.run.quote_date or page.run.data_date or exported_at.date().isoformat()
    return f"AShareRadar-market-scan-{date}-{page.run.mode}-run-{page.run.id}.xlsx"


__all__ = [
    "PUBLISHED_MARKET_SCAN_STATUSES",
    "XLSX_MEDIA_TYPE",
    "MarketScanExportFilters",
    "MarketScanWorkbookExport",
    "build_market_scan_workbook",
]
