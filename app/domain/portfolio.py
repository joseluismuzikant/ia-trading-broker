"""The shared portfolio format.

IOL field names never leave the IOL adapter. Everything else works with these
models, so a page or a strategy never depends on a broker-specific shape.

Amounts are plain floats for now. Day 4 replaces them with exact money types
before any sizing or risk decision uses them.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Instrument(BaseModel):
    """A tradable asset, independent of any broker."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    symbol: str
    description: str | None = None
    market: str | None = None
    instrument_type: str | None = None
    currency: str | None = None


class Position(BaseModel):
    """How much of one instrument an account holds."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    instrument: Instrument
    #: Total held, including any part reserved by a pending operation.
    total_quantity: float | None = None
    #: Held and free to trade.
    available_quantity: float | None = None
    #: Held but committed to a pending operation.
    committed_quantity: float | None = None
    last_price: float | None = None
    average_price: float | None = None
    market_value: float | None = None


class Portfolio(BaseModel):
    """Cash and positions for one country."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    country: str
    currency: str | None = None
    #: Free cash. None when no saved account status could supply it.
    cash: float | None = None
    positions: list[Position] = Field(default_factory=list)

    @property
    def positions_value(self) -> float:
        """Sum of the valued positions."""
        return sum(position.market_value or 0.0 for position in self.positions)

    @property
    def total_value(self) -> float | None:
        """Cash plus positions, or None while cash is unknown."""
        if self.cash is None:
            return None
        return self.cash + self.positions_value
