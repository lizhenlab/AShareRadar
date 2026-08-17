"""Canonical repository replay for current market-scan action claims."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, fields
import json
import sqlite3

from app.market_scan_repository_contracts import (
    FULL_MARKET_SCORE_RULE_VERSION,
    MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS,
    MARKET_SCAN_PUBLISH_MIN_COVERAGE,
    MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO,
    snapshot_publication_diagnostics,
    stable_score_spec_hash,
)
from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MarketScanPublicationDiagnostic,
    MarketScanPublicationDiagnostics,
    MarketScanPublicationSummary,
    MarketScanScoreDistribution,
    MarketScanScoreDistributionObservation,
    MarketScanScoreDistributionPolicy,
)
from app.repositories.market_scan_score_diagnostics import (
    read_publication_summary,
    read_success_score_observations,
)


MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE = "publication.canonical_replay.v1"
MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_PREFIX = "market-scan-publication-replay-v1:"


def validate_current_action_gate_claim(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    diagnostics: MarketScanPublicationDiagnostics,
) -> MarketScanPublicationDiagnostic | None:
    """Recompute a current production action-gate claim before sealing."""
    _require_no_caller_receipt(diagnostics)
    return replay_current_action_gate_receipt(conn, run, diagnostics)


def replay_current_action_gate_receipt(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    diagnostics_without_receipt: MarketScanPublicationDiagnostics,
) -> MarketScanPublicationDiagnostic | None:
    """Return the exact receipt implied by persisted current evidence."""
    return _replay_current_action_gate_receipt(
        conn,
        run,
        diagnostics_without_receipt,
        score_observations=None,
    )


def replay_current_action_gate_receipt_from_verified_observations(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    diagnostics_without_receipt: MarketScanPublicationDiagnostics,
    score_observations: tuple[MarketScanScoreDistributionObservation, ...],
) -> MarketScanPublicationDiagnostic | None:
    """Internal fused path used only after the snapshot verifier collected rows."""
    return _replay_current_action_gate_receipt(
        conn,
        run,
        diagnostics_without_receipt,
        score_observations=score_observations,
    )


def _replay_current_action_gate_receipt(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    diagnostics_without_receipt: MarketScanPublicationDiagnostics,
    *,
    score_observations: tuple[MarketScanScoreDistributionObservation, ...] | None,
) -> MarketScanPublicationDiagnostic | None:
    if str(run["scope"] or "") != MARKET_SCAN_FULL_MARKET_SCOPE:
        return None
    registered = _current_registry_row(conn, run)
    if registered is None:
        return None
    contract = _registered_rule_contract(registered["contract_json"])
    _require_current_publication_thresholds(contract)
    _require_mode_contract(run, contract)
    summary = read_publication_summary(conn, run)
    _require_action_publication_summary(summary)
    policy, distribution, audit = _replayed_distribution(
        conn,
        run,
        diagnostics_without_receipt,
        contract,
        score_observations=score_observations,
    )
    return _canonical_receipt(
        run,
        registered,
        summary=summary,
        policy=policy,
        distribution=distribution,
        audit=audit,
    )


def validate_current_rule_contract_policy(contract: Mapping[str, object]) -> None:
    """Reject caller-defined relaxations of non-configurable policy."""
    _require_current_publication_thresholds(contract)


def _require_no_caller_receipt(
    diagnostics: MarketScanPublicationDiagnostics,
) -> None:
    supplied = (
        *diagnostics.blockers,
        *diagnostics.passed_gates,
        *diagnostics.source_warnings,
    )
    if any(item.code == MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE for item in supplied):
        raise ValueError("发布重放回执只能由持久化边界生成")


def _current_registry_row(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
) -> sqlite3.Row | None:
    registered = conn.execute(
        """
        SELECT production_score_rule_version, production_score_spec_hash,
               contract_json
        FROM market_scan_rule_contract
        WHERE rule_version = ?
        """,
        (run["rule_version"],),
    ).fetchone()
    if registered is None:
        return None
    if str(registered["production_score_rule_version"] or "") != FULL_MARKET_SCORE_RULE_VERSION:
        return None
    return registered


def _require_mode_contract(
    run: sqlite3.Row,
    contract: Mapping[str, object],
) -> None:
    mode = str(run["mode"] or "")
    if mode != "official" and int(run["skipped_count"] or 0):
        raise ValueError("非盘后正式扫描不能用跳过样本授权动作")
    mode_contract = contract.get("mode")
    if not isinstance(mode_contract, Mapping) or mode_contract.get("id") != mode:
        raise ValueError("动作来源扫描模式与封存规则合同不一致")


def _replayed_distribution(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    diagnostics: MarketScanPublicationDiagnostics,
    contract: Mapping[str, object],
    *,
    score_observations: tuple[MarketScanScoreDistributionObservation, ...] | None,
) -> tuple[
    MarketScanScoreDistributionPolicy,
    MarketScanScoreDistribution,
    str,
]:
    policy = _score_distribution_policy(contract)
    observations = (
        read_success_score_observations(conn, int(run["id"]))
        if score_observations is None
        else score_observations
    )
    distribution = MarketScanScoreDistribution.from_score_observations(
        observations,
        expected_count=int(run["success_count"] or 0),
        policy=policy,
    )
    assessment = policy.assess(distribution)
    if assessment.status != "pass":
        reason = "；".join(assessment.reasons) or assessment.status
        raise ValueError(f"评分分布通过声明无法由持久化结果重放：{reason}")
    audit = distribution.audit_text().removeprefix("评分分布门禁 ")
    passed = [
        item
        for item in diagnostics.passed_gates
        if item.code == "score_distribution.pass" and item.severity == "info"
    ]
    if len(passed) != 1 or passed[0].detail != audit:
        raise ValueError("评分分布通过声明未精确绑定持久化分布审计")
    return policy, distribution, audit


def _canonical_receipt(
    run: sqlite3.Row,
    registered: sqlite3.Row,
    *,
    summary: MarketScanPublicationSummary,
    policy: MarketScanScoreDistributionPolicy,
    distribution: MarketScanScoreDistribution,
    audit: str,
) -> MarketScanPublicationDiagnostic:
    payload = {
        "contract_version": "market-scan-publication-replay-v1",
        "run": _receipt_run_contract(run),
        "production_score_rule_version": str(registered["production_score_rule_version"]),
        "production_score_spec_hash": str(registered["production_score_spec_hash"]),
        "publication_summary": asdict(summary),
        "score_distribution": asdict(distribution),
        "score_distribution_policy": policy.spec(),
        "score_distribution_audit": audit,
    }
    return MarketScanPublicationDiagnostic(
        code=MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE,
        label="规范发布重放",
        detail=(
            f"{MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_PREFIX}"
            f"{stable_score_spec_hash(payload)}"
        ),
        severity="info",
    )


def _receipt_run_contract(run: sqlite3.Row) -> dict[str, object]:
    return {
        "id": int(run["id"]),
        "mode": str(run["mode"]),
        "rule_version": str(run["rule_version"]),
        "as_of": str(run["as_of"]),
        "data_date": str(run["data_date"]),
        "quote_date": str(run["quote_date"]),
        "total_count": int(run["total_count"] or 0),
        "processed_count": int(run["processed_count"] or 0),
        "success_count": int(run["success_count"] or 0),
        "missing_count": int(run["missing_count"] or 0),
        "skipped_count": int(run["skipped_count"] or 0),
    }


def _registered_rule_contract(value: object) -> Mapping[str, object]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("当前扫描批次缺少封存规则合同")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("当前扫描批次封存规则合同无法解析") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("当前扫描批次封存规则合同格式无效")
    return decoded


def _publication_contract(contract: Mapping[str, object]) -> Mapping[str, object]:
    value = contract.get("publication")
    if not isinstance(value, Mapping):
        raise ValueError("当前扫描批次缺少发布规则合同")
    return value


def _require_current_publication_thresholds(contract: Mapping[str, object]) -> None:
    publication = _publication_contract(contract)
    if (
        publication.get("minimum_coverage") != MARKET_SCAN_PUBLISH_MIN_COVERAGE
        or publication.get("minimum_eligible_ratio") != MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO
        or publication.get("max_snapshot_span_seconds") != MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS
        or publication.get("score_distribution") != MarketScanScoreDistributionPolicy().spec()
    ):
        raise ValueError("当前扫描批次发布门槛合同不是受支持的固定策略")


def _score_distribution_policy(
    contract: Mapping[str, object],
) -> MarketScanScoreDistributionPolicy:
    value = _publication_contract(contract).get("score_distribution")
    expected_keys = {field.name for field in fields(MarketScanScoreDistributionPolicy)}
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise ValueError("当前扫描批次评分分布策略合同无效")
    try:
        return MarketScanScoreDistributionPolicy(**dict(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("当前扫描批次评分分布策略合同无效") from exc


def _require_action_publication_summary(summary: MarketScanPublicationSummary) -> None:
    blockers = [
        item.detail
        for item in snapshot_publication_diagnostics(
            summary,
            max_span_seconds=MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS,
        )
    ]
    overall = summary.coverage_for("ALL")
    if overall is None or overall.population_count != summary.expected_capture_count:
        raise ValueError("发布股票池计数无法由持久化结果重放")
    for coverage in summary.coverages:
        if coverage.coverage_ratio < MARKET_SCAN_PUBLISH_MIN_COVERAGE[coverage.scope]:
            blockers.append(f"{coverage.scope} coverage={coverage.coverage_ratio:.2%}")
        if coverage.eligible_ratio < MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO[coverage.scope]:
            blockers.append(f"{coverage.scope} eligible={coverage.eligible_ratio:.2%}")
    if blockers:
        raise ValueError(f"发布通过声明无法由持久化快照重放：{blockers[0]}")


__all__ = [
    "MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE",
    "MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_PREFIX",
    "replay_current_action_gate_receipt",
    "validate_current_action_gate_claim",
    "validate_current_rule_contract_policy",
]
