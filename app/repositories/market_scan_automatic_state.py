from __future__ import annotations

from pathlib import Path
import sqlite3

from app.models.market_scan import MarketScanAutomaticState


def market_scan_automatic_state_from_row(
    row: sqlite3.Row,
    *,
    database_path: Path,
    schema_version: int,
) -> MarketScanAutomaticState:
    """Map the scheduler-only header hint without granting snapshot authority."""
    stat = database_path.stat()
    return MarketScanAutomaticState(
        run_id=int(row["id"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        trigger=str(row["trigger"]),  # type: ignore[arg-type]
        data_date=str(row["data_date"]),
        scope=str(row["scope"]),
        mode=str(row["mode"]),  # type: ignore[arg-type]
        rule_version=str(row["rule_version"]),
        updated_at=str(row["updated_at"]),
        snapshot_digest=(
            str(row["snapshot_digest"])
            if row["snapshot_digest"] is not None
            else None
        ),
        snapshot_seal_origin=(
            str(row["snapshot_seal_origin"])  # type: ignore[arg-type]
            if row["snapshot_seal_origin"] is not None
            else None
        ),
        snapshot_sealed_at=(
            str(row["snapshot_sealed_at"])
            if row["snapshot_sealed_at"] is not None
            else None
        ),
        finished_at=(
            str(row["finished_at"])
            if row["finished_at"] is not None
            else None
        ),
        database_identity=f"{stat.st_dev}:{stat.st_ino}:{schema_version}",
    )


__all__ = ["market_scan_automatic_state_from_row"]
