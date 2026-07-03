from __future__ import annotations

from collections.abc import Iterable

from app.db.market_mappers import row_to_quote
from app.models.schemas import Quote
from app.utils.market_data import (
    QUOTE_OPTIONAL_FINITE_FIELDS,
    QUOTE_REQUIRED_FINITE_FIELDS,
    filter_valid_quotes,
    finite_float,
    valid_quote,
)
from app.utils.symbols import standard_symbol
from app.utils.time import now_text


def _column_names(columns: tuple[str, ...]) -> str:
    return ", ".join(columns)


def _placeholders(columns: tuple[str, ...]) -> str:
    return ", ".join("?" for _column in columns)


def _update_assignments(columns: Iterable[str]) -> str:
    return ", ".join(f"{column}=excluded.{column}" for column in columns)


QUOTE_SNAPSHOT_COLUMNS = (
    "symbol",
    "code",
    "market",
    "name",
    "price",
    "prev_close",
    "open",
    "high",
    "low",
    "volume",
    "amount",
    "change",
    "change_pct",
    "turnover_rate",
    "pe",
    "pb",
    "market_cap",
    "quote_timestamp",
    "source",
    "fetched_at",
)
QUOTE_HISTORY_COLUMNS = (
    "symbol",
    "code",
    "market",
    "name",
    "price",
    "change_pct",
    "pe",
    "pb",
    "market_cap",
    "source",
    "quote_timestamp",
    "trade_date",
    "fetched_at",
)
QUOTE_HISTORY_RESULT_COLUMNS = (
    "price",
    "change_pct",
    "pe",
    "pb",
    "market_cap",
    "quote_timestamp",
    "trade_date",
    "fetched_at",
)

_QUOTE_SNAPSHOT_SQL = (
    f"INSERT INTO quote_snapshot ({_column_names(QUOTE_SNAPSHOT_COLUMNS)}) "
    f"VALUES ({_placeholders(QUOTE_SNAPSHOT_COLUMNS)}) "
    "ON CONFLICT(symbol) DO UPDATE SET "
    + _update_assignments(column for column in QUOTE_SNAPSHOT_COLUMNS if column != "symbol")
)
_QUOTE_HISTORY_SQL = f"INSERT INTO quote_history ({_column_names(QUOTE_HISTORY_COLUMNS)}) VALUES ({_placeholders(QUOTE_HISTORY_COLUMNS)})"


class MarketQuoteRepositoryMixin:
    def save_quotes(self, quotes: list[Quote]) -> None:
        valid_quotes = filter_valid_quotes(quotes)
        if not valid_quotes:
            return
        fetched_at = now_text()
        snapshot_rows, history_rows = _quote_persistence_rows(valid_quotes, fetched_at)
        with self._lock, self._connect() as conn:
            conn.executemany(_QUOTE_SNAPSHOT_SQL, snapshot_rows)
            conn.executemany(_QUOTE_HISTORY_SQL, history_rows)

    def get_quotes(self, symbols: list[str], max_age_seconds: int) -> list[Quote]:
        if not symbols:
            return []
        normalized = [standard_symbol(symbol) for symbol in symbols]
        window = self._time_window(max_age_seconds)
        if window is None:
            return []
        placeholders = ",".join("?" for _ in normalized)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM quote_snapshot
                WHERE symbol IN ({placeholders}) AND fetched_at BETWEEN ? AND ?
                """,
                [*normalized, *window],
            ).fetchall()
        by_symbol = _valid_quotes_by_symbol(rows)
        return [by_symbol[symbol] for symbol in normalized if symbol in by_symbol]

    def quote_history(self, symbol: str, limit: int = 120) -> list[dict[str, float | str | None]]:
        if limit <= 0:
            return []
        normalized = standard_symbol(symbol)
        with self._lock, self._connect() as conn:
            rows = _latest_quote_history_rows(conn, normalized, limit)
        return _sorted_quote_history_rows([_quote_history_result(row) for row in rows if _valid_quote_history_row(row)])


def _latest_quote_history_rows(conn, symbol: str, limit: int):
    return conn.execute(
        f"""
        WITH recent_dates AS (
            SELECT trade_date
            FROM quote_history
            WHERE symbol = ? AND trade_date IS NOT NULL AND trade_date <> ''
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT ?
        ),
        latest_ids AS (
            SELECT
                recent_dates.trade_date,
                (
                    SELECT id
                    FROM quote_history AS q
                    WHERE q.symbol = ? AND q.trade_date = recent_dates.trade_date
                    ORDER BY q.fetched_at DESC, q.id DESC
                    LIMIT 1
                ) AS id
            FROM recent_dates
        )
        SELECT {_qualified_column_names("q", QUOTE_HISTORY_RESULT_COLUMNS)}
        FROM latest_ids
        JOIN quote_history AS q ON q.id = latest_ids.id
        """,
        (symbol, limit, symbol),
    ).fetchall()


def _quote_history_result(row) -> dict[str, float | str | None]:
    return {column: row[column] for column in QUOTE_HISTORY_RESULT_COLUMNS}


def _valid_quotes_by_symbol(rows) -> dict[str, Quote]:
    by_symbol = {}
    for row in rows:
        if not _valid_quote_snapshot_row(row):
            continue
        quote = row_to_quote(row)
        if valid_quote(quote):
            by_symbol[row["symbol"]] = quote
    return by_symbol


def _valid_quote_snapshot_row(row) -> bool:
    return _required_finite_columns(row, QUOTE_REQUIRED_FINITE_FIELDS) and _optional_finite_columns(
        row, QUOTE_OPTIONAL_FINITE_FIELDS
    )


def _valid_quote_history_row(row) -> bool:
    price = finite_float(row["price"])
    return (
        price is not None
        and price > 0
        and _required_finite_columns(row, ("change_pct",))
        and _optional_finite_columns(row, ("pe", "pb", "market_cap"))
    )


def _required_finite_columns(row, columns: Iterable[str]) -> bool:
    return all(finite_float(row[column]) is not None for column in columns)


def _optional_finite_columns(row, columns: Iterable[str]) -> bool:
    return all(row[column] is None or finite_float(row[column]) is not None for column in columns)


def _sorted_quote_history_rows(rows: list[dict[str, float | str | None]]) -> list[dict[str, float | str | None]]:
    return sorted(rows, key=_quote_history_sort_key)


def _quote_history_sort_key(row: dict[str, float | str | None]) -> str:
    return str(row.get("trade_date") or row.get("quote_timestamp") or row.get("fetched_at") or "")


def _qualified_column_names(prefix: str, columns: tuple[str, ...]) -> str:
    return ", ".join(f"{prefix}.{column}" for column in columns)


def _quote_trade_date(quote_timestamp: str | None, fetched_at: str) -> str:
    value = str(quote_timestamp or fetched_at or "").strip()
    return (value or fetched_at)[:10].replace("/", "-")


def _quote_persistence_rows(quotes: list[Quote], fetched_at: str) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    snapshot_rows = []
    history_rows = []
    for quote in quotes:
        symbol = standard_symbol(f"{quote.market}{quote.code}")
        snapshot_rows.append(_quote_snapshot_row(symbol, quote, fetched_at))
        history_rows.append(_quote_history_row(symbol, quote, fetched_at))
    return snapshot_rows, history_rows


def _quote_snapshot_row(symbol: str, quote: Quote, fetched_at: str) -> tuple[object, ...]:
    return _row_from_columns(_quote_values(symbol, quote, fetched_at), QUOTE_SNAPSHOT_COLUMNS)


def _quote_history_row(symbol: str, quote: Quote, fetched_at: str) -> tuple[object, ...]:
    return _row_from_columns(_quote_values(symbol, quote, fetched_at), QUOTE_HISTORY_COLUMNS)


def _quote_values(symbol: str, quote: Quote, fetched_at: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "code": quote.code,
        "market": quote.market,
        "name": quote.name,
        "price": quote.price,
        "prev_close": quote.prev_close,
        "open": quote.open,
        "high": quote.high,
        "low": quote.low,
        "volume": quote.volume,
        "amount": quote.amount,
        "change": quote.change,
        "change_pct": quote.change_pct,
        "turnover_rate": quote.turnover_rate,
        "pe": quote.pe,
        "pb": quote.pb,
        "market_cap": quote.market_cap,
        "quote_timestamp": quote.timestamp,
        "source": quote.source,
        "trade_date": _quote_trade_date(quote.timestamp, fetched_at),
        "fetched_at": fetched_at,
    }


def _row_from_columns(values: dict[str, object], columns: tuple[str, ...]) -> tuple[object, ...]:
    return tuple(values[column] for column in columns)


__all__ = ["MarketQuoteRepositoryMixin", "_quote_trade_date"]
