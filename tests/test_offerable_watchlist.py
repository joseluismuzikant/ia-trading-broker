"""Tests for which instruments a model is shown as candidates to buy.

An instrument whose single unit costs more than the position limit of the
account can never be bought, so it is not offered. Holdings always stay.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.money import money
from app.domain.trading import IndicatorEvidence, PaperLedger, PaperPosition
from app.domain.universe import Instrument, PortfolioConstraints, UniverseConfig
from app.services.advice import analysis_context, offerable_watchlist

UNIVERSE = UniverseConfig(
    instruments=(
        Instrument("GGAL", "Galicia", "argentina_stocks"),
        Instrument("VIST", "Vista", "argentina_stocks"),
        Instrument("META", "Meta", "cedears"),
        Instrument("SPY", "SPY", "cedears"),
        Instrument("AL30", "Bonar 2030", "bonds", quotation_factor=100),
    ),
    constraints=PortfolioConstraints(max_single_position_pct=Decimal("0.15")),
)


def evidence(**prices: str) -> dict[str, IndicatorEvidence]:
    return {symbol: IndicatorEvidence(last_price=money(price)) for symbol, price in prices.items()}


def account() -> PaperLedger:
    # 100,000 in total: a 15% position limit is 15,000.
    return PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money("90000.00"),
        positions=[
            PaperPosition(
                symbol="SPY",
                quantity=Decimal("1"),
                average_price=money("10000.00"),
                last_price=money("10000.00"),
                market="BCBA",
            )
        ],
    )


def test_an_instrument_dearer_than_the_position_limit_is_not_offered() -> None:
    offered, notes = offerable_watchlist(
        ledger=account(),
        evidence=evidence(GGAL="6000.00", VIST="100.00", META="48400.00", SPY="10000.00"),
        universe=UNIVERSE,
    )

    symbols = {instrument.symbol for instrument in offered}
    assert "META" not in symbols
    assert {"GGAL", "VIST"} <= symbols
    assert any("META" in note and "15%" in note for note in notes)


def test_a_holding_is_always_offered_even_when_one_unit_is_dear() -> None:
    offered, _ = offerable_watchlist(
        ledger=account(),
        evidence=evidence(SPY="50000.00"),
        universe=UNIVERSE,
    )
    assert "SPY" in {instrument.symbol for instrument in offered}


def test_an_instrument_with_no_price_is_not_offered() -> None:
    offered, notes = offerable_watchlist(
        ledger=account(), evidence=evidence(GGAL="6000.00"), universe=UNIVERSE
    )

    assert "VIST" not in {instrument.symbol for instrument in offered}
    assert any("no price is known" in note and "VIST" in note for note in notes)


def test_a_bond_unit_cost_divides_the_quote_by_its_factor() -> None:
    # Quoted 85,800 per 100 nominales: one nominal costs 858, well under 15,000.
    offered, _ = offerable_watchlist(
        ledger=account(), evidence=evidence(AL30="85800.00"), universe=UNIVERSE
    )
    assert "AL30" in {instrument.symbol for instrument in offered}


def test_the_context_shows_the_model_only_the_offered_instruments() -> None:
    context = analysis_context(
        country="argentina",
        ledger=account(),
        evidence=evidence(GGAL="6000.00", META="48400.00", SPY="10000.00"),
        universe=UNIVERSE,
    )

    assert "META" not in {instrument.symbol for instrument in context.watchlist}
    assert "GGAL" in {instrument.symbol for instrument in context.watchlist}
