from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, timedelta
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.services.market_scan_probability import (
    ProbabilityConfig, ProbabilitySample, fit_shadow_probability, predict_shadow_probability,
    stable_probability_hash, verify_shadow_probability_evidence,
)
from app.services import market_scan_probability_estimators as estimators
from app.services.market_scan_probability_values import ProbabilityReplayError


@dataclass(frozen=True)
class _NumericalConfig:
    l2_strength: float = 1.0
    maximum_iterations: int = 100
    convergence_tolerance: float = 1e-10


@dataclass(frozen=True)
class _Observation:
    features: dict[str, float]
    target: int | bool | None


def _samples() -> list[ProbabilitySample]:
    rows = []
    for session_index in range(42):
        shift = ((session_index % 5) - 2) * 0.08
        for stock_index, signal in enumerate((-1.2, -0.4, 0.4, 1.2)):
            outcome = int(signal + shift > 0)
            rows.append(ProbabilitySample(
                sample_id=f"{session_index}:600{stock_index:03d}.SH:1:net_excess_positive",
                session_date=(date(2025, 1, 1) + timedelta(days=session_index)).isoformat(),
                features={"trend": signal, "risk": -signal * 0.4 + shift},
                target=outcome,
                net_return=0.011 if outcome else -0.009,
                net_excess_return=0.006 if outcome else -0.004,
            ))
    return rows


def _config() -> ProbabilityConfig:
    return ProbabilityConfig(
        horizon=1, minimum_train_sessions=12, minimum_calibration_sessions=6,
        minimum_test_sessions=6, minimum_bin_sessions=1, bootstrap_samples=100,
        minimum_isotonic_calibration_sessions=6,
    )


# These values were captured before the structural refactor. BLAS/LAPACK may
# change the final floating-point ulps and the iteration crossing the tolerance;
# neither is a contract change. Exact byte equality remains a same-runtime
# migration check, while committed fixtures check semantic and numeric values.
_FLOAT_DERIVED_DIGESTS = frozenset({
    "model_digest", "calibrator_digest", "isotonic_calibrator_digest",
    "baseline_digest", "fold_digest", "evidence_digest",
})


def _assert_registered_values(actual: Any, expected: Any, path: str = "root") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict) and actual.keys() == expected.keys(), path
        for key, value in expected.items():
            if key in _FLOAT_DERIVED_DIGESTS:
                continue  # Full strict replay below validates their current bytes.
            if key == "iterations":
                assert type(actual[key]) is int and 1 <= actual[key] <= _NumericalConfig().maximum_iterations, path
                continue  # Stopping on an adjacent BLAS iteration is permitted.
            _assert_registered_values(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), path
        for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
            _assert_registered_values(left, right, f"{path}[{index}]")
    elif isinstance(expected, float):
        assert type(actual) is float, path
        assert actual == pytest.approx(expected, rel=1e-9, abs=1e-12), path
    else:
        assert type(actual) is type(expected) and actual == expected, path


@pytest.fixture
def golden() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "probability_estimators_golden.json"
    return json.loads(path.read_text())


def test_registered_numerics_match_pre_refactor_golden(golden: dict[str, Any]) -> None:
    rows, config = _samples(), _config()
    model = estimators.fit_probability_logistic_model(rows[:48], ("risk", "trend"), config)
    raw = [estimators.probability_model_probability(model, row.features) for row in rows[48:72]]
    calibrator = estimators.fit_probability_platt_calibrator(raw, [int(row.target) for row in rows[48:72]], config)
    isotonic = estimators.fit_probability_isotonic_calibrator([0.1, 0.1, 0.2, 0.3, 0.4, 0.9], [0, 1, 1, 0, 1, 1])
    predictions = []
    for row in rows[72:80]:
        score = estimators.probability_model_probability(model, row.features)
        predictions.append({"raw": score, "platt": estimators.probability_platt_probability(calibrator, score),
                            "isotonic": estimators.probability_isotonic_probability(isotonic, score)})
    for name, value in {"model": model, "calibrator": calibrator, "isotonic": isotonic, "predictions": predictions}.items():
        _assert_registered_values(value, golden[name], name)
    baseline = {"boundaries": [0.2, 0.8], "probabilities": [0.1, 0.5, 0.9]}
    assert [estimators.probability_baseline_probability(baseline, x) for x in [0.0, 0.2, 0.5, 0.8, 1.0]] == golden["baseline_predictions"]


@pytest.mark.parametrize("case", ["success", "insufficient", "nonconvergence"])
def test_complete_evidence_and_prediction_preserve_pre_refactor_contract(case: str, golden: dict[str, Any]) -> None:
    rows = _samples()[:8] if case == "insufficient" else _samples()
    config = replace(_config(), maximum_iterations=1) if case == "nonconvergence" else _config()
    evidence = fit_shadow_probability(rows, config=config, generated_at="2026-08-11T08:00:00Z")
    result = {"evidence": evidence,
              "projection": predict_shadow_probability(evidence, {"risk": 0.4, "trend": 0.8}, sample_id="fixed-current")}
    _assert_registered_values(result, golden["studies"][case], case)
    assert verify_shadow_probability_evidence(evidence, rows) is True


@pytest.mark.parametrize("component", ["model", "calibrator"])
def test_optimizer_nonconvergence_retains_original_error(component: str, golden: dict[str, Any]) -> None:
    rows = _samples()[:48]
    config = replace(_NumericalConfig(), maximum_iterations=1)
    with pytest.raises(estimators.ProbabilityModelConvergenceError) as caught:
        if component == "model":
            estimators.fit_probability_logistic_model(rows, ("risk", "trend"), config)
        else:
            estimators.fit_probability_platt_calibrator([0.1, 0.9], [0, 1], config)
    assert type(caught.value).__name__ == golden["nonconvergence"][component]["type"]
    assert str(caught.value) == golden["nonconvergence"][component]["message"]


@pytest.mark.parametrize("component", ["model", "calibrator"])
def test_singular_hessian_rejection_preserves_cause(monkeypatch: pytest.MonkeyPatch, component: str) -> None:
    error = np.linalg.LinAlgError("singular")

    def fail(*_args: object) -> None:
        raise error

    monkeypatch.setattr(np.linalg, "solve", fail)
    with pytest.raises(estimators.ProbabilityModelConvergenceError, match=f"{component}_singular_hessian") as caught:
        if component == "model":
            estimators.fit_probability_logistic_model([_Observation({"x": -1.0}, False), _Observation({"x": 1.0}, True)], ("x",), _NumericalConfig())
        else:
            estimators.fit_probability_platt_calibrator([0.1, 0.9], [0, 1], _NumericalConfig())
    assert caught.value.__cause__ is error


def test_estimator_accepts_structural_inputs_and_preserves_constant_feature_scaling() -> None:
    rows = [_Observation({"constant": 7.0, "trend": float(i % 3)}, bool(i % 2)) for i in range(18)]
    model = estimators.fit_probability_logistic_model(rows, ("constant", "trend"), _NumericalConfig())
    assert model["scales"][0] == 1.0
    assert model["coefficients"][0] == 0.0
    assert model["converged"] is True
    with pytest.raises(ValueError, match="训练分区缺少 target"):
        estimators.fit_probability_logistic_model([_Observation({"x": 1.0}, None)], ("x",), _NumericalConfig())
    with pytest.raises(ValueError, match="0、1 或 None"):
        estimators.fit_probability_logistic_model([_Observation({"x": 1.0}, 2)], ("x",), _NumericalConfig())


@pytest.mark.parametrize(("field", "value", "message"), [
    ("means", [], "维度损坏"), ("scales", [0.0, 1.0], "scale 无效"),
    ("coefficients", [float("inf"), 0.0], "有限数值"), ("means", None, "必须是数组"),
])
def test_prediction_rejects_corrupt_numeric_artifact(field: str, value: object, message: str, golden: dict[str, Any]) -> None:
    model = deepcopy(golden["model"])
    model[field] = value
    with pytest.raises(ProbabilityReplayError, match=message):
        estimators.probability_model_probability(model, {"risk": 0.4, "trend": 0.8})


def test_prediction_boundaries_retain_strict_rejection(golden: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="特征集合"):
        estimators.probability_model_probability(golden["model"], {"trend": 0.8})
    with pytest.raises(ValueError, match="必须是数值"):
        estimators.probability_model_probability(golden["model"], {"risk": True, "trend": 0.8})
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        estimators.probability_platt_probability(golden["calibrator"], 1.1)
    with pytest.raises(ProbabilityReplayError, match="维度损坏"):
        estimators.probability_isotonic_probability({"upper_bounds": [], "probabilities": []}, 0.5)
    with pytest.raises(ProbabilityReplayError, match="分箱维度损坏"):
        estimators.probability_baseline_probability({"boundaries": [0.5], "probabilities": [0.5]}, 0.5)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        estimators.fit_probability_isotonic_calibrator([float("nan")], [1])


def test_golden_comparison_allows_one_ulp_but_rejects_numeric_or_discrete_contract_drift(golden: dict[str, Any]) -> None:
    expected = golden["model"]
    actual = deepcopy(expected)
    actual["coefficients"][0] = float(np.nextafter(actual["coefficients"][0], np.inf))
    _assert_registered_values(actual, expected)
    actual["coefficients"][0] += 1e-5
    with pytest.raises(AssertionError):
        _assert_registered_values(actual, expected)
    for field, value in [("version", "unregistered"), ("converged", False), ("iterations", 0)]:
        actual = deepcopy(expected)
        actual[field] = value
        with pytest.raises(AssertionError):
            _assert_registered_values(actual, expected)
    with pytest.raises(AssertionError):
        _assert_registered_values({"counts": [2]}, {"counts": [1]})


def test_golden_numeric_tolerance_does_not_bypass_internal_digest_validation() -> None:
    rows = _samples()
    evidence = fit_shadow_probability(rows, config=_config(), generated_at="2026-08-11T08:00:00Z")
    evidence["model_digest"] = "0" * 64
    evidence["evidence_digest"] = stable_probability_hash({
        key: value for key, value in evidence.items() if key != "evidence_digest"
    })
    with pytest.raises(ProbabilityReplayError, match="model_digest 不一致"):
        verify_shadow_probability_evidence(evidence, rows)
