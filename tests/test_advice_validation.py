"""Tests for validating advice against the account: the closed portfolio.

The representative portfolio is the one from the design notes: 100,000 split
into 40% cash, 25% YPFD, 20% GGAL, 15% AL30, rebalanced into a target that
also buys VIST and SPY. The rule that matters most is that the target is a
redistribution of that same money: no new capital, nothing forgotten, nothing
invented.
"""

from __future__ import annotations

import pytest

from app.domain.analysis import (
    AssetAdvice,
    PortfolioAdvice,
    allocation_delta,
)
from app.domain.universe import (
    Instrument,
    UniverseConfig,
    get_universe,
)
from app.services.advice_validation import (
    PortfolioAdviceValidationError,
    validate_portfolio_advice,
)

#: The portfolio the design notes use, as shares of 100,000.
HELD = {"YPFD", "GGAL", "AL30"}
CURRENT_WEIGHTS = {"YPFD": 0.25, "GGAL": 0.20, "AL30": 0.15}
#: Cash is the rest: 0.40.
CASH_WEIGHT = 0.40


def universe(*instruments: Instrument) -> UniverseConfig:
    return UniverseConfig(instruments=tuple(instruments))


def small_universe() -> UniverseConfig:
    return universe(
        Instrument("YPFD", "YPF S.A.", "argentina_stocks"),
        Instrument("GGAL", "Grupo Financiero Galicia S.A.", "argentina_stocks"),
        Instrument("AL30", "Bonar 2030", "bonds"),
        Instrument("VIST", "Vista Energy S.A.", "argentina_stocks"),
        Instrument("SPY", "SPDR S&P 500 ETF Trust", "cedears"),
    )


def decision(symbol: str, action: str, target: float, **overrides: object) -> AssetAdvice:
    payload: dict = {
        "symbol": symbol,
        "action": action,
        "target_weight": target,
        "confidence": 0.6,
        "rationale": "A test reason.",
    }
    payload.update(overrides)
    return AssetAdvice.model_validate(payload)


def advice(*decisions: AssetAdvice, **overrides: object) -> PortfolioAdvice:
    payload: dict = {
        "summary": "A test summary.",
        "target_cash_weight": 0.10,
        "decisions": [item.model_dump() for item in decisions],
        "model_provider": "test",
        "model_name": "test-model",
        "analysis_version": "test-1",
    }
    payload.update(overrides)
    return PortfolioAdvice.model_validate(payload)


def representative_advice() -> PortfolioAdvice:
    """The design-notes rebalance: 10% cash and six instruments at 110% - 10%."""
    return advice(
        decision("YPFD", "SELL", 0.20),
        decision("GGAL", "SELL", 0.10),
        decision("AL30", "HOLD", 0.15),
        decision("VIST", "BUY", 0.20),
        decision("SPY", "BUY", 0.25),
        target_cash_weight=0.10,
    )


# --- The representative rebalance ----------------------------------------


def test_the_representative_rebalance_is_accepted() -> None:
    plan = representative_advice()

    result = validate_portfolio_advice(
        plan,
        held_symbols=HELD,
        universe=small_universe(),
        current_weights=CURRENT_WEIGHTS,
        min_cash_pct=0.10,
    )

    assert result is plan


def test_the_representative_allocation_is_closed() -> None:
    plan = representative_advice()

    assert plan.total_instrument_weight + plan.target_cash_weight == pytest.approx(1.0)
    assert plan.total_instrument_weight == pytest.approx(0.90)
    assert plan.target_cash_weight == pytest.approx(0.10)


def test_every_current_holding_is_represented() -> None:
    plan = representative_advice()

    assert HELD <= {item.symbol for item in plan.decisions}


def test_the_new_symbols_are_allowed_by_the_shipped_universe() -> None:
    # VIST and SPY are new positions; they may only come from the watchlist.
    config = get_universe()
    assert config.find("VIST") is not None
    assert config.find("SPY") is not None

    validate_portfolio_advice(
        representative_advice(),
        held_symbols=HELD,
        universe=config,
        current_weights=CURRENT_WEIGHTS,
        min_cash_pct=0.10,
    )


def test_no_new_money_is_assumed() -> None:
    plan = representative_advice()

    for item in plan.decisions:
        delta = allocation_delta(item.target_weight, CURRENT_WEIGHTS.get(item.symbol, 0.0))
        assert isinstance(delta, float)
    # 0.90 into instruments and 0.10 in cash: the same 100% as before.
    assert plan.total_instrument_weight + plan.target_cash_weight == 1.0


def test_a_zero_target_weight_is_a_complete_exit() -> None:
    plan = advice(
        decision("YPFD", "SELL", 0.0),
        decision("GGAL", "HOLD", 0.20),
        decision("AL30", "HOLD", 0.15),
        decision("VIST", "BUY", 0.25),
        target_cash_weight=0.40,
    )

    validate_portfolio_advice(
        plan,
        held_symbols=HELD,
        universe=small_universe(),
        current_weights=CURRENT_WEIGHTS,
        min_cash_pct=0.10,
    )
    assert plan.decisions[0].target_weight == 0.0


def test_the_differences_translate_into_sells_and_buys() -> None:
    plan = representative_advice()
    deltas = {
        item.symbol: allocation_delta(item.target_weight, CURRENT_WEIGHTS.get(item.symbol, 0.0))
        for item in plan.decisions
    }

    assert deltas["YPFD"] == pytest.approx(-0.05)  # sell a fifth of the position
    assert deltas["GGAL"] == pytest.approx(-0.10)  # sell half of the position
    assert deltas["AL30"] == pytest.approx(0.0)  # keep as it is
    assert deltas["VIST"] == pytest.approx(0.20)  # open with sale proceeds
    assert deltas["SPY"] == pytest.approx(0.25)  # open with sale proceeds
    # What is sold plus the freed cash is exactly what is bought.
    assert -sum(v for v in deltas.values() if v < 0) + CASH_WEIGHT - 0.10 == pytest.approx(
        sum(v for v in deltas.values() if v > 0)
    )


def test_the_advice_carries_no_order_fields() -> None:
    fields = set(AssetAdvice.model_fields)
    for forbidden in ("quantity", "shares", "order_size", "notional", "limit_price"):
        assert forbidden not in fields


# --- Invalid advice -------------------------------------------------------


@pytest.mark.parametrize("cash", [0.20, 0.0])
def test_an_allocation_that_does_not_add_up_is_rejected(cash: float) -> None:
    # 0.20 cash plus 0.90 in instruments is 110%; 0.0 cash is 90%.
    plan = advice(
        decision("YPFD", "HOLD", 0.25),
        decision("GGAL", "HOLD", 0.20),
        decision("AL30", "HOLD", 0.15),
        decision("VIST", "BUY", 0.15),
        decision("SPY", "BUY", 0.15),
        target_cash_weight=cash,
    )

    with pytest.raises(PortfolioAdviceValidationError, match="add up to 1.0"):
        validate_portfolio_advice(
            plan,
            held_symbols=HELD,
            universe=small_universe(),
            current_weights=CURRENT_WEIGHTS,
            min_cash_pct=0.10,
        )


def test_cash_below_the_minimum_is_rejected() -> None:
    plan = advice(
        decision("YPFD", "HOLD", 0.25),
        decision("GGAL", "HOLD", 0.20),
        decision("AL30", "HOLD", 0.15),
        decision("VIST", "BUY", 0.20),
        decision("SPY", "BUY", 0.19),
        target_cash_weight=0.01,
    )

    with pytest.raises(PortfolioAdviceValidationError, match="below the minimum"):
        validate_portfolio_advice(
            plan,
            held_symbols=HELD,
            universe=small_universe(),
            current_weights=CURRENT_WEIGHTS,
            min_cash_pct=0.10,
        )


def test_a_missing_current_holding_is_rejected() -> None:
    plan = advice(
        decision("YPFD", "HOLD", 0.25),
        decision("AL30", "HOLD", 0.15),
        decision("VIST", "BUY", 0.45),
        target_cash_weight=0.15,
    )

    with pytest.raises(PortfolioAdviceValidationError, match="missing: GGAL"):
        validate_portfolio_advice(
            plan,
            held_symbols=HELD,
            universe=small_universe(),
            current_weights=CURRENT_WEIGHTS,
            min_cash_pct=0.10,
        )


def test_an_unknown_new_symbol_is_rejected() -> None:
    plan = advice(
        decision("YPFD", "HOLD", 0.25),
        decision("GGAL", "HOLD", 0.20),
        decision("AL30", "HOLD", 0.15),
        decision("MSFT", "BUY", 0.30),
        target_cash_weight=0.10,
    )

    with pytest.raises(PortfolioAdviceValidationError, match="MSFT is unknown"):
        validate_portfolio_advice(
            plan,
            held_symbols=HELD,
            universe=small_universe(),
            current_weights=CURRENT_WEIGHTS,
            min_cash_pct=0.10,
        )


def test_a_sell_on_an_instrument_that_is_not_held_is_rejected() -> None:
    plan = advice(
        decision("YPFD", "HOLD", 0.25),
        decision("GGAL", "HOLD", 0.20),
        decision("AL30", "HOLD", 0.15),
        decision("VIST", "SELL", 0.30),
        target_cash_weight=0.10,
    )

    with pytest.raises(PortfolioAdviceValidationError, match="VIST cannot be sold"):
        validate_portfolio_advice(
            plan,
            held_symbols=HELD,
            universe=small_universe(),
            current_weights=CURRENT_WEIGHTS,
            min_cash_pct=0.10,
        )


def test_a_sell_that_increases_the_target_weight_is_rejected() -> None:
    plan = advice(
        decision("YPFD", "SELL", 0.30),  # says sell, allocates more
        decision("GGAL", "HOLD", 0.20),
        decision("AL30", "HOLD", 0.15),
        decision("VIST", "BUY", 0.25),
        target_cash_weight=0.10,
    )

    with pytest.raises(PortfolioAdviceValidationError, match="implies BUY"):
        validate_portfolio_advice(
            plan,
            held_symbols=HELD,
            universe=small_universe(),
            current_weights=CURRENT_WEIGHTS,
            min_cash_pct=0.10,
        )


def test_a_buy_that_reduces_the_target_weight_is_rejected() -> None:
    plan = advice(
        decision("YPFD", "BUY", 0.15),  # says buy, allocates less
        decision("GGAL", "HOLD", 0.20),
        decision("AL30", "HOLD", 0.15),
        decision("VIST", "BUY", 0.40),
        target_cash_weight=0.10,
    )

    with pytest.raises(PortfolioAdviceValidationError, match="implies SELL"):
        validate_portfolio_advice(
            plan,
            held_symbols=HELD,
            universe=small_universe(),
            current_weights=CURRENT_WEIGHTS,
            min_cash_pct=0.10,
        )


def test_a_hold_that_moves_the_weight_is_rejected() -> None:
    plan = advice(
        decision("YPFD", "HOLD", 0.15),  # says hold, moves by 10 points
        decision("GGAL", "HOLD", 0.20),
        decision("AL30", "HOLD", 0.15),
        decision("VIST", "BUY", 0.40),
        target_cash_weight=0.10,
    )

    with pytest.raises(PortfolioAdviceValidationError, match="implies SELL"):
        validate_portfolio_advice(
            plan,
            held_symbols=HELD,
            universe=small_universe(),
            current_weights=CURRENT_WEIGHTS,
            min_cash_pct=0.10,
        )


def test_a_small_move_within_the_tolerance_is_still_a_hold() -> None:
    plan = advice(
        decision("YPFD", "HOLD", 0.252),  # 0.2 points is not worth trading
        decision("GGAL", "HOLD", 0.20),
        decision("AL30", "HOLD", 0.15),
        decision("VIST", "BUY", 0.15),
        decision("SPY", "BUY", 0.14),
        target_cash_weight=0.108,
    )

    validate_portfolio_advice(
        plan,
        held_symbols=HELD,
        universe=small_universe(),
        current_weights=CURRENT_WEIGHTS,
        min_cash_pct=0.10,
    )


# --- Older rules that still hold -----------------------------------------


def test_an_unknown_symbol_is_rejected_with_a_clear_error() -> None:
    plan = advice(
        decision("TSLA", "BUY", 0.20),
        decision("YPFD", "HOLD", 0.25),
        decision("GGAL", "HOLD", 0.20),
        decision("AL30", "HOLD", 0.15),
        target_cash_weight=0.20,
    )

    with pytest.raises(
        PortfolioAdviceValidationError,
        match="unknown: the account does not hold it and the universe does not list it",
    ):
        validate_portfolio_advice(
            plan,
            held_symbols=HELD,
            universe=small_universe(),
            current_weights=CURRENT_WEIGHTS,
            min_cash_pct=0.10,
        )


def test_held_symbols_may_arrive_in_any_case() -> None:
    validate_portfolio_advice(
        representative_advice(),
        held_symbols={"ypfd", "ggal", "al30"},
        universe=small_universe(),
        current_weights={"ypfd": 0.25, "ggal": 0.20, "al30": 0.15},
        min_cash_pct=0.10,
    )


def test_the_error_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        validate_portfolio_advice(
            advice(decision("MSFT", "BUY", 1.0), target_cash_weight=0.0),
            held_symbols=set(),
            universe=small_universe(),
            current_weights={},
            min_cash_pct=0.0,
        )
