"""Tests for the deterministic rebalance: target allocation -> exact trades.

The model's weights are the input; these tests pin the arithmetic that turns
them into quantities: whole units, sells before buys, self-financing, and never
spending money the ledger does not have.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.money import money
from app.domain.trading import PaperLedger, PaperPosition
from app.domain.universe import Instrument, PortfolioConstraints, UniverseConfig
from app.infrastructure.simulation import advice_from_targets
from app.services.rebalance import RebalanceError, plan_rebalance

UNIVERSE = UniverseConfig(
    instruments=(
        Instrument("GGAL", "Grupo Financiero Galicia S.A.", "argentina_stocks"),
        Instrument("YPFD", "YPF S.A.", "argentina_stocks"),
        Instrument("VIST", "Vista Energy S.A.", "argentina_stocks"),
        Instrument("SPY", "SPDR S&P 500 ETF Trust", "cedears"),
    ),
    constraints=PortfolioConstraints(
        max_open_positions=8,
        max_argentina_stock_positions=4,
        max_cedear_positions=3,
        max_single_position_pct=Decimal("0.50"),
        min_cash_pct=Decimal("0.10"),
        max_trades_per_run=3,
    ),
)

PRICES = {
    "GGAL": money("60.00"),
    "YPFD": money("1200.00"),
    "VIST": money("100.00"),
    "SPY": money("200.00"),
}


def ledger(cash: str, positions: list[tuple[str, str, str]] | None = None) -> PaperLedger:
    """A paper account from ``(symbol, quantity, price)`` rows plus cash."""
    return PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money(cash),
        positions=[
            PaperPosition(
                symbol=symbol,
                quantity=Decimal(quantity),
                average_price=money(price),
                last_price=money(price),
                market="BCBA",
            )
            for symbol, quantity, price in (positions or [])
        ],
    )


def plan(*, advice, account: PaperLedger, prices=None):
    return plan_rebalance(
        advice=advice,
        ledger=account,
        universe=UNIVERSE,
        prices=prices if prices is not None else PRICES,
    )


def test_a_target_allocation_becomes_exact_whole_unit_trades() -> None:
    # 100 GGAL at 60 (6000) + 10000 cash = 16000. Exit GGAL and buy 40% VIST.
    account = ledger("10000.00", [("GGAL", "100", "60.00")])
    advice = advice_from_targets(
        {"GGAL": 0.0, "VIST": 0.40},
        cash=0.60,
        current={"GGAL": 0.375},
    )

    result = plan(advice=advice, account=account)

    trades = {(row.symbol, row.side): row.quantity for row in result.trades}
    assert trades[("GGAL", "SELL")] == Decimal("100")
    assert trades[("VIST", "BUY")] == Decimal("64")


def test_sells_are_planned_and_applied_before_buys() -> None:
    account = ledger("0.00", [("GGAL", "100", "60.00")])
    advice = advice_from_targets(
        {"GGAL": 0.0, "VIST": 0.45},
        cash=0.55,
        current={"GGAL": 1.0},
    )

    result = plan(advice=advice, account=account)

    sides = [row.side for row in result.trades]
    assert sides == ["SELL", "BUY"]
    # The buy is only affordable because the sell happened first.
    buy = next(row for row in result.trades if row.side == "BUY")
    assert buy.quantity > 0
    assert buy.notional <= money("6000.00")


def test_the_rebalance_is_self_financing() -> None:
    account = ledger("1000.00", [("GGAL", "100", "60.00"), ("YPFD", "5", "1200.00")])
    advice = advice_from_targets(
        {"GGAL": 0.10, "YPFD": 0.70, "VIST": 0.10},
        cash=0.10,
        current={"GGAL": 0.23, "YPFD": 0.69},
    )

    result = plan(advice=advice, account=account)

    assert result.ledger_after.total_value == account.total_value


def test_it_never_sells_more_than_is_held() -> None:
    account = ledger("5000.00", [("GGAL", "3", "60.00")])
    advice = advice_from_targets(
        {"GGAL": 0.0, "VIST": 0.50},
        cash=0.50,
        current={"GGAL": 0.035},
    )

    result = plan(advice=advice, account=account)

    sell = next(row for row in result.trades if row.side == "SELL")
    assert sell.quantity <= Decimal("3")
    after = result.ledger_after.position_for("GGAL")
    assert after is None or after.quantity == Decimal("0")


def test_it_never_buys_with_money_the_ledger_does_not_have() -> None:
    # Cash 200, one held position of 600. The 25% target is 200 but only 120
    # is spendable after the 10% cash floor, so exactly one unit is bought.
    account = ledger("200.00", [("GGAL", "10", "60.00")])
    advice = advice_from_targets(
        {"GGAL": 0.75, "VIST": 0.25},
        cash=0.00,
        current={"GGAL": 0.75},
    )

    result = plan(advice=advice, account=account)

    buy = next(row for row in result.trades if row.side == "BUY")
    assert buy.quantity == Decimal("1")
    assert buy.notional <= money("120.00")
    assert any("cash" in warning.lower() for warning in result.warnings)


def test_the_cash_floor_is_never_spent() -> None:
    account = ledger("1600.00", [])
    advice = advice_from_targets({"VIST": 0.95}, cash=0.05, current={})

    result = plan(advice=advice, account=account)

    assert result.ledger_after.cash >= account.total_value * Decimal("0.10")


def test_rounding_stays_in_cash_rather_than_inventing_money() -> None:
    # 1000 cash, target 55% in VIST at 100.03: not a whole number of units.
    account = ledger("1000.00", [])
    advice = advice_from_targets({"VIST": 0.55}, cash=0.45, current={})
    prices = {"VIST": money("100.03")}
    loose = UniverseConfig(
        instruments=UNIVERSE.instruments,
        constraints=PortfolioConstraints(
            max_single_position_pct=Decimal("1.0"),
            min_cash_pct=Decimal("0.10"),
            max_trades_per_run=3,
        ),
    )

    result = plan_rebalance(advice=advice, ledger=account, universe=loose, prices=prices)

    buy = next(row for row in result.trades if row.side == "BUY")
    assert buy.quantity == Decimal("5")
    assert result.ledger_after.cash > money("450.00")


def test_the_position_cap_is_enforced_with_a_warning() -> None:
    # Total 9600; the 50% cap allows at most 4800 in the new position.
    account = ledger("9000.00", [("GGAL", "10", "60.00")])
    advice = advice_from_targets(
        {"GGAL": 0.10, "VIST": 0.80},
        cash=0.10,
        current={"GGAL": 0.06},
    )

    result = plan(advice=advice, account=account)

    buy = next(row for row in result.trades if row.symbol == "VIST")
    assert buy.target_weight == pytest.approx(0.80)
    assert buy.quantity == Decimal("48")
    assert any(
        "cap" in warning.lower() or "limit" in warning.lower() for warning in result.warnings
    )


def test_the_after_ledger_matches_the_proposed_trades() -> None:
    account = ledger("2000.00", [("GGAL", "100", "60.00")])
    advice = advice_from_targets(
        {"GGAL": 0.20, "VIST": 0.60},
        cash=0.20,
        current={"GGAL": 0.75},
    )

    result = plan(advice=advice, account=account)

    before = {p.symbol: p.quantity for p in result.ledger_before.positions}
    after = {p.symbol: p.quantity for p in result.ledger_after.positions}
    for row in result.trades:
        if row.side == "BUY":
            assert (
                after.get(row.symbol, Decimal("0")) - before.get(row.symbol, Decimal("0"))
                == row.quantity
            )
        else:
            assert (
                before.get(row.symbol, Decimal("0")) - after.get(row.symbol, Decimal("0"))
                == row.quantity
            )


def test_the_trade_limit_marks_the_rest_not_executable() -> None:
    # Two sells and two buys compete for three slots per run.
    account = ledger(
        "1000.00",
        [("GGAL", "100", "60.00"), ("YPFD", "5", "1200.00")],
    )
    advice = advice_from_targets(
        {"GGAL": 0.10, "YPFD": 0.20, "VIST": 0.30, "SPY": 0.30},
        cash=0.10,
        current={"GGAL": 0.30, "YPFD": 0.60},
    )

    result = plan(advice=advice, account=account)

    assert len(result.trades) <= 3
    # The trade that did not fit is a visible NOT EXECUTABLE row, not a
    # silent warning.
    assert result.not_executable
    assert any("trade limit" in row.reason for row in result.not_executable)
    assert all(row.executable is False for row in result.not_executable)


def test_a_hold_never_produces_a_warning() -> None:
    # GGAL sits exactly on target: nothing to trade, nothing to warn about.
    account = ledger("9400.00", [("GGAL", "10", "60.00")])
    advice = advice_from_targets({"GGAL": 0.06}, cash=0.94, current={"GGAL": 0.06})

    result = plan(advice=advice, account=account)

    assert result.trades == []
    assert result.not_executable == []
    assert result.warnings == ()
    assert result.rows[0].is_hold


# --- Quotation factor: bonds quote per 100 nominales ----------------------


BOND_UNIVERSE = UniverseConfig(
    instruments=(Instrument("AL30", "Bonar 2030", "bonds", quotation_factor=100),),
    constraints=PortfolioConstraints(
        max_open_positions=8,
        max_bond_positions=2,
        max_single_position_pct=Decimal("0.50"),
        min_cash_pct=Decimal("0.10"),
        max_trades_per_run=3,
    ),
)


def test_a_bond_is_sized_in_nominales_not_per_quote() -> None:
    # 100.000 cash, target 15% in AL30. The quote is 85.800 per 100 nominales,
    # so one nominal costs 858 and 15% (15.000) buys 17 nominales (~14.586).
    account = ledger("100000.00")
    advice = advice_from_targets({"AL30": 0.15}, cash=0.85, current={})

    result = plan_rebalance(
        advice=advice,
        ledger=account,
        universe=BOND_UNIVERSE,
        prices={"AL30": money("85800.00")},
    )

    row = result.trades[0]
    assert row.symbol == "AL30"
    assert row.quotation_factor == 100
    assert row.unit_cost == money("858.00")
    assert row.quantity == Decimal("17")
    # notional = quoted_price * nominales / 100, NOT price * nominales.
    assert row.notional == money("14586.00")


def test_a_bond_position_is_valued_per_100_nominales() -> None:
    account = PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money("10000.00"),
        positions=[
            PaperPosition(
                symbol="AL30",
                quantity=Decimal("100"),
                average_price=money("80000.00"),
                last_price=money("85000.00"),
                quotation_factor=100,
            )
        ],
    )

    # 100 nominales at 85.000 per 100 = 85.000, not 8.500.000.
    assert account.positions[0].market_value == money("85000.00")
    assert account.total_value == money("95000.00")


def test_bond_minimum_quantity_marks_the_trade_not_executable() -> None:
    # IOL may demand a large minimum nominal (here 1.000). 17 nominales is
    # below it, so the trade is shown NOT EXECUTABLE, not submitted.
    universe = UniverseConfig(
        instruments=(
            Instrument("AL30", "Bonar 2030", "bonds", quotation_factor=100, min_quantity=1000),
        ),
        constraints=BOND_UNIVERSE.constraints,
    )
    account = ledger("100000.00")
    advice = advice_from_targets({"AL30": 0.15}, cash=0.85, current={})

    result = plan_rebalance(
        advice=advice,
        ledger=account,
        universe=universe,
        prices={"AL30": money("85800.00")},
    )

    assert result.trades == []
    blocked = result.not_executable[0]
    assert blocked.symbol == "AL30"
    assert blocked.min_quantity == Decimal("1000")
    assert "minimum" in blocked.reason


def test_stocks_and_cedears_keep_factor_one() -> None:
    account = ledger("10000.00")
    advice = advice_from_targets({"VIST": 0.50}, cash=0.50, current={})

    result = plan(advice=advice, account=account, prices={"VIST": money("100.00")})

    row = result.trades[0]
    assert row.quotation_factor == 1
    assert row.unit_cost == money("100.00")
    assert row.quantity == Decimal("50")
    assert row.notional == money("5000.00")


def test_a_missing_price_leaves_the_symbol_unchanged() -> None:
    account = ledger("5000.00", [("GGAL", "10", "60.00")])
    advice = advice_from_targets(
        {"GGAL": 0.10, "VIST": 0.80},
        cash=0.10,
        current={"GGAL": 0.11},
    )

    result = plan(advice=advice, account=account, prices={})

    assert any("No usable price for VIST" in warning for warning in result.warnings)
    assert result.ledger_after.cash == account.cash


def test_holds_carry_no_order() -> None:
    account = ledger("10000.00", [("GGAL", "10", "60.00")])
    advice = advice_from_targets(
        {"GGAL": 0.06},
        cash=0.94,
        current={"GGAL": 0.06},
    )

    result = plan(advice=advice, account=account)

    assert result.trades == []
    row = result.rows[0]
    assert row.action == "HOLD"
    assert row.side is None
    assert row.quantity == Decimal("0")


def test_an_empty_portfolio_cannot_be_rebalanced() -> None:
    account = ledger("0.00")
    advice = advice_from_targets({"VIST": 1.0}, cash=0.0, current={})

    with pytest.raises(RebalanceError):
        plan(advice=advice, account=account)


def test_an_unverified_quantity_category_marks_the_trade_not_executable() -> None:
    # The shipped bonds are not live-verified: the plan must say so instead of
    # letting the trade be approved and then blocked at submission.
    universe = UniverseConfig(
        instruments=(
            Instrument(
                "AL30",
                "Bonar 2030",
                "bonds",
                quotation_factor=100,
                live_quantity_confirmed=False,
            ),
        ),
        constraints=BOND_UNIVERSE.constraints,
    )
    account = ledger("100000.00")
    advice = advice_from_targets({"AL30": 0.15}, cash=0.85, current={})

    result = plan_rebalance(
        advice=advice,
        ledger=account,
        universe=universe,
        prices={"AL30": money("85800.00")},
    )

    assert result.trades == []
    blocked = result.not_executable[0]
    assert blocked.symbol == "AL30"
    assert "unverified" in blocked.reason


# --- Changes smaller than one unit ----------------------------------------


def test_a_trim_smaller_than_one_unit_is_a_hold_not_a_failure() -> None:
    # 5 YPFD at 1200 (6000) + 10000 cash = 16000. 32% is 5120: 880 less, but one
    # YPFD is worth 1200, so no whole unit can be sold.
    account = ledger("10000.00", [("YPFD", "5", "1200.00")])
    advice = advice_from_targets({"YPFD": 0.32}, cash=0.68, current={"YPFD": 0.375})

    result = plan(advice=advice, account=account)

    assert result.trades == []
    assert result.not_executable == []
    row = result.rows[0]
    assert row.action == "HOLD"
    assert row.executable is True
    assert "within one tradable unit" in row.reason


def test_a_top_up_smaller_than_one_unit_is_a_hold_not_a_failure() -> None:
    # 44% is 7040: 1040 more, less than one YPFD at 1200.
    account = ledger("10000.00", [("YPFD", "5", "1200.00")])
    advice = advice_from_targets({"YPFD": 0.44}, cash=0.56, current={"YPFD": 0.375})

    result = plan(advice=advice, account=account)

    assert result.trades == []
    assert result.not_executable == []
    assert result.rows[0].action == "HOLD"


def test_a_new_position_below_one_unit_is_still_not_executable() -> None:
    # Nothing is held, and 5% of 16000 is 800: less than one YPFD, so it can
    # never be filled and the model must hear about it.
    account = ledger("10000.00", [("GGAL", "100", "60.00")])
    advice = advice_from_targets({"GGAL": 0.375, "YPFD": 0.05}, cash=0.575, current={"GGAL": 0.375})

    result = plan(advice=advice, account=account)

    blocked = {row.symbol: row for row in result.not_executable}
    assert "YPFD" in blocked
    assert "does not fit" in blocked["YPFD"].reason


# --- Feedback for the model --------------------------------------------------


def test_correction_notes_name_each_trade_that_cannot_be_filled() -> None:
    from app.services.rebalance import correction_notes

    # Nothing held, 5% of 16000 is 800: less than one YPFD at 1200.
    account = ledger("10000.00", [("GGAL", "100", "60.00")])
    advice = advice_from_targets({"GGAL": 0.375, "YPFD": 0.05}, cash=0.575, current={"GGAL": 0.375})

    notes = correction_notes(plan(advice=advice, account=account))

    assert len(notes) == 1
    assert notes[0].startswith("YPFD: a target of 5.0% cannot be traded")
    assert "does not fit" in notes[0]


def test_a_plan_that_can_be_filled_needs_no_correction() -> None:
    from app.services.rebalance import correction_notes

    account = ledger("10000.00", [("GGAL", "100", "60.00")])
    advice = advice_from_targets({"GGAL": 0.0, "VIST": 0.40}, cash=0.60, current={"GGAL": 0.375})

    assert correction_notes(plan(advice=advice, account=account)) == []


# --- A target a hair short of one unit --------------------------------------------


def test_a_target_a_few_cents_short_of_one_unit_buys_that_unit() -> None:
    # 12000 in cash. 9.99% is 1198.80, which is 1.20 short of one YPFD at 1200.
    account = ledger("12000.00")
    advice = advice_from_targets({"YPFD": 0.0999}, cash=0.9001, current={})

    result = plan(advice=advice, account=account)

    assert [(row.symbol, row.side, row.quantity) for row in result.trades] == [
        ("YPFD", "BUY", Decimal("1"))
    ]
    assert result.not_executable == []


def test_a_target_clearly_short_of_a_unit_is_still_refused() -> None:
    # 9.5% is 1140: 5% short of one unit, beyond the tolerance.
    account = ledger("12000.00")
    advice = advice_from_targets({"YPFD": 0.095}, cash=0.905, current={})

    result = plan(advice=advice, account=account)

    assert result.trades == []
    assert [row.symbol for row in result.not_executable] == ["YPFD"]
    assert "does not fit" in result.not_executable[0].reason


def test_the_tolerance_is_exactly_one_percent_of_a_unit() -> None:
    from app.services.rebalance import UNIT_ROUNDING_TOLERANCE, _units_wanted

    assert UNIT_ROUNDING_TOLERANCE == Decimal("0.01")
    assert _units_wanted(Decimal("0.99")) == Decimal("1")
    assert _units_wanted(Decimal("0.9899")) == Decimal("0")
    assert _units_wanted(Decimal("2.995")) == Decimal("3")
    assert _units_wanted(Decimal("2.5")) == Decimal("2")  # never rounds to nearest


def test_a_trim_a_hair_short_of_one_unit_sells_that_unit_and_no_more_than_is_held() -> None:
    # 2 YPFD at 1200 plus 9600 cash. 10.01% is 1201.20, so 1198.80 comes off.
    account = ledger("9600.00", [("YPFD", "2", "1200.00")])
    advice = advice_from_targets({"YPFD": 0.1001}, cash=0.8999, current={"YPFD": 0.2})

    result = plan(advice=advice, account=account)

    assert [(row.symbol, row.side, row.quantity) for row in result.trades] == [
        ("YPFD", "SELL", Decimal("1"))
    ]


def test_a_trim_two_percent_short_of_one_unit_is_still_a_hold() -> None:
    # 1176 off a 1200 unit is 98% of one: outside the tolerance.
    account = ledger("9600.00", [("YPFD", "2", "1200.00")])
    advice = advice_from_targets({"YPFD": 0.102}, cash=0.898, current={"YPFD": 0.2})

    result = plan(advice=advice, account=account)

    assert result.trades == []
    assert result.rows[0].action == "HOLD"


def test_the_extra_unit_never_breaks_the_cash_floor() -> None:
    # 1250 in total and a 10% floor: after one 1200 unit only 50 would stay in
    # cash, but the floor is 125. The unit that rounding allowed cannot be paid.
    account = ledger("1250.00")
    advice = advice_from_targets({"YPFD": 0.9599}, cash=0.0401, current={})

    result = plan(advice=advice, account=account)

    assert result.trades == []
    assert [row.symbol for row in result.not_executable] == ["YPFD"]
