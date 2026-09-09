from __future__ import annotations

import hashlib
import json
from typing import Any, TypedDict

from app.models.analysis import AnalysisResult
from app.models.research import StockQuestionAnswer
from app.utils.market_data import finite_float


EVIDENCE_SELECTION_VERSION = "stock-qa-evidence-selection.v1"
MAX_SELECTED_EVIDENCE = 4


class LlmEvidenceUnit(TypedDict):
    id: str
    text: str
    source: str
    as_of: str


def answer_evidence_units(rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> list[LlmEvidenceUnit]:
    units: list[LlmEvidenceUnit] = []
    for index, text in enumerate(rule_answer.evidence[:6]):
        if isinstance(text, str) and text.strip():
            units.append(_unit(f"rule-{index}", text, "rule_answer.evidence", rule_answer.updated_at))
    quote = analysis.quote
    for name, value, label, unit, source in (
        ("current-price", quote.price, "现价", "元", "quote.price"),
        ("change-pct", quote.change_pct, "涨跌幅", "%", "quote.change_pct"),
    ):
        number = finite_float(value)
        if number is not None:
            units.append(_unit(name, f"{label} {number:.2f}{unit}", source, quote.timestamp))
    units.append(_unit("reliability", f"回答可靠度 {rule_answer.confidence}/100，该评分不是统计正确率或概率。", "rule_answer.confidence", rule_answer.updated_at))
    for index, text in enumerate(analysis.data_quality.notes[:4]):
        if isinstance(text, str) and text.strip():
            units.append(_unit(f"quality-{index}", text, "analysis.data_quality.notes", quote.timestamp))
    return units


def evidence_selection_contract(rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> dict[str, Any]:
    context = {
        "version": EVIDENCE_SELECTION_VERSION,
        "question": rule_answer.question,
        "topic": rule_answer.topic,
        "symbol": rule_answer.symbol,
        "quote_symbol": f"{analysis.quote.code}.{analysis.quote.market}",
        "as_of": rule_answer.updated_at,
        "conclusion": rule_answer.conclusion,
        "answer": rule_answer.answer,
        "actions": rule_answer.actions,
        "invalidations": rule_answer.invalidations,
        "units": answer_evidence_units(rule_answer, analysis),
    }
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    return {
        "contract_version": EVIDENCE_SELECTION_VERSION,
        "context_id": hashlib.sha256(encoded).hexdigest(),
    }


def _unit(identifier: str, text: str, source: str, as_of: str) -> LlmEvidenceUnit:
    return {"id": identifier, "text": " ".join(text.split()), "source": source, "as_of": as_of}
