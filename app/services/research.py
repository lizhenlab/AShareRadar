from __future__ import annotations

from app.services.research_alpha import build_alpha_evidence_report
from app.services.research_breadth import build_market_breadth_snapshot
from app.services.research_chip import build_chip_analysis
from app.services.research_diagnosis import build_stock_diagnosis
from app.services.research_evidence import build_evidence_chain_report
from app.services.research_events import build_event_digest_report
from app.services.research_factors import build_factor_lab_report
from app.services.research_features import build_feature_snapshot, build_leadership_report
from app.services.research_peer import build_peer_comparison_report
from app.services.research_qa import answer_stock_question, build_stock_qa_report
from app.services.research_regime import build_market_regime_report
from app.services.research_replay import build_replay_analysis, _replay_pattern_note
from app.services.research_risk import build_risk_radar_report
from app.services.research_risk_reward import build_risk_reward_report
from app.services.research_theme import build_theme_context_report
from app.services.research_timeframe import build_timeframe_alignment_report
from app.services.research_t_strategy import build_t_strategy_assistant_report
from app.services.research_validation import build_signal_validation_report


__all__ = [
    "_replay_pattern_note",
    "answer_stock_question",
    "build_alpha_evidence_report",
    "build_chip_analysis",
    "build_evidence_chain_report",
    "build_event_digest_report",
    "build_factor_lab_report",
    "build_feature_snapshot",
    "build_leadership_report",
    "build_market_breadth_snapshot",
    "build_market_regime_report",
    "build_peer_comparison_report",
    "build_replay_analysis",
    "build_risk_radar_report",
    "build_risk_reward_report",
    "build_signal_validation_report",
    "build_stock_diagnosis",
    "build_stock_qa_report",
    "build_t_strategy_assistant_report",
    "build_theme_context_report",
    "build_timeframe_alignment_report",
]
