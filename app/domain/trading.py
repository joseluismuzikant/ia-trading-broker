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

from app.domain.money import Money, money, quantity

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
#: it, so the application must never send it again automatically;
#: ``partially_filled`` means IOL reports some units executed and the rest is
#: still working, so it is never treated as a complete fill.
OrderStatus = Literal[
    "prepared",
    "submitted",
    "accepted",
    "filled",
    "partially_filled",
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
    #: How many units one quote covers (1 for a share, 100 for a sovereign
    #: bond). The market value is ``last_price * quantity / factor``.
    quotation_factor: int = 1

    @property
    def market_value(self) -> Money:
        """Current value of the holding, in the quote's own convention."""
        factor = self.quotation_factor if self.quotation_factor > 0 else 1
        return money((self.last_price.amount * self.quantity) / factor)


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


class AssetAdviceView(BaseModel):
    """One instrument's place in the model's target allocation.

    It mirrors :class:`~app.domain.analysis.AssetAdvice` with the account's own
    numbers next to it, so the review page can show the move the model wants
    without recomputing anything. Stored flat, like every other view here, so
    the proposal never depends on the analysis contract's module.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    #: The paper's name, so the review page never shows a bare ticker.
    name: str = ""
    category: str = ""
    action: Action
    #: Share of the portfolio this instrument should hold afterwards.
    target_weight: float
    #: Share of the portfolio it holds today, for the move being asked for.
    current_weight: float = 0.0
    confidence: float
    rationale: str
    risks: list[str] = Field(default_factory=list)


class PortfolioAdviceView(BaseModel):
    """The model's whole answer as the review page shows it.

    A target allocation of the money already in the account: the decisions,
    the share that should stay in cash, and which model said so. It is advice
    only — sizing and orders are built separately by deterministic code.
    """

    model_config = ConfigDict(frozen=True)

    summary: str
    #: Share of the portfolio the model wants kept in cash.
    target_cash_weight: float
    #: Share in cash today, when the account was read.
    current_cash_weight: float | None = None
    #: The catalog label of the model, e.g. "Gemini 2.5 Pro".
    model_label: str
    model_provider: str
    model_name: str
    analysis_version: str
    decisions: list[AssetAdviceView] = Field(default_factory=list)


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
    #: Weight the model asked for, so a skipped trade can show its target.
    target_weight: float | None = None
    #: Market code the broker needs to route the order (metadata, not guessed).
    market: str | None = None
    #: Whether Python could turn the target into an executable whole-unit
    #: order. False is shown as NOT EXECUTABLE with ``reason``.
    executable: bool = True
    #: Why a trade cannot be executed (cap, minimum, funding, unverified).
    reason: str = ""
    #: Whole units the target implies, and the smallest executable quantity.
    required_quantity: Decimal | None = None
    min_quantity: Decimal | None = None
    #: Cost of one tradable unit (quoted price / quotation factor).
    unit_cost: Money | None = None
    #: How many units one quote covers (1 for a share, 100 for a bond).
    quotation_factor: int = 1
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
    #: The model catalog key this plan was produced with, so a re-run can ask
    #: the same model again. Absent on proposals saved before this was kept.
    model_key: str | None = None
    #: How many model answers this plan took: 1, or more when an answer was
    #: sent back with corrections.
    attempts: int = 1
    approval_mode: ApprovalMode = "HUMAN_IN_THE_LOOP"
    execution_mode: ExecutionMode = "PAPER"
    status: ProposalStatus = "pending_review"
    portfolio_value: Money
    cash: Money
    #: The model's target allocation for this account, when the run asked one.
    #: Absent on a proposal saved before the model was wired in.
    advice: PortfolioAdviceView | None = None
    #: The paper portfolio this plan starts from: a copy of the real account
    #: taken at the beginning of the analysis. What the review compares against.
    ledger_before: PaperLedger | None = None
    #: The simulated portfolio after applying exactly the orders below. This
    #: is the preview the human reviews; the same orders are later executed.
    ledger_after: PaperLedger | None = None
    #: The instruments the market scanner kept, best first. Left empty on new
    #: proposals: the funnel no longer decides which orders exist.
    candidates: list[CandidateView] = Field(default_factory=list)
    #: The finalists the funnel picked, best first. Left empty on new
    #: proposals: the model's target allocation decides the rebalance now.
    finalists: list[FinalistView] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    #: Share of the portfolio the plan wants to trade.
    turnover: float = 0.0
    risk_checks: list[RiskCheck] = Field(default_factory=list)
    #: What the run could not do perfectly: failed reads, capped or deferred
    #: trades, rounding left in cash. Shown next to the review, never hidden.
    warnings: list[str] = Field(default_factory=list)
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
    #: Market code the broker needs to route the order. Taken from instrument
    #: or position metadata at plan time; never guessed from the ticker.
    market: str | None = None
    #: How many units one quote covers. The broker is sent the quoted price
    #: unchanged; only the cash the order moves divides by this factor.
    quotation_factor: int = 1
    status: OrderStatus = "prepared"
    execution_mode: ExecutionMode = "PAPER"
    #: The broker's order id. Empty for a paper fill, which has no broker id.
    broker_order_id: str | None = None
    #: The order's idempotency key, for venues that store a client-side id and
    #: can return it. It lets an ``unknown`` order be found again at the broker
    #: instead of being resent. Set by the execution service, never by a model.
    external_id: str | None = None
    filled_quantity: Decimal | None = None
    filled_price: Money | None = None
    filled_notional: Money | None = None
    #: Short, safe text about the last state change.
    message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_settled(self) -> bool:
        """True when the order can no longer change on its own.

        A partial fill is not settled: the rest may still execute, so it must
        keep being reconciled and must never be resent.
        """
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
