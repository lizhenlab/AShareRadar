from __future__ import annotations

from app.models.schemas import CacheStats
from app.repositories.base import SQLiteRepository


class CacheStatsRepository(SQLiteRepository):
    def stats(self) -> CacheStats:
        with self._lock, self._connect() as conn:
            quote_count = conn.execute("SELECT COUNT(*) FROM quote_snapshot").fetchone()[0]
            quote_history_count = conn.execute("SELECT COUNT(*) FROM quote_history").fetchone()[0]
            daily_kline_count = conn.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0]
            minute_kline_count = conn.execute("SELECT COUNT(*) FROM kline_minute").fetchone()[0]
            stock_count = conn.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0]
            plate_count = conn.execute("SELECT COUNT(*) FROM plate_rank").fetchone()[0]
            concept_count = conn.execute("SELECT COUNT(*) FROM stock_concept").fetchone()[0]
            provider_count = conn.execute("SELECT COUNT(*) FROM provider_status").fetchone()[0]
            latest_quote_at = conn.execute("SELECT MAX(fetched_at) FROM quote_snapshot").fetchone()[0]
            latest_daily_kline_at = conn.execute("SELECT MAX(fetched_at) FROM kline_daily").fetchone()[0]
            latest_minute_kline_at = conn.execute("SELECT MAX(fetched_at) FROM kline_minute").fetchone()[0]
            latest_stock_at = conn.execute("SELECT MAX(updated_at) FROM stock_master").fetchone()[0]
            latest_plate_rank_at = conn.execute("SELECT MAX(updated_at) FROM plate_rank").fetchone()[0]
            latest_concept_at = conn.execute("SELECT MAX(updated_at) FROM stock_concept").fetchone()[0]
            latest_plate_at = max([item for item in [latest_plate_rank_at, latest_concept_at] if item] or [None])
        return CacheStats(
            path=str(self._path),
            quote_count=quote_count,
            quote_history_count=quote_history_count,
            kline_count=daily_kline_count + minute_kline_count,
            daily_kline_count=daily_kline_count,
            minute_kline_count=minute_kline_count,
            stock_count=stock_count,
            plate_count=plate_count + concept_count,
            provider_count=provider_count,
            latest_quote_at=latest_quote_at,
            latest_kline_at=latest_daily_kline_at,
            latest_daily_kline_at=latest_daily_kline_at,
            latest_minute_kline_at=latest_minute_kline_at,
            latest_stock_at=latest_stock_at,
            latest_plate_at=latest_plate_at,
        )
