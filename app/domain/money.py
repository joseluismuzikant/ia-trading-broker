"""Exact money.

Sizing and risk decisions must not use binary floats: ``0.1 + 0.2`` is not
``0.3``, and a rounding error must never change an order quantity. Every amount
that feeds a decision is a :class:`Money`, a fixed-point decimal.

:class:`Money` is intentionally small. It adds, subtracts, multiplies by a
quantity, and compares. It does not know about currencies or brokers, so the
same type works for cash, prices, and notionals.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

#: Prices and cash are kept to the cent. Indicator ratios are not money and
#: never pass through here.
MONEY_PLACES = Decimal("0.01")

#: Quantities are kept to a fraction of a share, enough for any instrument this
#: application sizes.
QUANTITY_PLACES = Decimal("0.0001")


def _to_decimal(value: Any) -> Decimal:
    """Convert a supported input to :class:`Decimal` without binary error.

    Floats are accepted because broker payloads arrive as JSON numbers, but they
    are routed through ``str`` so ``Decimal(0.1)`` never leaks a binary
    expansion into a decision.
    """
    if isinstance(value, Money):
        return value.amount
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, str, float)):
        raise TypeError(f"cannot convert {type(value).__name__} to money")
    return Decimal(str(value))


class Money:
    """A currency amount, exact to the cent."""

    __slots__ = ("amount",)

    def __init__(self, amount: Decimal) -> None:
        if not isinstance(amount, Decimal):
            raise TypeError("Money stores a Decimal; use money() to build one")
        self.amount = amount

    def __add__(self, other: "Money") -> "Money":
        return Money(self.amount + _as_money(other).amount)

    def __sub__(self, other: "Money") -> "Money":
        return Money(self.amount - _as_money(other).amount)

    def __mul__(self, factor: Any) -> "Money":
        """Scale by a quantity or a ratio, rounding back to the cent."""
        scaled = self.amount * _to_decimal(factor)
        return Money(scaled.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP))

    def __rmul__(self, factor: Any) -> "Money":
        return self.__mul__(factor)

    def __truediv__(self, other: Any) -> Decimal:
        """Divide by money or a number and return an exact ratio."""
        divisor = other.amount if isinstance(other, Money) else _to_decimal(other)
        if divisor == 0:
            raise ZeroDivisionError("cannot divide money by zero")
        return self.amount / divisor

    def __neg__(self) -> "Money":
        return Money(-self.amount)

    def __abs__(self) -> "Money":
        return Money(abs(self.amount))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.amount == other.amount

    def __lt__(self, other: "Money") -> bool:
        return self.amount < _as_money(other).amount

    def __le__(self, other: "Money") -> bool:
        return self.amount <= _as_money(other).amount

    def __gt__(self, other: "Money") -> bool:
        return self.amount > _as_money(other).amount

    def __ge__(self, other: "Money") -> bool:
        return self.amount >= _as_money(other).amount

    def __hash__(self) -> int:
        return hash(self.amount)

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    def __str__(self) -> str:
        return f"{self.amount:.2f}"

    def __repr__(self) -> str:
        return f"Money({self.amount})"

    def __bool__(self) -> bool:
        return not self.is_zero

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source: type[Any], _handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """Accept numbers and strings on input; serialise as an exact string.

        A string keeps the exact value through JSON, which is how a proposal is
        stored. Reading it back reproduces the same cents.
        """
        return core_schema.no_info_plain_validator_function(
            _validate_money,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: str(value), when_used="json"
            ),
        )


def money(value: Any) -> Money:
    """Build a :class:`Money` rounded to the cent."""
    return Money(_to_decimal(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP))


def quantity(value: Any) -> Decimal:
    """Round a share quantity to the supported precision."""
    return _to_decimal(value).quantize(QUANTITY_PLACES, rounding=ROUND_HALF_UP)


def _as_money(value: Any) -> Money:
    if isinstance(value, Money):
        return value
    raise TypeError(f"expected Money, got {type(value).__name__}")


def _validate_money(value: Any) -> Money:
    if isinstance(value, Money):
        return value
    return money(value)
