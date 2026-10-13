"""Cross-checks a portfolio advice against the account and the universe.

The models in :mod:`app.domain.analysis` can only shape the advice; they cannot
know what the account holds, what it is worth today, or what the project may
trade at all. This module answers that half of the question, using the existing
:class:`~app.domain.universe.UniverseConfig` as the only symbol registry.

It also enforces the **closed-portfolio** rule. The advice is a rebalance of
the money already in the account: the instrument weights plus the target cash
weight must add up to 1.0, so a plan that quietly assumes new capital — or
loses some — is refused. ``target_weight`` is the authority; the
``BUY``/``HOLD``/``SELL`` label must agree with the move from the current
weight to the target one, so a contradiction is visible instead of being
quietly reconciled.

It does **not** size anything. Quantities, order sizes, cash allocation, and
the portfolio limits stay with the rebalance
(:mod:`app.services.rebalance`); validating advice must never turn into trading.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.analysis import (
    ALLOCATION_TOLERANCE,
    PortfolioAdvice,
    expected_action,
)
from app.domain.universe import UniverseConfig

#: Float slack for the "must add up to one" rule.
_SUM_EPSILON = 1e-9


class PortfolioAdviceValidationError(ValueError):
    """The advice cannot describe this account, this universe, or this money."""


def validate_portfolio_advice(
    advice: PortfolioAdvice,
    *,
    held_symbols: set[str],
    universe: UniverseConfig,
    current_weights: Mapping[str, float] | None = None,
    min_cash_pct: float = 0.0,
    tolerance: float = ALLOCATION_TOLERANCE,
) -> PortfolioAdvice:
    """Return the advice when it fits this account, else raise.

    ``held_symbols`` are the symbols the account currently holds and
    ``current_weights`` is each of their shares of the portfolio today; both
    are normalised here so callers may pass them exactly as the broker
    reported them. ``min_cash_pct`` is the configured cash floor.

    The rules are:

    * every currently held symbol appears in the decisions — a rebalance may
      not forget a position;
    * a new symbol is allowed only when the universe lists it;
    * a symbol may not appear twice;
    * every ``target_weight`` and the ``target_cash_weight`` lie in ``[0, 1]``;
    * the target allocation adds up to 1.0 within ``tolerance``;
    * the target cash is at least ``min_cash_pct``;
    * the action agrees with the move from the current weight to the target
      one: up is ``BUY``, down is ``SELL``, unchanged is ``HOLD``.

    Unknown symbols and contradictions fail with
    :class:`PortfolioAdviceValidationError` rather than being dropped or
    reconciled, so a model cannot quietly invent an instrument or a trade.
    """
    held = {symbol.strip().upper() for symbol in held_symbols}
    weights = {
        symbol.strip().upper(): float(weight) for symbol, weight in (current_weights or {}).items()
    }

    _check_symbols(advice, held=held, universe=universe)
    _check_allocation(advice, min_cash_pct=min_cash_pct, tolerance=tolerance)
    _check_actions(advice, weights=weights, tolerance=tolerance)
    return advice


def _check_symbols(advice: PortfolioAdvice, *, held: set[str], universe: UniverseConfig) -> None:
    seen: set[str] = set()
    for decision in advice.decisions:
        symbol = decision.symbol
        is_held = symbol in held
        is_configured = universe.find(symbol) is not None

        if not is_held and not is_configured:
            raise PortfolioAdviceValidationError(
                f"{symbol} is unknown: the account does not hold it and the "
                "universe does not list it"
            )
        if decision.action == "SELL" and not is_held:
            raise PortfolioAdviceValidationError(
                f"{symbol} cannot be sold because the account does not hold it"
            )
        if symbol in seen:
            raise PortfolioAdviceValidationError(f"{symbol} appears more than once")
        seen.add(symbol)

    missing = sorted(held - seen)
    if missing:
        raise PortfolioAdviceValidationError(
            "every current holding must appear in the decisions; missing: " + ", ".join(missing)
        )


def _check_allocation(advice: PortfolioAdvice, *, min_cash_pct: float, tolerance: float) -> None:
    allocated = advice.total_instrument_weight + advice.target_cash_weight
    if abs(allocated - 1.0) > tolerance + _SUM_EPSILON:
        raise PortfolioAdviceValidationError(
            f"the target allocation must add up to 1.0, got {allocated:.4f} "
            f"({advice.total_instrument_weight:.4f} in instruments and "
            f"{advice.target_cash_weight:.4f} in cash)"
        )
    if advice.target_cash_weight < min_cash_pct - _SUM_EPSILON:
        raise PortfolioAdviceValidationError(
            f"the target cash weight {advice.target_cash_weight:.4f} is below "
            f"the minimum {min_cash_pct:.4f}"
        )


def _check_actions(
    advice: PortfolioAdvice, *, weights: Mapping[str, float], tolerance: float
) -> None:
    for decision in advice.decisions:
        current = weights.get(decision.symbol, 0.0)
        expected = expected_action(decision.target_weight, current, tolerance=tolerance)
        if decision.action != expected:
            raise PortfolioAdviceValidationError(
                f"{decision.symbol} says {decision.action} but its target "
                f"weight {decision.target_weight:.4f} against a current weight "
                f"{current:.4f} implies {expected}"
            )
