"""DecisionPolicy: fixed sizing and fixed risk checks.

The strategy chooses an action. This module decides whether that action becomes
an order, and how big. Both steps are plain Python with no inputs other than the
ledger, the evidence, and the constants in :mod:`app.domain.trading`. An
optional analysis component cannot reach either step.

Sizing:

* A buy targets ``MAX_ORDER_WEIGHT`` of the portfolio, but never more cash than
  the account holds after ``CASH_BUFFER``, and never past ``MAX_POSITION_WEIGHT``.
* A sell reduces a position above ``MAX_POSITION_WEIGHT`` back toward it, and
  otherwise sells a slice of ``MAX_ORDER_WEIGHT``. It never sells more than the
  ledger holds.
* A hold is a quantity of zero.

Every risk check must pass before a recommendation counts as an order. A failed
check zeroes the quantity. The recommendation is still saved, with the failing
check visible, so the review page shows what was blocked and why.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from app.domain.money import Money, quantity
from app.domain.strategy import decide
from app.domain.trading import (
    CASH_BUFFER,
    MAX_ORDER_WEIGHT,
    MAX_POSITION_WEIGHT,
    MAX_TURNOVER,
    IndicatorEvidence,
    PaperLedger,
    Recommendation,
    RiskCheck,
)

#: Instruments the strategy is allowed to trade. Anything else is held and
#: reported as blocked, never silently skipped.
DEFAULT_ALLOWLIST: frozenset[str] = frozenset({"GGAL", "YPFD"})


def size_and_check(
    *,
    symbol: str,
    evidence: IndicatorEvidence,
    ledger: PaperLedger,
    allowlist: frozenset[str] = DEFAULT_ALLOWLIST,
    spent: Money | None = None,
) -> Recommendation:
    """Turn one strategy decision into a sized, risk-checked recommendation.

    ``spent`` is the notional already committed by earlier recommendations in
    the same run, so the turnover cap applies to the whole plan.
    """
    decision = decide(evidence)
    portfolio_value = ledger.total_value
    price = evidence.last_price
    held = ledger.position_for(symbol)
    held_quantity = held.quantity if held else quantity(0)
    already_spent = spent or Money(Decimal("0.00"))
    current_weight = _current_weight(price, held_quantity, portfolio_value)

    if decision.action == "HOLD" or price is None or not price.is_positive:
        return Recommendation(
            symbol=symbol,
            action="HOLD",
            quantity=quantity(0),
            limit_price=price,
            notional=None,
            weight_after=current_weight,
            rationale=decision.rationale,
            evidence=evidence,
            risk_checks=[
                RiskCheck(
                    name="no_order",
                    passed=True,
                    detail="No order is proposed, so no risk limit applies.",
                )
            ],
        )

    target = _target_quantity(
        decision.action, price, portfolio_value, held_quantity, ledger.cash
    )
    notional = price * target
    weight_after = _weight_after(
        decision.action, held_quantity, target, price, portfolio_value
    )
    checks = _risk_checks(
        symbol=symbol,
        action=decision.action,
        target=target,
        notional=notional,
        weight_after=weight_after,
        price=price,
        portfolio_value=portfolio_value,
        cash=ledger.cash,
        held_quantity=held_quantity,
        already_spent=already_spent,
        allowlist=allowlist,
    )
    blocked = any(not check.passed for check in checks)

    return Recommendation(
        symbol=symbol,
        action=decision.action,
        quantity=quantity(0) if blocked else target,
        limit_price=price,
        notional=None if blocked else notional,
        weight_after=weight_after,
        rationale=decision.rationale,
        evidence=evidence,
        risk_checks=checks,
    )


def _current_weight(
    price: Money | None, held_quantity: Decimal, portfolio_value: Money
) -> float | None:
    """Weight of an existing position, as a fraction, when it can be measured."""
    if price is None or portfolio_value.is_zero:
        return None
    return float((price * held_quantity) / portfolio_value)


def _target_quantity(
    action: str,
    price: Money,
    portfolio_value: Money,
    held_quantity: Decimal,
    cash: Money,
) -> Decimal:
    """The share quantity the sizing rules want, before risk checks."""
    order_budget = portfolio_value * MAX_ORDER_WEIGHT
    position_budget = portfolio_value * MAX_POSITION_WEIGHT

    if action == "BUY":
        room = position_budget - price * held_quantity
        if room.amount <= 0:
            return quantity(0)
        spendable = min(order_budget, room, cash * (1 - CASH_BUFFER))
        if spendable.amount <= 0:
            return quantity(0)
        # Round down so the order never costs more than the budget allows.
        shares = spendable.amount / price.amount
        return quantity(shares.to_integral_value(rounding=ROUND_DOWN))

    # A sell trims the excess over the position cap, or a single order slice,
    # whichever is smaller, and never more than the ledger holds.
    held_value = price * held_quantity
    excess = held_value - position_budget
    sell_value = excess if excess.amount > 0 else min(order_budget, held_value)
    sell_value = min(sell_value, held_value)
    if sell_value.amount <= 0:
        return quantity(0)
    shares = sell_value.amount / price.amount
    return quantity(min(shares.to_integral_value(rounding=ROUND_DOWN), held_quantity))


def _weight_after(
    action: str,
    held_quantity: Decimal,
    target: Decimal,
    price: Money,
    portfolio_value: Money,
) -> float | None:
    """The position's weight once the order fills, as a fraction."""
    if portfolio_value.is_zero:
        return None
    if action == "BUY":
        resulting = held_quantity + target
    else:
        resulting = held_quantity - target
    return float((price * resulting) / portfolio_value)


def _risk_checks(
    *,
    symbol: str,
    action: str,
    target: Decimal,
    notional: Money,
    weight_after: float | None,
    price: Money,
    portfolio_value: Money,
    cash: Money,
    held_quantity: Decimal,
    already_spent: Money,
    allowlist: frozenset[str],
) -> list[RiskCheck]:
    """Every rule the order must pass. Order is fixed so the page is stable."""
    checks = [
        _allowlist_check(symbol, allowlist),
        _price_check(price),
        _quantity_check(target),
        _order_weight_check(notional, portfolio_value, action),
        _position_weight_check(weight_after),
        _turnover_check(notional, already_spent, portfolio_value),
    ]
    if action == "BUY":
        checks.append(_cash_check(notional, cash))
    else:
        checks.append(_shares_check(target, held_quantity))
    return checks


def _allowlist_check(symbol: str, allowlist: frozenset[str]) -> RiskCheck:
    allowed = symbol in allowlist
    listed = ", ".join(sorted(allowlist)) or "none"
    return RiskCheck(
        name="allowlist",
        passed=allowed,
        detail=(
            f"{symbol} is on the allowlist."
            if allowed
            else f"{symbol} is not on the allowlist ({listed})."
        ),
    )


def _price_check(price: Money) -> RiskCheck:
    return RiskCheck(
        name="price",
        passed=price.is_positive,
        detail=f"Limit price is {price}." if price.is_positive else "No usable price.",
    )


def _quantity_check(target: Decimal) -> RiskCheck:
    return RiskCheck(
        name="quantity",
        passed=target > 0,
        detail=(
            f"Sized at {target} shares."
            if target > 0
            else "Sizing produced no shares, so nothing is ordered."
        ),
    )


def _order_weight_check(
    notional: Money, portfolio_value: Money, action: str
) -> RiskCheck:
    if action == "SELL":
        # A sell lowers exposure, so the per-order cap (which guards new
        # exposure) does not apply. The position cap and the shares held bound
        # it instead.
        return RiskCheck(
            name="order_weight",
            passed=True,
            detail="A sell reduces exposure, so the per-order cap does not apply.",
        )
    if portfolio_value.is_zero:
        return RiskCheck(
            name="order_weight",
            passed=False,
            detail="Portfolio value is zero, so no order weight can be measured.",
        )
    weight = notional / portfolio_value
    passed = weight <= MAX_ORDER_WEIGHT
    return RiskCheck(
        name="order_weight",
        passed=passed,
        detail=(
            f"Order is {weight:.2%} of the portfolio, "
            f"within the {MAX_ORDER_WEIGHT:.0%} limit."
            if passed
            else (
                f"Order is {weight:.2%} of the portfolio, "
                f"over the {MAX_ORDER_WEIGHT:.0%} limit."
            )
        ),
    )


def _position_weight_check(weight_after: float | None) -> RiskCheck:
    if weight_after is None:
        return RiskCheck(
            name="position_weight",
            passed=False,
            detail="Position weight could not be measured.",
        )
    limit = float(MAX_POSITION_WEIGHT)
    # A tiny tolerance, so a position landed exactly on the limit passes.
    passed = weight_after <= limit + 0.001
    return RiskCheck(
        name="position_weight",
        passed=passed,
        detail=(
            f"Position would be {weight_after:.2%} of the portfolio, "
            f"within the {limit:.0%} limit."
            if passed
            else (
                f"Position would be {weight_after:.2%} of the portfolio, "
                f"over the {limit:.0%} limit."
            )
        ),
    )


def _turnover_check(
    notional: Money, already_spent: Money, portfolio_value: Money
) -> RiskCheck:
    if portfolio_value.is_zero:
        return RiskCheck(
            name="turnover",
            passed=False,
            detail="Portfolio value is zero, so turnover cannot be measured.",
        )
    total = notional + already_spent
    ratio = total / portfolio_value
    passed = ratio <= MAX_TURNOVER
    return RiskCheck(
        name="turnover",
        passed=passed,
        detail=(
            f"Run turnover would be {ratio:.2%}, within the {MAX_TURNOVER:.0%} limit."
            if passed
            else f"Run turnover would be {ratio:.2%}, over the {MAX_TURNOVER:.0%} limit."
        ),
    )


def _cash_check(notional: Money, cash: Money) -> RiskCheck:
    spendable = cash * (1 - CASH_BUFFER)
    passed = notional <= spendable
    return RiskCheck(
        name="cash",
        passed=passed,
        detail=(
            f"Order costs {notional}, within the {spendable} available."
            if passed
            else f"Order costs {notional}, above the {spendable} available."
        ),
    )


def _shares_check(target: Decimal, held_quantity: Decimal) -> RiskCheck:
    passed = target <= held_quantity
    return RiskCheck(
        name="shares",
        passed=passed,
        detail=(
            f"Selling {target} of the {held_quantity} shares held."
            if passed
            else f"Selling {target} exceeds the {held_quantity} shares held."
        ),
    )
