"""Tests for the fake portfolio advisor (the offline simulator).

The simulator is a production adapter — a catalog entry with ``kind = "fake"``
builds it — and the whole suite runs against it so no test ever spends an API
call. Whatever it answers must be a valid, self-financing allocation, and its
provenance must say plainly that no model was involved.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.analysis import PortfolioAnalysisContext
from app.domain.portfolio import Instrument as BrokerInstrument
from app.domain.portfolio import Portfolio, Position
from app.domain.universe import Instrument, PortfolioConstraints
from app.infrastructure.simulation import (
    FakePortfolioAdvisor,
    advice_from_targets,
    keep_portfolio,
)
from app.ports.portfolio_advisor import PortfolioAdvisor
from app.services import advice as advice_service
from app.services.advice_validation import validate_portfolio_advice

WATCHLIST = (
    Instrument("VIST", "Vista Energy S.A.", "argentina_stocks"),
    Instrument("SPY", "SPDR S&P 500 ETF Trust", "cedears"),
)


def build_context(
    *, cash: float, positions: list[tuple[str, float]], min_cash_pct: float = 0.10
) -> PortfolioAnalysisContext:
    """A portfolio from ``(symbol, market value)`` pairs plus cash."""
    return PortfolioAnalysisContext(
        country="argentina",
        analysed_at=datetime(2026, 10, 2, 14, 42, 6, tzinfo=UTC),
        portfolio=Portfolio(
            country="argentina",
            currency="Peso_Argentino",
            cash=cash,
            positions=[
                Position(
                    instrument=BrokerInstrument(symbol=symbol),
                    total_quantity=1.0,
                    last_price=value,
                    market_value=value,
                )
                for symbol, value in positions
            ],
        ),
        watchlist=WATCHLIST,
        constraints=PortfolioConstraints(min_cash_pct=round(min_cash_pct, 2)),
    )


def universe_for(view: PortfolioAnalysisContext):
    from app.domain.universe import UniverseConfig

    held = [
        Instrument(symbol, f"{symbol} S.A.", "argentina_stocks")
        for symbol in sorted(view.held_symbols)
    ]
    return UniverseConfig(instruments=tuple(held) + tuple(view.watchlist))


# --- The double itself ----------------------------------------------------


async def test_the_fake_is_an_advisor() -> None:
    assert isinstance(FakePortfolioAdvisor(), PortfolioAdvisor)


async def test_by_default_it_keeps_the_portfolio_it_was_shown() -> None:
    # 40% cash, 25% YPFD, 20% GGAL, 15% AL30: nothing needs to move.
    view = build_context(
        cash=40_000.0,
        positions=[("YPFD", 25_000.0), ("GGAL", 20_000.0), ("AL30", 15_000.0)],
    )

    advice = await FakePortfolioAdvisor().analyze(view)

    assert advice.target_cash_weight == pytest.approx(0.40)
    assert [(d.symbol, d.action, d.target_weight) for d in advice.decisions] == [
        ("AL30", "HOLD", pytest.approx(0.15)),
        ("GGAL", "HOLD", pytest.approx(0.20)),
        ("YPFD", "HOLD", pytest.approx(0.25)),
    ]


async def test_its_allocation_is_self_financing() -> None:
    view = build_context(
        cash=40_000.0,
        positions=[("YPFD", 25_000.0), ("GGAL", 20_000.0), ("AL30", 15_000.0)],
    )

    advice = await FakePortfolioAdvisor().analyze(view)

    assert advice.total_instrument_weight + advice.target_cash_weight == pytest.approx(1.0)
    validate_portfolio_advice(
        advice,
        held_symbols=view.held_symbols,
        universe=universe_for(view),
        current_weights=view.current_weights,
        min_cash_pct=0.10,
    )


async def test_it_opens_no_new_position_on_its_own() -> None:
    view = build_context(cash=90_000.0, positions=[("YPFD", 10_000.0)])

    advice = await FakePortfolioAdvisor().analyze(view)

    assert [item.symbol for item in advice.decisions] == ["YPFD"]
    assert "VIST" not in {item.symbol for item in advice.decisions}


async def test_an_all_cash_account_stays_all_cash() -> None:
    view = build_context(cash=100_000.0, positions=[])

    advice = await FakePortfolioAdvisor().analyze(view)

    assert advice.decisions == []
    assert advice.target_cash_weight == pytest.approx(1.0)


async def test_a_supplied_answer_is_returned_unchanged() -> None:
    view = build_context(
        cash=40_000.0,
        positions=[("YPFD", 25_000.0), ("GGAL", 20_000.0), ("AL30", 15_000.0)],
    )
    given = advice_from_targets(
        {"YPFD": 0.20, "GGAL": 0.10, "AL30": 0.15, "VIST": 0.20, "SPY": 0.25},
        cash=0.10,
        current={"YPFD": 0.25, "GGAL": 0.20, "AL30": 0.15},
    )

    advice = await FakePortfolioAdvisor(payload=given).analyze(view)

    assert advice is given
    assert advice.total_instrument_weight + advice.target_cash_weight == pytest.approx(1.0)


async def test_it_can_be_made_to_fail() -> None:
    view = build_context(cash=100_000.0, positions=[])
    advisor = FakePortfolioAdvisor(error=RuntimeError("the model is unavailable"))

    with pytest.raises(RuntimeError, match="unavailable"):
        await advisor.analyze(view)


async def test_it_remember_the_contexts_it_was_shown() -> None:
    view = build_context(cash=100_000.0, positions=[])
    advisor = FakePortfolioAdvisor()

    await advisor.analyze(view)

    assert advisor.contexts == [view]


# --- Choosing it from the catalog -----------------------------------------


async def test_a_fake_catalog_entry_builds_the_simulator(real_advisors: bool) -> None:
    entry = advice_service.resolve_model("fake_simulator")

    advisor = advice_service.build_advisor(entry)

    assert entry.kind == "fake"
    assert isinstance(advisor, FakePortfolioAdvisor)


async def test_the_simulator_stamps_its_provenance_from_the_catalog_entry(
    real_advisors: bool,
) -> None:
    from app.infrastructure.simulation.portfolio_advisor import SIMULATOR_VERSION

    entry = advice_service.resolve_model("fake_simulator")
    advisor = advice_service.build_advisor(entry)
    view = build_context(cash=40_000.0, positions=[("YPFD", 60_000.0)])

    advice = await advisor.analyze(view)

    assert advice.model_provider == "simulator"
    assert advice.model_name == "fake-portfolio-advisor"
    assert advice.analysis_version == SIMULATOR_VERSION


# --- Building advice ------------------------------------------------------


def test_the_provenance_of_a_built_answer_is_unmistakable() -> None:
    advice = advice_from_targets({"YPFD": 1.0}, cash=0.0, current={"YPFD": 1.0})

    assert advice.model_provider == "fake"
    assert advice.model_name == "test"
    assert advice.analysis_version == "test-1"


def test_the_actions_follow_the_move() -> None:
    advice = advice_from_targets(
        {"YPFD": 0.20, "GGAL": 0.10, "VIST": 0.30},
        cash=0.40,
        current={"YPFD": 0.25, "GGAL": 0.10, "VIST": 0.0},
    )
    actions = {item.symbol: item.action for item in advice.decisions}

    assert actions == {"YPFD": "SELL", "GGAL": "HOLD", "VIST": "BUY"}


def test_keep_portfolio_is_just_a_helper_around_the_builder() -> None:
    view = build_context(cash=40_000.0, positions=[("YPFD", 60_000.0)])

    advice = keep_portfolio(view)

    assert advice.target_cash_weight == pytest.approx(0.40)
    assert advice.decisions[0].target_weight == pytest.approx(0.60)
