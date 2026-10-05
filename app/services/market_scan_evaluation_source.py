"""Read-only storage and frozen result evidence shared by scan research readers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from app.repositories.market_scan_mapping import decode_result_payload
from app.services.market_scan_probability_source import is_registered_production_score_contract
from app.services.market_scan_score_contract import stable_score_spec_hash
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence


def portable_database_label(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


@contextmanager
def readonly_evaluation_connection(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def frozen_result_raw_score(result: sqlite3.Row) -> float:
    if result["raw_score"] is not None:
        return float(result["raw_score"])
    if result["score"] is not None:
        return float(result["score"])
    return -float(result["rank"])


def frozen_score_contract(result: sqlite3.Row) -> tuple[str, str] | None:
    if "metrics_json" not in result.keys():
        return None
    _metrics, details = decode_result_payload(result["metrics_json"])
    spec, digest = details.get("score_spec"), details.get("score_spec_hash")
    if not isinstance(spec, Mapping) or not isinstance(digest, str):
        return None
    rule = spec.get("rule_version")
    if not isinstance(rule, str) or not is_registered_production_score_contract(rule, digest):
        return None
    return (rule, digest) if stable_score_spec_hash(spec) == digest else None


def frozen_source_evidence_digest(result: sqlite3.Row) -> str | None:
    if "metrics_json" not in result.keys():
        return None
    _metrics, details = decode_result_payload(result["metrics_json"])
    components = details.get("components")
    dimensions = components.get("score_dimensions") if isinstance(components, dict) else None
    evidence = dimensions.get("point_in_time_evidence") if isinstance(dimensions, dict) else None
    if not isinstance(evidence, dict) or not verify_market_scan_point_in_time_evidence(evidence):
        return None
    digest = evidence.get("payload_digest") if isinstance(evidence, dict) else None
    return digest if isinstance(digest, str) and len(digest) == 64 else None


__all__ = [
    "frozen_result_raw_score", "frozen_score_contract", "frozen_source_evidence_digest",
    "portable_database_label", "readonly_evaluation_connection",
]
