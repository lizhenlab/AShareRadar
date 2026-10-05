from copy import deepcopy

import pytest

from app.models.research import FactorLabReport
from app.services.research_factor_report import assemble_factor_lab_report
from tests.test_factor_evidence_contract import _factor, _feature


def _payload():
    return assemble_factor_lab_report(_feature(), "常规个股", [], [
        _factor(id="trend_momentum", score=80),
        _factor(id="fund_flow_proxy", score=20),
        _factor(id="risk_pressure", score=30, samples=0),
        _factor(id="unknown", score=100, samples=0),
    ]).model_dump()


@pytest.mark.parametrize("part,field,value", [
    ("report", "total_score", 99),
    ("aggregate", "total_score", 99),
    ("aggregate", "directional_score", 99),
    ("aggregate", "risk_penalty", 1),
    ("aggregate", "coverage_pct", 99),
    ("group", "budget_pct", 30),
    ("group", "contribution", 6),
    ("group", "coverage_pct", 100),
    ("factor", "score_share_pct", 99),
    ("factor", "score_usage", "risk_constraint"),
    ("factor", "weight", 1),
])
def test_v3_rejects_inconsistent_reconstructed_fields(part, field, value):
    payload = _payload()
    target = {"report": payload, "aggregate": payload["score_aggregation"],
              "group": payload["score_aggregation"]["groups"][0], "factor": payload["factors"][0]}[part]
    target[field] = value
    with pytest.raises(ValueError, match="aggregation"):
        FactorLabReport.model_validate(payload)


def test_excluded_unknown_factor_cannot_participate_after_reload():
    payload = _payload()
    payload["factors"][-1]["participates_in_current_score"] = True
    with pytest.raises(ValueError, match="aggregation"):
        FactorLabReport.model_validate(payload)


def test_missing_registered_id_mapping_cannot_misrepresent_factor_share():
    payload = _payload()
    payload["score_aggregation"]["factor_shares"].pop("trend_momentum")
    with pytest.raises(ValueError, match="aggregation"):
        FactorLabReport.model_validate(payload)


def test_valid_current_report_roundtrips_and_legacy_omission_stays_readable():
    payload = _payload()
    assert FactorLabReport.model_validate(payload).total_score == 45
    legacy = deepcopy(payload)
    legacy.pop("score_aggregation")
    legacy["total_score"] = 99
    for factor in legacy["factors"]:
        factor.pop("score_usage")
        factor.pop("score_share_pct")
    assert FactorLabReport.model_validate(legacy).total_score == 99


@pytest.mark.parametrize("change", ["duplicate_group", "missing_group", "duplicate_factor", "duplicate_exclusion",
                                    "missing_exclusion", "negative_share", "risk_share", "missing_usage", "missing_share",
                                    "nonfinite_weight", "unobserved_group_contribution"])
def test_v3_requires_complete_unambiguous_finite_accounting(change):
    payload = _payload()
    aggregate = payload["score_aggregation"]
    if change == "duplicate_group":
        aggregate["groups"][1]["name"] = aggregate["groups"][0]["name"]
    elif change == "missing_group":
        aggregate["groups"].pop()
    elif change == "duplicate_factor":
        payload["factors"].append(deepcopy(payload["factors"][0]))
    elif change == "duplicate_exclusion":
        aggregate["excluded_ids"].append("unknown")
    elif change == "missing_exclusion":
        aggregate["excluded_ids"] = []
    elif change == "negative_share":
        aggregate["factor_shares"]["trend_momentum"] = -1
    elif change == "risk_share":
        aggregate["factor_shares"]["risk_pressure"] = 1
        aggregate["factor_shares"]["valuation_anchor"] -= 1
    elif change == "missing_usage":
        payload["factors"][0]["score_usage"] = None
    elif change == "missing_share":
        payload["factors"][0]["score_share_pct"] = None
    elif change == "nonfinite_weight":
        payload["factors"][0]["weight"] = float("nan")
    else:
        aggregate["groups"][0]["coverage_pct"] = 0
        aggregate["groups"][1]["coverage_pct"] = 100
    with pytest.raises(ValueError, match="aggregation"):
        FactorLabReport.model_validate(payload)


@pytest.mark.parametrize("score,risk", [(0, 0), (20, 0), (100, 0), (50, 49)])
def test_v3_keeps_valid_clipping_and_half_point_rounding(score, risk):
    factor_ids = ["trend_momentum", "chip_position", "volume_confirmation", "fund_flow_proxy", "valuation_anchor"]
    factors = [_factor(id=factor_id, score=score, samples=0) for factor_id in factor_ids]
    report = assemble_factor_lab_report(_feature(), "常规个股", [], [*factors, _factor(id="risk_pressure", score=risk, samples=0)])
    assert FactorLabReport.model_validate_json(report.model_dump_json()).total_score == round(max(0, score - (50 - risk) * 0.25))


def test_serialization_roundoff_is_tolerated_without_relaxing_total_consistency():
    payload = _payload()
    payload["score_aggregation"]["groups"][0]["contribution"] += 1e-10
    assert FactorLabReport.model_validate(payload).total_score == 45
    payload["score_aggregation"]["groups"][0]["contribution"] += 1e-5
    with pytest.raises(ValueError, match="aggregation"):
        FactorLabReport.model_validate(payload)
