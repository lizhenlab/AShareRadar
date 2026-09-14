"""One admitted valuation observation feeds both current individual score views."""

from __future__ import annotations

from app.models.analysis import FactorScore, ValuationAnalysis
from app.models.fuyao_scoring import FuyaoValuationScore
from app.services.scoring import score_level
from app.utils.audit_time import parse_audit_time


def align_fuyao_valuation(
    result: FuyaoValuationScore | None, quote_timestamp: str,
) -> FuyaoValuationScore | None:
    """Keep the real freshness cutoff, and exclude observations later than the quote."""
    if result is None or not result.score_available:
        return result
    try:
        observed = parse_audit_time(result.fetched_at or "")
        quote_time = parse_audit_time(quote_timestamp)
        if observed <= quote_time:
            return result
        reason = "扶摇本地观察晚于本次行情时点，未用于该行情批次评分"
    except (ValueError, TypeError, OverflowError):
        reason = "扶摇观察与行情时点无法核验，未用于该行情批次评分"
    return result.model_copy(update={"score": None, "score_available": False, "unavailable_reason": reason})


def fuyao_valuation_fallback_note(result: FuyaoValuationScore) -> str:
    reason = result.unavailable_reason or "本地估值观察未通过评分准入"
    return f"扶摇估值未采用：{reason}；本次沿用行情估值路径。"


def fuyao_fundamental_factor(result: FuyaoValuationScore) -> FactorScore:
    if not result.score_available or result.score is None:
        raise ValueError("扶摇估值评分不可用")
    return FactorScore(
        name="基本面", score=result.score, level=score_level(result.score),
        summary="扶摇估值观察（PE TTM / PB MRQ），不代表完整财务健康评分。",
        evidence=list(result.evidence), missing_data=list(result.missing_data),
    )


def apply_fuyao_valuation(
    baseline: ValuationAnalysis, result: FuyaoValuationScore | None,
) -> ValuationAnalysis:
    result = align_fuyao_valuation(result, baseline.updated_at)
    if result is None:
        return baseline
    if result.symbol != baseline.symbol:
        raise ValueError("扶摇估值观察与当前股票不一致")
    if not result.score_available or result.score is None:
        note = fuyao_valuation_fallback_note(result)
        return baseline.model_copy(update={
            "evidence": [*baseline.evidence, note],
            "missing_data": [*baseline.missing_data, *result.missing_data],
            "watch_points": [*baseline.watch_points, *result.warnings],
            "score_unavailable_reason": result.unavailable_reason,
            "score_evaluated_at": result.evaluated_at,
        })
    return baseline.model_copy(update={
        "score": result.score, "score_available": True, "data_nature": "derived",
        "level": score_level(result.score),
        "summary": "依据本地扶摇 PE TTM / PB MRQ 观察形成估值压力分，仅辅助当前个股研究。",
        "pe": result.pe_ttm, "pb": result.pb_mrq,
        "market_cap": None, "market_cap_text": None,
        "pe_percentile": None, "pb_percentile": None,
        "peer_pe_percentile": None, "peer_pb_percentile": None, "peer_sample_count": 0,
        "valuation_anchor_label": "扶摇当前估值观察（非历史分位）",
        "evidence": list(result.evidence), "watch_points": list(result.warnings),
        "missing_data": [*result.missing_data, "扶摇同口径历史及同行估值分位"],
        "source": result.source, "input_basis": "fuyao_ttm_mrq",
        "score_rule_version": result.rule_version, "observation_id": result.observation_id,
        "observation_digest": result.observation_digest, "score_evaluated_at": result.evaluated_at,
        "observation_fetched_at": result.fetched_at,
        "score_unavailable_reason": None,
    })
