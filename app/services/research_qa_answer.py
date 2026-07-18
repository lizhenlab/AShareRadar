from __future__ import annotations

from app.services.research_qa_answer_confidence import (
    QUESTION_CONFIDENCE_PENALTIES as QUESTION_CONFIDENCE_PENALTIES,
    question_confidence as question_confidence,
)
from app.services.research_qa_answer_contracts import (
    ActionBuilder as ActionBuilder,
    ActionContext as ActionContext,
    AnswerPrefixBuilder as AnswerPrefixBuilder,
    ConfidenceContext as ConfidenceContext,
    ConfidencePenaltyRule as ConfidencePenaltyRule,
    ConclusionBuilder as ConclusionBuilder,
    ConclusionContext as ConclusionContext,
    EvidenceBuilder as EvidenceBuilder,
    EvidenceContext as EvidenceContext,
    InvalidationBuilder as InvalidationBuilder,
    InvalidationContext as InvalidationContext,
    StockQuestionContext as StockQuestionContext,
    TopicAnswerStrategy as TopicAnswerStrategy,
)
from app.services.research_qa_answer_report import answer_stock_question as answer_stock_question
from app.services.research_qa_answer_selectors import (
    question_actions as question_actions,
    question_answer_text as question_answer_text,
    question_conclusion as question_conclusion,
    question_evidence as question_evidence,
    question_invalidations as question_invalidations,
)
from app.services.research_qa_answer_strategies import TOPIC_ANSWER_STRATEGIES as TOPIC_ANSWER_STRATEGIES


__all__ = [
    "ActionBuilder",
    "ActionContext",
    "AnswerPrefixBuilder",
    "ConfidenceContext",
    "ConfidencePenaltyRule",
    "ConclusionBuilder",
    "ConclusionContext",
    "EvidenceBuilder",
    "EvidenceContext",
    "InvalidationBuilder",
    "InvalidationContext",
    "QUESTION_CONFIDENCE_PENALTIES",
    "StockQuestionContext",
    "TOPIC_ANSWER_STRATEGIES",
    "TopicAnswerStrategy",
    "answer_stock_question",
    "question_actions",
    "question_answer_text",
    "question_conclusion",
    "question_confidence",
    "question_evidence",
    "question_invalidations",
]
