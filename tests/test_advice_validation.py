"""Tests for contextual validation of a portfolio advice.

The models cannot know what the account holds or what the project may trade.
These tests cover the rules that need that context: a symbol must be held or
configured, and only a held position may be sold. Nothing here sizes a trade.
"""

from __future__ import annotations

import pytest

from app.domain.analysis import AssetAdvice, PortfolioAdvice
from app.domain.universe import Instrument, UniverseConfig
from app.services.advice_validation import (
    PortfolioAdviceValidationError,
    validate_portfolio_advice,
)


def universe() -> UniverseConfig:
    """A tiny universe with one share and one bond."""
    return UniverseConfig(
        instruments=(
            Instrument(
                symbol="GGAL",
                name="Grupo Financiero Galicia S.A.",
                category="argentina_stocks",
            ),
            Instrument(symbol="AL30", name="Bonar 2030", category="bonds"),
        )
    )


def decision(symbol: str, action: str) -> AssetAdvice:
    return AssetAdvice.model_validate(
        {
            "symbol": symbol,
            "action": action,
            "confidence": 0.6,
            "rationale": "A test reason.",
        }
    )


def advice(*decisions: AssetAdvice) -> PortfolioAdvice:
    return PortfolioAdvice.model_validate(
        {
            "summary": "A test summary.",
            "decisions": [item.model_dump() for item in decisions],
            "model_provider": "test",
            "model_name": "test-model",
            "analysis_version": "test-1",
        }
    )


# --- SELL -----------------------------------------------------------------


def test_a_sell_of_a_held_symbol_is_accepted() -> None:
    result = validate_portfolio_advice(
        advice(decision("GGAL", "SELL")),
        held_symbols={"GGAL"},
        universe=universe(),
    )

    assert result.decisions[0].action == "SELL"


def test_a_sell_of_a_symbol_the_account_does_not_hold_is_rejected() -> None:
    with pytest.raises(PortfolioAdviceValidationError, match="AL30"):
        validate_portfolio_advice(
            advice(decision("AL30", "SELL")),
            held_symbols={"GGAL"},
            universe=universe(),
        )


# --- BUY ------------------------------------------------------------------


def test_a_buy_of_a_held_symbol_is_accepted() -> None:
    result = validate_portfolio_advice(
        advice(decision("GGAL", "BUY")),
        held_symbols={"GGAL"},
        universe=universe(),
    )

    assert [item.symbol for item in result.decisions] == ["GGAL"]


def test_a_buy_of_a_configured_symbol_is_accepted() -> None:
    result = validate_portfolio_advice(
        advice(decision("AL30", "BUY")),
        held_symbols=set(),
        universe=universe(),
    )

    assert result.decisions[0].symbol == "AL30"


def test_a_buy_of_an_unknown_symbol_is_rejected() -> None:
    with pytest.raises(PortfolioAdviceValidationError, match="MSFT"):
        validate_portfolio_advice(
            advice(decision("MSFT", "BUY")),
            held_symbols={"GGAL"},
            universe=universe(),
        )


# --- HOLD -----------------------------------------------------------------


def test_a_hold_of_a_held_symbol_is_accepted() -> None:
    result = validate_portfolio_advice(
        advice(decision("GGAL", "HOLD")),
        held_symbols={"GGAL"},
        universe=universe(),
    )

    assert result.decisions[0].action == "HOLD"


def test_a_hold_of_a_configured_symbol_is_accepted() -> None:
    result = validate_portfolio_advice(
        advice(decision("AL30", "HOLD")),
        held_symbols=set(),
        universe=universe(),
    )

    assert result.decisions[0].symbol == "AL30"


# --- Unknown symbols and input handling -----------------------------------


def test_an_unknown_symbol_is_rejected_with_a_clear_error() -> None:
    with pytest.raises(
        PortfolioAdviceValidationError,
        match="unknown: the account does not hold it and the universe does not list it",
    ):
        validate_portfolio_advice(
            advice(decision("TSLA", "HOLD")),
            held_symbols=set(),
            universe=universe(),
        )


def test_held_symbols_may_arrive_in_any_case() -> None:
    result = validate_portfolio_advice(
        advice(decision("GGAL", "SELL")),
        held_symbols={"ggal"},
        universe=universe(),
    )

    assert result.decisions[0].symbol == "GGAL"


def test_advice_that_needs_no_action_is_returned_unchanged() -> None:
    plan = advice(decision("GGAL", "HOLD"), decision("AL30", "BUY"))

    result = validate_portfolio_advice(
        plan, held_symbols={"GGAL"}, universe=universe()
    )

    assert result is plan


def test_the_error_is_a_value_error() -> None:
    # Callers that only know ValueError still catch a bad advice.
    with pytest.raises(ValueError):
        validate_portfolio_advice(
            advice(decision("NOPE", "BUY")),
            held_symbols=set(),
            universe=universe(),
        )
