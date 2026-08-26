"""Compatibility facade for the repository-safe execution-session contract."""

from app.models.market_scan_execution_session import (
    MARKET_SCAN_EXECUTION_SESSION_CONTRACT_VERSION,
    MARKET_SCAN_EXECUTION_SESSION_SCHEMA_VERSION,
    MarketScanExecutionSessionEvidence,
    build_market_scan_execution_session_evidence,
    market_scan_execution_session_content_digest,
    verify_market_scan_execution_session_evidence,
)


__all__ = [
    "MARKET_SCAN_EXECUTION_SESSION_CONTRACT_VERSION",
    "MARKET_SCAN_EXECUTION_SESSION_SCHEMA_VERSION",
    "MarketScanExecutionSessionEvidence",
    "build_market_scan_execution_session_evidence",
    "market_scan_execution_session_content_digest",
    "verify_market_scan_execution_session_evidence",
]
