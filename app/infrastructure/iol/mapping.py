"""Translate IOL responses into the shared portfolio format.

This is the only place that knows how an IOL ``titulo`` becomes an
:class:`Instrument` and how ``cantidad``/``comprometido`` become an available
quantity. Keeping it here means the pages and later strategies only ever see
the broker-agnostic models.
"""

from __future__ import annotations

from app.domain.portfolio import Instrument, Portfolio, Position
from app.infrastructure.iol.schemas import IOLInstrument, IOLPortfolio, PortfolioAsset


def to_instrument(raw: IOLInstrument | None) -> Instrument:
    """Map one IOL ``titulo`` to a shared instrument."""
    return Instrument(
        symbol=(raw.simbolo if raw else None) or "",
        description=raw.descripcion if raw else None,
        market=raw.mercado if raw else None,
        instrument_type=raw.tipo if raw else None,
        currency=raw.moneda if raw else None,
    )


def to_position(asset: PortfolioAsset) -> Position:
    """Map one IOL portfolio asset to a shared position.

    ``available_quantity`` is the total held minus whatever is committed to a
    pending operation. It stays None when the broker omits the total.
    """
    total = asset.cantidad
    committed = asset.comprometido
    available = None
    if total is not None:
        available = total - (committed or 0.0)

    return Position(
        instrument=to_instrument(asset.titulo),
        total_quantity=total,
        available_quantity=available,
        committed_quantity=committed,
        last_price=asset.ultimo_precio,
        average_price=asset.ppc,
        market_value=asset.valorizado,
    )


def portfolio_currency(raw: IOLPortfolio) -> str | None:
    """Return the currency the holdings are quoted in, if the broker states it."""
    for asset in raw.activos:
        if asset.titulo and asset.titulo.moneda:
            return asset.titulo.moneda
    return None


def to_portfolio(
    country: str, raw: IOLPortfolio, *, cash: float | None = None
) -> Portfolio:
    """Map a full IOL country portfolio to the shared format."""
    return Portfolio(
        country=country,
        currency=portfolio_currency(raw),
        cash=cash,
        positions=[to_position(asset) for asset in raw.activos],
    )
