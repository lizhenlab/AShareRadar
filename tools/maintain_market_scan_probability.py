from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.market_mappers import row_to_kline  # noqa: E402
from app.services.market_scan_probability_maintenance import maintain_market_scan_probability  # noqa: E402


class _ReadOnlyKlineCache:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def get_klines_by_dates_many(self, symbols, dates, adjustment_mode="qfq"):
        values = tuple(dict.fromkeys(str(value) for value in dates))
        output = {str(symbol): [] for symbol in symbols}
        if not values:
            return output
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        placeholders = ",".join("?" for _value in values)
        sql = (
            "SELECT symbol,adjustment_mode,date,open,close,high,low,volume,as_of,"
            "data_version,contract_version,fallback_used,source,fetched_at FROM kline_daily "
            f"WHERE symbol=? AND adjustment_mode=? AND date IN ({placeholders}) ORDER BY date"
        )
        try:
            for symbol in output:
                output[symbol] = [
                    row_to_kline(row)
                    for row in connection.execute(sql, (symbol, adjustment_mode, *values)).fetchall()
                ]
        finally:
            connection.close()
        return output


def main() -> int:
    parser = argparse.ArgumentParser(description="只读SQLite、固定交易日维护上涨概率 outcome")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--outcome-dir", type=Path, required=True)
    parser.add_argument("--as-of-date", required=True)
    args = parser.parse_args()
    summary = maintain_market_scan_probability(
        _ReadOnlyKlineCache(args.database),
        as_of_date=args.as_of_date,
        source_directory=args.source_dir,
        outcome_directory=args.outcome_dir,
    )
    print(json.dumps(summary.__dict__, ensure_ascii=False, sort_keys=True))
    return 1 if summary.failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
