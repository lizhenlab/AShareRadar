"""Non-authorizing scalar projection of a freshly verified published snapshot.

Personal research needs the original identity/rank/score, not a replay of every
production score explanation. The entire sealed snapshot is still rehashed in
the same read-only transaction on every request. Formal readers are unchanged.
"""

from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import path_has_only_trusted_aliases
from app.db.market_scan_integrity import require_publication_market_scan_snapshot
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE, MarketScanRun
from app.repositories.market_scan_mapping import run_from_row
from app.repositories.market_scan_results import required_run_row


class ExperimentalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)

    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str
    market: Literal["SH", "SZ", "BJ"]
    rank: int = Field(ge=1, le=10000)
    score: int = Field(ge=0, le=100)
    raw_score: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def validate_market(self) -> "ExperimentalCandidate":
        if not self.symbol.endswith(f".{self.market}"):
            raise ValueError("实验候选股票与市场不匹配")
        return self


def read_experimental_candidates(database: Path, run_id: int) -> tuple[MarketScanRun, list[ExperimentalCandidate]]:
    if not path_has_only_trusted_aliases(database):
        raise ValueError("实验榜单路径无效")
    connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        run = run_from_row(required_run_row(connection, run_id))
        if run.status not in {"success", "degraded"} or run.scope != MARKET_SCAN_FULL_MARKET_SCOPE:
            raise ValueError("个人实验仅支持已发布的全市场榜单")
        if not 0 <= run.success_count <= 10000:
            raise ValueError("实验榜单数量无效")
        require_publication_market_scan_snapshot(connection, run_id)
        rows = connection.execute(
            "SELECT symbol,name,market,rank,score,raw_score FROM market_scan_result "
            "WHERE run_id=? AND status='success' ORDER BY rank,symbol LIMIT 10001", (run_id,),
        ).fetchall()
        items = [ExperimentalCandidate.model_validate(dict(row)) for row in rows]
        if len(items) != run.success_count or len({item.symbol for item in items}) != len(items):
            raise ValueError("实验候选数量与已验证快照不一致")
        if [item.rank for item in items] != list(range(1, len(items) + 1)):
            raise ValueError("实验候选原始排名不完整")
        return run, items
    finally:
        connection.close()
