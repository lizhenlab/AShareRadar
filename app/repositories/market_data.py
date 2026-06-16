from __future__ import annotations

from app.db.mappers import (
    row_to_kline,
    row_to_minute_kline,
    row_to_plate_item,
    row_to_quote,
    row_to_stock_concept_item,
    row_to_stock_info,
)
from app.models.schemas import Kline, MinuteKline, PlateItem, Quote, StockConceptItem, StockInfo
from app.repositories.base import SQLiteRepository
from app.utils.symbols import standard_symbol
from app.utils.time import now_text, seconds_ago_text


class MarketDataRepository(SQLiteRepository):
    def save_quotes(self, quotes: list[Quote]) -> None:
        if not quotes:
            return
        fetched_at = now_text()
        rows = []
        history_rows = []
        for quote in quotes:
            symbol = standard_symbol(f"{quote.market}{quote.code}")
            trade_date = _quote_trade_date(quote.timestamp, fetched_at)
            rows.append(
                (
                    symbol,
                    quote.code,
                    quote.market,
                    quote.name,
                    quote.price,
                    quote.prev_close,
                    quote.open,
                    quote.high,
                    quote.low,
                    quote.volume,
                    quote.amount,
                    quote.change,
                    quote.change_pct,
                    quote.turnover_rate,
                    quote.pe,
                    quote.pb,
                    quote.market_cap,
                    quote.timestamp,
                    quote.source,
                    fetched_at,
                )
            )
            history_rows.append(
                (
                    symbol,
                    quote.code,
                    quote.market,
                    quote.name,
                    quote.price,
                    quote.change_pct,
                    quote.pe,
                    quote.pb,
                    quote.market_cap,
                    quote.source,
                    quote.timestamp,
                    trade_date,
                    fetched_at,
                )
            )
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO quote_snapshot (
                    symbol, code, market, name, price, prev_close, open, high, low,
                    volume, amount, change, change_pct, turnover_rate, pe, pb,
                    market_cap, quote_timestamp, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    code=excluded.code,
                    market=excluded.market,
                    name=excluded.name,
                    price=excluded.price,
                    prev_close=excluded.prev_close,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    change=excluded.change,
                    change_pct=excluded.change_pct,
                    turnover_rate=excluded.turnover_rate,
                    pe=excluded.pe,
                    pb=excluded.pb,
                    market_cap=excluded.market_cap,
                    quote_timestamp=excluded.quote_timestamp,
                    source=excluded.source,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
            conn.executemany(
                """
                INSERT INTO quote_history (
                    symbol, code, market, name, price, change_pct, pe, pb, market_cap, source,
                    quote_timestamp, trade_date, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                history_rows,
            )

    def get_quotes(self, symbols: list[str], max_age_seconds: int) -> list[Quote]:
        if not symbols:
            return []
        normalized = [standard_symbol(symbol) for symbol in symbols]
        cutoff = self._cutoff(max_age_seconds)
        placeholders = ",".join("?" for _ in normalized)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM quote_snapshot
                WHERE symbol IN ({placeholders}) AND fetched_at >= ?
                """,
                [*normalized, cutoff],
            ).fetchall()
        by_symbol = {row["symbol"]: row_to_quote(row) for row in rows}
        return [by_symbol[symbol] for symbol in normalized if symbol in by_symbol]

    def quote_history(self, symbol: str, limit: int = 120) -> list[dict[str, float | str | None]]:
        normalized = standard_symbol(symbol)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
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
                SELECT q.price, q.change_pct, q.pe, q.pb, q.market_cap, q.quote_timestamp, q.trade_date, q.fetched_at
                FROM latest_ids
                JOIN quote_history AS q ON q.id = latest_ids.id
                """,
                (normalized, limit, normalized),
            ).fetchall()
        daily_rows = [
            {
                "price": row["price"],
                "change_pct": row["change_pct"],
                "pe": row["pe"],
                "pb": row["pb"],
                "market_cap": row["market_cap"],
                "quote_timestamp": row["quote_timestamp"],
                "trade_date": row["trade_date"],
                "fetched_at": row["fetched_at"],
            }
            for row in rows
        ]
        return sorted(daily_rows, key=lambda row: str(row.get("trade_date") or row.get("quote_timestamp") or row.get("fetched_at") or ""))

    def save_klines(self, symbol: str, klines: list[Kline], source: str) -> None:
        if not klines:
            return
        normalized = standard_symbol(symbol)
        fetched_at = now_text()
        rows = [
            (
                normalized,
                item.date,
                item.open,
                item.close,
                item.high,
                item.low,
                item.volume,
                source,
                fetched_at,
            )
            for item in klines
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO kline_daily (
                    symbol, date, open, close, high, low, volume, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                    open=excluded.open,
                    close=excluded.close,
                    high=excluded.high,
                    low=excluded.low,
                    volume=excluded.volume,
                    source=excluded.source,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )

    def get_klines(self, symbol: str, limit: int, max_age_seconds: int) -> list[Kline]:
        normalized = standard_symbol(symbol)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM kline_daily
                    WHERE symbol = ? AND fetched_at >= ?
                    ORDER BY date DESC
                    LIMIT ?
                )
                ORDER BY date ASC
                """,
                (normalized, self._cutoff(max_age_seconds), limit),
            ).fetchall()
        return [row_to_kline(row) for row in rows]

    def save_minute_klines(self, symbol: str, interval: str, rows: list[MinuteKline], source: str) -> None:
        if not rows:
            return
        normalized = standard_symbol(symbol)
        fetched_at = now_text()
        payload = [
            (
                normalized,
                interval,
                item.timestamp,
                item.open,
                item.close,
                item.high,
                item.low,
                item.volume,
                item.amount,
                item.turnover_rate,
                source,
                fetched_at,
            )
            for item in rows
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO kline_minute (
                    symbol, interval, timestamp, open, close, high, low, volume,
                    amount, turnover_rate, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, timestamp) DO UPDATE SET
                    open=excluded.open,
                    close=excluded.close,
                    high=excluded.high,
                    low=excluded.low,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    turnover_rate=excluded.turnover_rate,
                    source=excluded.source,
                    fetched_at=excluded.fetched_at
                """,
                payload,
            )

    def get_minute_klines(self, symbol: str, interval: str, limit: int, max_age_seconds: int) -> list[MinuteKline]:
        normalized = standard_symbol(symbol)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM kline_minute
                    WHERE symbol = ? AND interval = ? AND fetched_at >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
                ORDER BY timestamp ASC
                """,
                (normalized, interval, self._cutoff(max_age_seconds), limit),
            ).fetchall()
        return [row_to_minute_kline(row) for row in rows]

    def save_stock_pool(self, rows: list[StockInfo]) -> None:
        if not rows:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO stock_master (
                    symbol, code, market, name, industry, list_date, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    code=excluded.code,
                    market=excluded.market,
                    name=excluded.name,
                    industry=excluded.industry,
                    list_date=excluded.list_date,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        item.symbol,
                        item.code,
                        item.market,
                        item.name,
                        item.industry,
                        item.list_date,
                        item.source,
                        item.updated_at,
                    )
                    for item in rows
                ],
            )

    def get_stock_pool(
        self,
        max_age_seconds: int,
        limit: int = 5000,
        keyword: str | None = None,
    ) -> list[StockInfo]:
        params: list[object] = [self._cutoff(max_age_seconds)]
        where = "updated_at >= ?"
        if keyword:
            like = f"%{keyword.strip()}%"
            where += " AND (code LIKE ? OR name LIKE ? OR symbol LIKE ?)"
            params.extend([like, like, like])
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM stock_master
                WHERE {where}
                ORDER BY market, code
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [row_to_stock_info(row) for row in rows]

    def stock_pool_count(self, max_age_seconds: int | None = None) -> int:
        with self._lock, self._connect() as conn:
            if max_age_seconds is None:
                return int(conn.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0])
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM stock_master WHERE updated_at >= ?",
                    (self._cutoff(max_age_seconds),),
                ).fetchone()[0]
            )

    def save_plate_rank(self, rows: list[PlateItem]) -> None:
        if not rows:
            return
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM plate_rank")
            conn.executemany(
                """
                INSERT INTO plate_rank (
                    rank, name, change_pct, amount, turnover_rate, leading_stock,
                    leading_stock_change_pct, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.rank,
                        item.name,
                        item.change_pct,
                        item.amount,
                        item.turnover_rate,
                        item.leading_stock,
                        item.leading_stock_change_pct,
                        item.source,
                        item.updated_at,
                    )
                    for item in rows
                ],
            )

    def get_plate_rank(self, max_age_seconds: int, limit: int = 20) -> list[PlateItem]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM plate_rank
                WHERE updated_at >= ?
                ORDER BY rank ASC
                LIMIT ?
                """,
                (self._cutoff(max_age_seconds), limit),
            ).fetchall()
        return [row_to_plate_item(row) for row in rows]

    def save_stock_concepts(self, symbol: str, rows: list[StockConceptItem]) -> None:
        normalized = standard_symbol(symbol)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM stock_concept WHERE symbol = ?", (normalized,))
            if not rows:
                return
            conn.executemany(
                """
                INSERT INTO stock_concept (
                    symbol, rank, name, change_pct, amount, turnover_rate,
                    leading_stock, leading_stock_change_pct, match_reason, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, name) DO UPDATE SET
                    rank=excluded.rank,
                    change_pct=excluded.change_pct,
                    amount=excluded.amount,
                    turnover_rate=excluded.turnover_rate,
                    leading_stock=excluded.leading_stock,
                    leading_stock_change_pct=excluded.leading_stock_change_pct,
                    match_reason=excluded.match_reason,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        normalized,
                        item.rank,
                        item.name,
                        item.change_pct,
                        item.amount,
                        item.turnover_rate,
                        item.leading_stock,
                        item.leading_stock_change_pct,
                        item.match_reason,
                        item.source,
                        item.updated_at,
                    )
                    for item in rows
                ],
            )

    def get_stock_concepts(self, symbol: str, max_age_seconds: int, limit: int = 8) -> list[StockConceptItem]:
        normalized = standard_symbol(symbol)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM stock_concept
                WHERE symbol = ? AND updated_at >= ?
                ORDER BY change_pct DESC, rank ASC
                LIMIT ?
                """,
                (normalized, self._cutoff(max_age_seconds), limit),
            ).fetchall()
        return [row_to_stock_concept_item(row) for row in rows]

    @staticmethod
    def _cutoff(max_age_seconds: int) -> str:
        return seconds_ago_text(max_age_seconds)


def _quote_trade_date(quote_timestamp: str | None, fetched_at: str) -> str:
    value = str(quote_timestamp or fetched_at or "").strip()
    return (value or fetched_at)[:10]
