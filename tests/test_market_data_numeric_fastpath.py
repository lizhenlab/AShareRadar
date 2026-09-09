from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
import math
from typing import SupportsFloat

import pytest

from app.utils import market_data


@pytest.mark.parametrize("value,expected", [
    (0, 0.0), (-7, -7.0), (2**53 + 1, float(2**53 + 1)),
    (1.25, 1.25), (True, 1.0), (False, 0.0),
    (" 1.25 ", 1.25), (b"2.5", 2.5), (bytearray(b"3.5"), 3.5),
    (memoryview(b"4.5"), 4.5), (Decimal("1.25"), 1.25), (Fraction(1, 4), 0.25),
    (float("nan"), None), (float("inf"), None), (-float("inf"), None),
    ("1e309", None), ("NaN", None), ("bad", None), (None, None), (object(), None),
])
def test_finite_number_conversion_keeps_existing_supported_inputs(value: object, expected: float | None) -> None:
    assert market_data.finite_float(value) == expected


def test_builtin_conversion_preserves_negative_zero_and_integer_overflow() -> None:
    assert math.copysign(1, market_data.finite_float(-0.0)) == -1
    with pytest.raises(OverflowError):
        market_data.finite_float(10**1000)


@pytest.mark.parametrize("base", [float, int])
def test_numeric_subclasses_still_call_their_custom_conversion(base: type) -> None:
    calls = []

    class CustomNumber(base):
        def __float__(self):
            calls.append(self)
            return 17.5

    value = CustomNumber(3)
    assert market_data.finite_float(value) == 17.5
    assert calls == [value]


@pytest.mark.parametrize("method", ["__float__", "__index__"])
def test_custom_numeric_protocol_is_invoked_once(method: str) -> None:
    calls = []

    def convert(self):
        calls.append(self)
        return 9.5 if method == "__float__" else 9

    value = type("CustomNumber", (), {method: convert})()
    assert market_data.finite_float(value) == (9.5 if method == "__float__" else 9.0)
    assert calls == [value]


@pytest.mark.parametrize("error", [TypeError, ValueError, OverflowError, RuntimeError])
def test_custom_conversion_errors_keep_existing_failure_boundary(error: type[Exception]) -> None:
    class BrokenNumber:
        def __float__(self):
            raise error("custom conversion failed")

    if error in {TypeError, ValueError}:
        assert market_data.finite_float(BrokenNumber()) is None
    else:
        with pytest.raises(error, match="custom conversion failed"):
            market_data.finite_float(BrokenNumber())


def test_builtin_cache_numbers_avoid_runtime_protocol_checks_but_bool_keeps_original_path(monkeypatch) -> None:
    checks = []

    class ObservedProtocol(type):
        def __instancecheck__(cls, value):
            checks.append(type(value))
            return isinstance(value, SupportsFloat)

    monkeypatch.setattr(market_data, "SupportsFloat", ObservedProtocol("ObservedFloat", (), {}))
    for index in range(100):
        assert market_data.finite_float(index) == float(index)
        assert market_data.finite_float(index + 0.5) == index + 0.5
    assert checks == []
    assert market_data.finite_float(True) == 1.0
    assert checks == [bool]
