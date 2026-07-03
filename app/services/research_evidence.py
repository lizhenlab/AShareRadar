from __future__ import annotations

from app.models.schemas import AlphaEvidenceReport, EvidenceChainReport, RiskRewardReport, SignalValidationReport, StockDiagnosis


def build_evidence_chain_report(
    diagnosis: StockDiagnosis,
    alpha: AlphaEvidenceReport,
    validation: SignalValidationReport,
    risk_reward: RiskRewardReport,
) -> EvidenceChainReport:
    support = [f"{item.title}：{item.reason}" for item in alpha.positives[:4]]
    opposition = [f"{item.title}：{item.reason}" for item in alpha.negatives[:4]]
    confirmations = [*diagnosis.confirmation_signals[:3], *[f"{item.name}：{item.confirmation_condition}" for item in validation.items[:2]]]
    invalidations = [*diagnosis.hard_risks[:3], f"风险收益降为「{risk_reward.rating}」且收益风险比低于 1.2。"]
    return EvidenceChainReport(
        verdict=alpha.verdict,
        summary=f"当前结论「{diagnosis.action}」不是单一指标给出，而是由证据、验证闭环和风险收益共同约束。",
        support=support or ["暂未形成足够强的正向证据。"],
        opposition=opposition or ["暂未识别核心反向证据，但仍需按失效条件观察。"],
        confirmations=_dedupe(confirmations)[:5],
        invalidations=_dedupe(invalidations)[:5],
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
