from __future__ import annotations

import math

import pytest

from app.services.research_factor_specs import _factor_specs
from app.services.research_volume_scoring import volume_confirmation_score


@pytest.mark.parametrize("ratio", [0.01, 0.5, 0.69, 0.7, 0.85, 1.0, 1.19, 1.2, 1.25, 2.5, 1e308])
def test_volume_never_creates_direction_for_flat_prices(ratio: float) -> None:
    assert volume_confirmation_score(0, ratio) == 50


@pytest.mark.parametrize("ratio", [0.01, 0.5, 0.69, 0.7, 0.85, 1, 1.19, 1.2, 1.25, 2.5])
def test_volume_direction_is_symmetric_and_monotonic_in_return(ratio: float) -> None:
    changes = [-1e308, -8, -5, -2.01, -2, -1.99, -0.01, 0, 0.01, 1.99, 2, 2.01, 5, 8, 1e308]
    scores = [volume_confirmation_score(change, ratio) for change in changes]
    assert scores == sorted(scores)
    for change, score in zip(changes, scores, strict=True):
        assert 0 <= score <= 100
        assert score + volume_confirmation_score(-change, ratio) == 100
        assert score >= 50 if change >= 0 else score <= 50


def test_low_volume_up_move_cannot_flip_bearish_at_two_percent() -> None:
    assert volume_confirmation_score(1.99, 0.69) == 57
    assert volume_confirmation_score(2, 0.69) == 57
    assert volume_confirmation_score(-2, 0.69) == 43


def test_normal_volume_down_move_has_negative_direction() -> None:
    assert volume_confirmation_score(-2, 1) == 40
    assert volume_confirmation_score(2, 1) == 60


@pytest.mark.parametrize("change", [-5, -2, 0, 2, 5])
def test_lower_volume_only_shrinks_distance_from_neutral(change: float) -> None:
    ratios = [0.01, 0.5, 0.69, 0.7, 0.85, 1, 1.19, 1.2, 1.25, 2.5]
    deviations = [abs(volume_confirmation_score(change, ratio) - 50) for ratio in ratios]
    assert deviations == sorted(deviations)
    assert deviations[0] == deviations[1]
    assert deviations[-2] == deviations[-1]


@pytest.mark.parametrize("boundary", [0.7, 0.85, 1.2, 1.25])
@pytest.mark.parametrize("change", [-2, 0, 2])
def test_old_rule_thresholds_have_no_multi_point_jump(boundary: float, change: float) -> None:
    assert abs(volume_confirmation_score(change, boundary + 1e-9) - volume_confirmation_score(change, boundary - 1e-9)) <= 1


@pytest.mark.parametrize("change,ratio", [(math.nan, 1), (math.inf, 1), (-math.inf, 1), (1, math.nan), (1, math.inf), (1, 0), (1, -1)])
def test_invalid_observed_inputs_are_rejected(change: float, ratio: float) -> None:
    with pytest.raises(ValueError, match="量价"):
        volume_confirmation_score(change, ratio)


def test_volume_factor_has_an_explicit_distinct_current_and_replay_rule_version() -> None:
    assert _factor_specs()["volume_confirmation"].score_rule_version == "factor-volume-confirmation.v3"
