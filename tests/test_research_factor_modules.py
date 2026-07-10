from __future__ import annotations

from types import SimpleNamespace

from app.models.schemas import FactorCalibration, StandardFactor
from app.services import research_factors
from app.services.research_factor_current import build_current_factors
from app.services.research_factor_report import (
    _effective_calibration_sample_count,
    assemble_factor_lab_report,
    build_factor_lab_metrics,
)
from app.services.research_factor_specs import _factor_specs
from app.services.research_factor_text import _factor_confirmation_text, _factor_score_impact


def test_research_factors_facade_preserves_legacy_helpers() -> None:
    assert research_factors.build_current_factors is build_current_factors
    assert research_factors._factor_specs is _factor_specs
    assert research_factors._factor_score_impact is _factor_score_impact


def test_research_factor_report_module_exposes_report_assembly_helpers() -> None:
    assert callable(build_factor_lab_metrics)
    assert callable(assemble_factor_lab_report)


def test_factor_lab_uses_minimum_per_factor_samples_instead_of_summing_six_by_four() -> None:
    factors = [_factor(index, sample_count=4) for index in range(6)]

    report = assemble_factor_lab_report(_feature(), "常规个股", [], factors)

    assert _effective_calibration_sample_count(factors) == 4
    assert report.calibration_sample_count == 4
    assert "最低单因子有效样本只有 4 个" in _factor_confirmation_text(report)
    assert any("最低单因子相似样本数计为 4 个" in note and "不跨因子累加" in note for note in report.notes)


def test_factor_lab_preserves_a_single_factors_full_sample_support() -> None:
    report = assemble_factor_lab_report(_feature(), "常规个股", [], [_factor(1, sample_count=30)])

    assert report.calibration_sample_count == 30
    assert "最低单因子有效样本 30 个" in _factor_confirmation_text(report)
    assert "汇总置信度" in _factor_confirmation_text(report)


def _feature():
    return SimpleNamespace(
        symbol="600000.SH",
        updated_at="2026-07-10T10:00:00",
        signal_confidence=90,
        data_quality_score=90,
        data_quality_level="优秀",
    )


def _factor(index: int, *, sample_count: int) -> StandardFactor:
    return StandardFactor(
        id=f"factor_{index}",
        name=f"测试因子{index}",
        category="测试",
        value="偏强",
        score=70,
        level="偏强",
        direction="正向",
        weight=1.0,
        calibration=FactorCalibration(
            sample_count=sample_count,
            win_rate=65,
            avg_forward_5d_return=1.2,
            avg_forward_10d_return=1.8,
            max_adverse_return=-2.0,
            stability_score=80,
            expected_level="较强",
            confidence_level="较高" if sample_count >= 12 else "偏低",
            note="测试校准",
        ),
    )
