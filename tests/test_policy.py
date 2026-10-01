"""Tests for sizing and the fixed risk checks."""

from __future__ import annotations

from decimal import Decimal

from app.domain.money import money, quantity
from app.domain.policy import DEFAULT_ALLOWLIST, size_and_check
from app.domain.trading import (
    IndicatorEvidence,
    PaperLedger,
    PaperPosition,
    Recommendation,
)


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


def check_named(item: Recommendation, name: str):
    return next(check for check in item.risk_checks if check.name == name)


# --- Holds ----------------------------------------------------------------


def test_a_hold_proposes_no_order() -> None:
    # A flat trend is neither an uptrend nor a downtrend, so the strategy holds.
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(fast=100.0, slow=100.0),
        ledger=ledger(),
    )
    assert item.action == "HOLD"
    assert item.quantity == 0
    assert item.is_order is False
    assert check_named(item, "no_order").passed


# --- Buys -----------------------------------------------------------------


def test_a_buy_is_sized_within_the_order_and_cash_limits() -> None:
    item = size_and_check(symbol="GGAL", evidence=evidence(), ledger=ledger())
    assert item.action == "BUY"
    assert item.quantity == Decimal("100.0000")  # 10% of 100000 at 100.00
    assert item.notional == money("10000.00")
    assert item.is_order is True
    assert check_named(item, "cash").passed
    assert check_named(item, "order_weight").passed
    assert check_named(item, "position_weight").passed


def test_a_buy_is_blocked_when_no_shares_are_affordable() -> None:
    item = size_and_check(symbol="GGAL", evidence=evidence(), ledger=ledger(cash="5.00"))
    assert item.action == "BUY"
    assert item.quantity == 0
    assert item.is_order is False
    assert not check_named(item, "quantity").passed


def test_a_buy_is_blocked_by_the_turnover_cap() -> None:
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(),
        ledger=ledger(),
        spent=money("15000.00"),
    )
    assert item.quantity == 0
    assert item.is_order is False
    assert not check_named(item, "turnover").passed


def test_a_buy_is_blocked_when_the_symbol_is_not_allowed() -> None:
    item = size_and_check(symbol="AAPL", evidence=evidence(), ledger=ledger())
    assert item.action == "BUY"
    assert item.quantity == 0
    assert item.is_order is False
    assert not check_named(item, "allowlist").passed


def test_position_room_limits_a_buy_that_would_breach_the_position_cap() -> None:
    # The position is 24% of a 100000 portfolio, so only 1% of room is left,
    # which is less than the 10% order budget.
    held = position(held="240", price="100.00")
    book = ledger(cash="76000.00", positions=[held])  # value = 100000
    item = size_and_check(symbol="GGAL", evidence=evidence(), ledger=book)
    # 1% of 100000 is 1000, at 100.00 that is ten shares.
    assert item.quantity == Decimal("10.0000")
    assert item.is_order is True


# --- Sells ----------------------------------------------------------------


def test_a_sell_that_trims_an_oversized_position_targets_the_cap() -> None:
    # The position is 28% of a 100000 portfolio; the excess over the 25% cap is
    # trimmed, which is thirty shares at 100.00.
    held = position(held="280", price="100.00")
    book = ledger(cash="72000.00", positions=[held])  # value = 100000
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(fast=90.0, slow=100.0, rsi=40.0, hist=-1.0),
        ledger=book,
    )
    assert item.action == "SELL"
    assert item.quantity == Decimal("30.0000")
    assert item.weight_after is not None
    assert abs(item.weight_after - 0.25) < 0.001
    assert item.is_order is True


def test_a_sell_of_a_normal_position_takes_one_order_slice() -> None:
    # The position is 2% of the portfolio, well under the 25% cap, so the sell
    # takes a single 10% order slice instead of trimming an excess.
    held = position(held="20", price="100.00")
    book = ledger(cash="98000.00", positions=[held])  # value = 100000
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(fast=90.0, slow=100.0, rsi=40.0, hist=-1.0),
        ledger=book,
    )
    # 10% of 100000 is 10000, capped at the position value of 2000: 20 shares.
    assert item.quantity == Decimal("20.0000")
    assert item.is_order is True


def test_a_sell_is_blocked_by_the_allowlist() -> None:
    held = position(symbol="TSLA", held="50", price="100.00")
    book = ledger(cash="0.00", positions=[held])
    item = size_and_check(
        symbol="TSLA",
        evidence=evidence(fast=90.0, slow=100.0, rsi=40.0, hist=-1.0),
        ledger=book,
        allowlist=frozenset({"GGAL"}),
    )
    assert item.action == "SELL"
    assert item.quantity == 0
    assert not check_named(item, "allowlist").passed


def test_a_sell_never_exceeds_the_shares_held() -> None:
    held = position(held="5", price="100.00")
    book = ledger(cash="95000.00", positions=[held])  # value = 100000
    item = size_and_check(
        symbol="GGAL",
        evidence=evidence(fast=90.0, slow=100.0, rsi=40.0, hist=-1.0),
        ledger=book,
    )
    assert item.quantity <= Decimal("5.0000")
    assert check_named(item, "shares").passed
