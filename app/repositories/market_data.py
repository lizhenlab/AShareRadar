from __future__ import annotations

from app.repositories.base import SQLiteRepository
from app.repositories.market_klines import MarketKlineRepositoryMixin
from app.repositories.market_metadata import MarketMetadataRepositoryMixin
from app.repositories.market_quotes import MarketQuoteRepositoryMixin, _quote_trade_date
from app.utils.time import now_text, seconds_ago_text


class MarketDataRepository(
    MarketQuoteRepositoryMixin,
    MarketKlineRepositoryMixin,
    MarketMetadataRepositoryMixin,
    SQLiteRepository,
):
    @staticmethod
    def _cutoff(max_age_seconds: int) -> str | None:
        if max_age_seconds <= 0:
            return None
        return seconds_ago_text(max_age_seconds)

    @staticmethod
    def _time_window(max_age_seconds: int) -> tuple[str, str] | None:
        cutoff = MarketDataRepository._cutoff(max_age_seconds)
        if cutoff is None:
            return None
        return cutoff, now_text()


__all__ = ["MarketDataRepository", "_quote_trade_date"]
