"""Tests for sizing and the fixed risk checks.

The portfolio constraints come from the universe configuration, so these tests
are about the rules themselves: how big an order is, and which limit refuses it.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.money import money, quantity
from app.domain.policy import PlanState, advance, plan_risk_checks, size_and_check
from app.domain.trading import (
    IndicatorEvidence,
    PaperLedger,
    PaperPosition,
    Recommendation,
)
from app.domain.universe import PortfolioConstraints


def ledger(cash: str = "100000.00", positions=None, currency="Peso_Argentino") -> PaperLedger:
    return PaperLedger(
        country="argentina",
        currency=currency,
        cash=money(cash),
        positions=positions or [],
    )


def position(symbol="GGAL", held="100", price="100.00") -> PaperPosition:
    return PaperPosition(
        symbol=symbol,
        quantity=quantity(held),
        average_price=money(price),
        last_price=money(price),
        market="BCBA",
    )


def evidence(price="100.00", fast=110.0, slow=100.0, rsi=55.0, hist=1.0) -> IndicatorEvidence:
    return IndicatorEvidence(
        last_price=money(price),
        ema_fast=fast,
        ema_slow=slow,
        rsi=rsi,
        macd=2.0,
        macd_signal=1.0,
        macd_histogram=hist,
        observations=60,
    )


def sell_evidence(price="100.00") -> IndicatorEvidence:
    """A downtrend with faded momentum, which is what the strategy sells on."""
    return evidence(price=price, fast=90.0, slow=100.0, rsi=40.0, hist=-1.0)


def check_named(item: Recommendation, name: str):
    return next(check for check in item.risk_checks if check.name == name)


CONSTRAINTS = PortfolioConstraints()


# --- Holds ----------------------------------------------------------------


def test_a_hold_proposes_no_order() -> None:
    # A flat trend is neither an uptrend nor a downtrend, so the strategy holds.
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(fast=100.0, slow=100.0),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        is_finalist=True,
    )
    assert item.action == "HOLD"
    assert item.quantity == 0
    assert item.is_order is False
    assert check_named(item, "no_order").passed


def test_a_recommendation_carries_its_paper_name() -> None:
    item = size_and_check(
        symbol="GGAL",
        name="Grupo Financiero Galicia S.A.",
        evidence=evidence(fast=100.0, slow=100.0),
        ledger=ledger(),
        constraints=CONSTRAINTS,
    )
    assert item.name == "Grupo Financiero Galicia S.A."


# --- Buys -----------------------------------------------------------------


def test_a_buy_is_sized_to_the_position_limit() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    assert item.action == "BUY"
    # 15% of a 100,000 portfolio at 100.00 a share.
    assert item.quantity == Decimal("150.0000")
    assert item.notional == money("15000.00")
    assert item.is_order is True
    assert check_named(item, "cash_reserve").passed
    assert check_named(item, "position_size").passed
    assert check_named(item, "category_cap").passed
    assert check_named(item, "open_positions").passed


def test_a_buy_tops_up_a_position_it_already_holds() -> None:
    # 100 shares at 100.00 is 10% of a 100,000 portfolio; the limit is 15%.
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(cash="90000.00", positions=[position(held="100")]),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    assert item.quantity == Decimal("50.0000")
    assert item.weight_after == 0.15
    assert item.is_order is True


def test_a_buy_is_blocked_when_no_share_fits_the_budget() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(price="20000.00"),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    assert item.action == "BUY"
    assert item.quantity == 0
    assert item.is_order is False
    assert not check_named(item, "quantity").passed


def test_a_buy_is_blocked_when_the_cash_floor_would_be_breached() -> None:
    # Cash is already close to the 10% floor: a buy would push it under.
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(
            cash="5000.00",
            positions=[position("LOMA", held="1000")],
        ),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    assert item.is_order is False
    assert not check_named(item, "cash_reserve").passed


def test_a_buy_sizes_against_what_the_run_has_not_already_spent() -> None:
    # The run already committed 85,000 of a 100,000 account, so only the cash
    # above the 10% floor is left to spend, not the whole account.
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
        state=PlanState(spent=money("85000.00"), orders_used=1),
    )
    assert item.quantity == Decimal("50.0000")
    assert check_named(item, "cash_reserve").passed


def test_a_buy_is_blocked_when_the_symbol_is_not_a_finalist() -> None:
    item = size_and_check(
        symbol="THIN",
        evidence=evidence(),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=False,
    )
    assert item.action == "BUY"
    assert item.quantity == 0
    assert item.is_order is False
    assert not check_named(item, "finalists").passed
    assert "not among the finalists" in check_named(item, "finalists").detail


def test_a_sell_of_a_held_position_is_allowed_outside_the_finalists() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=sell_evidence(),
        ledger=ledger(positions=[position()]),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=False,
    )
    assert item.action == "SELL"
    assert item.is_order is True
    assert check_named(item, "finalists").passed


def test_the_category_cap_blocks_another_position_of_the_same_kind() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
        state=PlanState(
            open_counts={"argentina_stocks": 4, "cedears": 2}, open_total=6
        ),
    )
    assert item.is_order is False
    assert not check_named(item, "category_cap").passed
    assert check_named(item, "open_positions").passed


def test_the_open_positions_limit_blocks_a_ninth_position() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
        state=PlanState(
            open_counts={"argentina_stocks": 3, "cedears": 3, "bonds": 2},
            open_total=8,
        ),
    )
    assert item.is_order is False
    assert not check_named(item, "open_positions").passed


def test_the_run_trade_limit_blocks_a_fourth_order() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
        state=PlanState(orders_used=3),
    )
    assert item.is_order is False
    assert not check_named(item, "trade_limit").passed


def test_the_limits_are_shown_with_their_values() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    assert "15.0%" in check_named(item, "position_size").detail
    assert "10.0%" in check_named(item, "cash_reserve").detail
    assert "4" in check_named(item, "category_cap").detail


def test_the_plan_checks_describe_where_the_run_leaves_the_account() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    state = advance(PlanState(), item, category="argentina_stocks")

    checks = plan_risk_checks(state, ledger=ledger(), constraints=CONSTRAINTS)
    by_name = {check.name: check for check in checks}

    assert set(by_name) == {"trade_limit", "cash_reserve"}
    assert all(check.passed for check in checks)
    assert "1 of the 3 orders" in by_name["trade_limit"].detail
    assert "85.0%" in by_name["cash_reserve"].detail


# --- Sells ----------------------------------------------------------------


def test_a_sell_trims_an_oversized_position_back_to_the_cap() -> None:
    # 200 shares at 100.00 is 20% of a 100,000 portfolio; the cap is 15%.
    item = size_and_check(
        symbol="GGAL",
        evidence=sell_evidence(),
        ledger=ledger(cash="80000.00", positions=[position(held="200")]),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    assert item.action == "SELL"
    assert item.quantity == Decimal("50.0000")
    assert item.weight_after == 0.15
    assert item.is_order is True


def test_a_sell_of_a_position_inside_the_cap_exits_the_whole_position() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=sell_evidence(),
        ledger=ledger(positions=[position(held="100")]),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    assert item.quantity == Decimal("100.0000")
    assert item.weight_after == 0.0
    assert item.is_order is True


def test_a_sell_never_exceeds_the_shares_held() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=sell_evidence(),
        ledger=ledger(positions=[position(held="1")]),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    assert item.quantity == Decimal("1.0000")
    assert check_named(item, "shares").passed


def test_a_sell_with_nothing_held_is_a_hold() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=sell_evidence(),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        is_finalist=True,
    )
    assert item.action == "HOLD"
    assert item.is_order is False


# --- The run state --------------------------------------------------------


def test_advance_tracks_what_the_run_used() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    assert item.is_order

    state = advance(PlanState(), item, category="argentina_stocks")

    assert state.spent == money("15000.00")
    assert state.traded == money("15000.00")
    assert state.orders_used == 1
    assert state.open_total == 1
    assert state.open_counts == {"argentina_stocks": 1}


def test_advance_ignores_anything_that_is_not_an_order() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(fast=100.0, slow=100.0),
        ledger=ledger(),
        constraints=CONSTRAINTS,
    )
    assert item.is_order is False

    state = advance(PlanState(spent=money("1.00")), item)

    assert state.spent == money("1.00")
    assert state.orders_used == 0
    assert state.open_total == 0


def test_advance_counts_a_sell_as_a_closed_position() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=sell_evidence(),
        ledger=ledger(positions=[position()]),
        constraints=CONSTRAINTS,
        category="argentina_stocks",
        is_finalist=True,
    )
    state = advance(
        PlanState(open_counts={"argentina_stocks": 1}, open_total=1),
        item,
        category="argentina_stocks",
    )

    assert state.orders_used == 1
    assert state.open_total == 0
    assert state.open_counts == {"argentina_stocks": 0}
    # A sell releases cash and still counts as traded value.
    assert state.spent == money("-10000.00")
    assert state.traded == money("10000.00")
