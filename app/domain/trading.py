"""Shared trading models: paper ledger, evidence, and proposals.

These are broker-agnostic, like :mod:`app.domain.portfolio`. A proposal is the
complete trade plan shown before execution. Day 4 saves it and shows it; it does
not approve it and does not place an order. Day 5 adds approval and paper
execution on top of the same models.

Amounts are :class:`~app.domain.money.Money`, so a quantity or a risk result
cannot drift through binary rounding.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.money import Money, quantity

#: The only actions a strategy may recommend.
Action = Literal["BUY", "SELL", "HOLD"]

#: How a proposal may be approved. Day 4 only creates the human-review kind.
ApprovalMode = Literal["HUMAN_IN_THE_LOOP", "AUTOMATIC"]

#: Where an approved proposal would execute. Live stays unused until proven.
ExecutionMode = Literal["PAPER", "LIVE"]

#: A proposal is created, then later approved, rejected, or expired. Day 4 only
#: ever writes ``pending_review``.
ProposalStatus = Literal["pending_review", "approved", "rejected", "expired"]

#: Fixed paper limits. A single order may not spend more than this share of the
#: portfolio, one run may not turn over more than this share of it, and a buy
#: may not push one position past this share.
MAX_ORDER_WEIGHT = Decimal("0.10")
MAX_TURNOVER = Decimal("0.20")
MAX_POSITION_WEIGHT = Decimal("0.25")
#: Cash kept back so a buy can never spend the last cent of the account.
CASH_BUFFER = Decimal("0.01")


class PaperPosition(BaseModel):
    """One holding in the paper ledger."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    quantity: Decimal
    average_price: Money
    last_price: Money
    market: str | None = None
    currency: str | None = None

    @property
    def market_value(self) -> Money:
        """Current value of the holding."""
        return self.last_price * self.quantity


class PaperLedger(BaseModel):
    """The saved record of paper cash and positions.

    It is created from a portfolio snapshot and is never the broker's own
    record. Fills update it from Day 5 onward; Day 4 only creates it.
    """

    model_config = ConfigDict(frozen=True)

    country: str
    currency: str | None = None
    cash: Money
    positions: list[PaperPosition] = Field(default_factory=list)
    #: The portfolio snapshot this ledger was copied from, when known.
    source_snapshot_id: int | None = None
    created_at: datetime | None = None

    @property
    def positions_value(self) -> Money:
        """Sum of the valued holdings."""
        total = Money(Decimal("0.00"))
        for position in self.positions:
            total = total + position.market_value
        return total

    @property
    def total_value(self) -> Money:
        """Cash plus holdings."""
        return self.cash + self.positions_value

    def position_for(self, symbol: str) -> PaperPosition | None:
        """Return the holding for one symbol, if the ledger has it."""
        for position in self.positions:
            if position.symbol == symbol:
                return position
        return None


class IndicatorEvidence(BaseModel):
    """The indicator readings behind one recommendation.

    Stored with the proposal so the review page can show why the strategy chose
    its action without recomputing anything.
    """

    model_config = ConfigDict(frozen=True)

    last_price: Money | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    atr: float | None = None
    average_volume: float | None = None
    return_fraction: float | None = None
    observations: int = 0


class RiskCheck(BaseModel):
    """One risk rule and whether the recommendation passed it."""

    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    detail: str


class Recommendation(BaseModel):
    """One instrument's decision, with its size and its risk results."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    action: Action
    #: Shares to trade. Zero for a hold or a rejected recommendation.
    quantity: Decimal = Field(default_factory=lambda: quantity(0))
    limit_price: Money | None = None
    #: Cash the order would spend or release, at the limit price.
    notional: Money | None = None
    #: Weight of the position after the trade, as a fraction of the portfolio.
    weight_after: float | None = None
    rationale: str
    evidence: IndicatorEvidence
    risk_checks: list[RiskCheck] = Field(default_factory=list)

    @property
    def passed_risk(self) -> bool:
        """True when every risk check passed."""
        return all(check.passed for check in self.risk_checks)

    @property
    def is_order(self) -> bool:
        """True when this recommendation would become an order."""
        return self.action != "HOLD" and self.quantity > 0 and self.passed_risk


class Proposal(BaseModel):
    """The complete trade plan shown before execution.

    Once saved it is never edited. A later analysis creates a new proposal
    instead of changing this one, which is what keeps an approval (Day 5)
    pointed at exactly the plan the user reviewed.
    """

    model_config = ConfigDict(frozen=True)

    country: str
    currency: str | None = None
    approval_mode: ApprovalMode = "HUMAN_IN_THE_LOOP"
    execution_mode: ExecutionMode = "PAPER"
    status: ProposalStatus = "pending_review"
    portfolio_value: Money
    cash: Money
    recommendations: list[Recommendation] = Field(default_factory=list)
    #: Share of the portfolio the plan wants to trade.
    turnover: float = 0.0
    risk_checks: list[RiskCheck] = Field(default_factory=list)
    #: Human readable summary of why the plan looks the way it does.
    summary: str
    created_at: datetime | None = None

    @property
    def passed_risk(self) -> bool:
        """True when the plan and every recommendation passed risk checks."""
        return all(check.passed for check in self.risk_checks) and all(
            recommendation.passed_risk for recommendation in self.recommendations
        )

    @property
    def orders(self) -> list[Recommendation]:
        """Recommendations that would become orders. Day 4 never submits them."""
        return [item for item in self.recommendations if item.is_order]
