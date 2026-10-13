"""Tests for the portfolio-analysis context and the allocation helpers.

The context is what an advisor is shown: the account as one whole, with its
weights, the indicators already calculated, the watchlist, and the limits — and
nothing sensitive. The helpers are the vocabulary of a rebalance: how far a
target sits from the current weight, and what action that move implies.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.analysis import (
    ALLOCATION_TOLERANCE,
    PortfolioAnalysisContext,
    allocation_delta,
    current_weight,
    expected_action,
)
from app.domain.portfolio import Instrument as BrokerInstrument
from app.domain.portfolio import Portfolio, Position
from app.domain.trading import IndicatorEvidence
from app.domain.universe import Instrument, PortfolioConstraints


def test_allocation_delta_is_the_move_to_make() -> None:
    assert allocation_delta(0.20, 0.25) == pytest.approx(-0.05)
    assert allocation_delta(0.20, 0.0) == pytest.approx(0.20)


def test_expected_action_follows_the_move() -> None:
    assert expected_action(0.20, 0.10) == "BUY"
    assert expected_action(0.10, 0.20) == "SELL"
    assert expected_action(0.20, 0.20) == "HOLD"
    # Inside the tolerance nothing is worth trading.
    assert expected_action(0.203, 0.20) == "HOLD"
    assert expected_action(0.2049, 0.20) == "HOLD"
    assert expected_action(0.2070, 0.20) == "BUY"
    assert ALLOCATION_TOLERANCE == pytest.approx(0.005)


def test_current_weight_is_the_share_of_the_portfolio() -> None:
    position = Position(
        instrument=BrokerInstrument(symbol="YPFD"),
        market_value=25_000.0,
    )
    assert current_weight(position, 100_000.0) == pytest.approx(0.25)
    assert current_weight(position, None) == 0.0
    assert current_weight(position, 0.0) == 0.0


# --- PortfolioAnalysisContext ---------------------------------------------


def context() -> PortfolioAnalysisContext:
    portfolio = Portfolio(
        country="argentina",
        currency="Peso_Argentino",
        cash=40_000.0,
        positions=[
            Position(
                instrument=BrokerInstrument(symbol="ypfd", description="YPF S.A."),
                total_quantity=100.0,
                last_price=250.0,
                market_value=25_000.0,
            ),
            Position(
                instrument=BrokerInstrument(symbol="GGAL"),
                total_quantity=200.0,
                last_price=100.0,
                market_value=20_000.0,
            ),
        ],
    )
    return PortfolioAnalysisContext(
        country="argentina",
        analysed_at=datetime(2026, 10, 2, 14, 42, 6, tzinfo=UTC),
        portfolio=portfolio,
        evidence={"YPFD": IndicatorEvidence(observations=60)},
        watchlist=[
            Instrument(symbol="VIST", name="Vista Energy S.A.", category="argentina_stocks"),
            Instrument(symbol="SPY", name="SPDR S&P 500 ETF Trust", category="cedears"),
        ],
        constraints=PortfolioConstraints(),
        warnings=["AL30 history was not refreshed."],
    )


def test_the_context_describes_the_whole_portfolio() -> None:
    view = context()

    assert view.total_value == pytest.approx(85_000.0)
    assert view.cash == pytest.approx(40_000.0)
    assert view.cash_weight == pytest.approx(40_000.0 / 85_000.0)
    assert view.held_symbols == {"YPFD", "GGAL"}
    assert view.weight_of("YPFD") == pytest.approx(25_000.0 / 85_000.0)
    assert view.weight_of("VIST") == 0.0


def test_the_context_carries_the_watchlist_limits_and_warnings() -> None:
    view = context()

    assert [i.symbol for i in view.watchlist] == ["VIST", "SPY"]
    assert view.constraints.max_open_positions == 8
    assert view.warnings == ["AL30 history was not refreshed."]
    assert view.evidence["YPFD"].observations == 60
    assert view.analysed_at.isoformat().startswith("2026-10-02")


def test_the_context_carries_no_credentials() -> None:
    view = context()
    fields = set(vars(view))
    assert fields == {
        "country",
        "analysed_at",
        "portfolio",
        "evidence",
        "watchlist",
        "constraints",
        "warnings",
    }
    for name in ("api_key", "token", "password", "username", "session"):
        assert not hasattr(view, name)
