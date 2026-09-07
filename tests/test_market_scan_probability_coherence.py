from copy import deepcopy

import pytest

from app.services.market_scan_probability_coherence import audit_probability_coherence


def context(**changes):
    return {"symbol": "600519.SH", "decision_at": "2026-09-07T16:00:00+08:00", "horizon_sessions": 5,
            "return_definition": "entry-open-to-exit-close", "price_basis": "unadjusted-with-cash-ledger",
            "conditioning_digest": "a" * 64, "source_digest": "b" * 64, **changes}


def event(name, operator, threshold, probability):
    return {"event_id": name, "operator": operator, "threshold": threshold, "probability": probability}


def payload(*events):
    return {"schema_version": "market-scan-probability-event-input-v1",
            "groups": [{"context": context(), "events": list(events)}]}


def test_higher_return_threshold_cannot_have_higher_probability():
    value = payload(event("five", "ge", .05, .4), event("ten", "ge", .1, .7))
    original = deepcopy(value)
    result = audit_probability_coherence(value)
    assert result["status"] == "incoherent"
    assert result["groups"][0]["violations"][0]["relation"] == "subset"
    assert value == original and result["promotion_eligible"] is False


@pytest.mark.parametrize("left,right,expected", [
    (event("a", "gt", 0., .5), event("b", "le", 0., .5), False),
    (event("a", "gt", 0., .5), event("b", "le", 0., .4), True),
    (event("a", "ge", 0., .6), event("b", "lt", 0., .4), False),
    (event("a", "ge", 0., .7), event("b", "le", 0., .7), False),  # mass at zero can overlap
    (event("a", "ge", 0., .3), event("b", "le", 0., .3), True),
    (event("a", "gt", -.1, .3), event("b", "lt", .1, .3), True),
    (event("a", "gt", 0., .7), event("b", "lt", 0., .7), True),
    (event("a", "gt", 0., .5), event("b", "lt", 0., .4), False),  # mass at zero need not vanish
    (event("a", "gt", 0., .7), event("b", "ge", 0., .6), True),
    (event("a", "lt", 0., .7), event("b", "le", .1, .6), True),
    (event("a", "ge", .1, .7), event("b", "le", -.1, .5), True),
])
def test_boundary_atoms_complements_subsets_and_disjointness(left, right, expected):
    result = audit_probability_coherence(payload(left, right))
    assert (result["status"] == "incoherent") is expected


@pytest.mark.parametrize("change", [{"horizon_sessions": 10}, {"return_definition": "excess-return"},
                                    {"conditioning_digest": "c" * 64}, {"price_basis": "adjusted"}])
def test_incomparable_contexts_are_never_forced_to_be_monotonic(change):
    value = payload(event("a", "ge", .05, .2))
    value["groups"].append({"context": context(**change), "events": [event("b", "ge", .1, .9)]})
    result = audit_probability_coherence(value)
    assert result["status"] == "coherent_for_declared_relations"
    assert all(group["checked_relations"] == 0 for group in result["groups"])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -.1, 1.1, True, "0.5"])
def test_invalid_probability_rejected(value):
    with pytest.raises(ValueError):
        audit_probability_coherence(payload(event("a", "ge", 0., value)))


def test_duplicate_events_contexts_and_naive_time_rejected():
    base = payload(event("a", "ge", 0., .5))
    duplicate = deepcopy(base)
    duplicate["groups"].append(deepcopy(base["groups"][0]))
    with pytest.raises(ValueError, match="unique context"):
        audit_probability_coherence(duplicate)
    for field, value in (("decision_at", "2026-09-07T16:00:00"), ("horizon_sessions", True)):
        broken = deepcopy(base)
        broken["groups"][0]["context"][field] = value
        with pytest.raises(ValueError):
            audit_probability_coherence(broken)
    for duplicate_event in (event("a", "gt", 1., .2), event("b", "ge", 0., .5)):
        broken = deepcopy(base)
        broken["groups"][0]["events"].append(duplicate_event)
        with pytest.raises(ValueError, match="unique|duplicate"):
            audit_probability_coherence(broken)


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-12:00"])
def test_timezone_overflow_is_a_domain_error(stamp):
    value = payload(event("a", "gt", 0., .5))
    value["groups"][0]["context"]["decision_at"] = stamp
    with pytest.raises(ValueError, match="UTC datetime range"):
        audit_probability_coherence(value)
