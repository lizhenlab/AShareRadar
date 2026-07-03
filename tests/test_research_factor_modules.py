from __future__ import annotations

from app.services import research_factors
from app.services.research_factor_current import build_current_factors
from app.services.research_factor_report import assemble_factor_lab_report, build_factor_lab_metrics
from app.services.research_factor_specs import _factor_specs
from app.services.research_factor_text import _factor_score_impact


def test_research_factors_facade_preserves_legacy_helpers() -> None:
    assert research_factors.build_current_factors is build_current_factors
    assert research_factors._factor_specs is _factor_specs
    assert research_factors._factor_score_impact is _factor_score_impact


def test_research_factor_report_module_exposes_report_assembly_helpers() -> None:
    assert callable(build_factor_lab_metrics)
    assert callable(assemble_factor_lab_report)
