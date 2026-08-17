"""Immutable current-production rule-contract registration."""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3

from app.market_scan_repository_contracts import (
    is_current_market_scan_score_spec,
    stable_score_spec_hash,
)
from app.repositories.market_scan_action_gate_replay import (
    validate_current_rule_contract_policy,
)


def register_market_scan_rule_contract(
    conn: sqlite3.Connection,
    *,
    rule_version: str,
    contract: Mapping[str, object],
    stamp: str,
) -> None:
    expected_rule_version = f"full-market-scan-v6:{stable_score_spec_hash(contract)}"
    if rule_version != expected_rule_version:
        raise ValueError("扫描规则版本与待封存规则合同摘要不一致")
    score_spec = contract.get("score_spec")
    if not isinstance(score_spec, Mapping):
        raise ValueError("扫描规则合同缺少生产评分规范")
    score_spec_hash = stable_score_spec_hash(score_spec)
    if not is_current_market_scan_score_spec(score_spec, score_spec_hash):
        raise ValueError("新生产扫描规则合同必须使用当前可写 v5 评分规范")
    validate_current_rule_contract_policy(contract)
    score_rule_version = score_spec.get("rule_version")
    if not isinstance(score_rule_version, str):
        raise ValueError("扫描规则合同缺少生产评分规则版本")
    try:
        contract_json = json.dumps(
            contract,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("扫描规则合同不是有限、可序列化的 JSON") from exc
    conn.execute(
        """
        INSERT OR IGNORE INTO market_scan_rule_contract (
            rule_version, contract_json, production_score_rule_version,
            production_score_spec_hash, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            rule_version,
            contract_json,
            score_rule_version,
            score_spec_hash,
            stamp,
        ),
    )
    registered = conn.execute(
        """
        SELECT contract_json, production_score_rule_version,
               production_score_spec_hash
        FROM market_scan_rule_contract
        WHERE rule_version = ?
        """,
        (rule_version,),
    ).fetchone()
    if registered is None or tuple(registered) != (
        contract_json,
        score_rule_version,
        score_spec_hash,
    ):
        raise ValueError("扫描规则版本已绑定不同的不可变规则合同")


__all__ = ["register_market_scan_rule_contract"]
