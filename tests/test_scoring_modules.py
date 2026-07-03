from __future__ import annotations

import math

from app.services.research_alpha_points import bounded_int as alpha_bounded_int
from app.services.research_qa_utils import bounded_int as qa_bounded_int
from app.services.scoring import bounded_int, clamp_score, score_level


def test_clamp_score_rejects_non_finite_values_with_conservative_default() -> None:
    assert clamp_score(math.nan) == 0
    assert clamp_score(math.inf) == 0
    assert clamp_score(-math.inf) == 0
    assert clamp_score("bad", default=50) == 50


def test_bounded_int_sorts_bounds_and_preserves_rounding_choice() -> None:
    assert bounded_int(12.9, 0, 20) == 12
    assert bounded_int(12.5, 0, 20, round_value=True) == 12
    assert bounded_int(12.6, 0, 20, round_value=True) == 13
    assert bounded_int(30, 20, -5) == 20
    assert bounded_int(math.nan, -18, 18) == -18


def test_compatibility_bounded_int_exports_use_safe_shared_logic() -> None:
    assert alpha_bounded_int(math.inf, -18, 18) == -18
    assert qa_bounded_int(math.nan, 25, 92) == 25


def test_score_level_boundaries_are_stable() -> None:
    assert score_level(80) == "强"
    assert score_level(65) == "偏强"
    assert score_level(50) == "中性"
    assert score_level(35) == "偏弱"
    assert score_level(34) == "弱"
