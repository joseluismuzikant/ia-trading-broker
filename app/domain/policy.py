"""DecisionPolicy: fixed sizing and fixed risk checks.

The analysis funnel picks the finalists and the strategy chooses an action for
each one. This module decides whether that action becomes an order, and how
big, under the portfolio constraints declared in the universe configuration:

* at most ``max_open_positions`` open positions at once,
* at most one cap per category: shares, CEDEARs, and bonds,
* one position never past ``max_single_position_pct`` of the portfolio,
* cash never below ``min_cash_pct`` of the portfolio,
* at most ``max_trades_per_run`` orders in one run, which is what bounds a
  run's turnover instead of a separate turnover cap.

Sizing:

* A buy tops the position up to ``max_single_position_pct`` of the portfolio,
  never spending the cash the account must keep.
* A sell trims a position above ``max_single_position_pct`` back down to it,
  and otherwise exits the whole position the strategy wants out of.
* A hold is a quantity of zero.

Every instrument is either **a finalist of this run** or **held by the ledger**.
A finalist may be bought or sold. A held instrument that is not a finalist is
only ever sold, and only when the strategy says so: a buy outside the finalists
is refused by the ``finalists`` check rather than dropped in silence, so the
review page shows the refusal.

Every risk check must pass before a recommendation counts as an order. A failed
check zeroes the quantity. The recommendation is still saved, with the failing
check visible, so the review page shows what was blocked and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Mapping

from app.domain.money import Money, quantity
from app.domain.strategy import decide
from app.domain.trading import (
    IndicatorEvidence,
    PaperLedger,
    Recommendation,
    RiskCheck,
)
from app.domain.universe import PortfolioConstraints

#: Bucket used for a position whose category the universe does not know.
OTHER_CATEGORY = "other"


@dataclass(frozen=True)
class PlanState:
    """What earlier recommendations in the same run have already used up.

    It is what makes the run-level limits work: the trade count, the position
    caps, and the cash floor all need to know what the plan did before this
    recommendation was considered.
    """

    #: Cash the plan has moved so far: buys spend it, sells release it.
    spent: Money = field(default_factory=lambda: Money(Decimal("0.00")))
    #: Total value traded either way, which is what the summary reports.
    traded: Money = field(default_factory=lambda: Money(Decimal("0.00")))
    open_counts: Mapping[str, int] = field(default_factory=dict)
    open_total: int = 0
    orders_used: int = 0


def size_and_check(
    *,
    symbol: str,
    name: str = "",
    evidence: IndicatorEvidence,
    ledger: PaperLedger,
    constraints: PortfolioConstraints,
    category: str | None = None,
    is_finalist: bool = False,
    state: PlanState | None = None,
) -> Recommendation:
    """Turn one strategy decision into a sized, risk-checked recommendation.

    ``state`` carries what earlier recommendations of the same run already
    spent and opened; :func:`advance` returns the state to pass along after
    this one joins the plan.
    """
    current = state if state is not None else PlanState()
    decision = decide(evidence)
    portfolio_value = ledger.total_value
    price = evidence.last_price
    held = ledger.position_for(symbol)
    held_quantity = held.quantity if held else quantity(0)
    current_weight = _current_weight(price, held_quantity, portfolio_value)

    if price is None or not price.is_positive:
        return _hold(
            symbol=symbol,
            name=name,
            price=price,
            evidence=evidence,
            current_weight=current_weight,
            rationale="No usable price, so the position cannot be sized.",
        )

    # Cash is whatever the account started with, moved by the orders the run
    # has already proposed. A buy must not assume the run's earlier spends and
    # releases did not happen.
    cash_available = ledger.cash - current.spent

    action = decision.action
    if action == "HOLD":
        return _hold(
            symbol=symbol,
            name=name,
            price=price,
            evidence=evidence,
            current_weight=current_weight,
            rationale=decision.rationale,
        )
    if action == "SELL" and not held_quantity:
        return _hold(
            symbol=symbol,
            name=name,
            price=price,
            evidence=evidence,
            current_weight=current_weight,
            rationale="The strategy wants out, but the ledger holds nothing.",
        )

    target = _target_quantity(
        action=action,
        price=price,
        portfolio_value=portfolio_value,
        held_quantity=held_quantity,
        cash=cash_available,
        constraints=constraints,
    )
    notional = price * target
    weight_after = _weight_after(action, held_quantity, target, price, portfolio_value)
    checks = _risk_checks(
        symbol=symbol,
        action=action,
        target=target,
        notional=notional,
        weight_after=weight_after,
        price=price,
        portfolio_value=portfolio_value,
        cash=cash_available,
        held_quantity=held_quantity,
        category=category,
        is_finalist=is_finalist,
        state=current,
        constraints=constraints,
    )
    blocked = any(not check.passed for check in checks)

    return Recommendation(
        symbol=symbol,
        name=name,
        action=action,
        quantity=quantity(0) if blocked else target,
        limit_price=price,
        notional=None if blocked else notional,
        weight_after=weight_after,
        rationale=decision.rationale,
        evidence=evidence,
        risk_checks=checks,
    )


def advance(
    state: PlanState, recommendation: Recommendation, *, category: str | None = None
) -> PlanState:
    """The plan state to use after this recommendation joined the plan."""
    if not recommendation.is_order:
        return state
    bucket = category or OTHER_CATEGORY
    open_counts = dict(state.open_counts)
    open_total = state.open_total
    if recommendation.action == "BUY":
        open_counts[bucket] = open_counts.get(bucket, 0) + 1
        open_total += 1
    else:
        open_counts[bucket] = max(0, open_counts.get(bucket, 0) - 1)
        open_total = max(0, open_total - 1)
    notional = recommendation.notional or Money(Decimal("0.00"))
    moved = notional if recommendation.action == "BUY" else -notional
    return PlanState(
        spent=state.spent + moved,
        traded=state.traded + notional,
        open_counts=open_counts,
        open_total=open_total,
        orders_used=state.orders_used + 1,
    )


def _hold(
    *,
    symbol: str,
    name: str,
    price: Money | None,
    evidence: IndicatorEvidence,
    current_weight: float | None,
    rationale: str,
) -> Recommendation:
    """A recommendation that proposes nothing, with the reason it stays flat."""
    return Recommendation(
        symbol=symbol,
        name=name,
        action="HOLD",
        quantity=quantity(0),
        limit_price=price,
        notional=None,
        weight_after=current_weight,
        rationale=rationale,
        evidence=evidence,
        risk_checks=[
            RiskCheck(
                name="no_order",
                passed=True,
                detail="No order is proposed, so no risk limit applies.",
            )
        ],
    )


def _current_weight(
    price: Money | None, held_quantity: Decimal, portfolio_value: Money
) -> float | None:
    """Weight of an existing position, as a fraction, when it can be measured."""
    if price is None or portfolio_value.is_zero:
        return None
    return float((price * held_quantity) / portfolio_value)


def _target_quantity(
    *,
    action: str,
    price: Money,
    portfolio_value: Money,
    held_quantity: Decimal,
    cash: Money,
    constraints: PortfolioConstraints,
) -> Decimal:
    """The share quantity the sizing rules want, before risk checks."""
    position_budget = portfolio_value * constraints.max_single_position_pct
    cash_floor = portfolio_value * constraints.min_cash_pct

    if action == "BUY":
        room = position_budget - price * held_quantity
        spendable = min(room, cash - cash_floor)
        if spendable.amount <= 0:
            return quantity(0)
        # Round down so the order never costs more than the budget allows.
        shares = spendable.amount / price.amount
        return quantity(shares.to_integral_value(rounding=ROUND_DOWN))

    # A sell trims the excess over the position cap back to it, and otherwise
    # leaves the whole position: the strategy asked for an exit, and a position
    # inside the cap has nothing to trim.
    held_value = price * held_quantity
    excess = held_value - position_budget
    sell_value = excess if excess.amount > 0 else held_value
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
    resulting = held_quantity + target if action == "BUY" else held_quantity - target
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
    category: str | None,
    is_finalist: bool,
    state: PlanState,
    constraints: PortfolioConstraints,
) -> list[RiskCheck]:
    """Every rule the order must pass. Order is fixed so the page is stable."""
    checks = [
        _price_check(price),
        _quantity_check(target),
        _finalists_check(symbol, action, is_finalist),
        _trade_limit_check(action, state, constraints),
    ]
    if action == "BUY":
        checks.append(_position_size_check(weight_after, constraints))
    if action == "BUY":
        checks.append(
            _cash_reserve_check(notional, cash, portfolio_value, constraints)
        )
        checks.append(_category_cap_check(category, state, constraints))
        checks.append(_open_positions_check(state, constraints))
    else:
        checks.append(_shares_check(target, held_quantity))
    return checks


def _price_check(price: Money) -> RiskCheck:
    return RiskCheck(
        name="price",
        passed=price.is_positive,
        detail=(
            "A usable price is saved for this instrument."
            if price.is_positive
            else "No usable price is saved for this instrument."
        ),
    )


def _quantity_check(target: Decimal) -> RiskCheck:
    return RiskCheck(
        name="quantity",
        passed=target > 0,
        detail=(
            "The sizing rules produced a quantity of shares."
            if target > 0
            else "The sizing rules produced a quantity of zero."
        ),
    )


def _finalists_check(symbol: str, action: str, is_finalist: bool) -> RiskCheck:
    """A buy is reserved for the finalists of this run. A sell never is."""
    if action != "BUY" or is_finalist:
        return RiskCheck(
            name="finalists",
            passed=True,
            detail=(
                f"{symbol} is one of the finalists of this run."
                if is_finalist
                else f"{symbol} is not being bought, so the funnel does not matter."
            ),
        )
    return RiskCheck(
        name="finalists",
        passed=False,
        detail=(
            f"{symbol} is not among the finalists of this run, so it cannot be "
            "bought."
        ),
    )


def _trade_limit_check(
    action: str, state: PlanState, constraints: PortfolioConstraints
) -> RiskCheck:
    limit = constraints.max_trades_per_run
    if action == "HOLD":
        return RiskCheck(
            name="trade_limit",
            passed=True,
            detail="No order is proposed, so the run's trade limit does not apply.",
        )
    passed = state.orders_used < limit
    return RiskCheck(
        name="trade_limit",
        passed=passed,
        detail=(
            f"This run has {state.orders_used} of its {limit} orders so far."
            if passed
            else f"This run already used its {limit} orders."
        ),
    )


def _position_size_check(
    weight_after: float | None, constraints: PortfolioConstraints
) -> RiskCheck:
    cap = float(constraints.max_single_position_pct)
    if weight_after is None:
        return RiskCheck(
            name="position_size",
            passed=True,
            detail="The position size cannot be measured.",
        )
    passed = weight_after <= cap + 1e-9
    return RiskCheck(
        name="position_size",
        passed=passed,
        detail=(
            f"The position would reach {weight_after:.1%} of the portfolio, "
            f"within the {cap:.1%} limit."
            if passed
            else f"The position would reach {weight_after:.1%} of the portfolio, "
            f"past the {cap:.1%} limit."
        ),
    )


def _cash_reserve_check(
    notional: Money,
    cash: Money,
    portfolio_value: Money,
    constraints: PortfolioConstraints,
) -> RiskCheck:
    floor = portfolio_value * constraints.min_cash_pct
    remaining = cash - notional
    passed = remaining >= floor
    share = float(remaining / portfolio_value) if not portfolio_value.is_zero else 0.0
    return RiskCheck(
        name="cash_reserve",
        passed=passed,
        detail=(
            f"Cash would stay at {share:.1%} of the portfolio, at or above the "
            f"{float(constraints.min_cash_pct):.1%} floor."
            if passed
            else f"Cash would fall to {share:.1%} of the portfolio, below the "
            f"{float(constraints.min_cash_pct):.1%} floor."
        ),
    )


def _category_cap_check(
    category: str | None,
    state: PlanState,
    constraints: PortfolioConstraints,
) -> RiskCheck:
    bucket = category or OTHER_CATEGORY
    try:
        cap = constraints.max_positions_for(bucket)
    except Exception:  # noqa: BLE001 - an unknown category is simply uncapped
        return RiskCheck(
            name="category_cap",
            passed=True,
            detail=f"{bucket} has no configured cap.",
        )
    opened = state.open_counts.get(bucket, 0) + 1
    passed = opened <= cap
    return RiskCheck(
        name="category_cap",
        passed=passed,
        detail=(
            f"This would open {opened} of {cap} allowed {bucket} positions."
            if passed
            else f"This would open {opened} {bucket} positions, past the cap "
            f"of {cap}."
        ),
    )


def _open_positions_check(
    state: PlanState, constraints: PortfolioConstraints
) -> RiskCheck:
    opened = state.open_total + 1
    limit = constraints.max_open_positions
    passed = opened <= limit
    return RiskCheck(
        name="open_positions",
        passed=passed,
        detail=(
            f"This would hold {opened} of {limit} allowed positions."
            if passed
            else f"This would hold {opened} positions, past the limit of {limit}."
        ),
    )


def _shares_check(target: Decimal, held_quantity: Decimal) -> RiskCheck:
    passed = target <= held_quantity
    return RiskCheck(
        name="shares",
        passed=passed,
        detail=(
            "The order never sells more than the ledger holds."
            if passed
            else "The order would sell more shares than the ledger holds."
        ),
    )


def plan_risk_checks(
    state: PlanState,
    *,
    ledger: PaperLedger,
    constraints: PortfolioConstraints,
) -> list[RiskCheck]:
    """The checks that describe the whole plan rather than one order.

    Each order was already checked on its own; these describe where the run
    leaves the account, which is what the review page shows at the top.
    """
    cash_after = ledger.cash - state.spent
    portfolio_value = ledger.total_value
    return [
        _plan_trade_limit_check(state, constraints),
        _plan_cash_check(cash_after, portfolio_value, constraints),
    ]


def _plan_trade_limit_check(
    state: PlanState, constraints: PortfolioConstraints
) -> RiskCheck:
    limit = constraints.max_trades_per_run
    passed = state.orders_used <= limit
    return RiskCheck(
        name="trade_limit",
        passed=passed,
        detail=(
            f"This plan places {state.orders_used} of the {limit} orders a run "
            "may place."
            if passed
            else f"This plan places {state.orders_used} orders, past the {limit} "
            "a run may place."
        ),
    )


def _plan_cash_check(
    cash_after: Money, portfolio_value: Money, constraints: PortfolioConstraints
) -> RiskCheck:
    if portfolio_value.is_zero:
        return RiskCheck(
            name="cash_reserve", passed=False, detail="The portfolio has no value."
        )
    floor = portfolio_value * constraints.min_cash_pct
    share = float(cash_after / portfolio_value)
    passed = cash_after >= floor
    return RiskCheck(
        name="cash_reserve",
        passed=passed,
        detail=(
            f"Cash after this plan is {share:.1%} of the portfolio, at or above "
            f"the {float(constraints.min_cash_pct):.1%} floor."
            if passed
            else f"Cash after this plan is {share:.1%} of the portfolio, below "
            f"the {float(constraints.min_cash_pct):.1%} floor."
        ),
    )
