"""The typed contract of one portfolio analysis: its input and its output.

An analyst — the OpenAI one behind :class:`PortfolioAdvisor`, or the
deterministic fallback — reads a :class:`PortfolioAnalysisContext`, which
describes the account as one whole, and answers with a
:class:`PortfolioAdvice`: a complete **target allocation** of the money that is
already there. It never returns a quantity, an order size, or a broker request.
Those are derived later by deterministic Python (:mod:`app.services.rebalance`), so
no model can size a trade or reach a broker.

The output models are frozen and **forbid extra fields** on purpose. They are
the shape untrusted model output is parsed into: anything the analyst did not
mean to say — including executable fields such as ``quantity``,
``notional`` or ``broker_payload`` — is rejected instead of silently ignored.

The allocation is closed: the instrument weights plus the target cash weight
add up to 1.0. The advice is a rebalance of the existing portfolio value, not
a plan that quietly assumes new money.

Nothing here knows about OpenAI, HTTP, a database, or the broker.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.domain.portfolio import Portfolio, Position
from app.domain.trading import Action, IndicatorEvidence
from app.domain.universe import Instrument, PortfolioConstraints

#: How far a target weight may sit from the current one and still be called a
#: hold. Below this a rebalance has nothing worth trading for.
ALLOCATION_TOLERANCE = 0.005


# --- Input: what the analyst is shown -------------------------------------


def current_weight(position: Position, total_value: float | None) -> float:
    """One position's share of the portfolio, 0.0 when either is unknown."""
    if total_value is None or total_value <= 0:
        return 0.0
    return (position.market_value or 0.0) / total_value


def allocation_delta(target_weight: float, current_weight_value: float) -> float:
    """How far a target weight sits from the current one.

    Positive means the position should grow, negative that it should shrink.
    It is the number the rebalance turns into sells and buys; it is not an
    order.
    """
    return target_weight - current_weight_value


def expected_action(
    target_weight: float,
    current_weight_value: float,
    *,
    tolerance: float = ALLOCATION_TOLERANCE,
) -> Action:
    """The action that moves a position from its current to its target weight.

    One rule, used by the advisors to label their advice and by the validation
    to check that label, so the two can never disagree.
    """
    delta = allocation_delta(target_weight, current_weight_value)
    if delta > tolerance:
        return "BUY"
    if delta < -tolerance:
        return "SELL"
    return "HOLD"


@dataclass(frozen=True)
class PortfolioAnalysisContext:
    """Everything an advisor needs to see the portfolio as one whole.

    It is deliberately plain: the account's own numbers, the market evidence
    already calculated in Python, the instruments the project may add, and the
    hard limits. It carries no broker username, password, token, session, API
    key, database row, or raw broker response, so it can be handed to a model
    or written to an event without leaking anything.
    """

    country: str
    #: When the account was read. Every number below is a picture of that moment.
    analysed_at: datetime
    portfolio: Portfolio
    #: Indicator readings per symbol, keyed by upper-case ticker.
    evidence: Mapping[str, IndicatorEvidence] = field(default_factory=dict)
    #: Instruments the project is allowed to add, from ``config/universe.toml``.
    watchlist: Sequence[Instrument] = ()
    constraints: PortfolioConstraints = field(default_factory=PortfolioConstraints)
    #: Things the caller wants the analyst to know, such as a failed read.
    warnings: Sequence[str] = ()

    @property
    def total_value(self) -> float | None:
        """Cash plus positions: the complete pool of capital to allocate."""
        return self.portfolio.total_value

    @property
    def cash(self) -> float | None:
        return self.portfolio.cash

    @property
    def cash_weight(self) -> float | None:
        """The share of the portfolio sitting in cash, when both are known."""
        total = self.total_value
        if total is None or total <= 0 or self.cash is None:
            return None
        return self.cash / total

    @property
    def held_symbols(self) -> set[str]:
        """The symbols the account currently holds."""
        return {
            position.instrument.symbol.strip().upper()
            for position in self.portfolio.positions
            if position.instrument.symbol
        }

    @property
    def current_weights(self) -> dict[str, float]:
        """Each held symbol's share of the portfolio, keyed by ticker."""
        total = self.total_value
        return {
            position.instrument.symbol.strip().upper(): current_weight(position, total)
            for position in self.portfolio.positions
            if position.instrument.symbol
        }

    def weight_of(self, symbol: str) -> float:
        """The current weight of one symbol; 0.0 when it is not held."""
        return self.current_weights.get(symbol.strip().upper(), 0.0)


# --- Output: what the analyst must return ---------------------------------


class AssetAdvice(BaseModel):
    """One instrument's place in the target portfolio.

    ``target_weight`` is the authority: the share of the portfolio this
    instrument should occupy once the rebalance is done. ``action`` is the
    human-readable consequence of moving from the current weight to the target
    one, and is checked against it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    action: Action
    #: Share of the portfolio this instrument should hold afterwards, 0.0-1.0.
    #: Zero means a complete exit.
    target_weight: float
    #: How sure the analyst is of this advice, 0.0 to 1.0. Descriptive only:
    #: it never overrides a risk limit.
    confidence: float
    rationale: str
    #: Known risks the analyst wants shown next to its advice.
    risks: list[str] = Field(default_factory=list)

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalise_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("symbol")
    @classmethod
    def _symbol_is_present(cls, value: str) -> str:
        if not value:
            raise ValueError("symbol must not be empty")
        return value

    @field_validator("target_weight", "confidence")
    @classmethod
    def _fraction(cls, value: float, info: ValidationInfo) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{info.field_name} must be between 0.0 and 1.0")
        return value

    @field_validator("rationale")
    @classmethod
    def _rationale_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rationale must not be empty")
        return value

    @field_validator("risks", mode="before")
    @classmethod
    def _risks_may_be_missing(cls, value: object) -> object:
        return [] if value is None else value


class PortfolioAdvice(BaseModel):
    """One complete analyst answer: a target allocation of the whole portfolio.

    The decisions plus ``target_cash_weight`` describe where every unit of the
    existing portfolio value should end up, which is what makes the advice a
    rebalance rather than a list of unrelated ideas.

    ``model_provider``, ``model_name`` and ``analysis_version`` record who
    produced the answer and under which contract, so a saved proposal can be
    read later without guessing which prompt or model made it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str
    #: Share of the portfolio that should stay in cash, 0.0-1.0.
    target_cash_weight: float
    decisions: list[AssetAdvice]
    model_provider: str
    model_name: str
    analysis_version: str

    @field_validator("target_cash_weight")
    @classmethod
    def _cash_is_a_fraction(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("target_cash_weight must be between 0.0 and 1.0")
        return value

    @field_validator("summary", "model_provider", "model_name", "analysis_version")
    @classmethod
    def _text_is_present(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("decisions")
    @classmethod
    def _one_decision_per_symbol(cls, value: list[AssetAdvice]) -> list[AssetAdvice]:
        # AssetAdvice already normalised every symbol to upper case.
        seen: set[str] = set()
        for decision in value:
            if decision.symbol in seen:
                raise ValueError(f"duplicate advice for {decision.symbol}")
            seen.add(decision.symbol)
        return value

    @property
    def total_instrument_weight(self) -> float:
        """The share of the portfolio the decisions allocate to instruments."""
        return sum(decision.target_weight for decision in self.decisions)
