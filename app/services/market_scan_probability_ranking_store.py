"""Transactional immutable SQLite mirror for verified v6 ranking publications."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sqlite3

from app.services.market_scan_probability_ranking import (
    PROBABILITY_RANKING_PUBLICATION_SCHEMA_VERSION,
    PROBABILITY_RANKING_SCORE_RULE_VERSION,
    ProbabilityRankingError,
    VerifiedProbabilityRankingManualControl,
    VerifiedProbabilityRankingPublication,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE


class MarketScanProbabilityRankingStore:
    """Persist a replayed v6 artifact without modifying its v5 source rows."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().absolute()

    def publish(
        self,
        publication: VerifiedProbabilityRankingPublication,
        *,
        artifact_path: str | Path,
    ) -> None:
        if not isinstance(publication, VerifiedProbabilityRankingPublication):
            raise ProbabilityRankingError("ranking store requires verified publication token")
        path = self._publication_artifact_path(artifact_path)
        with self._connect() as conn:
            self._publish_locked(conn, publication, path)

    @staticmethod
    def _publication_artifact_path(artifact_path: str | Path) -> Path:
        path = Path(artifact_path).expanduser().absolute()
        if not path.name.endswith(".json.gz"):
            raise ProbabilityRankingError("ranking publication artifact path is invalid")
        return path

    def _publish_locked(
        self,
        conn: sqlite3.Connection,
        publication: VerifiedProbabilityRankingPublication,
        path: Path,
    ) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._verify_base_publication(conn, publication)
            existing = conn.execute(
                "SELECT * FROM market_scan_probability_ranking_publication WHERE run_id = ?",
                (publication.run_id,),
            ).fetchone()
            if existing is not None:
                self._verify_existing(conn, publication, path, existing)
            else:
                self._insert_publication_locked(conn, publication, path)
                self._verify_mirror_locked(conn, publication)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _insert_publication_locked(
        conn: sqlite3.Connection,
        publication: VerifiedProbabilityRankingPublication,
        path: Path,
    ) -> None:
        payload = publication.payload
        conn.execute(
            """
            INSERT INTO market_scan_probability_ranking_publication (
                run_id, schema_version, score_rule_version,
                score_spec_hash, base_snapshot_digest,
                prediction_artifact_digest, promotion_digest,
                artifact_digest, artifact_path, record_count, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                publication.run_id,
                PROBABILITY_RANKING_PUBLICATION_SCHEMA_VERSION,
                PROBABILITY_RANKING_SCORE_RULE_VERSION,
                publication.score_spec_hash,
                publication.base_snapshot_digest,
                payload["current_prediction_artifact_digest"],
                publication.promotion_digest,
                publication.artifact_digest,
                str(path),
                len(publication),
                publication.generated_at,
            ),
        )
        conn.executemany(
            """
            INSERT INTO market_scan_probability_ranking_result (
                run_id, symbol, rank, score, raw_score,
                base_rank, base_score, base_raw_score,
                probability, reference_base_rate,
                probability_adjustment, record_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(_row_values(publication.run_id, item) for item in publication),
        )

    def verify_mirror(
        self,
        publication: VerifiedProbabilityRankingPublication,
    ) -> bool:
        if not isinstance(publication, VerifiedProbabilityRankingPublication):
            return False
        try:
            with self._connect() as conn:
                self._verify_base_publication(conn, publication)
                self._verify_mirror_locked(conn, publication)
        except (ProbabilityRankingError, sqlite3.DatabaseError, OSError):
            return False
        return True

    def record_rollback(
        self,
        control: VerifiedProbabilityRankingManualControl,
    ) -> None:
        if (
            not isinstance(control, VerifiedProbabilityRankingManualControl)
            or control.action != "rollback"
        ):
            raise ProbabilityRankingError("ranking rollback store requires rollback token")
        payload = control.payload
        values = (
            control.integrity_digest,
            payload["promotion_digest"],
            payload["publication_artifact_digest"],
            control.effective_after_run_id,
            control.generated_at,
            payload["reason"],
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT control_digest, promotion_digest,
                           publication_artifact_digest, effective_after_run_id,
                           generated_at, reason
                    FROM market_scan_probability_ranking_rollback
                    WHERE control_digest = ?
                    """,
                    (control.integrity_digest,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO market_scan_probability_ranking_rollback (
                            control_digest, promotion_digest,
                            publication_artifact_digest, effective_after_run_id,
                            generated_at, reason
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                elif tuple(existing) != values:
                    raise ProbabilityRankingError(
                        "ranking rollback digest already binds different data"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def is_rolled_back(
        self,
        publication: VerifiedProbabilityRankingPublication,
    ) -> bool:
        if not isinstance(publication, VerifiedProbabilityRankingPublication):
            raise ProbabilityRankingError("ranking rollback lookup requires publication token")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM market_scan_probability_ranking_rollback
                WHERE promotion_digest = ?
                  AND (
                    publication_artifact_digest IS NULL
                    OR publication_artifact_digest = ?
                  )
                LIMIT 1
                """,
                (publication.promotion_digest, publication.artifact_digest),
            ).fetchone()
        return row is not None

    def _verify_existing(
        self,
        conn: sqlite3.Connection,
        publication: VerifiedProbabilityRankingPublication,
        path: Path,
        existing: sqlite3.Row,
    ) -> None:
        payload = publication.payload
        expected = {
            "run_id": publication.run_id,
            "schema_version": PROBABILITY_RANKING_PUBLICATION_SCHEMA_VERSION,
            "score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
            "score_spec_hash": publication.score_spec_hash,
            "base_snapshot_digest": publication.base_snapshot_digest,
            "prediction_artifact_digest": payload[
                "current_prediction_artifact_digest"
            ],
            "promotion_digest": publication.promotion_digest,
            "artifact_digest": publication.artifact_digest,
            "artifact_path": str(path),
            "record_count": len(publication),
            "generated_at": publication.generated_at,
        }
        if any(existing[name] != value for name, value in expected.items()):
            raise ProbabilityRankingError(
                "ranking run already has a different immutable v6 publication"
            )
        self._verify_mirror_locked(conn, publication)

    @staticmethod
    def _verify_base_publication(
        conn: sqlite3.Connection,
        publication: VerifiedProbabilityRankingPublication,
    ) -> None:
        run = conn.execute(
            """
            SELECT status, mode, scope, snapshot_digest
            FROM market_scan_run WHERE id = ?
            """,
            (publication.run_id,),
        ).fetchone()
        if (
            run is None
            or run["status"] not in {"success", "degraded"}
            or run["mode"] != "official"
            or run["scope"] != FULL_MARKET_SCOPE
            or run["snapshot_digest"] != publication.base_snapshot_digest
        ):
            raise ProbabilityRankingError(
                "v6 ranking database base publication identity is invalid"
            )
        base_rows = {
            str(row["symbol"]): row
            for row in conn.execute(
                """
                SELECT symbol, rank, score, raw_score
                FROM market_scan_result
                WHERE run_id = ? AND status = 'success'
                """,
                (publication.run_id,),
            )
        }
        records = publication.record_by_symbol()
        if set(base_rows) != set(records):
            raise ProbabilityRankingError("v6 ranking does not cover every v5 success row")
        for symbol, item in records.items():
            row = base_rows[symbol]
            if (
                row["rank"] != item["base_rank"]
                or row["score"] != item["base_score"]
                or not _same_float(row["raw_score"], item["base_raw_score"])
            ):
                raise ProbabilityRankingError(
                    "v6 ranking base row differs from immutable v5 publication"
                )

    @staticmethod
    def _verify_mirror_locked(
        conn: sqlite3.Connection,
        publication: VerifiedProbabilityRankingPublication,
    ) -> None:
        rows = conn.execute(
            """
            SELECT run_id, symbol, rank, score, raw_score,
                   base_rank, base_score, base_raw_score,
                   probability, reference_base_rate,
                   probability_adjustment, record_digest
            FROM market_scan_probability_ranking_result
            WHERE run_id = ? ORDER BY rank ASC
            """,
            (publication.run_id,),
        ).fetchall()
        expected = list(publication)
        if len(rows) != len(expected):
            raise ProbabilityRankingError("v6 ranking SQLite mirror is incomplete")
        for row, item in zip(rows, expected, strict=True):
            exact_names = (
                "run_id",
                "symbol",
                "rank",
                "score",
                "base_rank",
                "base_score",
                "record_digest",
            )
            float_names = (
                "raw_score",
                "base_raw_score",
                "probability",
                "reference_base_rate",
                "probability_adjustment",
            )
            if any(row[name] != item[name] for name in exact_names) or any(
                not _same_float(row[name], item[name]) for name in float_names
            ):
                raise ProbabilityRankingError("v6 ranking SQLite mirror differs")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn


def _row_values(run_id: int, item: Mapping[str, object]) -> tuple[object, ...]:
    return (
        run_id,
        item["symbol"],
        item["rank"],
        item["score"],
        item["raw_score"],
        item["base_rank"],
        item["base_score"],
        item["base_raw_score"],
        item["probability"],
        item["reference_base_rate"],
        item["probability_adjustment"],
        item["record_digest"],
    )


def _same_float(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        return False
    return abs(float(left) - float(right)) <= 1e-9


__all__ = ["MarketScanProbabilityRankingStore"]
