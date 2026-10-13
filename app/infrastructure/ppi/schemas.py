"""Typed views of the PPI responses the adapter relies on.

Only fields the OpenAPI reconstruction in ``docs/`` names are modelled, and
every model keeps unknown fields. That reconstruction's examples are
illustrative, not recorded from a live account, so a field that is missing or
malformed is an error rather than a guess.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class PPIToken(_Loose):
    """A session from ``LoginApi`` or ``RefreshToken``."""

    access_token: str = Field(alias="accessToken", min_length=1)
    refresh_token: str | None = Field(default=None, alias="refreshToken")
    token_type: str = Field(default="bearer", alias="tokenType")
    expiration_date: datetime | None = Field(default=None, alias="expirationDate")


class PPIAccount(_Loose):
    account_number: str = Field(alias="accountNumber", min_length=1)
    name: str = ""


class PPIBalance(_Loose):
    """One cash line: a currency (``simbol``) settled at one term."""

    name: str = ""
    simbol: str = ""
    amount: Decimal
    settlement: str = ""


class PPIQuote(_Loose):
    """One price point, current or historical."""

    date: datetime | None = None
    price: Decimal
    volume: Decimal | None = None
    opening_price: Decimal | None = Field(default=None, alias="openingPrice")
    max: Decimal | None = None
    min: Decimal | None = None


class PPIDisclaimer(_Loose):
    code: str = ""
    description: str = ""
    mandatory: bool = False
    accepted: bool = False


class PPIOrderDocument(_Loose):
    """An order as PPI describes it: a budget, a confirmation, or a detail."""

    id: int | None = None
    status: str | None = None
    ticker: str | None = None
    operation: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None
    external_id: str | None = Field(default=None, alias="externalID")
    disclaimers: list[PPIDisclaimer] = Field(default_factory=list)
