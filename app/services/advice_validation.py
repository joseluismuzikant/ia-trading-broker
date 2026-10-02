"""Cross-checks a portfolio advice against the account and the universe.

The models in :mod:`app.domain.analysis` can only shape the advice; they cannot
know what the account holds or what the project may trade at all. This module
answers that half of the question, using the existing
:class:`~app.domain.universe.UniverseConfig` as the only symbol registry.

It does **not** size anything. Quantities, order sizes, cash allocation, and
the portfolio constraints stay with the deterministic policy
(:mod:`app.domain.policy`); validating advice must never turn into trading.
"""

from __future__ import annotations

from app.domain.analysis import PortfolioAdvice
from app.domain.universe import UniverseConfig


class PortfolioAdviceValidationError(ValueError):
    """The advice cannot describe this account or this universe."""


def validate_portfolio_advice(
    advice: PortfolioAdvice,
    *,
    held_symbols: set[str],
    universe: UniverseConfig,
) -> PortfolioAdvice:
    """Return the advice when it fits this account, else raise.

    ``held_symbols`` are the symbols the account currently holds; they are
    normalised here so callers may pass them exactly as the broker reported
    them. The rules are:

    * every symbol is either held or listed in the universe — this is also the
      rule for ``BUY`` and ``HOLD``, which are only valid for an instrument the
      account could actually act on;
    * ``SELL`` is valid only for a symbol the account currently holds, because
      there is nothing to sell otherwise.

    Unknown symbols fail with :class:`PortfolioAdviceValidationError` rather
    than being dropped, so a model cannot quietly invent an instrument.
    """
    held = {symbol.strip().upper() for symbol in held_symbols}

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

    return advice
