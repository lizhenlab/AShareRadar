from __future__ import annotations

from collections.abc import Mapping
import math
import sqlite3
from typing import Literal

from app.models.market_scan import (
    MarketScanCoverage,
    MarketScanPublicationSummary,
    MarketScanProductionScoreContract,
    MarketScanScoreDistributionObservation,
    MarketScanStaleCluster,
)
from app.repositories.market_scan_publication_summary import (
    publication_summary_from_evidence,
)


_PRODUCTION_SCORE_CONTRACT_SQL = """
WITH score_contracts AS (
    SELECT
        CASE WHEN json_type(
            metrics_json,
            '$.score_details.score_spec.rule_version'
        ) = 'text' THEN json_extract(
            metrics_json,
            '$.score_details.score_spec.rule_version'
        ) END AS score_rule_version,
        CASE WHEN json_type(
            metrics_json,
            '$.score_details.score_spec_hash'
        ) = 'text' THEN json_extract(
            metrics_json,
            '$.score_details.score_spec_hash'
        ) END AS score_spec_hash
    FROM market_scan_result
    WHERE run_id = ? AND status = 'success'
)
SELECT
    COUNT(*) AS success_count,
    COUNT(score_rule_version) AS rule_count,
    COUNT(score_spec_hash) AS hash_count,
    COUNT(DISTINCT score_rule_version) AS distinct_rule_count,
    COUNT(DISTINCT score_spec_hash) AS distinct_hash_count,
    MIN(score_rule_version) AS score_rule_version,
    MIN(score_spec_hash) AS score_spec_hash
FROM score_contracts
"""

_SUCCESS_SCORE_OBSERVATIONS_SQL = """
SELECT
    symbol,
    raw_score,
    score,
    leader_score,
    trend_score,
    data_quality_score,
    json_extract(
        metrics_json,
        '$.score_details.components.final_score.base'
    ) AS base_score,
    COALESCE(
        json_extract(
            metrics_json,
            '$.score_details.components.continuous_trend.score'
        ),
        json_extract(
            metrics_json,
            '$.score_details.components.rank_refinement.score'
        )
    ) AS rank_refinement_score
FROM market_scan_result
WHERE run_id = ? AND status = 'success'
ORDER BY symbol ASC
"""


def read_production_score_contract(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    expected_count: int,
) -> MarketScanProductionScoreContract | None:
    row = conn.execute(_PRODUCTION_SCORE_CONTRACT_SQL, (run_id,)).fetchone()
    return _production_score_contract(row, expected_count=expected_count)


def read_success_score_observations(
    conn: sqlite3.Connection,
    run_id: int,
) -> tuple[MarketScanScoreDistributionObservation, ...]:
    rows = conn.execute(_SUCCESS_SCORE_OBSERVATIONS_SQL, (run_id,)).fetchall()
    return tuple(
        _score_observation(
            symbol=str(row["symbol"]),
            base_score=_optional_distribution_score(row["base_score"]),
            integer_score=_optional_distribution_integer(row["score"]),
            raw_score=_optional_distribution_score(row["raw_score"]),
            leader_score=_optional_distribution_score(row["leader_score"]),
            trend_score=_optional_distribution_score(row["trend_score"]),
            data_quality_score=_optional_distribution_score(row["data_quality_score"]),
            rank_refinement_score=_optional_distribution_unit_interval(
                row["rank_refinement_score"]
            ),
        )
        for row in rows
    )


def score_observation_from_canonical_result(
    row: Mapping[str, object],
) -> MarketScanScoreDistributionObservation | None:
    """Project one strict-decoded snapshot row like SQLite ``json_extract``."""

    if row.get("status") != "success":
        return None
    metrics = row.get("metrics_json")
    base_score = _sqlite_json_scalar(
        _nested_json_value(
            metrics,
            "score_details",
            "components",
            "final_score",
            "base",
        )
    )
    continuous_score = _sqlite_json_scalar(
        _nested_json_value(
            metrics,
            "score_details",
            "components",
            "continuous_trend",
            "score",
        )
    )
    legacy_score = _sqlite_json_scalar(
        _nested_json_value(
            metrics,
            "score_details",
            "components",
            "rank_refinement",
            "score",
        )
    )
    return _score_observation(
        symbol=str(row["symbol"]),
        base_score=_optional_distribution_score(base_score),
        integer_score=_optional_distribution_integer(row.get("score")),
        raw_score=_optional_distribution_score(row.get("raw_score")),
        leader_score=_optional_distribution_score(row.get("leader_score")),
        trend_score=_optional_distribution_score(row.get("trend_score")),
        data_quality_score=_optional_distribution_score(
            row.get("data_quality_score")
        ),
        rank_refinement_score=_optional_distribution_unit_interval(
            continuous_score if continuous_score is not None else legacy_score
        ),
    )


def _score_observation(
    *,
    symbol: str,
    base_score: float | None,
    integer_score: int | None,
    raw_score: float | None,
    leader_score: float | None,
    trend_score: float | None,
    data_quality_score: float | None,
    rank_refinement_score: float | None,
) -> MarketScanScoreDistributionObservation:
    return MarketScanScoreDistributionObservation(
        symbol=symbol,
        base_score=base_score,
        integer_score=integer_score,
        raw_score=raw_score,
        leader_score=leader_score,
        trend_score=trend_score,
        data_quality_score=data_quality_score,
        rank_refinement_score=rank_refinement_score,
    )


def _nested_json_value(value: object, *path: str) -> object:
    current = value
    for field in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(field)
    return current


def _sqlite_json_scalar(value: object) -> object:
    return int(value) if isinstance(value, bool) else value


def _production_score_contract(
    row: sqlite3.Row | None,
    *,
    expected_count: int,
) -> MarketScanProductionScoreContract | None:
    if row is None or expected_count <= 0 or int(row["success_count"] or 0) != expected_count:
        return None
    covered = all(
        int(row[field] or 0) == expected_count
        for field in ("rule_count", "hash_count")
    )
    unique = all(
        int(row[field] or 0) == 1
        for field in ("distinct_rule_count", "distinct_hash_count")
    )
    if not covered or not unique:
        return None
    try:
        return MarketScanProductionScoreContract(
            production_score_rule_version=row["score_rule_version"],
            production_score_spec_hash=row["score_spec_hash"],
            success_count=expected_count,
        )
    except (TypeError, ValueError):
        return None


def read_publication_summary(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
) -> MarketScanPublicationSummary:
    run_id = int(run["id"])
    coverage_rows = conn.execute(
        """
        SELECT market,
               SUM(CASE WHEN status IN ('success', 'missing') THEN 1 ELSE 0 END)
                   AS total_count,
               SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
               SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END) AS missing_count,
               SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count
        FROM market_scan_result
        WHERE run_id = ?
        GROUP BY market
        """,
        (run_id,),
    ).fetchall()
    stale_rows = conn.execute(
        """
        SELECT data_date, COUNT(*) AS stale_count,
               GROUP_CONCAT(DISTINCT market) AS markets
        FROM market_scan_result
        WHERE run_id = ?
          AND status IN ('missing', 'skipped')
          AND data_date IS NOT NULL
          AND data_date < ?
        GROUP BY data_date
        ORDER BY stale_count DESC, data_date DESC
        """,
        (run_id, run["data_date"]),
    ).fetchall()
    timestamp_rows = conn.execute(
        """
        SELECT market, quote_timestamp, quote_observed_at
        FROM market_scan_result
        WHERE run_id = ? AND status != 'pending'
        ORDER BY market ASC, symbol ASC
        """,
        (run_id,),
    ).fetchall()
    return publication_summary_from_evidence(
        run,
        _coverage_summary(coverage_rows),
        _systemic_stale_cluster(
            stale_rows,
            total_count=int(run["total_count"] or 0),
        ),
        _timestamp_evidence(timestamp_rows),
    )


def _timestamp_evidence(
    rows: list[sqlite3.Row],
) -> list[tuple[str, str | None, str | None]]:
    return [
        (
            str(row["market"]),
            str(row["quote_timestamp"]) if row["quote_timestamp"] is not None else None,
            str(row["quote_observed_at"])
            if row["quote_observed_at"] is not None
            else None,
        )
        for row in rows
    ]


def _coverage_summary(rows: list[sqlite3.Row]) -> tuple[MarketScanCoverage, ...]:
    by_market = {
        market: MarketScanCoverage(
            market,
            int(row["total_count"] or 0),
            int(row["success_count"] or 0),
            int(row["missing_count"] or 0),
            int(row["skipped_count"] or 0),
        )
        for row in rows
        if (market := _coverage_market(row["market"])) is not None
    }
    market_names: tuple[Literal["SH", "SZ", "BJ"], ...] = ("SH", "SZ", "BJ")
    markets = tuple(
        by_market.get(market, MarketScanCoverage(market, 0, 0))
        for market in market_names
    )
    overall = MarketScanCoverage(
        "ALL",
        sum(item.total_count for item in markets),
        sum(item.success_count for item in markets),
        sum(item.missing_count for item in markets),
        sum(item.skipped_count for item in markets),
    )
    return (overall, *markets)


def _coverage_market(value: object) -> Literal["SH", "SZ", "BJ"] | None:
    market = str(value)
    if market == "SH":
        return "SH"
    if market == "SZ":
        return "SZ"
    if market == "BJ":
        return "BJ"
    return None


def _systemic_stale_cluster(
    rows: list[sqlite3.Row],
    *,
    total_count: int,
) -> MarketScanStaleCluster | None:
    if not rows or total_count <= 0:
        return None
    row = rows[0]
    count = int(row["stale_count"] or 0)
    if count < max(3, math.ceil(total_count * 0.05)):
        return None
    markets = tuple(
        sorted(part for part in str(row["markets"] or "").split(",") if part)
    )
    return MarketScanStaleCluster(
        data_date=str(row["data_date"]),
        count=count,
        markets=markets,
        total_count=total_count,
    )


def _optional_distribution_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and 0 <= parsed <= 100 else None


def _optional_distribution_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 100 else None


def _optional_distribution_unit_interval(value: object) -> float | None:
    parsed = _optional_distribution_score(value)
    return parsed if parsed is not None and parsed <= 1 else None


__all__ = [
    "read_production_score_contract",
    "read_publication_summary",
    "read_success_score_observations",
    "score_observation_from_canonical_result",
]
