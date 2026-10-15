"""Exact money.

Sizing and risk decisions must not use binary floats: ``0.1 + 0.2`` is not
``0.3``, and a rounding error must never change an order quantity. Every amount
that feeds a decision is a :class:`Money`, a fixed-point decimal.

:class:`Money` is intentionally small. It adds, subtracts, multiplies by a
quantity, and compares, so the same type works for cash, prices, and notionals.

A :class:`Money` may carry a currency. One without a currency is compatible with
any (every amount in the application started out that way), while two amounts
in different currencies can never be added, subtracted, compared or treated as
equal: an ARS and a USD amount are never mixed without an explicit conversion.
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


ARS = "ARS"
USD = "USD"

#: Names brokers use, mapped to the ISO code the application keeps.
_CURRENCY_NAMES = {
    "ars": ARS,
    "peso": ARS,
    "pesos": ARS,
    "peso_argentino": ARS,
    "pesos_argentinos": ARS,
    "usd": USD,
    "dolar": USD,
    "dolares": USD,
    "dolar_estadounidense": USD,
    "dolares_estadounidenses": USD,
}


class CurrencyMismatchError(ValueError):
    """Two amounts in different currencies were combined without a conversion."""


def normalise_currency(value: str | None) -> str | None:
    """The currency code for a broker's name for it, or None when there is none.

    ``"Peso_Argentino"`` and ``"peso"`` are ARS, ``"Dolar_Estadounidense"`` is USD.
    A code the application does not know is kept, upper-cased, so a new broker
    currency is carried through instead of being silently treated as pesos.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _CURRENCY_NAMES.get(text.lower(), text.upper())


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

    __slots__ = ("amount", "currency")

    def __init__(self, amount: Decimal, currency: str | None = None) -> None:
        if not isinstance(amount, Decimal):
            raise TypeError("Money stores a Decimal; use money() to build one")
        self.amount = amount
        self.currency = normalise_currency(currency)

    def with_currency(self, currency: str | None) -> Money:
        """The same amount in the given currency, which must not contradict this one."""
        wanted = normalise_currency(currency)
        if self.currency is not None and wanted is not None and self.currency != wanted:
            raise CurrencyMismatchError(
                f"cannot relabel {self.currency} money as {wanted} without a conversion"
            )
        return Money(self.amount, wanted or self.currency)

    def __add__(self, other: Money) -> Money:
        other = _as_money(other)
        return Money(self.amount + other.amount, _shared_currency(self, other))

    def __sub__(self, other: Money) -> Money:
        other = _as_money(other)
        return Money(self.amount - other.amount, _shared_currency(self, other))

    def __mul__(self, factor: Any) -> Money:
        """Scale by a quantity or a ratio, rounding back to the cent."""
        scaled = self.amount * _to_decimal(factor)
        return Money(scaled.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP), self.currency)

    def __rmul__(self, factor: Any) -> Money:
        return self.__mul__(factor)

    def __truediv__(self, other: Any) -> Decimal:
        """Divide by money or a number and return an exact ratio."""
        if isinstance(other, Money):
            _shared_currency(self, other)  # a ratio of two currencies is meaningless
        divisor = other.amount if isinstance(other, Money) else _to_decimal(other)
        if divisor == 0:
            raise ZeroDivisionError("cannot divide money by zero")
        return self.amount / divisor

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency and other.currency and self.currency != other.currency:
            return False
        return self.amount == other.amount

    def __lt__(self, other: Money) -> bool:
        other = _as_money(other)
        _shared_currency(self, other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        other = _as_money(other)
        _shared_currency(self, other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        other = _as_money(other)
        _shared_currency(self, other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        other = _as_money(other)
        _shared_currency(self, other)
        return self.amount >= other.amount

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
        if self.currency:
            return f"Money({self.amount} {self.currency})"
        return f"Money({self.amount})"

    def __bool__(self) -> bool:
        return not self.is_zero

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source: type[Any], _handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """Accept numbers and strings on input; serialise as an exact string.

        A string keeps the exact value through JSON, which is how a proposal is
        stored. Reading it back reproduces the same cents. An amount that has a
        currency is stored as ``{"amount": "...", "currency": "ARS"}`` so the
        currency survives the round trip; an amount without one stays a plain
        string, exactly as every saved proposal already is.
        """
        return core_schema.no_info_plain_validator_function(
            _validate_money,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _serialise_money, when_used="json"
            ),
        )


def money(value: Any, currency: str | None = None) -> Money:
    """Build a :class:`Money` rounded to the cent, optionally in a currency."""
    carried = value.currency if isinstance(value, Money) else None
    return Money(
        _to_decimal(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP),
        currency or carried,
    )


def quantity(value: Any) -> Decimal:
    """Round a share quantity to the supported precision."""
    return _to_decimal(value).quantize(QUANTITY_PLACES, rounding=ROUND_HALF_UP)


def _shared_currency(left: Money, right: Money) -> str | None:
    """The currency of a combination, or an error when the two contradict."""
    if left.currency and right.currency and left.currency != right.currency:
        raise CurrencyMismatchError(
            f"cannot combine {left.currency} and {right.currency} amounts without a conversion"
        )
    return left.currency or right.currency


def _serialise_money(value: Money) -> Any:
    if value.currency:
        return {"amount": str(value), "currency": value.currency}
    return str(value)


def _as_money(value: Any) -> Money:
    if isinstance(value, Money):
        return value
    raise TypeError(f"expected Money, got {type(value).__name__}")


def _validate_money(value: Any) -> Money:
    if isinstance(value, Money):
        return value
    if isinstance(value, dict):
        return money(value["amount"], value.get("currency"))
    return money(value)
