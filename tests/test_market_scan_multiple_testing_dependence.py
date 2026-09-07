from __future__ import annotations

from fractions import Fraction

import pytest

from app.services.market_scan_evaluation_statistics import benjamini_hochberg, benjamini_yekutieli


def test_by_matches_hand_calculated_step_up_adjustment_in_original_order() -> None:
    # m=4, H_m=25/12; the second sorted hypothesis inherits the third one's bound.
    adjusted, rejected = benjamini_yekutieli([.04, .001, .02, .015], alpha=.05)

    assert adjusted == pytest.approx([float(Fraction(1, 12)), float(Fraction(1, 120)),
                                      float(Fraction(1, 18)), float(Fraction(1, 18))])
    assert rejected == (False, True, False, False)


def test_perfectly_duplicated_candidates_keep_each_declared_hypothesis() -> None:
    values = [.02, .02, .02, .02]

    adjusted, rejected = benjamini_yekutieli(values, alpha=.03)

    assert adjusted == pytest.approx([float(Fraction(1, 24))] * 4)
    assert rejected == (False,) * 4
    assert benjamini_hochberg(values, alpha=.03) == ((.02,) * 4, (True,) * 4)
    assert values == [.02, .02, .02, .02]


def test_missing_hypotheses_count_in_both_family_size_and_harmonic_penalty() -> None:
    adjusted, rejected = benjamini_yekutieli([.01, None, .04, None, .005], alpha=.05)

    assert adjusted[0] == adjusted[4] == pytest.approx(float(Fraction(137, 2400)))
    assert adjusted[2] == pytest.approx(float(Fraction(137, 900)))
    assert adjusted[1] is adjusted[3] is None
    assert rejected == (False, None, False, None, False)
    completed, _ = benjamini_yekutieli([.01, 1, .04, 1, .005], alpha=.05)
    assert [adjusted[index] for index in (0, 2, 4)] == [completed[index] for index in (0, 2, 4)]


@pytest.mark.parametrize("values, expected", [([], ((), ())), ([None, None], ((None, None), (None, None)))])
def test_empty_and_unavailable_families_remain_explicit(values, expected) -> None:
    assert benjamini_yekutieli(values, alpha=.05) == expected


def test_single_test_has_no_multiplicity_penalty_and_boundary_is_inclusive() -> None:
    assert benjamini_yekutieli([.05], alpha=.05) == ((.05,), (True,))
    assert benjamini_yekutieli([1], alpha=.05) == ((1,), (False,))
    assert benjamini_yekutieli([0], alpha=.05) == ((0,), (True,))


def test_adjusted_probabilities_are_capped_and_permutation_equivariant() -> None:
    values = [.4, .8, .001, None]
    adjusted, rejected = benjamini_yekutieli(values, alpha=.05)
    permutation = [3, 2, 0, 1]

    permuted = benjamini_yekutieli([values[index] for index in permutation], alpha=.05)

    assert adjusted[:2] == (1.0, 1.0)
    assert permuted == (tuple(adjusted[index] for index in permutation),
                        tuple(rejected[index] for index in permutation))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -.001, 1.001, "invalid"])
def test_invalid_p_values_are_rejected(value) -> None:
    with pytest.raises(ValueError):
        benjamini_yekutieli([None, .01, value], alpha=.05)


@pytest.mark.parametrize("alpha", [0, 1, -.01, 1.01, float("nan"), float("inf"), -float("inf")])
def test_invalid_alpha_is_rejected_even_for_empty_family(alpha: float) -> None:
    with pytest.raises(ValueError, match="alpha"):
        benjamini_yekutieli([], alpha=alpha)
