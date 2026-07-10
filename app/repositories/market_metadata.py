from __future__ import annotations

from collections.abc import Iterable
from math import isfinite

from app.db.market_mappers import row_to_plate_item, row_to_stock_concept_item, row_to_stock_info
from app.models.schemas import PlateItem, StockConceptItem, StockInfo
from app.utils.symbols import standard_symbol


def _column_names(columns: Iterable[str]) -> str:
    return ", ".join(columns)


def _placeholders(columns: Iterable[str]) -> str:
    return ", ".join("?" for _column in columns)


def _update_assignments(columns: Iterable[str]) -> str:
    return ", ".join(f"{column}=excluded.{column}" for column in columns)


def _upsert_sql(table: str, columns: tuple[str, ...], conflict_columns: tuple[str, ...]) -> str:
    update_columns = tuple(column for column in columns if column not in conflict_columns)
    return (
        f"INSERT INTO {table} ({_column_names(columns)}) "
        f"VALUES ({_placeholders(columns)}) "
        f"ON CONFLICT({_column_names(conflict_columns)}) DO UPDATE SET "
        + _update_assignments(update_columns)
    )


STOCK_MASTER_COLUMNS = (
    "symbol",
    "code",
    "market",
    "name",
    "industry",
    "list_date",
    "source",
    "updated_at",
)
PLATE_RANK_COLUMNS = (
    "rank",
    "name",
    "change_pct",
    "amount",
    "turnover_rate",
    "leading_stock",
    "leading_stock_change_pct",
    "source",
    "updated_at",
)
STOCK_CONCEPT_COLUMNS = (
    "symbol",
    "rank",
    "name",
    "change_pct",
    "amount",
    "turnover_rate",
    "leading_stock",
    "leading_stock_change_pct",
    "match_reason",
    "source",
    "updated_at",
)

_STOCK_MASTER_SELECT_COLUMNS = _column_names(STOCK_MASTER_COLUMNS)
_PLATE_RANK_SELECT_COLUMNS = _column_names(PLATE_RANK_COLUMNS)
_STOCK_CONCEPT_SELECT_COLUMNS = _column_names(STOCK_CONCEPT_COLUMNS)

_STOCK_MASTER_UPSERT_SQL = _upsert_sql("stock_master", STOCK_MASTER_COLUMNS, ("symbol",))
_PLATE_RANK_INSERT_SQL = (
    f"INSERT INTO plate_rank ({_column_names(PLATE_RANK_COLUMNS)}) "
    f"VALUES ({_placeholders(PLATE_RANK_COLUMNS)})"
)
_STOCK_CONCEPT_UPSERT_SQL = _upsert_sql("stock_concept", STOCK_CONCEPT_COLUMNS, ("symbol", "name"))


class MarketMetadataRepositoryMixin:
    def save_stock_pool(self, rows: list[StockInfo]) -> None:
        if not rows:
            return
        payload = _stock_pool_rows(rows)
        if not payload:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(_STOCK_MASTER_UPSERT_SQL, payload)

    def get_stock_pool(
        self,
        max_age_seconds: int,
        limit: int = 5000,
        keyword: str | None = None,
    ) -> list[StockInfo]:
        if limit <= 0:
            return []
        window = self._time_window(max_age_seconds)
        if window is None:
            return []
        params: list[object] = [*window]
        where = "updated_at BETWEEN ? AND ?"
        keyword_text = _required_text(keyword)
        if keyword_text:
            like = f"%{keyword_text}%"
            where += " AND (code LIKE ? OR name LIKE ? OR symbol LIKE ?)"
            params.extend([like, like, like])
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_STOCK_MASTER_SELECT_COLUMNS} FROM stock_master
                WHERE {where}
                ORDER BY market ASC, code ASC, symbol ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [row_to_stock_info(row) for row in rows]

    def stock_pool_count(self, max_age_seconds: int | None = None) -> int:
        with self._lock, self._connect() as conn:
            if max_age_seconds is None:
                return int(conn.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0])
            window = self._time_window(max_age_seconds)
            if window is None:
                return 0
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM stock_master WHERE updated_at BETWEEN ? AND ?",
                    window,
                ).fetchone()[0]
            )

    def save_plate_rank(self, rows: list[PlateItem]) -> None:
        if not rows:
            return
        payload = _plate_rank_rows(rows)
        if not payload:
            return
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM plate_rank")
            conn.executemany(_PLATE_RANK_INSERT_SQL, payload)

    def get_plate_rank(self, max_age_seconds: int, limit: int = 20) -> list[PlateItem]:
        if limit <= 0:
            return []
        window = self._time_window(max_age_seconds)
        if window is None:
            return []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_PLATE_RANK_SELECT_COLUMNS} FROM plate_rank
                WHERE updated_at BETWEEN ? AND ?
                ORDER BY rank ASC, id ASC
                LIMIT ?
                """,
                (*window, limit),
            ).fetchall()
        return [row_to_plate_item(row) for row in rows]

    def save_stock_concepts(self, symbol: str, rows: list[StockConceptItem]) -> None:
        normalized = standard_symbol(symbol)
        payload = _stock_concept_rows(normalized, rows or [])
        if not rows or not payload:
            return
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM stock_concept WHERE symbol = ?", (normalized,))
            conn.executemany(_STOCK_CONCEPT_UPSERT_SQL, payload)

    def get_stock_concepts(self, symbol: str, max_age_seconds: int, limit: int = 8) -> list[StockConceptItem]:
        if limit <= 0:
            return []
        normalized = standard_symbol(symbol)
        window = self._time_window(max_age_seconds)
        if window is None:
            return []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_STOCK_CONCEPT_SELECT_COLUMNS} FROM stock_concept
                WHERE symbol = ? AND updated_at BETWEEN ? AND ?
                ORDER BY change_pct DESC, rank ASC, name ASC
                LIMIT ?
                """,
                (normalized, *window, limit),
            ).fetchall()
        return [row_to_stock_concept_item(row) for row in rows]


def _stock_pool_rows(rows: Iterable[StockInfo]) -> list[tuple[object, ...]]:
    payload: list[tuple[object, ...]] = []
    for item in rows:
        row = _stock_pool_row(item)
        if row is not None:
            payload.append(row)
    return payload


def _stock_pool_row(item: StockInfo) -> tuple[object, ...] | None:
    symbol = _required_text(item.symbol)
    code = _required_text(item.code)
    market = _required_text(item.market)
    name = _required_text(item.name)
    source = _required_text(item.source)
    updated_at = _required_text(item.updated_at)
    if not all((symbol, code, market, name, source, updated_at)):
        return None
    return (
        symbol,
        code,
        market,
        name,
        _optional_text(item.industry),
        _optional_text(item.list_date),
        source,
        updated_at,
    )


def _plate_rank_rows(rows: Iterable[PlateItem]) -> list[tuple[object, ...]]:
    payload: list[tuple[object, ...]] = []
    for item in rows:
        row = _plate_rank_row(item)
        if row is not None:
            payload.append(row)
    return payload


def _plate_rank_row(item: PlateItem) -> tuple[object, ...] | None:
    rank = _positive_rank(item.rank)
    name = _required_text(item.name)
    change_pct = _finite_float(item.change_pct)
    source = _required_text(item.source)
    updated_at = _required_text(item.updated_at)
    if rank is None or change_pct is None or not all((name, source, updated_at)):
        return None
    return (
        rank,
        name,
        change_pct,
        _optional_non_negative_float(item.amount),
        _optional_non_negative_float(item.turnover_rate),
        _optional_text(item.leading_stock),
        _optional_finite_float(item.leading_stock_change_pct),
        source,
        updated_at,
    )


def _stock_concept_rows(symbol: str, rows: Iterable[StockConceptItem]) -> list[tuple[object, ...]]:
    payload: list[tuple[object, ...]] = []
    seen_names: set[str] = set()
    for item in rows:
        row = _stock_concept_row(symbol, item)
        if row is None:
            continue
        name = str(row[2])
        if name in seen_names:
            continue
        seen_names.add(name)
        payload.append(row)
    return payload


def _stock_concept_row(symbol: str, item: StockConceptItem) -> tuple[object, ...] | None:
    rank = _positive_rank(item.rank)
    name = _required_text(item.name)
    change_pct = _finite_float(item.change_pct)
    source = _required_text(item.source)
    updated_at = _required_text(item.updated_at)
    if rank is None or change_pct is None or not all((name, source, updated_at)):
        return None
    return (
        symbol,
        rank,
        name,
        change_pct,
        _optional_non_negative_float(item.amount),
        _optional_non_negative_float(item.turnover_rate),
        _optional_text(item.leading_stock),
        _optional_finite_float(item.leading_stock_change_pct),
        _required_text(item.match_reason) or "概念成分匹配",
        source,
        updated_at,
    )


def _required_text(value: str | None) -> str:
    return str(value or "").strip()


def _optional_text(value: str | None) -> str | None:
    text = _required_text(value)
    return text or None


def _positive_rank(value: int) -> int | None:
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None


def _finite_float(value: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _optional_finite_float(value: float | None) -> float | None:
    if value is None:
        return None
    return _finite_float(value)


def _optional_non_negative_float(value: float | None) -> float | None:
    number = _optional_finite_float(value)
    return number if number is not None and number >= 0 else None


__all__ = ["MarketMetadataRepositoryMixin"]
