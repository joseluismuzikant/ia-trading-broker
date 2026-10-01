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

#: The direction of a concrete order intent.
OrderSide = Literal["BUY", "SELL"]

#: How a proposal may be approved. Day 4 only creates the human-review kind.
ApprovalMode = Literal["HUMAN_IN_THE_LOOP", "AUTOMATIC"]

#: Where an approved proposal would execute. Live stays unused until proven.
ExecutionMode = Literal["PAPER", "LIVE"]

#: A proposal is created, then later approved, rejected, or expired. Day 4 only
#: ever writes ``pending_review``.
ProposalStatus = Literal["pending_review", "approved", "rejected", "expired"]

#: The lifecycle of a human approval. ``consumed`` means the approval was used
#: exactly once to reach the order service, so it can never be used again.
ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "consumed"]

#: The lifecycle of one order. ``prepared`` means the order is saved before the
#: executor is called; ``unknown`` means the executor did not clearly confirm
#: it, so the application must never send it again automatically.
OrderStatus = Literal[
    "prepared",
    "submitted",
    "accepted",
    "filled",
    "rejected",
    "cancelled",
    "unknown",
]

#: A human approval expires this many hours after it is requested.
APPROVAL_TTL_HOURS = 24

#: The fresh-data check stops an order when the price or the cash has moved by
#: more than this fraction since the proposal was created.
MAX_PRICE_DRIFT = Decimal("0.05")
MAX_CASH_DRIFT = Decimal("0.05")


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


class CandidateView(BaseModel):
    """One instrument the market scanner kept, as the review page shows it.

    Stored with the proposal like every other funnel result, so the page shows
    which instruments were considered and not just which ones won.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    #: The paper's name: the company, fund, or bond the ticker stands for.
    name: str
    category: str
    #: How the scanner ranked the instrument on cheap market readings.
    scan_score: float


class FinalistView(BaseModel):
    """One finalist as the review page shows it: both scores and the reason.

    It is stored with the proposal, which is insert-once, so the page keeps the
    exact funnel result the plan was checked against instead of recomputing it
    from a universe that may have moved on.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    #: The paper's name: the company, fund, or bond the ticker stands for.
    name: str
    category: str
    technical_score: float
    fundamental_score: float
    fundamental_summary: str
    combined_score: float


class Recommendation(BaseModel):
    """One instrument's decision, with its size and its risk results."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    #: The paper's name, so the review page never shows a bare ticker.
    name: str = ""
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
    #: The instruments the market scanner kept, best first. Absent only on a
    #: proposal saved before the funnel existed.
    candidates: list[CandidateView] = Field(default_factory=list)
    #: The finalists the funnel picked, best first. Absent only on a proposal
    #: saved before the funnel existed, or when nothing survived it.
    finalists: list[FinalistView] = Field(default_factory=list)
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


class Order(BaseModel):
    """One concrete order intent, from preparation to its final state.

    The order is saved before the executor is called (``prepared``), so a
    repeated request or a restart can find it and never create a second one. The
    same model carries the fill, so a page shows one row from intent to result.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    side: OrderSide
    quantity: Decimal
    limit_price: Money
    #: Cash the order spends (buy) or releases (sell), at the limit price.
    notional: Money
    status: OrderStatus = "prepared"
    execution_mode: ExecutionMode = "PAPER"
    #: The broker's order id. Empty for a paper fill, which has no broker id.
    broker_order_id: str | None = None
    filled_quantity: Decimal | None = None
    filled_price: Money | None = None
    filled_notional: Money | None = None
    #: Short, safe text about the last state change.
    message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_settled(self) -> bool:
        """True when the order can no longer change on its own."""
        return self.status in {"filled", "rejected", "cancelled"}


class OrderResult(BaseModel):
    """What an :class:`~app.ports.execution.OrderExecutor` returned.

    ``accepted`` must be true only when the executor clearly confirmed the
    order. When it is false and the status is ``unknown`` the application must
    never send the order again automatically: it is reconciled instead.
    """

    model_config = ConfigDict(frozen=True)

    status: OrderStatus
    accepted: bool
    filled_quantity: Decimal = Field(default_factory=lambda: quantity(0))
    filled_price: Money | None = None
    filled_notional: Money | None = None
    broker_order_id: str | None = None
    message: str = ""

    @property
    def is_fill(self) -> bool:
        """True when the order was clearly filled."""
        return self.status == "filled" and self.filled_quantity > 0
