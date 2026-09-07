from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

import pytest

from app.services.market_scan_evaluation import EvaluationConfig


@pytest.mark.parametrize("kwargs", [
    {"execution_notional": float("nan")}, {"execution_notional": float("inf")},
    {"execution_notional": True}, {"execution_notional": "100000"},
    {"bootstrap_samples": 100.5}, {"bootstrap_samples": float("nan")},
    {"minimum_sample_size": float("nan")}, {"minimum_session_count": True},
    {"max_exit_delay_sessions": .5}, {"horizons": (True,)}, {"horizons": (1.5,)},
    {"top_sizes": (20, 20)}, {"top_sizes": "20"}, {"horizons": None},
    {"complete_day_coverage": True}, {"max_daily_participation_rate": False},
    {"hysteresis_buffer_ratio": "0.2"}, {"cost_profile": "typo"},
])
def test_invalid_configuration_fails_at_construction(kwargs):
    with pytest.raises(ValueError):
        EvaluationConfig(**kwargs)


def test_caller_mutation_cannot_change_a_frozen_research_configuration():
    sizes, horizons = [50, 20], [5, 1]
    config = EvaluationConfig(top_sizes=sizes, horizons=horizons)
    sizes.append(100)
    horizons.clear()
    assert config.top_sizes == (50, 20)
    assert config.horizons == (5, 1)


def test_default_configuration_contract_preserves_original_fingerprint():
    encoded = json.dumps(asdict(EvaluationConfig()), sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == "c1cee52b3c3e2da49375840c5d6a611b722f44132fbc9e7a1fd5e44ed50e67bf"


@pytest.mark.parametrize("kwargs", [
    {"execution_notional": 0}, {"execution_notional": 10**500},
    {"bootstrap_samples": 99}, {"horizons": ()}, {"horizons": (0,)},
    {"complete_day_coverage": 0}, {"max_daily_participation_rate": 1.01},
    {"hysteresis_buffer_ratio": -.01},
])
def test_configuration_enforces_declared_bounds(kwargs):
    with pytest.raises(ValueError):
        EvaluationConfig(**kwargs)


def test_valid_boundary_values_and_legacy_import_remain_available():
    from app.services.market_scan_evaluation_config import EvaluationConfig as CanonicalConfig
    assert EvaluationConfig is CanonicalConfig
    config = EvaluationConfig(bootstrap_samples=100, max_exit_delay_sessions=0,
                              complete_day_coverage=1, hysteresis_buffer_ratio=0)
    assert config.complete_day_coverage == 1
    assert config.hysteresis_buffer_ratio == 0
