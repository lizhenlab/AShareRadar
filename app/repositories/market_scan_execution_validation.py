"""Repository-safe validation for immutable execution-quote evidence."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import sqlite3

from app.models.market_scan import MarketScanResultWrite
from app.models.market_scan_execution_quote import (
    MARKET_SCAN_EXECUTION_QUOTE_CONTRACT_VERSION,
    MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY,
    MARKET_SCAN_EXECUTION_QUOTE_SCHEMA_VERSION,
    verify_market_scan_execution_quote_evidence,
)
from app.utils.market_time import market_datetime_epoch


def required_run_score_contract(
    result: MarketScanResultWrite,
    run: sqlite3.Row,
    conn: sqlite3.Connection,
) -> sqlite3.Row:
    contract = conn.execute(
        """
        SELECT contract_json, production_score_rule_version, production_score_spec_hash
        FROM market_scan_rule_contract
        WHERE rule_version = ?
        """,
        (run["rule_version"],),
    ).fetchone()
    if contract is None:
        raise ValueError(f"生产扫描批次缺少封存的评分合同：{result.symbol}")
    return contract


def require_production_execution_quote_evidence(
    result: MarketScanResultWrite,
    run: sqlite3.Row,
    conn: sqlite3.Connection,
) -> None:
    contract = required_run_score_contract(result, run, conn)
    try:
        rule_payload = json.loads(str(contract["contract_json"]))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"生产扫描批次规则合同无法解析：{result.symbol}") from exc
    if not isinstance(rule_payload, Mapping):
        raise ValueError(f"生产扫描批次规则合同不是对象：{result.symbol}")
    declared = rule_payload.get("execution_quote_evidence")
    if declared is None:
        return
    expected_declaration = {
        "schema_version": MARKET_SCAN_EXECUTION_QUOTE_SCHEMA_VERSION,
        "contract_version": MARKET_SCAN_EXECUTION_QUOTE_CONTRACT_VERSION,
        "adjustment_mode": "none",
        "source_authority": "vendor_normalized_quote",
        "corporate_action_status": "unknown",
        "formal_pit_authority": False,
    }
    if declared != expected_declaration:
        raise ValueError(f"生产扫描批次执行行情证据合同不受支持：{result.symbol}")
    raw = result.score_details.get(MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY)
    if raw is None:
        if result.quote_timestamp is None and result.status == "missing":
            return
        raise ValueError(f"生产扫描结果缺少未复权执行行情证据：{result.symbol}")
    evidence = verify_market_scan_execution_quote_evidence(raw)
    _require_execution_quote_outer_binding(result, run, evidence)


def _require_execution_quote_outer_binding(
    result: MarketScanResultWrite,
    run: sqlite3.Row,
    evidence: Mapping[str, object],
) -> None:
    bar = evidence.get("bar")
    if not isinstance(bar, Mapping):  # pragma: no cover - strict verifier invariant
        raise ValueError(f"生产扫描执行行情证据缺少 bar：{result.symbol}")
    if (
        evidence.get("symbol") != result.symbol
        or evidence.get("mode") != run["mode"]
        or evidence.get("quote_date") != run["quote_date"]
        or evidence.get("source") != result.quote_source
        or evidence.get("fallback_used") is not result.quote_fallback_used
    ):
        raise ValueError(f"生产扫描执行行情证据与结果身份冲突：{result.symbol}")
    expected_times = (
        (evidence.get("provider_event_at"), result.quote_timestamp),
        (evidence.get("captured_at"), result.quote_observed_at),
    )
    if any(
        market_datetime_epoch(left) is None or market_datetime_epoch(right) is None or market_datetime_epoch(left) != market_datetime_epoch(right)
        for left, right in expected_times
    ):
        raise ValueError(f"生产扫描执行行情证据与结果时间边界冲突：{result.symbol}")
    numeric_bindings = (
        (result.price, bar.get("close")),
        (result.amount, bar.get("amount")),
    )
    if any(
        outer is not None
        and (isinstance(observed, bool) or not isinstance(observed, int | float) or not math.isclose(float(outer), float(observed), rel_tol=0, abs_tol=1e-9))
        for outer, observed in numeric_bindings
    ):
        raise ValueError(f"生产扫描执行行情证据与结果价格/成交额冲突：{result.symbol}")


__all__ = [
    "require_production_execution_quote_evidence",
    "required_run_score_contract",
]
