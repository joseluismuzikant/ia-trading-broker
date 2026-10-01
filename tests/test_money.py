"""Tests for the exact money type."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel

from app.domain.money import Money, money, quantity


def test_money_rounds_to_the_cent() -> None:
    assert money("1.005").amount == Decimal("1.01")
    assert money("1.004").amount == Decimal("1.00")


def test_a_float_does_not_leak_its_binary_error() -> None:
    # 0.1 + 0.2 is 0.30000000000000004 in binary floating point.
    total = money(0.1) + money(0.2)
    assert total == money("0.3")
    assert str(total) == "0.30"


def test_addition_and_subtraction_are_exact() -> None:
    assert money("10.00") + money("2.50") == money("12.50")
    assert money("10.00") - money("12.50") == money("-2.50")
    assert (money("10.00") - money("10.00")).is_zero


def test_multiplying_by_a_quantity_rounds_back_to_the_cent() -> None:
    # The price is stored to the cent first, so 60.555 becomes 60.56.
    price = money("60.555")
    assert price == money("60.56")
    value = price * Decimal("7")
    assert value == money("423.92")
    # Multiplication is commutative with a plain number.
    assert Decimal("7") * price == value


def test_dividing_returns_a_ratio_and_guards_against_zero() -> None:
    ratio = money("5.00") / money("20.00")
    assert ratio == Decimal("0.25")
    with pytest.raises(ZeroDivisionError):
        money("5.00") / money("0.00")


def test_comparison_and_hashing() -> None:
    assert money("1.00") < money("1.01")
    assert money("1.01") >= money("1.01")
    assert money("1.00") != money("0.99")
    assert len({money("1.00"), money("1.00")}) == 1
    assert money("0.00").is_zero
    assert money("0.01").is_positive
    assert not bool(money("0.00"))


def test_negation_and_absolute_value() -> None:
    assert -money("3.00") == money("-3.00")
    assert abs(money("-3.00")) == money("3.00")


def test_money_rejects_values_it_cannot_represent() -> None:
    with pytest.raises(TypeError):
        money(True)
    with pytest.raises(TypeError):
        money([1, 2])
    with pytest.raises(TypeError):
        # The constructor insists on a Decimal, not a string or a float.
        Money("3.00")  # type: ignore[arg-type]


def test_quantity_rounds_to_four_places() -> None:
    assert quantity("1.23456") == Decimal("1.2346")
    assert quantity(3) == Decimal("3.0000")


class _Payload(BaseModel):
    """A model that stores money, used to prove JSON round-tripping."""

    amount: Money


def test_money_survives_a_pydantic_json_round_trip() -> None:
    original = _Payload(amount=money("1234.56"))
    dumped = original.model_dump(mode="json")
    # A string keeps the exact value through JSON.
    assert dumped == {"amount": "1234.56"}
    restored = _Payload.model_validate(dumped)
    assert restored.amount == original.amount
